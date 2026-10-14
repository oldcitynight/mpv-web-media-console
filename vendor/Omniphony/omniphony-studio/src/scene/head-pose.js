// Live head-tracking pose applied to the centre head model (the Dame de
// Brassempouy). The renderer broadcasts `binaural.headPose` (~10 Hz), the
// world→head quaternion it actually applies to object positions. When the
// binaural output stage is active we show the *head orientation in world* —
// the conjugate — on the statue; otherwise the head eases back to neutral.
//
// The 10 Hz updates are smoothed by slerping toward the target every frame,
// so the motion looks continuous in the 3D view.

import * as THREE from 'three';
import { headPoseGroup } from './setup.js';

// Omniphony axes: x = right, y = front, z = up.
// Scene axes (see coordinates.js): scene.x = omni.y, scene.y = omni.z,
// scene.z = omni.x — a cyclic permutation (det +1), so a quaternion maps by
// permuting its vector part the same way.
const target = new THREE.Quaternion();
const identity = new THREE.Quaternion();
// Gate for the fast pose channel: only rotate while the binaural stage is
// actually applying the pose. Maintained by the (10 Hz) full-state path.
let trackingActive = false;

// Fraction of the remaining arc covered per rendered frame. The dedicated
// ~30 Hz pose channel leaves little to mask, so this can stay snappy
// (~25 ms time constant at 60 fps).
const SLERP_PER_FRAME = 0.4;

/**
 * Update the target orientation from the renderer's `binaural` state object.
 * Only an actually-applied pose rotates the head: outside binaural output
 * mode the target is neutral, whatever the tracker keeps sending.
 */
export function setHeadPoseTarget(binaural) {
  if (!binaural || typeof binaural !== 'object') return;
  const pose = binaural.headPose;
  const active = binaural.outputMode === 'binaural'
    && pose && typeof pose.w === 'number'
    && [pose.x, pose.y, pose.z].every((v) => typeof v === 'number');
  trackingActive = active;
  if (!active) {
    target.copy(identity);
    return;
  }
  // Conjugate (head-in-world), then permute omni (x, y, z) → scene (y, z, x).
  target.set(-pose.y, -pose.z, -pose.x, pose.w).normalize();
}

/**
 * Fast path: a bare quaternion from the dedicated ~30 Hz `head_pose`
 * channel. Activity gating still comes from the full-state path above.
 */
export function setHeadPoseQuat(pose) {
  if (!trackingActive || !pose || typeof pose.w !== 'number') return;
  target.set(-pose.y, -pose.z, -pose.x, pose.w).normalize();
}

/** Per-frame tick from the animation loop: ease the statue toward the target. */
export function updateHeadPose() {
  if (!headPoseGroup) return;
  headPoseGroup.quaternion.slerp(target, SLERP_PER_FRAME);
}
