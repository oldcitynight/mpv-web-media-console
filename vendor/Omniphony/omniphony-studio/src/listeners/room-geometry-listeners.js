import { app, isRoomRatioFrozen } from '../state.js';
import {
  getRoomCenterBlendFromInput, renderRoomCenterBlendControl,
  normalizeRoomGeometryInputDisplays, updateRoomGeometryButtonsState,
  applyRoomGeometryNow, scheduleRoomGeometryApply,
  updateRoomGeometryLivePreview, setRoomGeometryExpanded,
  previewRoomGeometryScene
} from '../controls/room-geometry.js';

export function setupRoomGeometryListeners() {
  const roomGeometryToggleBtnEl = document.getElementById('roomGeometryToggleBtn');
  const roomDimWidthInputEl = document.getElementById('roomDimWidthInput');
  const roomDimLengthInputEl = document.getElementById('roomDimLengthInput');
  const roomDimHeightInputEl = document.getElementById('roomDimHeightInput');
  const roomDimRearInputEl = document.getElementById('roomDimRearInput');
  const roomDimLowerInputEl = document.getElementById('roomDimLowerInput');
  const roomRatioCenterBlendSliderEl = document.getElementById('roomRatioCenterBlendSlider');
  const roomRatioCenterBlendValueEl = document.getElementById('roomRatioCenterBlendValue');

  if (roomGeometryToggleBtnEl) {
    roomGeometryToggleBtnEl.addEventListener('click', () => {
      setRoomGeometryExpanded(!app.roomGeometryExpanded);
    });
  }

  [
    roomDimWidthInputEl,
    roomDimLengthInputEl,
    roomDimHeightInputEl,
    roomDimRearInputEl,
    roomDimLowerInputEl
  ].forEach((el) => {
    if (!el) return;
    // While typing: only preview the 3D scene from the typed (uncommitted)
    // values — no field rewrite, no push to orender — so a partial keystroke
    // isn't reset mid-edit. Commit on blur ('change') or Enter.
    el.addEventListener('input', () => {
      if (isRoomRatioFrozen()) return;
      previewRoomGeometryScene();
      updateRoomGeometryButtonsState();
    });
    el.addEventListener('change', () => {
      if (isRoomRatioFrozen()) return;
      normalizeRoomGeometryInputDisplays();
      updateRoomGeometryLivePreview();
      updateRoomGeometryButtonsState();
      applyRoomGeometryNow();
    });
    el.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      if (isRoomRatioFrozen()) return;
      e.preventDefault();
      el.blur(); // fires 'change' → commit
    });
  });

  if (roomRatioCenterBlendSliderEl) {
    roomRatioCenterBlendSliderEl.addEventListener('input', () => {
      if (isRoomRatioFrozen()) return;
      renderRoomCenterBlendControl(getRoomCenterBlendFromInput());
      updateRoomGeometryLivePreview();
      updateRoomGeometryButtonsState();
      scheduleRoomGeometryApply();
    });
    roomRatioCenterBlendSliderEl.addEventListener('change', () => {
      if (isRoomRatioFrozen()) return;
      renderRoomCenterBlendControl(getRoomCenterBlendFromInput());
      updateRoomGeometryLivePreview();
      updateRoomGeometryButtonsState();
      applyRoomGeometryNow();
    });
    roomRatioCenterBlendSliderEl.addEventListener('dblclick', () => {
      if (isRoomRatioFrozen()) return;
      renderRoomCenterBlendControl(0.5);
      updateRoomGeometryLivePreview();
      updateRoomGeometryButtonsState();
      applyRoomGeometryNow();
    });
  }

  if (roomRatioCenterBlendValueEl) {
    roomRatioCenterBlendValueEl.addEventListener('dblclick', () => {
      if (isRoomRatioFrozen()) return;
      renderRoomCenterBlendControl(0.5);
      updateRoomGeometryLivePreview();
      updateRoomGeometryButtonsState();
      applyRoomGeometryNow();
    });
  }
}
