import { app, sourceLabels, speakerLabels } from './state.js';
import { pushLog } from './log.js';
import { camera, renderer, scene, rebuildRendererOnFreshCanvas, teardownRenderer } from './scene/setup.js';
import { rebuildLabelSpriteTexture } from './scene/labels.js';
import { rebuildAllTrailRenderables } from './trails.js';
import { rebuildRoomDimensionGuideResources, updateRoomDimensionGuides } from './controls/room-geometry.js';
import { rebindPointerListeners } from './picking.js';
import { getRenderSurfaceInvariantState } from './core/render/render-surface-controller.js';
import { pointerEventToNdc } from './core/render/projection-service.js';

let recoveryCanvas = null;
let scheduledRecoveryTimer = null;
const visualRecoveryStats = {
  rebuilds: 0,
  rendererRebuilds: 0,
  overlayEvents: 0,
  overlayResizeSessions: 0,
  overlayResizeFallbacks: 0,
  overlayInvariantFailures: 0,
  reasons: {},
  recentReasons: [],
  recentSnapshots: []
};
const overlayResizeState = {
  active: null
};

function pushRecent(list, value, maxLength) {
  list.push(value);
  if (list.length > maxLength) {
    list.shift();
  }
}

function getLabelDebugStats() {
  if (typeof window === 'undefined') {
    return null;
  }
  return window.omniphonyDebug?.labelStats ?? null;
}

function summarizeCounters() {
  const labelStats = getLabelDebugStats();
  return {
    visualRebuilds: visualRecoveryStats.rebuilds,
    rendererRebuilds: visualRecoveryStats.rendererRebuilds,
    labelTextureRebuilds: labelStats?.textureRebuilds ?? null
  };
}

function captureRecoverySnapshot(reason, phase = 'sample') {
  if (typeof window === 'undefined') {
    return null;
  }
  const surface = getRenderSurfaceInvariantState();
  const viewport = surface.viewport;
  const screenPoint = {
    x: Math.max(0, Math.min(viewport.width - 1, 160)),
    y: Math.max(0, Math.min(viewport.height - 1, 160))
  };
  const ndc = surface.rect
    ? pointerEventToNdc(
      { clientX: screenPoint.x, clientY: screenPoint.y },
      surface.rect,
      { x: 0, y: 0 }
    )
    : null;
  const expectedAspect = viewport.width / viewport.height;
  const snapshot = {
    t: Date.now(),
    reason,
    phase,
    surface,
    screenPoint,
    ndc: ndc ? { x: ndc.x, y: ndc.y } : null,
    cameraAspect: camera.aspect,
    expectedAspect,
    cameraAspectDelta: Math.abs(camera.aspect - expectedAspect),
    counters: summarizeCounters()
  };
  pushRecent(visualRecoveryStats.recentSnapshots, snapshot, 60);
  return snapshot;
}

function attachVisualRecoveryDebugHandle() {
  if (typeof window === 'undefined') {
    return;
  }
  const existing = window.omniphonyDebug && typeof window.omniphonyDebug === 'object'
    ? window.omniphonyDebug
    : {};
  window.omniphonyDebug = {
    ...existing,
    visualRecoveryStats,
    captureVisualRecoverySnapshot: captureRecoverySnapshot
  };
}

attachVisualRecoveryDebugHandle();

function onContextLost(event) {
  event.preventDefault();
  app.webglContextLossCount = (Number(app.webglContextLossCount) || 0) + 1;
  pushLog('warn', `WebGL context lost (#${app.webglContextLossCount}).`);
}

function onContextRestored() {
  pushLog('warn', 'WebGL context restored. Rebuilding WebGL renderer and visual resources.');
  requestAnimationFrame(() => {
    rebuildRenderer('webglcontextrestored');
  });
}

function bindRecoveryListeners() {
  const canvas = renderer.domElement;
  if (recoveryCanvas === canvas) {
    return;
  }
  if (recoveryCanvas) {
    recoveryCanvas.removeEventListener('webglcontextlost', onContextLost);
    recoveryCanvas.removeEventListener('webglcontextrestored', onContextRestored);
  }
  canvas.addEventListener('webglcontextlost', onContextLost);
  canvas.addEventListener('webglcontextrestored', onContextRestored);
  recoveryCanvas = canvas;
}

function rendererContextIsLost() {
  try {
    const gl = renderer.getContext();
    return Boolean(gl?.isContextLost?.());
  } catch (_error) {
    return true;
  }
}

function flagMaterial(material) {
  if (!material) {
    return;
  }
  material.needsUpdate = true;
  if (material.map) {
    material.map.needsUpdate = true;
  }
}

function flagObjectResources(object) {
  if (object.geometry) {
    Object.values(object.geometry.attributes || {}).forEach((attribute) => {
      if (attribute) {
        attribute.needsUpdate = true;
      }
    });
  }
  if (Array.isArray(object.material)) {
    object.material.forEach(flagMaterial);
    return;
  }
  flagMaterial(object.material);
}

function isResizeDragReason(reason) {
  return reason === 'side-panel-resize-left' || reason === 'side-panel-resize-right';
}

function isResizeEndReason(reason) {
  return reason === 'side-panel-resize-end-left' || reason === 'side-panel-resize-end-right';
}

function getResizeSide(reason) {
  if (reason.endsWith('-left')) {
    return 'left';
  }
  if (reason.endsWith('-right')) {
    return 'right';
  }
  return 'unknown';
}

function recordReason(reason) {
  visualRecoveryStats.reasons[reason] = (visualRecoveryStats.reasons[reason] || 0) + 1;
  pushRecent(visualRecoveryStats.recentReasons, { t: Date.now(), reason }, 40);
}

function beginOrContinueResizeSession(reason) {
  const side = getResizeSide(reason);
  if (!overlayResizeState.active || overlayResizeState.active.side !== side) {
    overlayResizeState.active = {
      side,
      baselineSnapshot: captureRecoverySnapshot(reason, 'resize-start')
    };
    visualRecoveryStats.overlayResizeSessions += 1;
  } else {
    overlayResizeState.active.lastSnapshot = captureRecoverySnapshot(reason, 'resize-drag');
  }
}

function getSnapshotFailures(baseline, current) {
  const failures = [];
  if (!current?.surface?.ok) {
    failures.push('render-surface-invariant');
  }
  if ((current?.cameraAspectDelta ?? 0) > 1e-3) {
    failures.push('camera-aspect-drift');
  }
  if (baseline?.surface?.rect && current?.surface?.rect) {
    const rectDelta = Math.max(
      Math.abs(current.surface.rect.left - baseline.surface.rect.left),
      Math.abs(current.surface.rect.top - baseline.surface.rect.top),
      Math.abs(current.surface.rect.width - baseline.surface.rect.width),
      Math.abs(current.surface.rect.height - baseline.surface.rect.height)
    );
    if (rectDelta > 1) {
      failures.push('canvas-rect-drift');
    }
  }
  if (baseline?.ndc && current?.ndc) {
    const ndcDelta = Math.max(
      Math.abs(current.ndc.x - baseline.ndc.x),
      Math.abs(current.ndc.y - baseline.ndc.y)
    );
    if (ndcDelta > 0.01) {
      failures.push('ndc-drift');
    }
  }
  return failures;
}

function finalizeResizeSession(reason) {
  const session = overlayResizeState.active ?? {
    side: getResizeSide(reason),
    baselineSnapshot: captureRecoverySnapshot(reason, 'resize-start')
  };
  overlayResizeState.active = null;
  if (scheduledRecoveryTimer !== null && typeof window !== 'undefined') {
    window.clearTimeout(scheduledRecoveryTimer);
    scheduledRecoveryTimer = null;
  }
  requestAnimationFrame(() => {
    rebuildVisualResources(`${reason}:visual`);
    const afterVisual = captureRecoverySnapshot(reason, 'resize-end');
    const failures = getSnapshotFailures(session.baselineSnapshot, afterVisual);
    if (failures.length === 0) {
      return;
    }
    visualRecoveryStats.overlayInvariantFailures += failures.length;
    visualRecoveryStats.overlayResizeFallbacks += 1;
    pushLog('warn', `Overlay recovery fallback (${reason}) after visual rebuild: ${failures.join(', ')}.`);
    rebuildRenderer(`${reason}:renderer-fallback`);
  });
}

function onOverlayLayoutChanged(event) {
  const reason = typeof event?.detail?.reason === 'string' && event.detail.reason
    ? event.detail.reason
    : 'overlay-layout-change';
  visualRecoveryStats.overlayEvents += 1;
  if (isResizeDragReason(reason)) {
    beginOrContinueResizeSession(reason);
    scheduleVisualRecovery(reason, 120);
    return;
  }
  if (isResizeEndReason(reason)) {
    finalizeResizeSession(reason);
    return;
  }
  captureRecoverySnapshot(reason, 'event');
  scheduleVisualRecovery(reason, 40);
}

export function scheduleVisualRecovery(reason = 'manual', delayMs = 120) {
  if (typeof window === 'undefined') {
    rebuildVisualResources(reason);
    return;
  }
  if (scheduledRecoveryTimer !== null) {
    window.clearTimeout(scheduledRecoveryTimer);
  }
  scheduledRecoveryTimer = window.setTimeout(() => {
    scheduledRecoveryTimer = null;
    rebuildVisualResources(reason);
  }, delayMs);
}

export function rebuildVisualResources(reason = 'manual') {
  visualRecoveryStats.rebuilds += 1;
  recordReason(reason);
  captureRecoverySnapshot(reason, 'before-rebuild');
  if (rendererContextIsLost()) {
    pushLog('warn', `Skipped visual rebuild (${reason}) because the WebGL context is currently lost.`);
    return false;
  }
  sourceLabels.forEach((label) => {
    rebuildLabelSpriteTexture(label);
  });
  speakerLabels.forEach((label) => {
    if (label) {
      rebuildLabelSpriteTexture(label);
    }
  });
  rebuildRoomDimensionGuideResources();
  updateRoomDimensionGuides();
  rebuildAllTrailRenderables();
  scene.traverse(flagObjectResources);
  renderer.resetState();
  try {
    renderer.compile(scene, camera);
  } catch (error) {
    pushLog('warn', `Renderer compile skipped during visual rebuild (${reason}): ${error instanceof Error ? error.message : String(error)}`);
  }
  captureRecoverySnapshot(reason, 'after-rebuild');
  pushLog('warn', `Visual resources rebuilt (${reason}).`);
  return true;
}

export function setupVisualRecovery() {
  bindRecoveryListeners();
  if (typeof window !== 'undefined') {
    window.addEventListener('omniphony:overlay-layout-changed', onOverlayLayoutChanged);
  }
  if (typeof window !== 'undefined') {
    const existing = window.omniphonyDebug && typeof window.omniphonyDebug === 'object'
      ? window.omniphonyDebug
      : {};
    window.omniphonyDebug = {
      ...existing,
      rebuildVisualResources,
      rebuildRenderer,
      scheduleVisualRecovery,
      captureVisualRecoverySnapshot: captureRecoverySnapshot
    };
  }
}

export function rebuildRenderer(reason = 'renderer-rebuild') {
  visualRecoveryStats.rendererRebuilds += 1;
  recordReason(reason);
  try {
    rebuildRendererOnFreshCanvas();
    rebindPointerListeners();
    bindRecoveryListeners();
  } catch (error) {
    pushLog('error', `Renderer rebuild failed: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
  rebuildVisualResources(reason);
  captureRecoverySnapshot(reason, 'after-renderer-rebuild');
  pushLog('warn', `WebGL renderer rebuilt (${reason}).`);
  return true;
}

export function teardownVisualRecovery() {
  if (scheduledRecoveryTimer !== null && typeof window !== 'undefined') {
    window.clearTimeout(scheduledRecoveryTimer);
    scheduledRecoveryTimer = null;
  }
  overlayResizeState.active = null;
  if (typeof window !== 'undefined') {
    window.removeEventListener('omniphony:overlay-layout-changed', onOverlayLayoutChanged);
  }
  if (recoveryCanvas) {
    recoveryCanvas.removeEventListener('webglcontextlost', onContextLost);
    recoveryCanvas.removeEventListener('webglcontextrestored', onContextRestored);
    recoveryCanvas = null;
  }
  teardownRenderer(true);
}
