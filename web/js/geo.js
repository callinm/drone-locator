/** Local tangent-plane projection and polygon helpers for small survey areas. */

export const EARTH_RADIUS_M = 6378137;

export function toRad(deg) {
  return (deg * Math.PI) / 180;
}

export function toDeg(rad) {
  return (rad * 180) / Math.PI;
}

export function wrapDeg(deg) {
  let d = deg % 360;
  if (d < 0) d += 360;
  return d;
}

export function wrap180(deg) {
  let d = ((deg + 180) % 360 + 360) % 360 - 180;
  return d;
}

export function centroid(points) {
  if (!points.length) return { lat: 0, lon: 0 };
  let lat = 0;
  let lon = 0;
  for (const p of points) {
    lat += p.lat;
    lon += p.lon;
  }
  return { lat: lat / points.length, lon: lon / points.length };
}

export function originOf(points) {
  return centroid(points);
}

export function toLocal(lat, lon, origin) {
  const x =
    EARTH_RADIUS_M * toRad(lon - origin.lon) * Math.cos(toRad(origin.lat));
  const y = EARTH_RADIUS_M * toRad(lat - origin.lat);
  return { x, y };
}

export function toLatLon(x, y, origin) {
  const lat = origin.lat + toDeg(y / EARTH_RADIUS_M);
  const lon =
    origin.lon + toDeg(x / (EARTH_RADIUS_M * Math.cos(toRad(origin.lat))));
  return { lat, lon };
}

export function rotate(x, y, angleDeg) {
  const a = toRad(angleDeg);
  const c = Math.cos(a);
  const s = Math.sin(a);
  return { x: x * c - y * s, y: x * s + y * c };
}

/** Rotate so that a flight heading of `gridAngleDeg` aligns with +X. */
export function rotateToGrid(x, y, gridAngleDeg) {
  return rotate(x, y, -gridAngleDeg);
}

export function rotateFromGrid(x, y, gridAngleDeg) {
  return rotate(x, y, gridAngleDeg);
}

export function haversineM(a, b) {
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(s)));
}

export function bearingDeg(a, b) {
  const y = Math.sin(toRad(b.lon - a.lon)) * Math.cos(toRad(b.lat));
  const x =
    Math.cos(toRad(a.lat)) * Math.sin(toRad(b.lat)) -
    Math.sin(toRad(a.lat)) *
      Math.cos(toRad(b.lat)) *
      Math.cos(toRad(b.lon - a.lon));
  return wrapDeg(toDeg(Math.atan2(y, x)));
}

export function destination(start, distanceM, bearingDegVal) {
  const d = distanceM / EARTH_RADIUS_M;
  const br = toRad(bearingDegVal);
  const lat1 = toRad(start.lat);
  const lon1 = toRad(start.lon);
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(br)
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(br) * Math.sin(d) * Math.cos(lat1),
      Math.cos(d) - Math.sin(lat1) * Math.sin(lat2)
    );
  return { lat: toDeg(lat2), lon: toDeg(lon2) };
}

export function closePolygon(points) {
  if (points.length < 3) return points.slice();
  const first = points[0];
  const last = points[points.length - 1];
  if (first.lat === last.lat && first.lon === last.lon) return points.slice();
  return [...points, { lat: first.lat, lon: first.lon }];
}

export function uniqueVertices(points) {
  const closed = closePolygon(points);
  return closed.slice(0, -1);
}

export function polygonBounds(points) {
  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (const p of points) {
    minLat = Math.min(minLat, p.lat);
    maxLat = Math.max(maxLat, p.lat);
    minLon = Math.min(minLon, p.lon);
    maxLon = Math.max(maxLon, p.lon);
  }
  return { minLat, maxLat, minLon, maxLon };
}

export function polygonAreaM2(points, origin = originOf(points)) {
  const verts = uniqueVertices(points).map((p) => toLocal(p.lat, p.lon, origin));
  if (verts.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < verts.length; i++) {
    const a = verts[i];
    const b = verts[(i + 1) % verts.length];
    sum += a.x * b.y - b.x * a.y;
  }
  return Math.abs(sum) / 2;
}

/**
 * Intersect a horizontal line y = yLine with a polygon in local XY.
 * Returns non-overlapping segments [{x0, x1}, ...] with x0 <= x1.
 */
export function scanlineSegments(localVerts, yLine, eps = 1e-9) {
  const n = localVerts.length;
  if (n < 3) return [];
  const xs = [];
  for (let i = 0; i < n; i++) {
    const a = localVerts[i];
    const b = localVerts[(i + 1) % n];
    const y0 = a.y;
    const y1 = b.y;
    // Skip edges that don't straddle the line. Treat a vertex as belonging
    // to the edge where it is the upper endpoint so it is not counted twice.
    if (y0 <= yLine && y1 > yLine) {
      const t = (yLine - y0) / (y1 - y0);
      xs.push(a.x + t * (b.x - a.x));
    } else if (y1 <= yLine && y0 > yLine) {
      const t = (yLine - y0) / (y1 - y0);
      xs.push(a.x + t * (b.x - a.x));
    }
  }
  xs.sort((p, q) => p - q);
  const segments = [];
  for (let i = 0; i + 1 < xs.length; i += 2) {
    const x0 = xs[i];
    const x1 = xs[i + 1];
    if (x1 - x0 > eps) segments.push({ x0, x1 });
  }
  return segments;
}
