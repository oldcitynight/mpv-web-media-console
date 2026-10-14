import * as THREE from 'three';

export const SOURCE_BASE_RADIUS = 0.07;
export const SPEAKER_BASE_SIZE = 0.08;

export const sourceMaterial = new THREE.MeshPhysicalMaterial({
  color: 0xff7c4d,
  emissive: 0x64210c,
  transparent: true,
  opacity: 0.7,
  roughness: 0.08,
  metalness: 0.04,
  clearcoat: 1.0,
  clearcoatRoughness: 0.03,
  sheen: 0.75,
  sheenRoughness: 0.18,
  specularIntensity: 1.6,
  reflectivity: 0.8
});
export const sourceGeometry = new THREE.SphereGeometry(SOURCE_BASE_RADIUS, 24, 24);
export const speakerGeometry = new THREE.BoxGeometry(SPEAKER_BASE_SIZE, SPEAKER_BASE_SIZE, SPEAKER_BASE_SIZE);
export const speakerMaterial = new THREE.MeshStandardMaterial({
  color: 0x8ec8ff,
  emissive: 0x10253a,
  transparent: true,
  opacity: 0.65
});

// "Driver" marker drawn on a speaker's front face so its orientation is visible
// when speakers are aimed at the listener. Shared across all speakers (static,
// flat disc facing local +Z); added as a child of each speaker cube so it
// inherits the cube's position/level scaling and rotation.
export const speakerDriverGeometry = new THREE.CircleGeometry(SPEAKER_BASE_SIZE * 0.36, 24);
export const speakerDriverMaterial = new THREE.MeshBasicMaterial({
  color: 0x0c1118,
  transparent: true,
  opacity: 0.85
});

export const speakerBaseColor = new THREE.Color(0x8ec8ff);
export const speakerHotColor = new THREE.Color(0xff3030);
export const speakerSelectedColor = new THREE.Color(0x4dff88);
export const sourceHotColor = new THREE.Color(0xff3030);
export const sourceDefaultEmissive = new THREE.Color(0x64210c);
export const sourceNeutralEmissive = new THREE.Color(0x10161d);
export const sourceContributionEmissive = new THREE.Color(0x10311a);
export const sourceSelectedEmissive = new THREE.Color(0x9b7f22);
export const sourceOutlineColor = new THREE.Color(0xd9ecff);
export const sourceOutlineSelectedColor = new THREE.Color(0xffde8a);

export const raycaster = new THREE.Raycaster();
raycaster.params.Line.threshold = 0.08;
export const pointer = new THREE.Vector2();
