/**
 * Stockpile volume on a DSM, computed in the browser.
 *
 * Auto-detect: a robust plane is fit to the ground (iteratively ignoring
 * high outliers), pixels raised above that plane are taken as pile material,
 * and either the largest connected blob or a region grown from a click
 * becomes the pile mask.
 *
 * Base plane: least-squares plane z = ax + by + c fit only to the mask
 * edge (the polygon ring the user drew, or the blob boundary). That is the
 * ground under the toe of the pile, even when the pad itself is sloped.
 *
 * Volume: vertical prismoidal sum over the mask,
 *   dV = (z - plane(x, y)) * pixelArea
 * Fill is the positive part (the pile), cut is the negative part, net is
 * fill − cut. Tonnage is fill × an editable bulk density (default 1.60 t/m³
 * for typical crushed stone / gravel).
 */
export const DEFAULT_DENSITY_T_PER_M3 = 1.6;
export const DEFAULT_MIN_HEIGHT_M = 0.15;

export function planeZ(plane, x, y) {
  return plane.a * x + plane.b * y + plane.c;
}

/**
 * Least-squares plane z = ax + by + c through points [{x,y,z}, ...].
 * Falls back to a horizontal plane at mean z if the points are degenerate.
 */
export function fitPlane(points) {
  const n = points?.length ?? 0;
  if (n === 0) return { a: 0, b: 0, c: 0 };
  if (n < 3) {
    const c = points.reduce((s, p) => s + p.z, 0) / n;
    return { a: 0, b: 0, c };
  }

  // Accumulate in a local frame around the mean (x, y). UTM / projected
  // coordinates are large (1e5–1e7 m); forming sxx = Σx² without a shift
  // makes the normal equations ill-conditioned and the ground plane wrong.
  let sw = 0, sx = 0, sy = 0, sz = 0;
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y) || !Number.isFinite(p.z)) continue;
    sw += 1;
    sx += p.x;
    sy += p.y;
    sz += p.z;
  }
  if (sw < 3) {
    return { a: 0, b: 0, c: sw ? sz / sw : 0 };
  }
  const mx = sx / sw;
  const my = sy / sw;

  let sxx = 0, sxy = 0, syy = 0, sxz = 0, syz = 0, szc = 0;
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y) || !Number.isFinite(p.z)) continue;
    const x = p.x - mx;
    const y = p.y - my;
    const z = p.z;
    sxx += x * x;
    sxy += x * y;
    syy += y * y;
    sxz += x * z;
    syz += y * z;
    szc += z;
  }

  // Normal equations for [a, b, d] in the local frame z = a x' + b y' + d,
  // then c = d - a mx - b my so z = a x + b y + c in world metres.
  const solved = solve3(
    [sxx, sxy, 0, sxy, syy, 0, 0, 0, sw],
    [sxz, syz, szc]
  );
  if (!solved) {
    return { a: 0, b: 0, c: sz / sw };
  }
  return { a: solved[0], b: solved[1], c: solved[2] - solved[0] * mx - solved[1] * my };
}

function solve3(m, b) {
  // Gaussian elimination with partial pivoting on a row-major 3x3.
  const a = [
    [m[0], m[1], m[2], b[0]],
    [m[3], m[4], m[5], b[1]],
    [m[6], m[7], m[8], b[2]],
  ];
  for (let col = 0; col < 3; col++) {
    let pivot = col;
    let best = Math.abs(a[col][col]);
    for (let r = col + 1; r < 3; r++) {
      const v = Math.abs(a[r][col]);
      if (v > best) {
        best = v;
        pivot = r;
      }
    }
    if (best < 1e-12) return null;
    if (pivot !== col) {
      const tmp = a[col];
      a[col] = a[pivot];
      a[pivot] = tmp;
    }
    const div = a[col][col];
    for (let c = col; c < 4; c++) a[col][c] /= div;
    for (let r = 0; r < 3; r++) {
      if (r === col) continue;
      const f = a[r][col];
      for (let c = col; c < 4; c++) a[r][c] -= f * a[col][c];
    }
  }
  return [a[0][3], a[1][3], a[2][3]];
}

/** Iteratively fit a ground plane, discarding high outliers (the pile). */
export function fitGroundPlane(points, { keepLow = 0.7, rounds = 3 } = {}) {
  let pts = points.filter((p) => Number.isFinite(p.z));
  if (pts.length < 3) return fitPlane(pts);
  let plane = fitPlane(pts);
  for (let i = 0; i < rounds; i++) {
    const residuals = pts.map((p) => ({ p, r: p.z - planeZ(plane, p.x, p.y) }));
    residuals.sort((a, b) => a.r - b.r);
    const keep = Math.max(3, Math.floor(residuals.length * keepLow));
    pts = residuals.slice(0, keep).map((d) => d.p);
    plane = fitPlane(pts);
  }
  return plane;
}

export function pixelCenter(dsm, col, row) {
  return {
    x: dsm.originX + (col + 0.5) * dsm.pixelWidthM,
    y: dsm.originY - (row + 0.5) * dsm.pixelHeightM,
  };
}

export function samplePoints(dsm, predicate, { maxPoints = 25000 } = {}) {
  const pts = [];
  const { width, height, z } = dsm;
  const target = Math.max(1, maxPoints);
  const stride = Math.max(1, Math.ceil(Math.sqrt((width * height) / target)));
  for (let row = 0; row < height; row += stride) {
    for (let col = 0; col < width; col += stride) {
      if (predicate && !predicate(col, row)) continue;
      const elev = z[row * width + col];
      if (!Number.isFinite(elev)) continue;
      const { x, y } = pixelCenter(dsm, col, row);
      pts.push({ x, y, z: elev, col, row });
    }
  }
  return pts;
}

export function pixelAreaM2(dsm) {
  return Math.abs(dsm.pixelWidthM * dsm.pixelHeightM);
}

/**
 * Vertical prismoidal volume over a boolean mask (Uint8Array / typed, 1 = inside).
 */
export function volumeFromMask(dsm, mask, plane) {
  const area = pixelAreaM2(dsm);
  const { width, height, z } = dsm;
  let fillM3 = 0;
  let cutM3 = 0;
  let areaM2 = 0;
  let count = 0;
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const i = row * width + col;
      if (!mask[i]) continue;
      const elev = z[i];
      if (!Number.isFinite(elev)) continue;
      const { x, y } = pixelCenter(dsm, col, row);
      const d = elev - planeZ(plane, x, y);
      const dV = d * area;
      if (dV >= 0) fillM3 += dV;
      else cutM3 += -dV;
      areaM2 += area;
      count += 1;
    }
  }
  return {
    fillM3,
    cutM3,
    netM3: fillM3 - cutM3,
    areaM2,
    pixelCount: count,
    plane,
    pixelAreaM2: area,
  };
}

export function tonnage(volumeM3, densityTPerM3 = DEFAULT_DENSITY_T_PER_M3) {
  if (!Number.isFinite(volumeM3) || !Number.isFinite(densityTPerM3)) return 0;
  return volumeM3 * densityTPerM3;
}

export function edgeMaskPixels(dsm, mask) {
  const { width, height, z } = dsm;
  const pts = [];
  const inside = (c, r) =>
    r >= 0 && r < height && c >= 0 && c < width && mask[r * width + c];
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const i = row * width + col;
      if (!mask[i] || !Number.isFinite(z[i])) continue;
      const onEdge =
        !inside(col - 1, row) ||
        !inside(col + 1, row) ||
        !inside(col, row - 1) ||
        !inside(col, row + 1);
      if (onEdge) {
        const { x, y } = pixelCenter(dsm, col, row);
        pts.push({ x, y, z: z[i], col, row });
      }
    }
  }
  return pts;
}

/**
 * Ground samples just outside the mask — the toe of the pile, not the
 * raised contour used to find it. Prefer these when fitting the base plane.
 */
export function exteriorMaskPixels(dsm, mask) {
  const { width, height, z } = dsm;
  const pts = [];
  const inside = (c, r) =>
    r >= 0 && r < height && c >= 0 && c < width && mask[r * width + c];
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const i = row * width + col;
      if (mask[i] || !Number.isFinite(z[i])) continue;
      const touches =
        inside(col - 1, row) ||
        inside(col + 1, row) ||
        inside(col, row - 1) ||
        inside(col, row + 1);
      if (touches) {
        const { x, y } = pixelCenter(dsm, col, row);
        pts.push({ x, y, z: z[i], col, row });
      }
    }
  }
  return pts;
}

export function fitBasePlaneFromMask(dsm, mask) {
  const outside = exteriorMaskPixels(dsm, mask);
  if (outside.length >= 3) return fitPlane(outside);
  return fitPlane(edgeMaskPixels(dsm, mask));
}

/** Grow a detected core down the slope to the fitted base (recover the toe). */
export function expandToToe(dsm, mask, plane, toeHeightM = 0.02) {
  const { width, height, z } = dsm;
  const out = new Uint8Array(mask);
  const stack = [];
  for (let i = 0; i < out.length; i++) if (out[i]) stack.push(i);
  while (stack.length) {
    const i = stack.pop();
    const r0 = (i / width) | 0;
    const c0 = i - r0 * width;
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;
        const c = c0 + dx;
        const r = r0 + dy;
        if (c < 0 || r < 0 || c >= width || r >= height) continue;
        const j = r * width + c;
        if (out[j]) continue;
        const elev = z[j];
        if (!Number.isFinite(elev)) continue;
        const { x, y } = pixelCenter(dsm, c, r);
        if (elev - planeZ(plane, x, y) >= toeHeightM) {
          out[j] = 1;
          stack.push(j);
        }
      }
    }
  }
  return out;
}

function refineDetected(dsm, mask, groundPlane) {
  // Grow down to the robust ground plane, then fit the base to the
  // true toe (pixels just outside the expanded mask sit on the pad).
  const expanded = expandToToe(dsm, mask, groundPlane, 0.02);
  const base = fitBasePlaneFromMask(dsm, expanded);
  return { mask: expanded, basePlane: base };
}

export function raisedMask(dsm, plane, minHeightM = DEFAULT_MIN_HEIGHT_M) {
  const { width, height, z } = dsm;
  const mask = new Uint8Array(width * height);
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const i = row * width + col;
      const elev = z[i];
      if (!Number.isFinite(elev)) continue;
      const { x, y } = pixelCenter(dsm, col, row);
      if (elev - planeZ(plane, x, y) >= minHeightM) mask[i] = 1;
    }
  }
  return mask;
}

/** 8-connected components. Returns an Int32Array of labels (0 = background). */
export function labelBlobs(mask, width, height) {
  const labels = new Int32Array(mask.length);
  const sizes = [0];
  let next = 1;
  const stack = [];
  for (let start = 0; start < mask.length; start++) {
    if (!mask[start] || labels[start]) continue;
    const id = next++;
    let size = 0;
    stack.push(start);
    labels[start] = id;
    while (stack.length) {
      const i = stack.pop();
      size += 1;
      const row = (i / width) | 0;
      const col = i - row * width;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (dx === 0 && dy === 0) continue;
          const c = col + dx;
          const r = row + dy;
          if (c < 0 || r < 0 || c >= width || r >= height) continue;
          const j = r * width + c;
          if (!mask[j] || labels[j]) continue;
          labels[j] = id;
          stack.push(j);
        }
      }
    }
    sizes[id] = size;
  }
  return { labels, sizes, count: next - 1 };
}

export function maskFromLabel(labels, id) {
  const mask = new Uint8Array(labels.length);
  for (let i = 0; i < labels.length; i++) if (labels[i] === id) mask[i] = 1;
  return mask;
}

export function largestBlobMask(mask, width, height) {
  const { labels, sizes, count } = labelBlobs(mask, width, height);
  if (count === 0) return new Uint8Array(mask.length);
  let best = 1;
  for (let id = 2; id < sizes.length; id++) {
    if (sizes[id] > sizes[best]) best = id;
  }
  return maskFromLabel(labels, best);
}

export function regionGrowFrom(dsm, plane, col, row, minHeightM = DEFAULT_MIN_HEIGHT_M) {
  const { width, height, z } = dsm;
  const mask = new Uint8Array(width * height);
  if (row < 0 || col < 0 || row >= height || col >= width) return mask;
  const seedI = row * width + col;
  const seedZ = z[seedI];
  if (!Number.isFinite(seedZ)) return mask;
  const seedPt = pixelCenter(dsm, col, row);
  if (seedZ - planeZ(plane, seedPt.x, seedPt.y) < minHeightM) return mask;

  const stack = [seedI];
  mask[seedI] = 1;
  while (stack.length) {
    const i = stack.pop();
    const r0 = (i / width) | 0;
    const c0 = i - r0 * width;
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;
        const c = c0 + dx;
        const r = r0 + dy;
        if (c < 0 || r < 0 || c >= width || r >= height) continue;
        const j = r * width + c;
        if (mask[j]) continue;
        const elev = z[j];
        if (!Number.isFinite(elev)) continue;
        const { x, y } = pixelCenter(dsm, c, r);
        if (elev - planeZ(plane, x, y) >= minHeightM) {
          mask[j] = 1;
          stack.push(j);
        }
      }
    }
  }
  return mask;
}

export function detectLargestPile(dsm, minHeightM = DEFAULT_MIN_HEIGHT_M) {
  const ground = fitGroundPlane(samplePoints(dsm));
  const raised = raisedMask(dsm, ground, minHeightM);
  const core = largestBlobMask(raised, dsm.width, dsm.height);
  const refined = refineDetected(dsm, core, ground);
  return { mask: refined.mask, groundPlane: ground, basePlane: refined.basePlane };
}

export function detectPileFromClick(dsm, col, row, minHeightM = DEFAULT_MIN_HEIGHT_M) {
  const ground = fitGroundPlane(samplePoints(dsm));
  const core = regionGrowFrom(dsm, ground, col, row, minHeightM);
  const refined = refineDetected(dsm, core, ground);
  return { mask: refined.mask, groundPlane: ground, basePlane: refined.basePlane };
}

/** Even-odd point-in-polygon. Ring is [{x,y}, ...] in the same metric space as the DSM. */
export function pointInPolygon(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i].x, yi = ring[i].y;
    const xj = ring[j].x, yj = ring[j].y;
    const denom = yj - yi;
    const intersect =
      yi > y !== yj > y &&
      x < ((xj - xi) * (y - yi)) / (denom === 0 ? 1e-16 : denom) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

export function maskFromPolygon(dsm, ring) {
  const { width, height } = dsm;
  const mask = new Uint8Array(width * height);
  if (!ring || ring.length < 3) return mask;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of ring) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const { x, y } = pixelCenter(dsm, col, row);
      if (x < minX || x > maxX || y < minY || y > maxY) continue;
      if (pointInPolygon(x, y, ring)) mask[row * width + col] = 1;
    }
  }
  return mask;
}

/**
 * Sample a closed ring along each edge so the base-plane fit uses the
 * true polygon boundary, not just pixel corners.
 */
export function sampleRingElevations(dsm, ring, stepM) {
  const pts = [];
  if (!ring || ring.length < 2) return pts;
  const step = stepM || Math.min(dsm.pixelWidthM, dsm.pixelHeightM);
  const n = ring.length;
  for (let i = 0; i < n; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % n];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy);
    const samples = Math.max(1, Math.ceil(len / step));
    for (let s = 0; s < samples; s++) {
      const t = s / samples;
      const x = a.x + dx * t;
      const y = a.y + dy * t;
      const z = bilinearZ(dsm, x, y);
      if (Number.isFinite(z)) pts.push({ x, y, z });
    }
  }
  return pts;
}

export function bilinearZ(dsm, x, y) {
  const colF = (x - dsm.originX) / dsm.pixelWidthM - 0.5;
  const rowF = (dsm.originY - y) / dsm.pixelHeightM - 0.5;
  const c0 = Math.floor(colF);
  const r0 = Math.floor(rowF);
  const c1 = c0 + 1;
  const r1 = r0 + 1;
  if (c0 < 0 || r0 < 0 || c1 >= dsm.width || r1 >= dsm.height) {
    const c = Math.round(colF);
    const r = Math.round(rowF);
    if (c < 0 || r < 0 || c >= dsm.width || r >= dsm.height) return NaN;
    return dsm.z[r * dsm.width + c];
  }
  const tx = colF - c0;
  const ty = rowF - r0;
  const z00 = dsm.z[r0 * dsm.width + c0];
  const z10 = dsm.z[r0 * dsm.width + c1];
  const z01 = dsm.z[r1 * dsm.width + c0];
  const z11 = dsm.z[r1 * dsm.width + c1];
  if (![z00, z10, z01, z11].every(Number.isFinite)) return z00;
  return (
    z00 * (1 - tx) * (1 - ty) +
    z10 * tx * (1 - ty) +
    z01 * (1 - tx) * ty +
    z11 * tx * ty
  );
}

export function measurePolygon(dsm, ring, densityTPerM3 = DEFAULT_DENSITY_T_PER_M3) {
  const mask = maskFromPolygon(dsm, ring);
  const edge = sampleRingElevations(dsm, ring);
  const base = edge.length >= 3 ? fitPlane(edge) : fitBasePlaneFromMask(dsm, mask);
  return measureMask(dsm, mask, base, densityTPerM3);
}

export function measureMask(dsm, mask, basePlane, densityTPerM3 = DEFAULT_DENSITY_T_PER_M3) {
  const vol = volumeFromMask(dsm, mask, basePlane);
  return {
    ...vol,
    tons: tonnage(vol.fillM3, densityTPerM3),
    densityTPerM3,
  };
}

/** Build a metric DSM from a row-major elevation array. */
export function makeDsm({
  z,
  width,
  height,
  pixelWidthM = 1,
  pixelHeightM = 1,
  originX = 0,
  originY = null,
}) {
  const data = z instanceof Float32Array ? z : Float32Array.from(z);
  const oy = originY == null ? height * pixelHeightM : originY;
  return {
    z: data,
    width,
    height,
    pixelWidthM,
    pixelHeightM,
    originX,
    originY: oy,
  };
}

/** Cone standing on a plane: z = plane(x,y) + h * (1 - r/radius) for r < radius. */
export function addCone(dsm, { cx, cy, radius, height, plane = { a: 0, b: 0, c: 0 } }) {
  const { width, height: rows, z } = dsm;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < width; col++) {
      const { x, y } = pixelCenter(dsm, col, row);
      const r = Math.hypot(x - cx, y - cy);
      const base = planeZ(plane, x, y);
      const cap = r < radius ? height * (1 - r / radius) : 0;
      const i = row * width + col;
      const prev = Number.isFinite(z[i]) ? z[i] : base;
      z[i] = Math.max(prev, base + cap);
    }
  }
  return dsm;
}

/** Square pyramid: base is 2*half wide, apex at (cx, cy), faces meet along diagonals of a square rotated 0°. */
export function addPyramid(dsm, { cx, cy, half, height, plane = { a: 0, b: 0, c: 0 } }) {
  const { width, height: rows, z } = dsm;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < width; col++) {
      const { x, y } = pixelCenter(dsm, col, row);
      const u = Math.abs(x - cx);
      const v = Math.abs(y - cy);
      const base = planeZ(plane, x, y);
      const chebyshev = Math.max(u, v);
      const cap = chebyshev < half ? height * (1 - chebyshev / half) : 0;
      const i = row * width + col;
      const prev = Number.isFinite(z[i]) ? z[i] : base;
      z[i] = Math.max(prev, base + cap);
    }
  }
  return dsm;
}

export function fillPlane(dsm, plane = { a: 0, b: 0, c: 0 }) {
  const { width, height, z } = dsm;
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      const { x, y } = pixelCenter(dsm, col, row);
      z[row * width + col] = planeZ(plane, x, y);
    }
  }
  return dsm;
}

export const CONE_VOLUME = (radius, height) => (Math.PI * radius * radius * height) / 3;
export const PYRAMID_VOLUME = (half, height) => (4 * half * half * height) / 3;
