/**
 * Side panels (#overlay, #speakersOverlay) collapse/resize behaviour.
 *
 * Each panel exposes a collapse button and a draggable handle on its inner
 * edge. The current width and collapsed state are persisted in localStorage
 * and surfaced as CSS custom properties (--panel-width-left,
 * --panel-width-right) so that the log overlay can be positioned accordingly.
 */

import {
  collapsedPx,
  emitOverlayLayoutChanged,
  getOverlayLayoutState,
  initOverlayLayoutState,
  resetOverlayPanelWidth,
  setOverlayPanelCollapsed,
  setOverlayPanelWidth,
  subscribeOverlayLayout
} from './layout/overlay-layout-state.js';

const HAMBURGER_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none" aria-hidden="true"><path d="M3 5h10M3 8h10M3 11h10"/></svg>';
const SPEAKER_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2.5 6h2L8 3v10L4.5 10h-2z"/><path d="M10 5.5q1.5 2.5 0 5" stroke="currentColor" stroke-width="1" fill="none" stroke-linecap="round"/><path d="M12 4q2 4 0 8" stroke="currentColor" stroke-width="1" fill="none" stroke-linecap="round"/></svg>';

const SIDES = {
  left: {
    panelId: 'overlay',
    cssVar: '--panel-width-left',
    handleEdge: 'right',
    collapseBtnId: 'leftPanelCollapseBtn',
    resizeHandleId: 'leftPanelResizeHandle',
    direction: 1
  },
  right: {
    panelId: 'speakersOverlay',
    cssVar: '--panel-width-right',
    handleEdge: 'left',
    collapseBtnId: 'rightPanelCollapseBtn',
    resizeHandleId: 'rightPanelResizeHandle',
    direction: -1
  }
};

function setResizeVisualState(active) {
  document.body.classList.toggle('overlay-layout-resizing', active);
}

function applyToDom(state) {
  const root = document.documentElement;
  for (const side of ['left', 'right']) {
    const cfg = SIDES[side];
    const panel = document.getElementById(cfg.panelId);
    const s = state[side];
    const effective = s.collapsed ? collapsedPx() : s.width;
    root.style.setProperty(cfg.cssVar, `${effective}px`);
    if (panel) {
      panel.classList.toggle('panel-collapsed', s.collapsed);
      const btn = document.getElementById(cfg.collapseBtnId);
      if (btn) {
        btn.setAttribute('aria-expanded', String(!s.collapsed));
      }
    }
  }
}

function makeResizeHandle(side) {
  const cfg = SIDES[side];
  const panel = document.getElementById(cfg.panelId);
  if (!panel) return;
  if (document.getElementById(cfg.resizeHandleId)) return;
  const handle = document.createElement('div');
  handle.id = cfg.resizeHandleId;
  handle.className = `panel-resize-handle panel-resize-handle-${cfg.handleEdge}`;
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', 'vertical');
  panel.appendChild(handle);

  let startX = 0;
  let startWidth = 0;
  let activePointerId = null;

  const onPointerMove = (ev) => {
    const dx = ev.clientX - startX;
    const proposed = startWidth + cfg.direction * dx;
    setOverlayPanelWidth(side, proposed, `side-panel-resize-${side}`);
  };

  const onPointerUp = (ev) => {
    const didResize = activePointerId !== null;
    if (activePointerId !== null) {
      try { handle.releasePointerCapture(activePointerId); } catch (_e) {}
    }
    activePointerId = null;
    handle.classList.remove('panel-resize-handle-active');
    document.body.style.userSelect = '';
    setResizeVisualState(false);
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('pointercancel', onPointerUp);
    if (didResize) {
      emitOverlayLayoutChanged(`side-panel-resize-end-${side}`);
    }
  };

  handle.addEventListener('pointerdown', (ev) => {
    const state = getOverlayLayoutState();
    if (state[side].collapsed) return;
    ev.preventDefault();
    startX = ev.clientX;
    startWidth = state[side].width;
    activePointerId = ev.pointerId;
    try { handle.setPointerCapture(ev.pointerId); } catch (_e) {}
    handle.classList.add('panel-resize-handle-active');
    document.body.style.userSelect = 'none';
    setResizeVisualState(true);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
  });

  handle.addEventListener('dblclick', () => {
    resetOverlayPanelWidth(side, `side-panel-width-reset-${side}`);
  });
}

function makeCollapseButton(side) {
  const cfg = SIDES[side];
  const panel = document.getElementById(cfg.panelId);
  if (!panel) return;
  if (document.getElementById(cfg.collapseBtnId)) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = cfg.collapseBtnId;
  btn.className = `panel-side-collapse-btn panel-side-collapse-btn-${side}`;
  btn.setAttribute('aria-expanded', 'true');
  btn.title = side === 'left' ? 'Toggle controls' : 'Toggle speakers';
  btn.innerHTML = side === 'left' ? HAMBURGER_SVG : SPEAKER_SVG;
  panel.appendChild(btn);

  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const state = getOverlayLayoutState();
    setOverlayPanelCollapsed(side, !state[side].collapsed, `side-panel-collapse-${side}`);
  });

  // Body fallback: clicking anywhere on the collapsed strip (outside the
  // button and the resize handle) re-expands the panel.
  panel.addEventListener('click', (ev) => {
    const state = getOverlayLayoutState();
    if (!state[side].collapsed) return;
    if (ev.target.closest('.panel-resize-handle')) return;
    if (ev.target.closest(`#${cfg.collapseBtnId}`)) return;
    setOverlayPanelCollapsed(side, false, `side-panel-expand-${side}`);
  });
}

export function initSidePanels() {
  initOverlayLayoutState();
  for (const side of ['left', 'right']) {
    makeResizeHandle(side);
    makeCollapseButton(side);
  }
  subscribeOverlayLayout(applyToDom);
}
