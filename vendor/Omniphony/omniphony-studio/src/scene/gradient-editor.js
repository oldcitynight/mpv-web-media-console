/**
 * Compact editor for the custom gradients backing the 'custom' colormap.
 *
 * There are two independent gradients — object energy field
 * (`app.objectCustomGradientStops`) and speaker heatmap
 * (`app.speakerCustomGradientStops`) — so each editor is bound to a `target`
 * ('object' | 'speaker') and only ever touches that target's stops.
 *
 * UI: a gradient bar with a row of draggable handles beneath it (one per stop).
 *  - drag a handle to move its stop;
 *  - click a handle to select it → a popover opens under it with a colour picker
 *    and a delete button;
 *  - double-click the bar to add a stop there, coloured by sampling the current
 *    gradient at that position.
 *
 * Stops are kept sorted by `pos` (consumers assume order). Edits bump the speaker
 * gradient's version (so the static speaker volume's rebuild guard re-runs) and
 * call `onChange(target)` (refresh the matching render + persist + overlay push).
 */

import { app } from '../state.js';
import { t } from '../i18n.js';
import { MAX_CUSTOM_STOPS } from './object-energy-shared.js';

const editors = []; // { el, target }
const selectedByTarget = { object: null, speaker: null };
let onChange = () => {};

export function setGradientEditorOnChange(cb) {
  onChange = typeof cb === 'function' ? cb : () => {};
}

function clamp01(v) {
  return Math.max(0, Math.min(1, Number(v) || 0));
}

function toHex(r, g, b) {
  const h = (v) => Math.round(clamp01(v) * 255).toString(16).padStart(2, '0');
  return `#${h(r)}${h(g)}${h(b)}`;
}

function rgbToHsv(r, g, b) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
    if (h < 0) h += 1;
  }
  return { h, s: max === 0 ? 0 : d / max, v: max };
}

function hsvToRgb(h, s, v) {
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s);
  const q = v * (1 - f * s);
  const tt = v * (1 - (1 - f) * s);
  switch (((i % 6) + 6) % 6) {
    case 0: return { r: v, g: tt, b: p };
    case 1: return { r: q, g: v, b: p };
    case 2: return { r: p, g: v, b: tt };
    case 3: return { r: p, g: q, b: v };
    case 4: return { r: tt, g: p, b: v };
    default: return { r: v, g: p, b: q };
  }
}

// Pointer drag on `el`: call `handler(event)` on press and while held (captured,
// so it keeps tracking outside the element).
function attachDrag(el, handler) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      el.setPointerCapture(e.pointerId);
    } catch (_) {
      // ignore if the pointer is already gone
    }
    handler(e);
    const move = (ev) => handler(ev);
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
}

// Inline HSV picker (saturation/value square + hue slider) — no native dialog or
// swatch palette. `onColor({r,g,b})` fires live while dragging.
function buildColorPicker(rgb, onColor) {
  const W = 132;
  const H = 84;
  let { h, s, v } = rgbToHsv(rgb.r, rgb.g, rgb.b);

  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;flex-direction:column;gap:0.3rem';

  const sv = document.createElement('div');
  sv.style.cssText = `position:relative;width:${W}px;height:${H}px;border-radius:4px;cursor:crosshair;touch-action:none`;
  const svThumb = document.createElement('div');
  svThumb.style.cssText =
    'position:absolute;width:10px;height:10px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 2px rgba(0,0,0,0.9);transform:translate(-50%,-50%);pointer-events:none';
  sv.appendChild(svThumb);

  const hue = document.createElement('div');
  hue.style.cssText =
    `position:relative;width:${W}px;height:12px;border-radius:6px;cursor:ew-resize;touch-action:none;` +
    'background:linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)';
  const hueThumb = document.createElement('div');
  hueThumb.style.cssText =
    'position:absolute;top:50%;width:12px;height:12px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 2px rgba(0,0,0,0.9);transform:translate(-50%,-50%);pointer-events:none';
  hue.appendChild(hueThumb);

  const paint = () => {
    sv.style.background =
      `linear-gradient(to top, #000, rgba(0,0,0,0)), linear-gradient(to right, #fff, rgba(255,255,255,0)), hsl(${(h * 360).toFixed(0)}, 100%, 50%)`;
    svThumb.style.left = `${s * 100}%`;
    svThumb.style.top = `${(1 - v) * 100}%`;
    hueThumb.style.left = `${h * 100}%`;
  };
  const commit = () => onColor(hsvToRgb(h, s, v));
  paint();

  attachDrag(sv, (e) => {
    const rect = sv.getBoundingClientRect();
    s = clamp01((e.clientX - rect.left) / Math.max(1, rect.width));
    v = clamp01(1 - (e.clientY - rect.top) / Math.max(1, rect.height));
    paint();
    commit();
  });
  attachDrag(hue, (e) => {
    const rect = hue.getBoundingClientRect();
    h = clamp01((e.clientX - rect.left) / Math.max(1, rect.width));
    paint();
    commit();
  });

  wrap.appendChild(sv);
  wrap.appendChild(hue);
  return wrap;
}

function stopsOf(target) {
  return target === 'speaker' ? app.speakerCustomGradientStops : app.objectCustomGradientStops;
}

function sortStops(target) {
  stopsOf(target).sort((a, b) => a.pos - b.pos);
}

function cssGradient(target) {
  const s = stopsOf(target);
  if (!s.length) return 'linear-gradient(to right, #000, #fff)';
  if (s.length === 1) {
    const c = toHex(s[0].r, s[0].g, s[0].b);
    return `linear-gradient(to right, ${c}, ${c})`;
  }
  const sorted = [...s].sort((a, b) => a.pos - b.pos);
  return `linear-gradient(to right, ${sorted
    .map((x) => `${toHex(x.r, x.g, x.b)} ${(clamp01(x.pos) * 100).toFixed(1)}%`)
    .join(', ')})`;
}

// Sample the target's gradient at `pos` so an inserted stop blends in seamlessly.
function sampleColorAt(target, pos) {
  const s = [...stopsOf(target)].sort((a, b) => a.pos - b.pos);
  if (!s.length) return { r: 1, g: 1, b: 1 };
  if (pos <= s[0].pos) return { r: s[0].r, g: s[0].g, b: s[0].b };
  const last = s[s.length - 1];
  if (pos >= last.pos) return { r: last.r, g: last.g, b: last.b };
  for (let i = 0; i + 1 < s.length; i += 1) {
    const a = s[i];
    const b = s[i + 1];
    if (pos <= b.pos) {
      const f = b.pos > a.pos ? (pos - a.pos) / (b.pos - a.pos) : 0;
      return { r: a.r + (b.r - a.r) * f, g: a.g + (b.g - a.g) * f, b: a.b + (b.b - a.b) * f };
    }
  }
  return { r: last.r, g: last.g, b: last.b };
}

function emit(target) {
  if (target === 'speaker') {
    app.speakerCustomGradientVersion = (app.speakerCustomGradientVersion | 0) + 1;
  }
  onChange(target);
}

// Refresh just the bar backgrounds for a target (no re-render → smooth dragging).
function updateBars(target) {
  const bg = cssGradient(target);
  for (const e of editors) {
    if (e.target !== target) continue;
    const bar = e.el.querySelector('.gradient-bar');
    if (bar) bar.style.background = bg;
  }
}

function addStopAt(target, pos) {
  const s = stopsOf(target);
  if (s.length >= MAX_CUSTOM_STOPS) return;
  const col = sampleColorAt(target, pos);
  const stop = { pos: clamp01(pos), r: col.r, g: col.g, b: col.b };
  s.push(stop);
  sortStops(target);
  selectedByTarget[target] = stop;
  renderTarget(target);
  emit(target);
}

function removeStop(target, stop) {
  const s = stopsOf(target);
  if (s.length <= 2) return;
  const idx = s.indexOf(stop);
  if (idx >= 0) {
    s.splice(idx, 1);
    if (selectedByTarget[target] === stop) selectedByTarget[target] = null;
    renderTarget(target);
    emit(target);
  }
}

function startDrag(editor, stop, handleEl, bar, pointerId, wasSelected, startX) {
  let moved = false;
  const move = (ev) => {
    if (!moved && Math.abs(ev.clientX - startX) > 3) moved = true;
    const rect = bar.getBoundingClientRect();
    stop.pos = clamp01((ev.clientX - rect.left) / Math.max(1, rect.width));
    handleEl.style.left = `${stop.pos * 100}%`;
    handleEl.title = `${Math.round(stop.pos * 100)}%`;
    sortStops(editor.target);
    updateBars(editor.target);
    emit(editor.target);
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    // A click (no real drag) on the already-selected handle closes the picker.
    if (!moved && wasSelected) selectedByTarget[editor.target] = null;
    renderOne(editor); // resync handle order + reposition/close the popover
  };
  try {
    handleEl.setPointerCapture(pointerId);
  } catch (_) {
    // setPointerCapture can throw if the pointer is already gone; ignore.
  }
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

function renderOne(editor) {
  const { el, target } = editor;
  const stops = stopsOf(target);
  const selected = selectedByTarget[target];
  const hasSelection = selected && stops.includes(selected);

  el.innerHTML = '';
  el.style.position = 'relative';
  // Reserve room for the absolutely-positioned popover (inline picker) when a stop
  // is selected.
  el.style.paddingBottom = hasSelection ? '150px' : '';

  const bar = document.createElement('div');
  bar.className = 'gradient-bar';
  bar.style.cssText =
    'height:20px;border-radius:4px;border:1px solid rgba(255,255,255,0.15);position:relative;cursor:crosshair;background:' +
    cssGradient(target);
  bar.title = t('heatmap.gradient.addHint');
  bar.addEventListener('dblclick', (e) => {
    const rect = bar.getBoundingClientRect();
    addStopAt(target, (e.clientX - rect.left) / Math.max(1, rect.width));
  });
  el.appendChild(bar);

  // Pulled up so the handles' pointed tips overlap the bottom of the bar. The strip
  // ignores pointer events (only the handles capture) so double-click-to-add still
  // works in the overlap zone.
  const strip = document.createElement('div');
  strip.style.cssText = 'position:relative;height:22px;margin-top:-6px;pointer-events:none';
  el.appendChild(strip);

  // Pin shape: a pentagon with a point at the top centre, overlapping the bar.
  const PIN_CLIP = 'polygon(50% 0%, 100% 34%, 100% 100%, 0% 100%, 0% 34%)';
  const pinShadow = (sel) =>
    `drop-shadow(0 0 1px rgba(0,0,0,0.85))${sel ? ' drop-shadow(0 0 2px #fff) drop-shadow(0 0 1px #fff)' : ''}`;

  let selectedHandleEl = null;
  stops.forEach((stop) => {
    const handle = document.createElement('div');
    const isSel = stop === selected;
    handle.style.cssText =
      `position:absolute;left:${clamp01(stop.pos) * 100}%;top:0;transform:translateX(-50%);` +
      `width:14px;height:18px;background:${toHex(stop.r, stop.g, stop.b)};clip-path:${PIN_CLIP};` +
      `filter:${pinShadow(isSel)};cursor:ew-resize;touch-action:none;pointer-events:auto`;
    handle.title = `${Math.round(stop.pos * 100)}%`;
    if (isSel) selectedHandleEl = handle;
    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const wasSelected = selectedByTarget[target] === stop;
      selectedByTarget[target] = stop;
      handle.style.filter = pinShadow(true); // immediate selection feedback
      startDrag(editor, stop, handle, bar, e.pointerId, wasSelected, e.clientX);
    });
    strip.appendChild(handle);
  });

  if (hasSelection) {
    const pop = document.createElement('div');
    pop.style.cssText =
      `position:absolute;left:clamp(80px, ${clamp01(selected.pos) * 100}%, calc(100% - 80px));` +
      'transform:translateX(-50%);top:38px;display:flex;flex-direction:column;gap:0.3rem;' +
      'background:rgba(20,20,24,0.96);border:1px solid rgba(255,255,255,0.18);border-radius:6px;' +
      'padding:0.3rem;z-index:5';

    // Header: a live colour swatch + delete (trash) and close buttons.
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;gap:0.35rem';
    const swatch = document.createElement('div');
    swatch.style.cssText =
      `width:18px;height:18px;border-radius:50%;border:1px solid rgba(255,255,255,0.3);flex:none;background:${toHex(selected.r, selected.g, selected.b)}`;
    const spacer = document.createElement('div');
    spacer.style.flex = '1';
    const del = document.createElement('button');
    del.type = 'button';
    del.textContent = '🗑';
    del.title = t('heatmap.gradient.removeStop');
    del.style.cssText =
      'border:none;background:rgba(220,80,80,0.2);color:#fff;border-radius:4px;cursor:pointer;width:22px;height:22px;flex:none;font-size:12px;line-height:1';
    del.disabled = stops.length <= 2;
    if (del.disabled) del.style.opacity = '0.4';
    del.addEventListener('click', () => removeStop(target, selected));
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.textContent = '✕';
    closeBtn.title = t('heatmap.gradient.close');
    closeBtn.style.cssText =
      'border:none;background:rgba(255,255,255,0.1);color:#fff;border-radius:4px;cursor:pointer;width:22px;height:22px;flex:none';
    closeBtn.addEventListener('click', () => deselect(target));
    head.appendChild(swatch);
    head.appendChild(spacer);
    head.appendChild(del);
    head.appendChild(closeBtn);

    const picker = buildColorPicker({ r: selected.r, g: selected.g, b: selected.b }, (c) => {
      selected.r = c.r;
      selected.g = c.g;
      selected.b = c.b;
      const hex = toHex(c.r, c.g, c.b);
      swatch.style.background = hex;
      if (selectedHandleEl) selectedHandleEl.style.background = hex;
      updateBars(target);
      emit(target);
    });

    pop.appendChild(head);
    pop.appendChild(picker);
    el.appendChild(pop);
  }
}

// Re-render only the editors bound to a given target (the gradients are independent).
function renderTarget(target) {
  editors.forEach((e) => {
    if (e.target === target) renderOne(e);
  });
}

// Close the picker for a target (clear selection + re-render).
function deselect(target) {
  if (selectedByTarget[target]) {
    selectedByTarget[target] = null;
    renderTarget(target);
  }
}

/** Mount the editor for `target` ('object' | 'speaker') into a container. */
export function registerGradientEditor(container, target) {
  if (!container || editors.some((e) => e.el === container)) return;
  const editor = { el: container, target: target === 'speaker' ? 'speaker' : 'object' };
  editors.push(editor);
  renderOne(editor);
}

/** Re-render all editors (e.g. after the stops were loaded from prefs). */
export function refreshGradientEditors() {
  editors.forEach(renderOne);
}
