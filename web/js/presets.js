/**
 * Simple-mode flight presets for the Mavic Air 2 planner.
 * Numbers are derived from the verified Air 2 camera model in camera.js
 * (12 MP 4000×3000, 24 mm-eq, 84° diagonal FOV, 2 s minimum photo interval,
 * Normal-mode max 12 m/s, FAA Part 107 cap 400 ft / 121.92 m AGL).
 *
 * Internal values stay in SI. The planner UI shows them in imperial units.
 *
 * Pile-count flights stay at 50 m (165 ft) with 80/70 overlap and 5 m/s. That is
 * 3–4× the height of a typical 10–15 m pile, above Propeller's
 * 120 ft / 36 m recommended minimum, and in the 125 ft accuracy sweet
 * spot from volumetric studies — not the 40–50 ft / 12–15 m guess, which
 * is too low for obstacle clearance, photo count, and stitching of uniform
 * material. See tests/presets.test.mjs comments and the conversion report.
 *
 * Find drone is 40 m (130 ft) with 70/60 overlap and 6 m/s so a ~10 in
 * quad is about 18 px across and every spot is photographed at least twice
 * (typically three times along-track), with speed under the 2 s interval cap.
 */
import {
  capSpeedMps,
  computeGsdCm,
  computeLineSpacingM,
  computeTriggerDistanceM,
  getCamera,
  MAVIC_AIR_2,
} from "./camera.js?v=20261114dlpm1";

export const PRESETS = Object.freeze({
  stockpile: Object.freeze({
    id: "stockpile",
    name: "Pile count",
    description:
      "165 ft AGL for ~0.71 in/px GSD, 80/70 overlap for pile volumes, 11 mph so photos stay above the 2 s interval.",
    altitudeM: 50,
    frontOverlap: 0.8,
    sideOverlap: 0.7,
    speedMps: 5,
    gridAngleDeg: 0,
    crosshatch: false,
  }),
  findDrone: Object.freeze({
    id: "findDrone",
    name: "Find drone",
    description:
      "130 ft AGL so a ~10 in quad is about 18 px across, 70/60 overlap so every spot is seen at least twice, and 13 mph under the 2 s photo-interval cap.",
    altitudeM: 40,
    frontOverlap: 0.7,
    sideOverlap: 0.6,
    speedMps: 6,
    gridAngleDeg: 0,
    crosshatch: false,
  }),
});

export function presetSettings(preset, camera = MAVIC_AIR_2) {
  const cam = getCamera(camera);
  const speedMps = capSpeedMps(
    preset.speedMps,
    computeTriggerDistanceM(preset.altitudeM, preset.frontOverlap, cam),
    cam.minPhotoIntervalSec,
    cam.maxSpeedNormalMps
  );
  return {
    altitudeM: preset.altitudeM,
    frontOverlap: preset.frontOverlap,
    sideOverlap: preset.sideOverlap,
    speedMps,
    gridAngleDeg: preset.gridAngleDeg,
    crosshatch: !!preset.crosshatch,
  };
}

export function presetStats(preset, camera = MAVIC_AIR_2) {
  const cam = getCamera(camera);
  const s = presetSettings(preset, cam);
  return {
    ...s,
    gsdCmPerPx: computeGsdCm(s.altitudeM, cam),
    lineSpacingM: computeLineSpacingM(s.altitudeM, s.sideOverlap, cam),
    triggerDistanceM: computeTriggerDistanceM(s.altitudeM, s.frontOverlap, cam),
    camera: cam,
  };
}

export function applyPreset(preset, camera = MAVIC_AIR_2) {
  return presetSettings(preset, getCamera(camera));
}
