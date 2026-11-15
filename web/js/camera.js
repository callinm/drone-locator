/**
 * DJI Mavic Air 2 camera model for mapping calculations.
 *
 * Specs verified against DJI's official product page and User Manual v1.2:
 * https://www.dji.com/support/product/mavic-air-2
 * https://dl.djicdn.com/downloads/Mavic_Air_2/20200615/Mavic_Air_2_User_Manual_v1.2_en.pdf
 *
 * Sensor: 1/2" CMOS (Type 1/2 optical format = 6.4 x 4.8 mm)
 * Lens: 24 mm 35 mm-equivalent, f/2.8, diagonal FOV 84°
 * Photo (12 MP mode, used for mapping): 4000 x 3000
 * Photo (48 MP mode): 8000 x 6000 — slower, not used here
 * Timed shot minimum interval in 12 MP: 2 seconds
 * Max flight time: 34 min (ideal). Max speed in Normal mode: 12 m/s (26.8 mph)
 * FAA Part 107 altitude limit: 400 ft AGL = 121.92 m (international foot).
 */

import { FAA_MAX_ALTITUDE_M } from "./units.js?v=20261114dlrr2";
export { FAA_MAX_ALTITUDE_M };
export const DEFAULT_ALTITUDE_M = 60;
export const DEFAULT_FRONT_OVERLAP = 0.75;
export const DEFAULT_SIDE_OVERLAP = 0.70;
export const DEFAULT_SPEED_MPS = 5;

export const MAVIC_AIR_2 = Object.freeze({
  id: "mavic-air-2",
  name: "DJI Mavic Air 2",
  sensorWidthMm: 6.4,
  sensorHeightMm: 4.8,
  imageWidthPx: 4000,
  imageHeightPx: 3000,
  focalLengthMm: 4.5, // ~24 mm-eq on a 1/2" sensor; official diagonal FOV 84°
  focalLength35mmEq: 24,
  diagonalFovDeg: 84,
  shutter: "rolling",
  minPhotoIntervalSec: 2.0,
  maxFlightTimeMin: 34,
  usableFlightTimeMin: 28,
  maxSpeedNormalMps: 12,
  maxAscentMps: 4,
  maxDescentMps: 3,
  yawRateDegPerSec: 45,
});

/**
 * DJI Phantom 4 Pro / Pro V2.0 camera (1" 20 MP, mechanical shutter).
 *
 * Specs from DJI's Phantom 4 Pro product page and user manual:
 * Sensor: 1-inch CMOS, 13.2 × 8.8 mm, 20 MP
 * Lens: 8.8 mm (24 mm 35 mm-equivalent), f/2.8–f/11, FOV 84°
 * Still size (3:2, used for mapping): 5472 × 3648
 * Mechanical shutter (no rolling-shutter smear on mapping flights)
 * Interval shooting typically 2 s minimum for JPEG
 * Max flight time about 30 min. P-mode max speed about 14 m/s (50 kph)
 */
export const PHANTOM_4_PRO = Object.freeze({
  id: "phantom-4-pro",
  name: "DJI Phantom 4 Pro",
  sensorWidthMm: 13.2,
  sensorHeightMm: 8.8,
  imageWidthPx: 5472,
  imageHeightPx: 3648,
  focalLengthMm: 8.8,
  focalLength35mmEq: 24,
  diagonalFovDeg: 84,
  shutter: "mechanical",
  minPhotoIntervalSec: 2.0,
  maxFlightTimeMin: 30,
  usableFlightTimeMin: 24,
  maxSpeedNormalMps: 14,
  maxAscentMps: 6,
  maxDescentMps: 4,
  yawRateDegPerSec: 70,
});

export const CAMERAS = Object.freeze({
  "mavic-air-2": MAVIC_AIR_2,
  "phantom-4-pro": PHANTOM_4_PRO,
});

export function getCamera(id) {
  if (id && typeof id === "object" && id.imageWidthPx) return id;
  return CAMERAS[id] || MAVIC_AIR_2;
}

/**
 * Horizontal and vertical FOV from the official 84° diagonal FOV and 4:3 aspect.
 * 24 mm-eq and 84° diagonal are consistent for a Type 1/2" sensor.
 */
export function computeFovDeg(camera = MAVIC_AIR_2) {
  if (camera.focalLengthMm && camera.sensorWidthMm && camera.sensorHeightMm) {
    const toDeg = (rad) => (rad * 180) / Math.PI;
    return {
      horizontalDeg: toDeg(2 * Math.atan(camera.sensorWidthMm / (2 * camera.focalLengthMm))),
      verticalDeg: toDeg(2 * Math.atan(camera.sensorHeightMm / (2 * camera.focalLengthMm))),
    };
  }
  const aspect = camera.imageWidthPx / camera.imageHeightPx;
  const tDiag = Math.tan((camera.diagonalFovDeg * Math.PI) / 180 / 2);
  // tan(H/2)^2 + tan(V/2)^2 = tan(D/2)^2, and tan(H/2) / tan(V/2) = aspect
  const tVert = Math.sqrt((tDiag * tDiag) / (aspect * aspect + 1));
  const tHorz = aspect * tVert;
  return {
    horizontalDeg: ((2 * Math.atan(tHorz)) * 180) / Math.PI,
    verticalDeg: ((2 * Math.atan(tVert)) * 180) / Math.PI,
  };
}

/** Ground sample distance in cm/pixel at the given AGL altitude. */
export function computeGsdCm(altitudeM, camera = MAVIC_AIR_2) {
  if (altitudeM <= 0) return 0;
  if (camera.sensorWidthMm && camera.focalLengthMm && camera.imageWidthPx) {
    return ((camera.sensorWidthMm * altitudeM) / (camera.focalLengthMm * camera.imageWidthPx)) * 100;
  }
  const { horizontalDeg } = computeFovDeg(camera);
  const groundWidthM =
    2 * altitudeM * Math.tan((horizontalDeg * Math.PI) / 180 / 2);
  return (groundWidthM / camera.imageWidthPx) * 100;
}

/** Across-track footprint (image width, perpendicular to heading), meters. */
export function footprintWidthM(altitudeM, camera = MAVIC_AIR_2) {
  const { horizontalDeg } = computeFovDeg(camera);
  return 2 * altitudeM * Math.tan((horizontalDeg * Math.PI) / 180 / 2);
}

/** Along-track footprint (image height, in direction of flight), meters. */
export function footprintHeightM(altitudeM, camera = MAVIC_AIR_2) {
  const { verticalDeg } = computeFovDeg(camera);
  return 2 * altitudeM * Math.tan((verticalDeg * Math.PI) / 180 / 2);
}

/** Distance between adjacent flight lines. */
export function computeLineSpacingM(altitudeM, sideOverlap, camera = MAVIC_AIR_2) {
  const overlap = clamp01(sideOverlap);
  return footprintWidthM(altitudeM, camera) * (1 - overlap);
}

/** Distance between photo triggers along a flight line. */
export function computeTriggerDistanceM(altitudeM, frontOverlap, camera = MAVIC_AIR_2) {
  const overlap = clamp01(frontOverlap);
  return footprintHeightM(altitudeM, camera) * (1 - overlap);
}

/**
 * Cap cruise speed so the aircraft cannot outrun the camera's minimum
 * shot interval at the planned trigger distance.
 */
export function capSpeedMps(requestedMps, triggerDistanceM, minIntervalSec, maxSpeedMps) {
  if (triggerDistanceM <= 0 || minIntervalSec <= 0) {
    return Math.min(requestedMps, maxSpeedMps);
  }
  const maxForCamera = triggerDistanceM / minIntervalSec;
  return Math.min(requestedMps, maxForCamera, maxSpeedMps);
}

export function clampAltitudeM(altitudeM, max = FAA_MAX_ALTITUDE_M) {
  if (!Number.isFinite(altitudeM)) return DEFAULT_ALTITUDE_M;
  return Math.min(Math.max(altitudeM, 5), max);
}

function clamp01(v) {
  if (!Number.isFinite(v)) return 0;
  return Math.min(Math.max(v, 0), 0.95);
}
