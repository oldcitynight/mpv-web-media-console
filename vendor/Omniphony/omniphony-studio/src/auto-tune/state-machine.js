// PI auto-tune finite-state machine for the Omniphony adaptive resampler.
//
// Drives the Ziegler-Nichols-style procedure described in
// Omniphony/PI_TUNING_PROCEDURE.md. Pure logic: no DOM, no Tauri, no app
// state. Caller is responsible for:
//   - feeding telemetry via pushSample({ t, latencySmoothedMs,
//     latencyTargetMs, resampleRatio, phase })
//   - reacting to emitted events: 'applyParams', 'progress',
//     'awaitUserAction', 'sourceLost', 'sourceRecovered', 'complete',
//     'cancelled', 'error'
//   - invoking userAck(kind), cancel(), abbreviate() in response.
//
// Notes:
// - Touches only kpNear / ki / maxAdjust / updateIntervalCallbacks. Never
//   touches integralDischargeRatio (non-operative on current hardware).
// - kp_max = 5000 by default (override via options); initial max_adjust =
//   0.10 to clear observed -55000 ppm drift; integral_discharge_ratio is
//   deliberately not patched.

import {
  computePalierStats,
  detectOscillationByJump,
  detectOscillationAbsolute,
  detectSaturation,
  detectConvergence,
  detectSourceLoss,
  computeRateStats,
  errorMs,
} from './detectors.js';

export const AUTO_TUNE_DEFAULTS = Object.freeze({
  initialKp: 1,
  initialMaxAdjust: 0.10,
  initialUpdateInterval: 1,
  kpMax: 5000,
  kpPalierMs: 30000,
  kpBaselinePaliers: 3,
  kiPalierMs: 60000,
  kiMaxIterations: 4,
  kiMin: 1e-3,
  perturbationRecoverMs: 15000,
  longRunDefaultMs: 600000,
  longRunMinAbbreviateMs: 120000,
  longRunStatsWindowMs: 120000,
  tighteningPalierMs: 30000,
  ziegerKpScale: 0.6,
  initialKiFromKpDivisor: 5,
  sampleRetentionMs: 600000,
  maxAdjustFloor: 0.02,
  maxAdjustSafetyMargin: 1.5,
  maxAdjustWarnThreshold: 0.15,
  updateIntervalCleanStdPpm: 50,
  updateIntervalClean: 5,
  updateIntervalDefault: 10,
});

function meanAbsErr(samples, fromMs, toMs) {
  let sum = 0;
  let n = 0;
  for (const s of samples) {
    if (s.t < fromMs || s.t > toMs) continue;
    const e = errorMs(s);
    if (e === null) continue;
    sum += Math.abs(e);
    n += 1;
  }
  return n ? sum / n : null;
}

function peakAbsErr(samples, fromMs, toMs) {
  let peak = 0;
  let found = false;
  for (const s of samples) {
    if (s.t < fromMs || s.t > toMs) continue;
    const e = errorMs(s);
    if (e === null) continue;
    const a = Math.abs(e);
    if (a > peak) peak = a;
    found = true;
  }
  return found ? peak : null;
}

function freshContext() {
  return {
    samples: [],
    currentKp: 0,
    currentKi: 0,
    kpCrit: null,
    kpFinal: null,
    kiFinal: null,
    maxAdjustFinal: null,
    updateIntervalFinal: null,
    palierStartMs: 0,
    kiIteration: 0,
    bestKi: null,
    bestKiErr: Infinity,
    longRunStartMs: 0,
    longRunDurationMs: 0,
    abbreviateRequested: false,
    perturbationStartMs: 0,
    suspendedFromState: null,
    kpHistory: [],
  };
}

export function createAutoTuneStateMachine(options = {}) {
  const opts = { ...AUTO_TUNE_DEFAULTS, ...options };
  const listeners = new Set();
  let state = 'idle';
  let ctx = freshContext();

  function emit(event, payload) {
    for (const fn of listeners) {
      try {
        fn(event, payload);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[auto-tune FSM] listener error', err);
      }
    }
  }

  function setState(next, payload) {
    state = next;
    emit('progress', { step: next, ...payload });
  }

  function pushSample(sample) {
    if (state === 'idle' || state === 'cancelled' || state === 'error' || state === 'completed') {
      return;
    }
    if (!sample || typeof sample.t !== 'number') return;
    ctx.samples.push(sample);
    const cutoff = sample.t - opts.sampleRetentionMs;
    while (ctx.samples.length && ctx.samples[0].t < cutoff) ctx.samples.shift();

    // Source-loss watchdog (skip during the manual perturbation step).
    if (state !== 'awaitPerturbation' && state !== 'perturbationRecovering' && state !== 'suspended') {
      const sl = detectSourceLoss(ctx.samples);
      if (sl.lost) {
        ctx.suspendedFromState = state;
        state = 'suspended';
        emit('sourceLost', { events: sl.events });
        return;
      }
    }

    switch (state) {
      case 'holdKp': tickHoldKp(sample); break;
      case 'tuningKi': tickTuningKi(sample); break;
      case 'perturbationRecovering': tickPerturbationRecovering(sample); break;
      case 'longRun': tickLongRun(sample); break;
      case 'tightening': tickTightening(sample); break;
      default: break;
    }
  }

  function start(startTimeMs) {
    if (state !== 'idle' && state !== 'cancelled' && state !== 'completed' && state !== 'error') {
      return false;
    }
    ctx = freshContext();
    ctx.currentKp = opts.initialKp;
    ctx.currentKi = 0;
    ctx.palierStartMs = startTimeMs;
    emit('applyParams', {
      kpNear: opts.initialKp,
      ki: 0,
      maxAdjust: opts.initialMaxAdjust,
      updateIntervalCallbacks: opts.initialUpdateInterval,
    });
    setState('holdKp', { currentKp: opts.initialKp, palier: 1 });
    return true;
  }

  function tickHoldKp(sample) {
    const elapsed = sample.t - ctx.palierStartMs;
    if (elapsed < opts.kpPalierMs) return;

    const palierStats = computePalierStats(ctx.samples, ctx.palierStartMs);
    const sat = detectSaturation(ctx.samples, opts.initialMaxAdjust);
    const baselineStats = ctx.kpHistory
      .filter((p) => !p.saturated && p.stats !== null)
      .slice(-opts.kpBaselinePaliers)
      .map((p) => p.stats);
    const verdict = detectOscillationByJump(palierStats, baselineStats);

    ctx.kpHistory.push({
      kp: ctx.currentKp,
      stats: palierStats,
      saturated: sat.saturated,
    });

    if (verdict.oscillating) {
      ctx.kpCrit = ctx.currentKp;
      ctx.kpFinal = opts.ziegerKpScale * ctx.kpCrit;
      const initialKi = ctx.kpFinal / opts.initialKiFromKpDivisor;
      ctx.currentKi = initialKi;
      ctx.kiIteration = 0;
      ctx.bestKi = initialKi;
      ctx.bestKiErr = Infinity;
      emit('applyParams', { kpNear: ctx.kpFinal, ki: initialKi });
      ctx.palierStartMs = sample.t;
      ctx.samples = [sample];
      setState('tuningKi', {
        kpCrit: ctx.kpCrit,
        kpFinal: ctx.kpFinal,
        currentKi: initialKi,
        kiIteration: 0,
        verdict,
      });
      return;
    }

    const nextKp = ctx.currentKp * 2;
    if (nextKp > opts.kpMax) {
      state = 'error';
      emit('error', {
        kind: 'no-oscillation',
        kpReached: ctx.currentKp,
        lastStats: palierStats,
        history: ctx.kpHistory,
      });
      return;
    }
    ctx.currentKp = nextKp;
    ctx.palierStartMs = sample.t;
    ctx.samples = [sample];
    emit('applyParams', { kpNear: nextKp });
    emit('progress', {
      step: 'holdKp',
      currentKp: nextKp,
      saturated: sat.saturated,
      palierStats,
      verdict,
    });
  }

  function tickTuningKi(sample) {
    const elapsed = sample.t - ctx.palierStartMs;
    if (elapsed < opts.kiPalierMs) return;

    const conv = detectConvergence(ctx.samples);
    if (conv.converged) {
      ctx.kiFinal = ctx.currentKi;
      setState('awaitPerturbation', { kpFinal: ctx.kpFinal, kiFinal: ctx.kiFinal });
      emit('awaitUserAction', { kind: 'perturbation', kpFinal: ctx.kpFinal, kiFinal: ctx.kiFinal });
      return;
    }

    // Iteration budget reached: settle on best ki seen so far.
    if (ctx.kiIteration >= opts.kiMaxIterations) {
      ctx.kiFinal = ctx.bestKi ?? ctx.currentKi;
      setState('awaitPerturbation', {
        kpFinal: ctx.kpFinal,
        kiFinal: ctx.kiFinal,
        hitIterationCap: true,
      });
      emit('awaitUserAction', {
        kind: 'perturbation',
        kpFinal: ctx.kpFinal,
        kiFinal: ctx.kiFinal,
        hitIterationCap: true,
      });
      return;
    }

    // Heuristic: compare error in first vs second half of the palier.
    const half = ctx.palierStartMs + opts.kiPalierMs / 2;
    const firstHalfMean = meanAbsErr(ctx.samples, ctx.palierStartMs, half);
    const secondHalfMean = meanAbsErr(ctx.samples, half, sample.t);
    const secondHalfPeak = peakAbsErr(ctx.samples, half, sample.t);
    const overshootSuspect = secondHalfPeak !== null && secondHalfMean !== null
      && secondHalfPeak > 2 * secondHalfMean
      && secondHalfPeak > 1.0;
    const improving = firstHalfMean !== null && secondHalfMean !== null
      && secondHalfMean < firstHalfMean * 0.8;

    let nextKi;
    let reason;
    if (overshootSuspect || (firstHalfMean !== null && secondHalfMean !== null && secondHalfMean > firstHalfMean)) {
      nextKi = ctx.currentKi / 2;
      reason = overshootSuspect ? 'overshoot' : 'diverging';
    } else if (!improving) {
      nextKi = ctx.currentKi * 2;
      reason = 'too-slow';
    } else {
      // Improving but not yet converged within the palier: nudge ki up.
      nextKi = ctx.currentKi * 2;
      reason = 'still-converging';
    }

    if (nextKi < opts.kiMin) {
      ctx.kiFinal = ctx.bestKi ?? ctx.currentKi;
      setState('awaitPerturbation', { kpFinal: ctx.kpFinal, kiFinal: ctx.kiFinal, kiCollapsed: true });
      emit('awaitUserAction', { kind: 'perturbation', kpFinal: ctx.kpFinal, kiFinal: ctx.kiFinal, kiCollapsed: true });
      return;
    }

    // Track best ki by recent mean abs error.
    if (secondHalfMean !== null && secondHalfMean < ctx.bestKiErr) {
      ctx.bestKiErr = secondHalfMean;
      ctx.bestKi = ctx.currentKi;
    }

    ctx.kiIteration += 1;
    ctx.currentKi = nextKi;
    ctx.palierStartMs = sample.t;
    ctx.samples = [sample];
    emit('applyParams', { ki: nextKi });
    emit('progress', {
      step: 'tuningKi',
      currentKi: nextKi,
      kiIteration: ctx.kiIteration,
      reason,
      firstHalfMeanErr: firstHalfMean,
      secondHalfMeanErr: secondHalfMean,
    });
  }

  function tickPerturbationRecovering(sample) {
    const elapsed = sample.t - ctx.perturbationStartMs;
    if (elapsed < opts.perturbationRecoverMs) return;
    const osc = detectOscillationAbsolute(ctx.samples, ctx.perturbationStartMs);
    if (osc.oscillating) {
      // Recovery left residual oscillation: reduce ki and re-tune briefly.
      ctx.currentKi *= 0.7;
      ctx.kiIteration = Math.max(0, opts.kiMaxIterations - 1);
      ctx.palierStartMs = sample.t;
      ctx.samples = [sample];
      emit('applyParams', { ki: ctx.currentKi });
      setState('tuningKi', {
        currentKi: ctx.currentKi,
        reason: 'perturbation-oscillation',
      });
      return;
    }
    ctx.kiFinal = ctx.currentKi;
    ctx.longRunStartMs = sample.t;
    ctx.samples = [sample];
    setState('longRun', {
      kpFinal: ctx.kpFinal,
      kiFinal: ctx.kiFinal,
      longRunTargetMs: opts.longRunDefaultMs,
    });
  }

  function tickLongRun(sample) {
    const elapsed = sample.t - ctx.longRunStartMs;
    const canAbbreviate = elapsed >= opts.longRunMinAbbreviateMs;
    const reached = elapsed >= opts.longRunDefaultMs;
    if (canAbbreviate && !ctx.longRunCanAbbreviateEmitted) {
      ctx.longRunCanAbbreviateEmitted = true;
      emit('progress', { step: 'longRun', canAbbreviate: true, elapsedMs: elapsed });
    }
    if (reached || (canAbbreviate && ctx.abbreviateRequested)) {
      finishLongRun(sample);
    } else {
      // Light periodic progress (every ~5 s).
      if (!ctx.lastLongRunEmitMs || sample.t - ctx.lastLongRunEmitMs > 5000) {
        ctx.lastLongRunEmitMs = sample.t;
        emit('progress', { step: 'longRun', elapsedMs: elapsed });
      }
    }
  }

  function finishLongRun(sample) {
    const stats = computeRateStats(ctx.samples, opts.longRunStatsWindowMs);
    const rawMax = (stats.peakAbsPpm * opts.maxAdjustSafetyMargin) / 1e6;
    const maxAdjust = Math.max(rawMax, opts.maxAdjustFloor);
    const updateInterval = stats.stdPpm < opts.updateIntervalCleanStdPpm
      ? opts.updateIntervalClean
      : opts.updateIntervalDefault;
    ctx.maxAdjustFinal = maxAdjust;
    ctx.updateIntervalFinal = updateInterval;
    ctx.longRunDurationMs = sample.t - ctx.longRunStartMs;
    emit('applyParams', {
      maxAdjust,
      updateIntervalCallbacks: updateInterval,
    });
    ctx.palierStartMs = sample.t;
    ctx.samples = [sample];
    setState('tightening', {
      maxAdjustFinal: maxAdjust,
      updateIntervalFinal: updateInterval,
      maxAdjustWarn: maxAdjust > opts.maxAdjustWarnThreshold,
      rateStats: stats,
    });
  }

  function tickTightening(sample) {
    const elapsed = sample.t - ctx.palierStartMs;
    if (elapsed < opts.tighteningPalierMs) return;
    const osc = detectOscillationAbsolute(ctx.samples, ctx.palierStartMs);
    const conv = detectConvergence(ctx.samples);
    state = 'completed';
    const result = {
      kpCrit: ctx.kpCrit,
      kpFinal: ctx.kpFinal,
      kiFinal: ctx.kiFinal,
      maxAdjustFinal: ctx.maxAdjustFinal,
      updateIntervalFinal: ctx.updateIntervalFinal,
      tighteningOscillation: osc.oscillating,
      tighteningConverged: conv.converged,
    };
    emit('complete', result);
  }

  function userAck(kind) {
    if (kind === 'perturbation' && state === 'awaitPerturbation') {
      ctx.perturbationStartMs = Date.now();
      ctx.samples = [];
      setState('perturbationRecovering', {});
      return true;
    }
    if (kind === 'skipPerturbation' && state === 'awaitPerturbation') {
      ctx.kiFinal = ctx.currentKi;
      ctx.longRunStartMs = Date.now();
      ctx.samples = [];
      setState('longRun', {
        kpFinal: ctx.kpFinal,
        kiFinal: ctx.kiFinal,
        longRunTargetMs: opts.longRunDefaultMs,
        skippedPerturbation: true,
      });
      return true;
    }
    if (kind === 'resumeAfterSourceLoss' && state === 'suspended') {
      const restored = ctx.suspendedFromState;
      ctx.suspendedFromState = null;
      ctx.samples = [];
      // Restart the current palier from now to avoid biasing decisions with
      // pre-loss data.
      ctx.palierStartMs = Date.now();
      if (restored === 'longRun') {
        ctx.longRunStartMs = Date.now();
      }
      state = restored;
      emit('sourceRecovered', { restoredState: restored });
      return true;
    }
    return false;
  }

  function abbreviate() {
    if (state !== 'longRun') return false;
    ctx.abbreviateRequested = true;
    return true;
  }

  function cancel() {
    if (state === 'cancelled' || state === 'completed' || state === 'error') return false;
    state = 'cancelled';
    emit('cancelled', {});
    return true;
  }

  function getState() {
    return state;
  }

  function getContext() {
    return {
      currentKp: ctx.currentKp,
      currentKi: ctx.currentKi,
      kpCrit: ctx.kpCrit,
      kpFinal: ctx.kpFinal,
      kiFinal: ctx.kiFinal,
      maxAdjustFinal: ctx.maxAdjustFinal,
      updateIntervalFinal: ctx.updateIntervalFinal,
      kiIteration: ctx.kiIteration,
    };
  }

  return { on, pushSample, start, userAck, abbreviate, cancel, getState, getContext };

  function on(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }
}
