import { getWindowViewport, subscribeWindowViewport } from '../../core/viewport/window-viewport.js';

const STORAGE_KEY = 'spatialviz.side_panels';
const MIN_WIDTH = 220;
const DEFAULT_WIDTH = 440;
const COLLAPSED_WIDTH_REM = 1.8;
const PANEL_EDGE_MARGIN_REM = 1; // CSS left/right: 1rem on each panel
// Panels are content-box: --panel-width-X is the content width only, so the
// on-screen footprint adds the horizontal padding (2 x 1rem) and border (2 x 1px).
const PANEL_PADDING_REM = 2;
const PANEL_BORDER_PX = 2;
const PANEL_SAFETY_GAP_PX = 4;

function remToPx(rem) {
  const fontSize = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  return rem * fontSize;
}

export function collapsedPx() {
  return remToPx(COLLAPSED_WIDTH_REM);
}

function panelChromePx() {
  return remToPx(PANEL_PADDING_REM) + PANEL_BORDER_PX;
}

// Only the outer margins plus a tiny gap are reserved, so the two panels can
// meet at the center (hiding it) without overlapping.
function reservePx() {
  return remToPx(2 * PANEL_EDGE_MARGIN_REM) + PANEL_SAFETY_GAP_PX;
}

const state = {
  left: { width: DEFAULT_WIDTH, collapsed: false },
  right: { width: DEFAULT_WIDTH, collapsed: false }
};

const subscribers = new Set();
let initialized = false;

export function emitOverlayLayoutChanged(reason) {
  if (typeof window === 'undefined') {
    return;
  }
  window.dispatchEvent(new CustomEvent('omniphony:overlay-layout-changed', {
    detail: { reason }
  }));
}

function snapshot() {
  return {
    left: { ...state.left },
    right: { ...state.right }
  };
}

function notifySubscribers() {
  const nextState = snapshot();
  subscribers.forEach((listener) => {
    listener(nextState);
  });
}

function persistState() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (_e) {
    // ignore
  }
}

// On-screen footprint of a panel. Collapsed panels drop their padding/border
// (see .panel-collapsed), so they only occupy collapsedPx().
function effectiveWidth(side) {
  const s = state[side];
  return s.collapsed ? collapsedPx() : s.width + panelChromePx();
}

function clampWidth(width, side) {
  const { width: viewportWidth } = getWindowViewport();
  const other = side === 'left' ? 'right' : 'left';
  const maxWidth = Math.max(
    MIN_WIDTH,
    Math.floor(viewportWidth - effectiveWidth(other) - panelChromePx() - reservePx())
  );
  return Math.min(Math.max(Number(width) || DEFAULT_WIDTH, MIN_WIDTH), maxWidth);
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return;
    }
    const parsed = JSON.parse(raw);
    for (const side of ['left', 'right']) {
      const entry = parsed?.[side];
      if (!entry) {
        continue;
      }
      const nextWidth = Number(entry.width);
      if (Number.isFinite(nextWidth) && nextWidth >= MIN_WIDTH) {
        state[side].width = clampWidth(nextWidth, side);
      }
      state[side].collapsed = !!entry.collapsed;
    }
  } catch (_e) {
    // ignore
  }
}

function clampAllWidths() {
  state.left.width = clampWidth(state.left.width, 'left');
  state.right.width = clampWidth(state.right.width, 'right');
}

export function initOverlayLayoutState() {
  if (initialized) {
    return;
  }
  initialized = true;
  loadState();
  clampAllWidths();
  subscribeWindowViewport(() => {
    clampAllWidths();
    notifySubscribers();
  });
}

export function getOverlayLayoutState() {
  initOverlayLayoutState();
  return snapshot();
}

export function subscribeOverlayLayout(listener) {
  initOverlayLayoutState();
  subscribers.add(listener);
  listener(snapshot());
  return () => {
    subscribers.delete(listener);
  };
}

export function setOverlayPanelWidth(side, width, reason = 'side-panel-resize') {
  initOverlayLayoutState();
  if (!state[side]) {
    return;
  }
  state[side].width = clampWidth(width, side);
  persistState();
  notifySubscribers();
  emitOverlayLayoutChanged(reason);
}

export function setOverlayPanelCollapsed(side, collapsed, reason = 'side-panel-collapse') {
  initOverlayLayoutState();
  if (!state[side]) {
    return;
  }
  state[side].collapsed = !!collapsed;
  clampAllWidths();
  persistState();
  notifySubscribers();
  emitOverlayLayoutChanged(reason);
}

export function resetOverlayPanelWidth(side, reason = 'side-panel-width-reset') {
  setOverlayPanelWidth(side, DEFAULT_WIDTH, reason);
}
