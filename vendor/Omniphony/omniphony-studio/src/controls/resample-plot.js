// Persistent sparkline shown between the resample gauge and the target
// latency block. Reuses the auto-tune sparkline renderer; runs only while
// visible to avoid extra polling cost when closed.

import { app } from '../state.js';
import { createSparkline } from '../auto-tune/sparkline.js';
import { getPlotWindowMs } from './diag-plot.js';

const POLL_INTERVAL_MS = 20;

let canvasEl = null;
let containerEl = null;
let toggleBtnEl = null;
let sparkline = null;
let intervalId = null;
let visible = false;

function getElements() {
  if (!canvasEl) canvasEl = document.getElementById('resamplePlotCanvas');
  if (!containerEl) containerEl = document.getElementById('resamplePlotContainer');
  if (!toggleBtnEl) toggleBtnEl = document.getElementById('resamplePlotToggleBtn');
  if (canvasEl && !sparkline) sparkline = createSparkline(canvasEl, { windowMs: getPlotWindowMs });
  return { canvasEl, containerEl, toggleBtnEl };
}

function tick() {
  if (!sparkline) return;
  sparkline.push({
    t: Date.now(),
    latencySmoothedMs: typeof app.latencySmoothedMs === 'number' ? app.latencySmoothedMs : null,
    latencyTargetMs: typeof app.latencyTargetMs === 'number' ? app.latencyTargetMs : null,
    resampleRatio: typeof app.resampleRatio === 'number' ? app.resampleRatio : null,
  });
  sparkline.render();
}

export function setResamplePlotVisible(open) {
  const { containerEl: c, toggleBtnEl: btn } = getElements();
  if (!c) return;
  visible = Boolean(open);
  c.style.display = visible ? 'block' : 'none';
  if (btn) {
    btn.classList.toggle('is-active', visible);
    btn.setAttribute('aria-pressed', visible ? 'true' : 'false');
  }
  if (visible) {
    if (!intervalId) intervalId = setInterval(tick, POLL_INTERVAL_MS);
    tick();
  } else {
    if (intervalId) {
      clearInterval(intervalId);
      intervalId = null;
    }
    if (sparkline) sparkline.clear();
  }
}

export function isResamplePlotVisible() {
  return visible;
}

export function toggleResamplePlot() {
  setResamplePlotVisible(!visible);
}
