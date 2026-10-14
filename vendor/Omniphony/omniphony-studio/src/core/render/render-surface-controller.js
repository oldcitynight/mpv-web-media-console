import { getWindowViewport, subscribeWindowViewport } from '../viewport/window-viewport.js';
import { reapplyViewOffset } from '../../scene/setup.js';

let getRendererRef = null;
let getCameraRef = null;
let unsubscribeViewport = null;

function currentRenderer() {
  return typeof getRendererRef === 'function' ? getRendererRef() : null;
}

function currentCamera() {
  return typeof getCameraRef === 'function' ? getCameraRef() : null;
}

function applyViewport(viewport) {
  const renderer = currentRenderer();
  const camera = currentCamera();
  if (!renderer || !camera) {
    return;
  }
  camera.aspect = viewport.width / viewport.height;
  camera.updateProjectionMatrix();
  renderer.setPixelRatio(viewport.dpr);
  renderer.setSize(viewport.width, viewport.height);
  // Re-apply the head-pivot pan lens shift for the new surface size (the view
  // offset is relative to the full size, so it must follow a resize).
  reapplyViewOffset(viewport.width, viewport.height);
}

export function syncRenderSurface() {
  applyViewport(getWindowViewport());
}

export function getRenderSurfaceRect() {
  const renderer = currentRenderer();
  return renderer?.domElement?.getBoundingClientRect?.() ?? null;
}

function serializeRect(rect) {
  if (!rect) {
    return null;
  }
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height
  };
}

export function getRenderSurfaceInvariantState() {
  const renderer = currentRenderer();
  const viewport = getWindowViewport();
  const canvas = renderer?.domElement ?? null;
  const mount = canvas?.parentElement ?? null;
  const rect = canvas?.getBoundingClientRect?.() ?? null;
  const mountStyle = typeof window !== 'undefined' && mount ? window.getComputedStyle(mount) : null;
  const canvasStyle = typeof window !== 'undefined' && canvas ? window.getComputedStyle(canvas) : null;
  const mountPinned = Boolean(mountStyle)
    && mountStyle.position === 'fixed'
    && mountStyle.top === '0px'
    && mountStyle.right === '0px'
    && mountStyle.bottom === '0px'
    && mountStyle.left === '0px';
  const rectPinned = Boolean(rect)
    && Math.abs(rect.left) <= 1
    && Math.abs(rect.top) <= 1
    && Math.abs(rect.width - viewport.width) <= 1
    && Math.abs(rect.height - viewport.height) <= 1;
  return {
    ok: Boolean(renderer && mountPinned && rectPinned),
    viewport,
    rect: serializeRect(rect),
    mount: mountStyle ? {
      position: mountStyle.position,
      top: mountStyle.top,
      right: mountStyle.right,
      bottom: mountStyle.bottom,
      left: mountStyle.left
    } : null,
    canvas: canvasStyle ? {
      position: canvasStyle.position,
      width: canvasStyle.width,
      height: canvasStyle.height
    } : null,
    flags: {
      mountPinned,
      rectPinned
    }
  };
}

export function initRenderSurfaceController({ getRenderer, getCamera }) {
  getRendererRef = getRenderer;
  getCameraRef = getCamera;
  if (unsubscribeViewport) {
    unsubscribeViewport();
  }
  unsubscribeViewport = subscribeWindowViewport((viewport) => {
    applyViewport(viewport);
  });
  syncRenderSurface();
}
