/**
 * Load a DSM / orthophoto GeoTIFF in the browser (geotiff.js) and
 * build the synthetic demo pile. Volume math lives in volume.js and
 * stays independent of the GeoTIFF library so tests can run in Node.
 */
import { toLocal, toLatLon, EARTH_RADIUS_M, toRad, toDeg } from "./geo.js?v=20261114dlpm2";
import {
  makeDsm,
  fillPlane,
  addCone,
  pixelCenter,
} from "./volume.js?v=20261114dlpm2";

export const DEMO = Object.freeze({
  // A quiet corner of the public South Farms demo field — synthetic only.
  origin: { lat: 40.08340, lon: -88.22620 },
  n: 241,
  pixelM: 0.05,
  plane: { a: 0.02, b: 0.01, c: 185 },
  cone: { cx: 6.025, cy: 6.025, radius: 4, height: 3 },
  decoy: { cx: 10.6, cy: 1.9, radius: 1.1, height: 0.9 },
});

export function metersPerDegree(lat) {
  const mLat = EARTH_RADIUS_M * (Math.PI / 180);
  const mLon = mLat * Math.cos(toRad(lat));
  return { mLat, mLon };
}

export function attachGeographic(dsm, origin) {
  dsm.originLatLon = origin;
  dsm.toLatLon = (x, y) => toLatLon(x, y, origin);
  dsm.toXY = (lat, lon) => toLocal(lat, lon, origin);
  const sw = toLatLon(dsm.originX, dsm.originY - dsm.height * dsm.pixelHeightM, origin);
  const ne = toLatLon(dsm.originX + dsm.width * dsm.pixelWidthM, dsm.originY, origin);
  // Leaflet imageOverlay wants [[south, west], [north, east]].
  const south = Math.min(sw.lat, ne.lat);
  const north = Math.max(sw.lat, ne.lat);
  const west = Math.min(sw.lon, ne.lon);
  const east = Math.max(sw.lon, ne.lon);
  dsm.leafletBounds = [
    [south, west],
    [north, east],
  ];
  return dsm;
}

export function latLngRingToMetric(dsm, ring) {
  if (!ring || !ring.length) return [];
  return ring.map((ll) => {
    if (ll && Number.isFinite(ll.x) && Number.isFinite(ll.y) && ll.lat == null && ll.lon == null && !Array.isArray(ll)) {
      return { x: ll.x, y: ll.y };
    }
    const lat = Array.isArray(ll) ? Number(ll[0]) : Number(ll.lat);
    const lon = Array.isArray(ll) ? Number(ll[1]) : Number(ll.lng ?? ll.lon);
    if (dsm && typeof dsm.toXY === "function") return dsm.toXY(lat, lon);
    throw new Error("This map has no metric coordinates for the outline.");
  });
}

export function colRowFromLatLon(dsm, lat, lon) {
  const { x, y } = dsm.toXY(lat, lon);
  const col = Math.floor((x - dsm.originX) / dsm.pixelWidthM);
  const row = Math.floor((dsm.originY - y) / dsm.pixelHeightM);
  return { col, row, x, y };
}

export function makeDemoPile() {
  const { n, pixelM, plane, cone, decoy, origin } = DEMO;
  const dsm = makeDsm({
    z: new Float32Array(n * n),
    width: n,
    height: n,
    pixelWidthM: pixelM,
    pixelHeightM: pixelM,
    originX: 0,
    originY: n * pixelM,
  });
  fillPlane(dsm, plane);
  addCone(dsm, { ...cone, plane });
  addCone(dsm, { ...decoy, plane });
  dsm.name = "Demo pile (synthetic cone)";
  attachGeographic(dsm, origin);
  dsm.orthoCanvas = renderDemoOrtho(dsm);
  return dsm;
}

export function renderDemoOrtho(dsm) {
  const { width, height, z } = dsm;
  const canvas = rawCanvas(width, height);
  const ctx = canvas.getContext && canvas.getContext("2d");
  if (!ctx) return canvas;
  const img = ctx.createImageData(width, height);
  const shade = hillshadeArray(dsm);
  const planeZ0 = dsm.z[0];
  // Rough ground at first pixel; use a percentile-free stretch from min/max.
  let min = Infinity, max = -Infinity;
  for (let i = 0; i < z.length; i++) {
    const v = z[i];
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const span = Math.max(0.2, max - min);
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const i = row * width + col;
      const t = (z[i] - min) / span;
      const hs = shade[i];
      // Pad: muted field green. Pile: tan / crushed-stone.
      const pile = t > 0.12;
      let r, g, b;
      if (pile) {
        r = 148 + t * 70 + (col * 13 + row * 7) % 18;
        g = 118 + t * 40 + (col * 5 + row * 11) % 14;
        b = 88 + t * 20;
      } else {
        r = 86 + (col * 3 + row * 5) % 16;
        g = 108 + (col * 7 + row * 3) % 18;
        b = 72 + (row * 2) % 10;
      }
      const k = 0.35 + 0.65 * hs;
      const o = (i * 4);
      img.data[o] = Math.max(0, Math.min(255, r * k));
      img.data[o + 1] = Math.max(0, Math.min(255, g * k));
      img.data[o + 2] = Math.max(0, Math.min(255, b * k));
      img.data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

function rawCanvas(width, height) {
  if (typeof document !== "undefined") {
    const c = document.createElement("canvas");
    c.width = width;
    c.height = height;
    return c;
  }
  return { width, height, getContext: () => null };
}

export function hillshadeArray(dsm, azimuthDeg = 315, altitudeDeg = 45) {
  const { width, height, z, pixelWidthM, pixelHeightM } = dsm;
  const out = new Float32Array(width * height);
  const az = toRad(360 - azimuthDeg + 90);
  const alt = toRad(altitudeDeg);
  const sinAlt = Math.sin(alt);
  const cosAlt = Math.cos(alt);
  for (let row = 1; row < height - 1; row++) {
    for (let col = 1; col < width - 1; col++) {
      const i = row * width + col;
      const z1 = z[i - width - 1], z2 = z[i - width], z3 = z[i - width + 1];
      const z4 = z[i - 1], z6 = z[i + 1];
      const z7 = z[i + width - 1], z8 = z[i + width], z9 = z[i + width + 1];
      const dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8 * pixelWidthM);
      const dzdy = ((z7 + 2 * z8 + z9) - (z1 + 2 * z2 + z3)) / (8 * pixelHeightM);
      const slope = Math.atan(Math.hypot(dzdx, dzdy));
      const aspect = Math.atan2(dzdy, -dzdx);
      let hs = sinAlt * Math.cos(slope) + cosAlt * Math.sin(slope) * Math.cos(az - aspect);
      if (!Number.isFinite(hs)) hs = 0.5;
      out[i] = Math.max(0, Math.min(1, hs));
    }
  }
  // Copy edges
  for (let col = 0; col < width; col++) {
    out[col] = out[width + col] || 0.5;
    out[(height - 1) * width + col] = out[(height - 2) * width + col] || 0.5;
  }
  for (let row = 0; row < height; row++) {
    out[row * width] = out[row * width + 1] || 0.5;
    out[row * width + width - 1] = out[row * width + width - 2] || 0.5;
  }
  return out;
}

export function dsmToCanvas(dsm, { orthoCanvas = null, mask = null, maxSize = 1024 } = {}) {
  const scale = Math.min(1, maxSize / Math.max(dsm.width, dsm.height));
  const w = Math.max(1, Math.round(dsm.width * scale));
  const h = Math.max(1, Math.round(dsm.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(w, h);
  const shade = hillshadeArray(dsm);
  let min = Infinity, max = -Infinity;
  for (const v of dsm.z) {
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const span = Math.max(0.2, max - min);
  const ortho = orthoCanvas
    ? orthoCanvas.getContext("2d").getImageData(0, 0, orthoCanvas.width, orthoCanvas.height)
    : null;
  for (let row = 0; row < h; row++) {
    for (let col = 0; col < w; col++) {
      const srcC = Math.min(dsm.width - 1, Math.floor(col / scale));
      const srcR = Math.min(dsm.height - 1, Math.floor(row / scale));
      const si = srcR * dsm.width + srcC;
      const o = (row * w + col) * 4;
      const t = Number.isFinite(dsm.z[si]) ? (dsm.z[si] - min) / span : 0;
      const hs = shade[si] ?? 0.5;
      let r, g, b;
      if (ortho && ortho.width === dsm.width && ortho.height === dsm.height) {
        const oi = si * 4;
        r = ortho.data[oi];
        g = ortho.data[oi + 1];
        b = ortho.data[oi + 2];
      } else {
        // Color-relief: low green-blue, high tan.
        r = 40 + t * 180;
        g = 70 + t * 120;
        b = 90 + t * 40;
        r *= 0.35 + 0.65 * hs;
        g *= 0.35 + 0.65 * hs;
        b *= 0.35 + 0.65 * hs;
      }
      if (mask && mask[si]) {
        r = r * 0.55 + 240 * 0.45;
        g = g * 0.55 + 170 * 0.45;
        b = b * 0.55 + 40 * 0.45;
      }
      img.data[o] = Math.max(0, Math.min(255, r));
      img.data[o + 1] = Math.max(0, Math.min(255, g));
      img.data[o + 2] = Math.max(0, Math.min(255, b));
      img.data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

export function maskOutline(mask, width, height) {
  const ring = [];
  let start = -1;
  for (let i = 0; i < mask.length; i++) {
    if (mask[i]) {
      start = i;
      break;
    }
  }
  if (start < 0) return ring;
  // Walk the outer boundary in pixel-corner space (Moore neighbor).
  const sr = (start / width) | 0;
  const sc = start - sr * width;
  const dirs = [
    [1, 0],
    [1, 1],
    [0, 1],
    [-1, 1],
    [-1, 0],
    [-1, -1],
    [0, -1],
    [1, -1],
  ];
  const inside = (c, r) =>
    r >= 0 && c >= 0 && r < height && c < width && mask[r * width + c];
  let c = sc, r = sr;
  let dir = 0;
  const key = (x, y, d) => `${x},${y},${d}`;
  const seen = new Set();
  for (let step = 0; step < mask.length * 4; step++) {
    const k = key(c, r, dir);
    if (seen.has(k)) break;
    seen.add(k);
    ring.push({ col: c, row: r });
    // Find the next clockwise boundary neighbor, starting from dir-2.
    let found = false;
    for (let kdir = 0; kdir < 8; kdir++) {
      const nd = (dir + 6 + kdir) % 8; // turn left first
      const nc = c + dirs[nd][0];
      const nr = r + dirs[nd][1];
      if (inside(nc, nr)) {
        c = nc;
        r = nr;
        dir = nd;
        found = true;
        break;
      }
    }
    if (!found) break;
    if (c === sc && r === sr && ring.length > 2) break;
  }
  return ring;
}

export function outlineLatLngs(dsm, mask) {
  if (!dsm.toLatLon) return [];
  const pix = maskOutline(mask, dsm.width, dsm.height);
  return pix.map(({ col, row }) => {
    const { x, y } = pixelCenter(dsm, col, row);
    const ll = dsm.toLatLon(x, y);
    return [ll.lat, ll.lon];
  });
}

let geotiffModule = null;
async function loadGeoTiffLib() {
  if (geotiffModule) return geotiffModule;
  if (typeof document !== "undefined") {
    const GeoTIFF = await loadVendoredGeoTiff();
    geotiffModule = GeoTIFF;
    return geotiffModule;
  }
  geotiffModule = await import("https://unpkg.com/geotiff@2.1.3/dist-module/geotiff.js");
  return geotiffModule;
}

function loadVendoredGeoTiff() {
  if (typeof globalThis !== "undefined" && globalThis.GeoTIFF) return Promise.resolve(globalThis.GeoTIFF);
  return new Promise((resolve, reject) => {
    const src = new URL("../vendor/geotiff/geotiff.js", import.meta.url).href;
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => {
      if (globalThis.GeoTIFF) resolve(globalThis.GeoTIFF);
      else reject(new Error("The GeoTIFF library did not load."));
    };
    s.onerror = () => reject(new Error("Could not load the GeoTIFF library."));
    document.head.appendChild(s);
  });
}

export async function dsmFromGeoTiff(buffer, name = "DSM") {
  const lib = await loadGeoTiffLib();
  const tiff = await lib.fromArrayBuffer(buffer);
  const image = await tiff.getImage();
  const width = image.getWidth();
  const height = image.getHeight();
  const rasters = await image.readRasters({ interleave: false, samples: [0] });
  const band = rasters[0];
  const nodata = Number(image.getGDALNoData());
  const z = new Float32Array(width * height);
  for (let i = 0; i < z.length; i++) {
    const v = Number(band[i]);
    z[i] = !Number.isFinite(v) || v === nodata || v < -1e10 ? NaN : v;
  }

  const bbox = image.getBoundingBox(); // [minX, minY, maxX, maxY]
  const res = image.getResolution();
  const origin = image.getOrigin();
  const fileDir = image.fileDirectory || {};
  const keys = image.getGeoKeys ? image.getGeoKeys() : {};
  const epsg =
    keys.ProjectedCSTypeGeoKey ||
    keys.ProjectedCSType ||
    keys.GeographicTypeGeoKey ||
    keys.GeographicType ||
    0;

  const minX = bbox[0], minY = bbox[1], maxX = bbox[2], maxY = bbox[3];
  const pixelW = Math.abs((maxX - minX) / width);
  const pixelH = Math.abs((maxY - minY) / height);
  const isGeographic = isGeographicCrs(epsg, minX, maxX, minY, maxY);

  let dsm;
  if (isGeographic) {
    const midLat = (minY + maxY) / 2;
    const midLon = (minX + maxX) / 2;
    const { mLat, mLon } = metersPerDegree(midLat);
    const originLL = { lat: midLat, lon: midLon };
    // Metric origin at the image's SW, y up.
    const sw = toLocal(minY, minX, originLL);
    const ne = toLocal(maxY, maxX, originLL);
    dsm = makeDsm({
      z,
      width,
      height,
      pixelWidthM: Math.abs(ne.x - sw.x) / width,
      pixelHeightM: Math.abs(ne.y - sw.y) / height,
      originX: sw.x,
      originY: sw.y + Math.abs(ne.y - sw.y),
    });
    attachGeographic(dsm, originLL);
    // Recompute leaflet bounds from the file, which is more accurate.
    dsm.leafletBounds = [
      [minY, minX],
      [maxY, maxX],
    ];
    dsm.toLatLon = (x, y) => {
      const fx = (x - dsm.originX) / (dsm.width * dsm.pixelWidthM);
      const fy = (y - (dsm.originY - dsm.height * dsm.pixelHeightM)) /
        (dsm.height * dsm.pixelHeightM);
      return { lat: minY + fy * (maxY - minY), lon: minX + fx * (maxX - minX) };
    };
    dsm.toXY = (lat, lon) => {
      const fx = (lon - minX) / (maxX - minX);
      const fy = (lat - minY) / (maxY - minY);
      return {
        x: dsm.originX + fx * dsm.width * dsm.pixelWidthM,
        y: dsm.originY - dsm.height * dsm.pixelHeightM + fy * dsm.height * dsm.pixelHeightM,
      };
    };
  } else if (isUtmEpsg(epsg)) {
    // Measure in a local metre frame (origin at the SW corner). Raw UTM
    // eastings/northings are ~1e6 m and break unshifted plane fits.
    const originLL = utmToLatLon(minX, minY, epsg);
    const widthM = Math.abs(maxX - minX);
    const heightM = Math.abs(maxY - minY);
    dsm = makeDsm({
      z,
      width,
      height,
      pixelWidthM: pixelW,
      pixelHeightM: pixelH,
      originX: 0,
      originY: heightM,
    });
    const ne = utmToLatLon(maxX, maxY, epsg);
    dsm.originLatLon = originLL;
    dsm.projectedOrigin = { easting: minX, northing: minY, epsg };
    dsm.toLatLon = (x, y) => utmToLatLon(minX + x, minY + y, epsg);
    dsm.toXY = (lat, lon) => {
      const p = latLonToUtm(lat, lon, epsg);
      return { x: p.easting - minX, y: p.northing - minY };
    };
    dsm.leafletBounds = [
      [Math.min(originLL.lat, ne.lat), Math.min(originLL.lon, ne.lon)],
      [Math.max(originLL.lat, ne.lat), Math.max(originLL.lon, ne.lon)],
    ];
  } else {
    // Unknown projected / local metre system — shift to a local metre frame
    // so volume math is well-conditioned; map overlay may be local-only.
    const widthM = Math.abs((maxX - minX) || width * (pixelW || 1));
    const heightM = Math.abs((maxY - minY) || height * (pixelH || 1));
    dsm = makeDsm({
      z,
      width,
      height,
      pixelWidthM: pixelW || 1,
      pixelHeightM: pixelH || 1,
      originX: 0,
      originY: heightM,
    });
    const fakeOrigin = { lat: 0, lon: 0 };
    dsm.toLatLon = (x, y) => ({
      lat: y * 1e-6,
      lon: x * 1e-6,
    });
    dsm.toXY = (lat, lon) => ({
      x: lon * 1e6,
      y: lat * 1e6,
    });
    dsm.leafletBounds = null;
    dsm.localOnly = true;
  }

  dsm.name = name;
  dsm.epsg = epsg;
  dsm.bbox = bbox;
  void origin;
  void res;
  void fileDir;
  return dsm;
}

export async function orthoFromGeoTiff(buffer) {
  const lib = await loadGeoTiffLib();
  const tiff = await lib.fromArrayBuffer(buffer);
  const image = await tiff.getImage();
  const width = image.getWidth();
  const height = image.getHeight();
  const samples = Math.min(3, image.getSamplesPerPixel());
  const rasters = await image.readRasters({ samples: [...Array(samples).keys()] });
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, height);
  const scale = (v) => {
    if (v > 1 && v <= 255) return v;
    if (v >= 0 && v <= 1) return Math.round(v * 255);
    return Math.max(0, Math.min(255, v));
  };
  for (let i = 0; i < width * height; i++) {
    const r = scale(rasters[0][i]);
    const g = scale(rasters[Math.min(1, samples - 1)][i]);
    const b = scale(rasters[Math.min(2, samples - 1)][i]);
    img.data[i * 4] = r;
    img.data[i * 4 + 1] = g;
    img.data[i * 4 + 2] = b;
    img.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  const bbox = image.getBoundingBox();
  return { canvas, bbox, width, height };
}

export async function orthoFromImageFile(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await loadHtmlImage(url);
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext("2d").drawImage(img, 0, 0);
    return { canvas, bbox: null, width: canvas.width, height: canvas.height };
  } finally {
    URL.revokeObjectURL(url);
  }
}

function loadHtmlImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Could not read that image."));
    img.src = url;
  });
}

function isGeographicCrs(epsg, minX, maxX, minY, maxY) {
  if (epsg === 4326 || epsg === 4269 || epsg === 4258) return true;
  // Heuristic: values look like lon/lat.
  return (
    Math.abs(maxX) <= 180 &&
    Math.abs(minX) <= 180 &&
    Math.abs(maxY) <= 90 &&
    Math.abs(minY) <= 90 &&
    Math.abs(maxX - minX) < 2 &&
    Math.abs(maxY - minY) < 2
  );
}

function isUtmEpsg(epsg) {
  return (epsg >= 32601 && epsg <= 32660) || (epsg >= 32701 && epsg <= 32760);
}

function utmParams(epsg) {
  const north = epsg < 32700;
  const zone = north ? epsg - 32600 : epsg - 32700;
  return { zone, north };
}

export function latLonToUtm(lat, lon, epsg) {
  const { zone, north } = utmParams(epsg);
  const a = 6378137.0;
  const f = 1 / 298.257223563;
  const k0 = 0.9996;
  const e = Math.sqrt(f * (2 - f));
  const latRad = toRad(lat);
  const lonRad = toRad(lon);
  const lon0 = toRad((zone - 1) * 6 - 180 + 3);
  const N = a / Math.sqrt(1 - e * e * Math.sin(latRad) ** 2);
  const T = Math.tan(latRad) ** 2;
  const C = (e * e / (1 - e * e)) * Math.cos(latRad) ** 2;
  const A = Math.cos(latRad) * (lonRad - lon0);
  const M =
    a *
    ((1 - e * e / 4 - 3 * e ** 4 / 64 - 5 * e ** 6 / 256) * latRad -
      (3 * e * e / 8 + 3 * e ** 4 / 32 + 45 * e ** 6 / 1024) * Math.sin(2 * latRad) +
      (15 * e ** 4 / 256 + 45 * e ** 6 / 1024) * Math.sin(4 * latRad) -
      (35 * e ** 6 / 3072) * Math.sin(6 * latRad));
  const easting =
    k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * (e * e / (1 - e * e))) * A ** 5 / 120) +
    500000;
  let northing =
    k0 *
    (M +
      N *
        Math.tan(latRad) *
        (A * A / 2 +
          (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24 +
          (61 - 58 * T + T * T + 600 * C - 330 * (e * e / (1 - e * e))) * A ** 6 / 720));
  if (!north) northing += 10000000;
  return { easting, northing, zone, north };
}

export function utmToLatLon(easting, northing, epsg) {
  const { zone, north } = utmParams(epsg);
  const a = 6378137.0;
  const f = 1 / 298.257223563;
  const k0 = 0.9996;
  const e = Math.sqrt(f * (2 - f));
  const e1 = (1 - Math.sqrt(1 - e * e)) / (1 + Math.sqrt(1 - e * e));
  const x = easting - 500000;
  let y = northing;
  if (!north) y -= 10000000;
  const lon0 = (zone - 1) * 6 - 180 + 3;
  const M = y / k0;
  const mu =
    M /
    (a * (1 - e * e / 4 - 3 * e ** 4 / 64 - 5 * e ** 6 / 256));
  const phi1 =
    mu +
    (3 * e1 / 2 - 27 * e1 ** 3 / 32) * Math.sin(2 * mu) +
    (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * Math.sin(4 * mu) +
    (151 * e1 ** 3 / 96) * Math.sin(6 * mu);
  const N1 = a / Math.sqrt(1 - e * e * Math.sin(phi1) ** 2);
  const T1 = Math.tan(phi1) ** 2;
  const C1 = (e * e / (1 - e * e)) * Math.cos(phi1) ** 2;
  const R1 = (a * (1 - e * e)) / Math.pow(1 - e * e * Math.sin(phi1) ** 2, 1.5);
  const D = x / (N1 * k0);
  const lat =
    phi1 -
    (N1 * Math.tan(phi1) / R1) *
      (D * D / 2 -
        (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * (e * e / (1 - e * e))) * D ** 4 / 24 +
        (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * (e * e / (1 - e * e)) - 3 * C1 * C1) *
          D ** 6 /
          720);
  const lon =
    toRad(lon0) +
    (D -
      (1 + 2 * T1 + C1) * D ** 3 / 6 +
      (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * (e * e / (1 - e * e)) + 24 * T1 * T1) *
        D ** 5 /
        120) /
      Math.cos(phi1);
  return { lat: toDeg(lat), lon: toDeg(lon) };
}
