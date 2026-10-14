// Minimal Canvas 2D sparkline for the PI auto-tune wizard.
// Renders two traces over the last `windowMs` of samples:
//   - latencySmoothedMs (with a dashed target line)
//   - rate_adjust_ppm (computed from resampleRatio)
// All in one canvas, two vertically stacked sub-areas.

export function createSparkline(canvas, options = {}) {
  const ctx = canvas.getContext('2d');
  const opts = {
    windowMs: 30000,
    background: 'rgba(15, 23, 36, 0.9)',
    latencyColor: '#7ad7ff',
    targetColor: 'rgba(255, 213, 74, 0.6)',
    rateColor: '#d6ff8c',
    gridColor: 'rgba(255, 255, 255, 0.07)',
    textColor: 'rgba(217, 236, 255, 0.85)',
    ...options,
  };

  // `windowMs` can be either a fixed number or a function — the latter lets
  // callers (e.g. the resample plot) follow a user-selected setting that
  // lives elsewhere. `retainMs` is the upper bound used for buffer pruning
  // so we keep enough history to render at the largest visible window.
  function currentWindowMs() {
    return typeof opts.windowMs === 'function' ? opts.windowMs() : opts.windowMs;
  }
  const retainMs = typeof opts.windowMs === 'function' ? 60000 : opts.windowMs;

  const buffer = [];

  function push(sample) {
    if (!sample || typeof sample.t !== 'number') return;
    buffer.push(sample);
    const cutoff = sample.t - retainMs;
    while (buffer.length && buffer[0].t < cutoff) buffer.shift();
  }

  function clear() {
    buffer.length = 0;
    render();
  }

  function rateAdjustPpm(s) {
    return typeof s.resampleRatio === 'number' && isFinite(s.resampleRatio)
      ? (s.resampleRatio - 1) * 1e6
      : null;
  }

  function render() {
    const w = canvas.width;
    const h = canvas.height;
    ctx.fillStyle = opts.background;
    ctx.fillRect(0, 0, w, h);

    if (buffer.length < 2) {
      ctx.fillStyle = opts.textColor;
      ctx.font = '10px sans-serif';
      ctx.fillText('Waiting for telemetry…', 6, h / 2);
      return;
    }

    const tMax = buffer[buffer.length - 1].t;
    const win = currentWindowMs();
    const tMin = tMax - win;
    const xFor = (t) => ((t - tMin) / win) * w;
    // Only consider samples within the visible window when computing
    // axis ranges; otherwise an older spike off-screen would compress
    // the live trace.
    const inWindow = (s) => s.t >= tMin;

    const topH = Math.floor(h * 0.5);
    const botH = h - topH;
    const botY0 = topH;

    // Compute trace ranges over the visible window only.
    let lMin = Infinity, lMax = -Infinity;
    let rMin = Infinity, rMax = -Infinity;
    let tgtMin = Infinity, tgtMax = -Infinity;
    for (const s of buffer) {
      if (!inWindow(s)) continue;
      if (typeof s.latencySmoothedMs === 'number') {
        if (s.latencySmoothedMs < lMin) lMin = s.latencySmoothedMs;
        if (s.latencySmoothedMs > lMax) lMax = s.latencySmoothedMs;
      }
      if (typeof s.latencyTargetMs === 'number') {
        if (s.latencyTargetMs < tgtMin) tgtMin = s.latencyTargetMs;
        if (s.latencyTargetMs > tgtMax) tgtMax = s.latencyTargetMs;
      }
      const r = rateAdjustPpm(s);
      if (r !== null) {
        if (r < rMin) rMin = r;
        if (r > rMax) rMax = r;
      }
    }
    if (lMin === Infinity) { lMin = 0; lMax = 1; }
    if (tgtMin !== Infinity) {
      lMin = Math.min(lMin, tgtMin);
      lMax = Math.max(lMax, tgtMax);
    }
    if (rMin === Infinity) { rMin = -1; rMax = 1; }
    // Capture the un-padded extents for the readout labels — the padded
    // versions below are only used for axis scaling.
    const lMinRaw = lMin;
    const lMaxRaw = lMax;
    const rMinRaw = rMin;
    const rMaxRaw = rMax;
    if (lMax - lMin < 1e-3) { lMax = lMin + 1; }
    if (rMax - rMin < 1e-3) { rMax = rMin + 1; }
    const lPad = (lMax - lMin) * 0.1;
    const rPad = (rMax - rMin) * 0.1;
    lMin -= lPad; lMax += lPad;
    rMin -= rPad; rMax += rPad;

    // Vertical time reference lines: bold every 1 s, light every 250 ms.
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.beginPath();
    let gt = Math.ceil(tMin / 250) * 250;
    while (gt <= tMax) {
      if (gt % 1000 !== 0) {
        const gx = Math.round(xFor(gt)) + 0.5;
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, h);
      }
      gt += 250;
    }
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
    ctx.beginPath();
    gt = Math.ceil(tMin / 1000) * 1000;
    while (gt <= tMax) {
      const gx = Math.round(xFor(gt)) + 0.5;
      ctx.moveTo(gx, 0);
      ctx.lineTo(gx, h);
      gt += 1000;
    }
    ctx.stroke();

    // Grid dividing the two areas.
    ctx.strokeStyle = opts.gridColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, topH + 0.5);
    ctx.lineTo(w, topH + 0.5);
    ctx.stroke();

    const yLatency = (v) => ((lMax - v) / (lMax - lMin)) * (topH - 4) + 2;
    const yRate = (v) => botY0 + ((rMax - v) / (rMax - rMin)) * (botH - 4) + 2;

    // Target dashed line (top area).
    if (tgtMin !== Infinity) {
      ctx.strokeStyle = opts.targetColor;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      const lastTarget = (() => {
        for (let i = buffer.length - 1; i >= 0; i -= 1) {
          if (typeof buffer[i].latencyTargetMs === 'number') return buffer[i].latencyTargetMs;
        }
        return null;
      })();
      if (lastTarget !== null) {
        const y = yLatency(lastTarget);
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }

    // Latency smoothed trace.
    ctx.strokeStyle = opts.latencyColor;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    for (const s of buffer) {
      if (typeof s.latencySmoothedMs !== 'number') continue;
      const x = xFor(s.t);
      const y = yLatency(s.latencySmoothedMs);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else { ctx.lineTo(x, y); }
    }
    if (started) ctx.stroke();

    // Rate-adjust zero baseline.
    ctx.strokeStyle = opts.gridColor;
    ctx.beginPath();
    const yZero = yRate(0);
    ctx.moveTo(0, yZero);
    ctx.lineTo(w, yZero);
    ctx.stroke();

    // Rate-adjust trace.
    ctx.strokeStyle = opts.rateColor;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    started = false;
    for (const s of buffer) {
      const v = rateAdjustPpm(s);
      if (v === null) continue;
      const x = xFor(s.t);
      const y = yRate(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else { ctx.lineTo(x, y); }
    }
    if (started) ctx.stroke();

    // Labels (use the un-padded extents and report the peak-to-peak Δ).
    ctx.fillStyle = opts.textColor;
    ctx.font = '10px sans-serif';
    ctx.textBaseline = 'top';
    const lDelta = lMaxRaw - lMinRaw;
    const rDelta = rMaxRaw - rMinRaw;
    ctx.fillText(`latency ${lMinRaw.toFixed(1)}…${lMaxRaw.toFixed(1)} ms (Δ ${lDelta.toFixed(1)})`, 4, 2);
    ctx.fillText(`rate ${rMinRaw.toFixed(0)}…${rMaxRaw.toFixed(0)} ppm (Δ ${rDelta.toFixed(0)})`, 4, botY0 + 2);
  }

  return { push, clear, render };
}
