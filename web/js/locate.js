/**
 * Pixel-to-ground location for a Mavic Air 2 still.
 *
 * The aircraft is at EXIF GPS, at height RelativeAltitude (DJI XMP) above a
 * flat ground plane. The Air 2's 84° diagonal FOV (≈71.5° H × 56.8° V) gives
 * the focal length in pixels. A ray is sent through the match pixel, rotated
 * by gimbal pitch / yaw / roll (DJI: pitch 0 = horizon, −90 = nadir; yaw 0 =
 * north, clockwise), and intersected with the ground.
 *
 * The result is an estimated WGS84 point plus the east/north offset from the
 * camera. Lens distortion is not modelled; the ground is assumed flat.
 */
import { computeFovDeg, MAVIC_AIR_2 } from "./camera.js";
import { toLatLon, toRad } from "./geo.js";

export function cameraIntrinsics(imageWidth, imageHeight, camera = MAVIC_AIR_2) {
  const w = imageWidth || camera.imageWidthPx;
  const h = imageHeight || camera.imageHeightPx;
  const { horizontalDeg, verticalDeg } = computeFovDeg(camera);
  const fx = w / 2 / Math.tan(toRad(horizontalDeg / 2));
  const fy = h / 2 / Math.tan(toRad(verticalDeg / 2));
  return {
    fx,
    fy,
    cx: w / 2,
    cy: h / 2,
    imageWidth: w,
    imageHeight: h,
    horizontalDeg,
    verticalDeg,
  };
}

/**
 * Camera axes in ENU (east, north, up) for DJI gimbal angles.
 * Camera frame is X right, Y down in the image, Z along the optical axis.
 */
export function cameraAxesENU(pitchDeg, yawDeg, rollDeg = 0) {
  const p = toRad(pitchDeg);
  const y = toRad(yawDeg);
  const r = toRad(rollDeg);
  // Unrolled (roll = 0): X is heading + 90°, optical axis is in the heading
  // vertical plane, Y completes a right-handed frame (Z × X).
  let X = { e: Math.cos(y), n: -Math.sin(y), u: 0 };
  const Z = {
    e: Math.sin(y) * Math.cos(p),
    n: Math.cos(y) * Math.cos(p),
    u: Math.sin(p),
  };
  let Y = cross(Z, X);
  if (Math.abs(rollDeg) > 1e-9) {
    // Roll: rotate X and Y around the optical axis (right-hand, positive clock-wise
    // in the image would be negative in a right-handed cam frame; DJI roll is
    // typically small. We use a right-handed rotation of X toward Y by `roll`).
    const c = Math.cos(r);
    const s = Math.sin(r);
    const Xr = add(scale(X, c), scale(Y, s));
    const Yr = add(scale(X, -s), scale(Y, c));
    X = Xr;
    Y = Yr;
  }
  return { X, Y, Z };
}

function cross(a, b) {
  return {
    e: a.n * b.u - a.u * b.n,
    n: a.u * b.e - a.e * b.u,
    u: a.e * b.n - a.n * b.e,
  };
}
function scale(v, s) {
  return { e: v.e * s, n: v.n * s, u: v.u * s };
}
function add(a, b) {
  return { e: a.e + b.e, n: a.n + b.n, u: a.u + b.u };
}

/**
 * Project a pixel to the ground plane z = 0, camera at (0,0,h) in a local
 * ENU frame whose origin is the GPS tag.
 *
 * @returns {{ ok, lat, lon, eastM, northM, rangeM, warning }}
 */
export function pixelToGround({
  pixelX,
  pixelY,
  imageWidth,
  imageHeight,
  cameraLat,
  cameraLon,
  relativeAltitudeM,
  gimbalPitchDeg = -90,
  gimbalYawDeg = 0,
  gimbalRollDeg = 0,
  camera = MAVIC_AIR_2,
} = {}) {
  const warning = [];
  if (!Number.isFinite(cameraLat) || !Number.isFinite(cameraLon)) {
    return { ok: false, error: "Camera GPS is missing.", warning };
  }
  const h = relativeAltitudeM;
  if (!Number.isFinite(h) || h <= 0) {
    return {
      ok: false,
      lat: cameraLat,
      lon: cameraLon,
      eastM: 0,
      northM: 0,
      rangeM: 0,
      error: "Relative altitude is missing, so the ground point cannot be projected. Showing the photo’s GPS instead.",
      warning,
    };
  }
  const K = cameraIntrinsics(imageWidth, imageHeight, camera);
  const { X, Y, Z } = cameraAxesENU(
    Number.isFinite(gimbalPitchDeg) ? gimbalPitchDeg : -90,
    Number.isFinite(gimbalYawDeg) ? gimbalYawDeg : 0,
    Number.isFinite(gimbalRollDeg) ? gimbalRollDeg : 0
  );
  if (!Number.isFinite(gimbalPitchDeg)) warning.push("Pitch was missing; assumed nadir (−90°).");
  if (!Number.isFinite(gimbalYawDeg)) warning.push("Yaw was missing; assumed 0° (north).");

  const dirCam = {
    x: (pixelX - K.cx) / K.fx,
    y: (pixelY - K.cy) / K.fy,
    z: 1,
  };
  const dir = add(add(scale(X, dirCam.x), scale(Y, dirCam.y)), scale(Z, dirCam.z));
  if (!(dir.u < -1e-9)) {
    return {
      ok: false,
      lat: cameraLat,
      lon: cameraLon,
      eastM: 0,
      northM: 0,
      rangeM: 0,
      error: "That pixel’s ray does not hit the ground (camera is looking at the sky).",
      warning,
    };
  }
  const t = -h / dir.u;
  const eastM = t * dir.e;
  const northM = t * dir.n;
  const ll = toLatLon(eastM, northM, { lat: cameraLat, lon: cameraLon });
  return {
    ok: true,
    lat: ll.lat,
    lon: ll.lon,
    eastM,
    northM,
    rangeM: Math.hypot(eastM, northM),
    altitudeM: h,
    warning,
  };
}

export function googleMapsUrl(lat, lon) {
  return `https://www.google.com/maps?q=${lat.toFixed(7)},${lon.toFixed(7)}`;
}

/** Prefer gimbal yaw (world heading when present), else aircraft flight yaw. */
export function cameraHeadingDeg(meta) {
  if (Number.isFinite(meta?.gimbalYawDeg)) return meta.gimbalYawDeg;
  if (Number.isFinite(meta?.flightYawDeg)) return meta.flightYawDeg;
  return null;
}

export function relativeAltitudeM(meta) {
  if (Number.isFinite(meta?.relativeAltitudeM) && meta.relativeAltitudeM > 0) {
    return meta.relativeAltitudeM;
  }
  return null;
}
