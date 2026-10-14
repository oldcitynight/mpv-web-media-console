// Pure detector functions used by the PI auto-tune state machine.
//
// Each detector receives a window of telemetry samples shaped as
//   { t: ms, latencySmoothedMs, latencyTargetMs, resampleRatio, phase }
// and returns a verdict object. No DOM, no Tauri, no app state — these are
// fully testable in isolation.

export const TUNE_THRESHOLDS = {
  oscillation: {
    palierWarmupMs: 10000,            // discard the first 10 s of each palier (transient response to the kp patch)
    hysteresisPpm: 200,                // dead-band for crossing counting
    minCrossingsAbsolute: 4,           // floor on crossings for the current palier
    minAbsolutePeakToPeakPpm: 1500,    // floor on amplitude for the current palier
    peakToPeakJumpRatio: 3.0,          // current p-p must be ≥ this × max(baseline p-p) to declare oscillation
    crossingJumpRatio: 2.0,            // and crossing rate must be ≥ this × max(baseline crossing rate)
    baselinePaliers: 3,                // look back at this many non-saturated paliers for the baseline
    minBaselinePaliers: 1,             // need at least this many baselines to fire (otherwise: keep doubling kp)
  },
  saturation: {
    holdMs: 3000,
    threshold: 0.98,
  },
  convergence: {
    errFraction: 0.0002,  // |smoothed - target| < target * errFraction (= 0.02%)
    errFloorMs: 0.0,      // absolute floor (0 = trust the fraction entirely)
    holdMs: 10000,
  },
  sourceLoss: {
    windowMs: 10000,
    minLowRecoverEvents: 2,
  },
};

export function rateAdjustPpm(sample) {
  if (!sample || typeof sample.resampleRatio !== 'number' || !isFinite(sample.resampleRatio)) {
    return null;
  }
  return (sample.resampleRatio - 1) * 1e6;
}

export function errorMs(sample) {
  if (!sample || typeof sample.latencySmoothedMs !== 'number' || typeof sample.latencyTargetMs !== 'number') {
    return null;
  }
  return sample.latencySmoothedMs - sample.latencyTargetMs;
}

function sliceByWindow(samples, windowMs) {
  if (!samples.length) return [];
  const cutoff = samples[samples.length - 1].t - windowMs;
  let i = 0;
  while (i < samples.length && samples[i].t < cutoff) i += 1;
  return i > 0 ? samples.slice(i) : samples;
}

// Descriptive statistics for one kp palier on rate_adjust_ppm. The first
// `palierWarmupMs` of samples are discarded (transient response to the kp
// patch is not representative of the steady-state regime).
export function computePalierStats(samples, palierStartMs, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.oscillation, ...opts };
  if (!samples.length) return null;
  const fromMs = palierStartMs + cfg.palierWarmupMs;
  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  let n = 0;
  let firstStableT = null;
  let lastT = null;
  for (const s of samples) {
    if (s.t < fromMs) continue;
    const v = rateAdjustPpm(s);
    if (v === null) continue;
    if (firstStableT === null) firstStableT = s.t;
    lastT = s.t;
    if (v < min) min = v;
    if (v > max) max = v;
    sum += v;
    n += 1;
  }
  if (n < 4 || firstStableT === null) return null;
  const mean = sum / n;
  let state = 0;
  let crossings = 0;
  for (const s of samples) {
    if (s.t < fromMs) continue;
    const v = rateAdjustPpm(s);
    if (v === null) continue;
    if (v > mean + cfg.hysteresisPpm) {
      if (state === -1) crossings += 1;
      state = 1;
    } else if (v < mean - cfg.hysteresisPpm) {
      if (state === 1) crossings += 1;
      state = -1;
    }
  }
  const stableDurationMs = lastT - firstStableT;
  const crossingRate = stableDurationMs > 0 ? (crossings / stableDurationMs) * 1000 : 0;
  return {
    peakToPeakPpm: max - min,
    crossings,
    crossingRate,
    meanPpm: mean,
    samples: n,
    stableDurationMs,
  };
}

// Standalone "is this palier oscillating?" using only absolute floors.
// Used for post-perturbation recovery and the tightening palier — there is
// no kp-sweep baseline to compare against in those phases.
export function detectOscillationAbsolute(samples, palierStartMs, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.oscillation, ...opts };
  const stats = computePalierStats(samples, palierStartMs, cfg);
  if (!stats) {
    return { oscillating: false, reason: 'insufficient-samples', stats: null };
  }
  if (stats.crossings < cfg.minCrossingsAbsolute) {
    return { oscillating: false, reason: 'crossings-below-floor', stats };
  }
  if (stats.peakToPeakPpm < cfg.minAbsolutePeakToPeakPpm) {
    return { oscillating: false, reason: 'amplitude-below-floor', stats };
  }
  return { oscillating: true, reason: null, stats };
}

// Declare oscillation by comparing the current palier against the previous
// non-saturated paliers (the "baseline"). The point at which the signal goes
// from quasi-flat noise to actual oscillation is characterised by a sharp
// jump in BOTH amplitude (peak-to-peak) AND crossing rate. Using ratios
// removes the dependency on absolute thresholds that drift between hardware.
//
// Returns `{ oscillating, reason, ... }`. `reason` is set on rejection.
export function detectOscillationByJump(currentStats, baselineStats, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.oscillation, ...opts };
  if (!currentStats) {
    return { oscillating: false, reason: 'no-current-stats' };
  }
  if (currentStats.crossings < cfg.minCrossingsAbsolute) {
    return { oscillating: false, reason: 'crossings-below-floor', currentStats };
  }
  if (currentStats.peakToPeakPpm < cfg.minAbsolutePeakToPeakPpm) {
    return { oscillating: false, reason: 'amplitude-below-floor', currentStats };
  }
  const baselines = (baselineStats || []).filter((s) => s !== null);
  if (baselines.length < cfg.minBaselinePaliers) {
    return { oscillating: false, reason: 'baseline-too-short', currentStats, baselines };
  }
  const maxBaselinePP = Math.max(...baselines.map((s) => s.peakToPeakPpm));
  const maxBaselineCR = Math.max(...baselines.map((s) => s.crossingRate));
  const ppJump = maxBaselinePP > 0 ? currentStats.peakToPeakPpm / maxBaselinePP : Infinity;
  const crJump = maxBaselineCR > 0 ? currentStats.crossingRate / maxBaselineCR : Infinity;
  const oscillating = ppJump >= cfg.peakToPeakJumpRatio && crJump >= cfg.crossingJumpRatio;
  return {
    oscillating,
    reason: oscillating ? null : 'jump-below-ratio',
    currentStats,
    maxBaselinePeakToPeakPpm: maxBaselinePP,
    maxBaselineCrossingRate: maxBaselineCR,
    peakToPeakJump: ppJump,
    crossingJump: crJump,
  };
}

// Saturation: |rate_adjust_ppm| stays at or above `threshold × max_adjust × 1e6`
// over the trailing `holdMs`.
export function detectSaturation(samples, maxAdjustRatio, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.saturation, ...opts };
  if (!samples.length || !maxAdjustRatio) {
    return { saturated: false, durationMs: 0 };
  }
  const limit = cfg.threshold * Math.abs(maxAdjustRatio) * 1e6;
  const nowMs = samples[samples.length - 1].t;
  let startTime = null;
  for (let i = samples.length - 1; i >= 0; i -= 1) {
    const v = rateAdjustPpm(samples[i]);
    if (v === null || Math.abs(v) < limit) break;
    startTime = samples[i].t;
  }
  if (startTime === null) {
    return { saturated: false, durationMs: 0 };
  }
  const durationMs = nowMs - startTime;
  return { saturated: durationMs >= cfg.holdMs, durationMs };
}

// Convergence: |latencySmoothedMs − latencyTargetMs| stays under
// max(errFloorMs, |target| × errFraction) over the trailing `holdMs`.
// The fraction-of-target form makes the threshold scale with the operating
// point (0.02 % of 200 ms = 0.04 ms; 0.02 % of 500 ms = 0.10 ms).
export function detectConvergence(samples, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.convergence, ...opts };
  if (!samples.length) {
    return { converged: false, durationMs: 0 };
  }
  const nowMs = samples[samples.length - 1].t;
  let startTime = nowMs;
  let limitUsed = null;
  for (let i = samples.length - 1; i >= 0; i -= 1) {
    const s = samples[i];
    const err = errorMs(s);
    if (err === null) break;
    const tgt = typeof s.latencyTargetMs === 'number' ? s.latencyTargetMs : null;
    const limit = tgt !== null
      ? Math.max(cfg.errFloorMs, Math.abs(tgt) * cfg.errFraction)
      : cfg.errFloorMs;
    if (limit <= 0 || Math.abs(err) >= limit) break;
    limitUsed = limit;
    startTime = s.t;
  }
  const durationMs = nowMs - startTime;
  return { converged: durationMs >= cfg.holdMs, durationMs, limitMs: limitUsed };
}

// Source loss: count low-recover phase entries within the trailing window.
// One entry per transition (idle/stable → low-recover).
export function detectSourceLoss(samples, opts = {}) {
  const cfg = { ...TUNE_THRESHOLDS.sourceLoss, ...opts };
  if (!samples.length) {
    return { lost: false, events: 0 };
  }
  const window = sliceByWindow(samples, cfg.windowMs);
  let events = 0;
  let inLowRecover = false;
  for (const s of window) {
    const lr = s.phase === 'low-recover';
    if (lr && !inLowRecover) events += 1;
    inLowRecover = lr;
  }
  return { lost: events >= cfg.minLowRecoverEvents, events };
}

// Long-run statistics on rate_adjust_ppm — used to size max_adjust_final.
export function computeRateStats(samples, windowMs) {
  const window = windowMs ? sliceByWindow(samples, windowMs) : samples;
  let peakAbs = 0;
  let sum = 0;
  let sumSq = 0;
  let n = 0;
  for (const s of window) {
    const v = rateAdjustPpm(s);
    if (v === null) continue;
    if (Math.abs(v) > peakAbs) peakAbs = Math.abs(v);
    sum += v;
    sumSq += v * v;
    n += 1;
  }
  if (n === 0) {
    return { peakAbsPpm: 0, meanPpm: 0, stdPpm: 0, samples: 0 };
  }
  const mean = sum / n;
  const variance = Math.max(0, sumSq / n - mean * mean);
  return { peakAbsPpm: peakAbs, meanPpm: mean, stdPpm: Math.sqrt(variance), samples: n };
}
