import * as THREE from 'three';
import { app, speakerMeshes, sourceMeshes, sourceLabels, sourceOutlines, sourceNames, speakerLabels, speakerBandBars } from './state.js';
import { scene, camera, renderer, controls } from './scene/setup.js';
import { raycaster, pointer } from './scene/materials.js';
import { speakerGizmo, distanceGizmo, cartesianGizmo } from './scene/gizmos.js';
import { normalizeAngleDeg, snapAngleDeg, sphericalToCartesianDeg, cartesianToSpherical } from './coordinates.js';
import { applySpeakerSceneCartesianEdit, setSelectedSpeaker, resolveEditTarget, updateSpeakerGizmo } from './speakers.js';
import { setSelectedSource, updateSourceDecorations } from './sources.js';
import { applyChannelPolar, previewChannelEditorFromScene } from './controls/virtual-bed.js';
import { projectRayOntoAxis } from './input.js';
import { getCanvasClientRect, pointerEventToNdc as projectPointerEventToNdc } from './core/render/projection-service.js';

let boundCanvas = null;

function onPointerDown(event) {
  app.pointerDownPosition = { x: event.clientX, y: event.clientY };
  if (beginSpeakerDrag(event)) {
    app.pointerDownPosition = null;
  }
}

function onPointerUp(event) {
  if (app.isDraggingSpeaker && event.pointerId === app.draggingPointerId) {
    endSpeakerDrag();
  }
  if (!app.pointerDownPosition) {
    return;
  }

  const dx = event.clientX - app.pointerDownPosition.x;
  const dy = event.clientY - app.pointerDownPosition.y;
  app.pointerDownPosition = null;

  if (Math.hypot(dx, dy) <= 6) {
    const hitSceneItem = selectSceneItemFromPointer(event);
    if (hitSceneItem) {
      return;
    }
    setSelectedSource(null);
    setSelectedSpeaker(null);
    updateControlsForEditMode();
  }
}

function onPointerMove(event) {
  if (app.isDraggingSpeaker && event.pointerId === app.draggingPointerId) {
    updateSpeakerDrag(event);
  }
}

function onPointerCancel() {
  endSpeakerDrag();
}

function onPointerLeave() {
  endSpeakerDrag();
}

function onWheel(event) {
  const target = resolveEditTarget();
  if (app.activeEditMode !== 'polar' || !target || !app.polarEditArmed) {
    return;
  }
  if (!event.ctrlKey && !event.shiftKey) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const prevZoom = controls.enableZoom;
  controls.enableZoom = false;

  const delta = -Math.sign(event.deltaY);
  const step = event.shiftKey ? 0.01 : 0.05;
  const next = Math.min(2.0, Math.max(0.2, app.dragDistance + delta * step));
  if (next === app.dragDistance) {
    return;
  }
  app.dragDistance = next;
  const pos = sphericalToCartesianDeg(app.dragAzimuthDeg, app.dragElevationDeg, app.dragDistance);
  // Speakers don't send OSC per wheel tick (the panel commits); channels do, so
  // a wheel distance change persists instead of being snapped back by the bed.
  commitTargetScene(target, pos.x, pos.y, pos.z, target.kind === 'channel');
  controls.enableZoom = prevZoom;
}

export function rebindPointerListeners() {
  const canvas = renderer.domElement;
  if (boundCanvas === canvas) {
    return;
  }
  if (boundCanvas) {
    boundCanvas.removeEventListener('pointerdown', onPointerDown);
    boundCanvas.removeEventListener('pointerup', onPointerUp);
    boundCanvas.removeEventListener('pointermove', onPointerMove);
    boundCanvas.removeEventListener('pointercancel', onPointerCancel);
    boundCanvas.removeEventListener('pointerleave', onPointerLeave);
    boundCanvas.removeEventListener('wheel', onWheel, true);
  }
  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointercancel', onPointerCancel);
  canvas.addEventListener('pointerleave', onPointerLeave);
  canvas.addEventListener('wheel', onWheel, { passive: false, capture: true });
  boundCanvas = canvas;
}

function updateControlsForEditMode() {
  controls.enableZoom = true;
}

export function pointerEventToNdc(event) {
  projectPointerEventToNdc(event, getCanvasClientRect(renderer.domElement), pointer);
}

export function getPickableSceneTargets() {
  const sourceTargets = [
    ...sourceLabels.values(),
    ...sourceMeshes.values(),
    ...sourceOutlines.values()
  ].filter((object) => object && object.visible !== false);
  const speakerTargets = [
    ...speakerMeshes,
    ...speakerLabels,
    ...speakerBandBars
  ].filter((object) => object && object.visible !== false);
  return [...sourceTargets, ...speakerTargets];
}

export function pickSpeakerFromIntersects(intersects) {
  for (const hit of intersects) {
    const object = hit.object;
    const speakerIdx = speakerMeshes.indexOf(object);
    if (speakerIdx >= 0) {
      setSelectedSource(null);
      setSelectedSpeaker(speakerIdx);
      return true;
    }

    const labelIdx = speakerLabels.indexOf(object);
    if (labelIdx >= 0) {
      setSelectedSource(null);
      setSelectedSpeaker(labelIdx);
      return true;
    }

    const bandIdx = speakerBandBars.indexOf(object);
    if (bandIdx >= 0) {
      setSelectedSource(null);
      setSelectedSpeaker(bandIdx);
      return true;
    }
  }
  return false;
}

export function selectSceneItemFromPointer(event) {
  pointerEventToNdc(event);
  raycaster.setFromCamera(pointer, camera);
  const intersects = raycaster.intersectObjects(getPickableSceneTargets(), false);

  if (pickSpeakerFromIntersects(intersects)) {
    return true;
  }

  for (const hit of intersects) {
    const object = hit.object;
    const sourceId = object?.userData?.sourceId;
    if (sourceId !== undefined && sourceId !== null) {
      setSelectedSource(sourceId);
      setSelectedSpeaker(null);
      updateControlsForEditMode();
      return true;
    }

    const speakerIdx = speakerMeshes.indexOf(object);
    if (speakerIdx >= 0) {
      setSelectedSource(null);
      setSelectedSpeaker(speakerIdx);
      return true;
    }

    const labelIdx = speakerLabels.indexOf(object);
    if (labelIdx >= 0) {
      setSelectedSource(null);
      setSelectedSpeaker(labelIdx);
      return true;
    }
  }

  return false;
}

// Commit a scene-space cartesian position to the active edit target. Speakers go
// through the layout model; virtual-bed channels (shown at their pure angle) map
// the scene direction straight to azimuth/elevation/distance and push the bed.
function commitTargetScene(target, x, y, z, send) {
  if (target.kind === 'speaker') {
    // applySpeakerSceneCartesianEdit moves the mesh and (via
    // updateSpeakerVisualsFromState) makes the gizmo follow + refreshes the panel.
    applySpeakerSceneCartesianEdit(target.index, x, y, z, send);
    return;
  }
  target.mesh.position.set(x, y, z);
  if (target.label) target.label.position.set(x, y + 0.12, z);
  // Keep the editor-authoritative pin on the live position so a stream packet
  // arriving mid-drag holds here instead of fighting the gizmo.
  if (app.channelEditPinId === target.id && app.channelEditPinPos) {
    app.channelEditPinPos.x = x;
    app.channelEditPinPos.y = y;
    app.channelEditPinPos.z = z;
  }
  // Decorations (outline, halo, label, effective-render marker) are normally
  // moved by updateSource, which is suppressed while pinned — move them here so
  // they don't lag behind the dragged sphere.
  updateSourceDecorations(target.id);
  // Make the gizmo (ring/arc/handles + distance line) and the editor's numeric
  // fields track the channel mesh as it moves, exactly like the speaker editor.
  updateSpeakerGizmo();
  previewChannelEditorFromScene(x, y, z);
  if (send) {
    const sph = cartesianToSpherical({ x, y, z });
    applyChannelPolar(target.name, sph.az, sph.el, Math.max(0.01, sph.dist));
  }
}

function beginDragCommon(target, event) {
  app.dragEditTarget = target;
  app.isDraggingSpeaker = true;
  app.draggingPointerId = event.pointerId;
  controls.enabled = false;
  if (target.kind === 'channel') {
    // Pin the object to the editor (no expiry) so the live OSC stream can't fight
    // the drag; the gizmo/drag drives `channelEditPinPos` from here on.
    app.isDraggingVirtualBed = true;
    app.draggingVirtualBedSourceId = target.id;
    app.draggingVirtualBedChannel = target.name;
    app.channelEditPinId = target.id;
    app.channelEditPinPos = {
      x: target.mesh.position.x,
      y: target.mesh.position.y,
      z: target.mesh.position.z
    };
    app.channelEditPinUntil = 0;
  }
}

export function beginSpeakerDrag(event) {
  const target = resolveEditTarget();
  if (!target) {
    return false;
  }
  pointerEventToNdc(event);
  raycaster.setFromCamera(pointer, camera);

  if (app.activeEditMode === 'polar' && app.polarEditArmed) {
    const gizmoHits = raycaster.intersectObjects([speakerGizmo.ring, speakerGizmo.arc], false);
    if (gizmoHits.length === 0) {
      return false;
    }
    const hit = gizmoHits[0].object;
    app.dragMode = hit === speakerGizmo.ring ? 'azimuth' : 'elevation';
    app.dragAzimuthDelta = 1;
    app.dragElevationDelta = 1;
    beginDragCommon(target, event);
    return true;
  }

  if (app.activeEditMode === 'cartesian' && app.cartesianEditArmed) {
    const handleHits = raycaster.intersectObjects(
      [cartesianGizmo.xHandle, cartesianGizmo.yHandle, cartesianGizmo.zHandle],
      false
    );
    if (handleHits.length === 0) {
      return false;
    }
    const axis = handleHits[0].object?.userData?.axis;
    if (!axis) {
      return false;
    }
    app.dragMode = 'cartesian';
    app.dragAxis = axis;
    app.dragAxisDirection.set(
      axis === 'x' ? 1 : 0,
      axis === 'y' ? 1 : 0,
      axis === 'z' ? 1 : 0
    );
    app.dragAxisOrigin.copy(target.mesh.position);
    app.dragSpeakerStartPosition.copy(target.mesh.position);
    app.dragAxisStartT = projectRayOntoAxis(
      raycaster.ray.origin,
      raycaster.ray.direction,
      app.dragAxisOrigin,
      app.dragAxisDirection
    );
    beginDragCommon(target, event);
    return true;
  }

  return false;
}

export function updateSpeakerDrag(event) {
  const target = app.dragEditTarget;
  if (!app.isDraggingSpeaker || !target) {
    return;
  }
  pointerEventToNdc(event);
  raycaster.setFromCamera(pointer, camera);

  if (app.dragMode === 'azimuth') {
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const hitPoint = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(plane, hitPoint)) {
      app.dragAzimuthDeg = (Math.atan2(hitPoint.z, hitPoint.x) * 180) / Math.PI;
      app.dragAzimuthDeg = normalizeAngleDeg(app.dragAzimuthDeg);
      const radial = Math.sqrt(hitPoint.x * hitPoint.x + hitPoint.z * hitPoint.z);
      const delta = (radial - app.dragDistance) / app.dragDistance;
      app.dragAzimuthDelta = delta;
      if (delta >= 0 && delta <= 0.1) {
        app.dragAzimuthDeg = snapAngleDeg(app.dragAzimuthDeg, 1, 0.5);
      } else if (delta > 0.1) {
        app.dragAzimuthDeg = snapAngleDeg(app.dragAzimuthDeg, 5, 2.5);
      }
    }
  } else if (app.dragMode === 'elevation') {
    const azRad = (app.dragAzimuthDeg * Math.PI) / 180;
    const dir = new THREE.Vector3(Math.cos(azRad), 0, Math.sin(azRad));
    const normal = new THREE.Vector3().crossVectors(dir, new THREE.Vector3(0, 1, 0)).normalize();
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, new THREE.Vector3(0, 0, 0));
    const hitPoint = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(plane, hitPoint)) {
      const planar = Math.sqrt(hitPoint.x * hitPoint.x + hitPoint.z * hitPoint.z);
      app.dragElevationDeg = (Math.atan2(hitPoint.y, planar) * 180) / Math.PI;
      app.dragElevationDeg = Math.max(-90, Math.min(90, app.dragElevationDeg));
      const radius = Math.sqrt(hitPoint.x * hitPoint.x + hitPoint.y * hitPoint.y + hitPoint.z * hitPoint.z);
      const delta = (radius - app.dragDistance) / app.dragDistance;
      app.dragElevationDelta = delta;
      if (delta >= 0 && delta <= 0.1) {
        app.dragElevationDeg = snapAngleDeg(app.dragElevationDeg, 1, 0.5);
      } else if (delta > 0.1) {
        app.dragElevationDeg = snapAngleDeg(app.dragElevationDeg, 5, 2.5);
      }
    }
  } else if (app.dragMode === 'cartesian') {
    const tNow = projectRayOntoAxis(
      raycaster.ray.origin,
      raycaster.ray.direction,
      app.dragAxisOrigin,
      app.dragAxisDirection
    );
    const delta = tNow - app.dragAxisStartT;
    const pos = app.dragSpeakerStartPosition.clone().add(app.dragAxisDirection.clone().multiplyScalar(delta));
    commitTargetScene(target, pos.x, pos.y, pos.z, false);
    return;
  }

  const pos = sphericalToCartesianDeg(app.dragAzimuthDeg, app.dragElevationDeg, app.dragDistance);
  target.mesh.position.set(pos.x, pos.y, pos.z);
  if (target.label) {
    target.label.position.set(pos.x, pos.y + 0.12, pos.z);
  }
  commitTargetScene(target, pos.x, pos.y, pos.z, false);
}

export function endSpeakerDrag() {
  const target = app.dragEditTarget;
  if (!app.isDraggingSpeaker || !target) {
    return;
  }
  app.isDraggingSpeaker = false;
  app.dragMode = null;
  app.dragAxis = null;
  app.draggingPointerId = null;
  controls.enabled = true;

  // Commit the final dragged position to the model + OSC.
  const pos = target.mesh.position;
  commitTargetScene(target, pos.x, pos.y, pos.z, true);

  app.dragEditTarget = null;
  if (target.kind === 'channel') {
    app.isDraggingVirtualBed = false;
    app.draggingVirtualBedSourceId = null;
    app.draggingVirtualBedChannel = null;
    // Keep the pin for a short settle window: stream packets carrying the
    // pre-edit position are ignored until the renderer applies the new bed.
    app.channelEditPinUntil = performance.now() + 600;
  }
}

export function setupPointerListeners() {
  rebindPointerListeners();
}
