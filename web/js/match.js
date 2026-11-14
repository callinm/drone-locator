/**
 * Client-side target matching for Find My Drone.
 *
 * Primary (browser): OpenCV.js ORB or AKAZE, Lowe ratio test, RANSAC
 * homography. Fallback (always available, and used in tests): multi-scale
 * normalized cross-correlation plus a hue/color-spot search for small or
 * distant markings that do not yield enough features.
 */

export function toGray(image) {
  const { data, width, height } = image;
  const g = new Float32Array(width * height);
  for (let i = 0, p = 0; i < data.length; i += 4, p++) {
    g[p] = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
  }
  return { data: g, width, height };
}

export function resizeRgba(image, width, height) {
  if (image.width === width && image.height === height) return image;
  const out = new Uint8ClampedArray(width * height * 4);
  const xR = image.width / width;
  const yR = image.height / height;
  for (let y = 0; y < height; y++) {
    const sy = Math.min(image.height - 1, Math.floor((y + 0.5) * yR));
    for (let x = 0; x < width; x++) {
      const sx = Math.min(image.width - 1, Math.floor((x + 0.5) * xR));
      const si = (sy * image.width + sx) * 4;
      const di = (y * width + x) * 4;
      out[di] = image.data[si];
      out[di + 1] = image.data[si + 1];
      out[di + 2] = image.data[si + 2];
      out[di + 3] = 255;
    }
  }
  return { data: out, width, height };
}

function meanStd(arr, n) {
  let s = 0;
  for (let i = 0; i < n; i++) s += arr[i];
  const m = s / n;
  let v = 0;
  for (let i = 0; i < n; i++) {
    const d = arr[i] - m;
    v += d * d;
  }
  return { mean: m, std: Math.sqrt(v) };
}

/**
 * Sliding-window NCC of `needle` inside `haystack` (both grayscale Float32).
 * `step` trades speed for precision. Returns the best window in haystack
 * pixels (already scaled if the inputs were downsampled).
 */
export function matchNccGray(hay, needle, { step = 1 } = {}) {
  if (needle.width > hay.width || needle.height > hay.height) return null;
  const nw = needle.width;
  const nh = needle.height;
  const n = nw * nh;
  const { mean: nMean, std: nStd } = meanStd(needle.data, n);
  if (nStd < 1e-3) return null;
  let best = -1;
  let bestX = 0;
  let bestY = 0;
  const win = new Float32Array(n);
  const maxX = hay.width - nw;
  const maxY = hay.height - nh;
  for (let y = 0; y <= maxY; y += step) {
    for (let x = 0; x <= maxX; x += step) {
      let k = 0;
      for (let r = 0; r < nh; r++) {
        const row = (y + r) * hay.width + x;
        for (let c = 0; c < nw; c++) win[k++] = hay.data[row + c];
      }
      const { mean, std } = meanStd(win, n);
      if (std < 1e-3) continue;
      let dot = 0;
      for (let i = 0; i < n; i++) {
        dot += (win[i] - mean) * (needle.data[i] - nMean);
      }
      const score = dot / (std * nStd);
      if (score > best) {
        best = score;
        bestX = x;
        bestY = y;
      }
    }
  }
  if (best < 0) return null;
  return { x: bestX, y: bestY, width: nw, height: nh, score: best };
}

/**
 * Multi-scale template match. Images are RGBA {data,width,height}.
 * Scales are applied to the template relative to its native size.
 */
export function matchTemplate(haystack, needle, { scales, maxHay = 480, minScore = 0.35 } = {}) {
  const scaleList = scales || [0.5, 0.75, 1, 1.25];
  const hayScale = Math.min(1, maxHay / Math.max(haystack.width, haystack.height));
  const hayR = hayScale < 1
    ? resizeRgba(haystack, Math.round(haystack.width * hayScale), Math.round(haystack.height * hayScale))
    : haystack;
  const hayG = toGray(hayR);
  let best = null;
  for (const s of scaleList) {
    const tw = Math.max(8, Math.round(needle.width * s * hayScale));
    const th = Math.max(8, Math.round(needle.height * s * hayScale));
    if (tw >= hayG.width || th >= hayG.height) continue;
    const nd = toGray(resizeRgba(needle, tw, th));
    const step = tw >= 40 ? Math.max(2, Math.round(tw / 8)) : 1;
    const hit = matchNccGray(hayG, nd, { step });
    if (!hit) continue;
    // Optional 1-pixel refine at step 1 around the peak when step was 2.
    const mapped = {
      x: hit.x / hayScale,
      y: hit.y / hayScale,
      width: hit.width / hayScale,
      height: hit.height / hayScale,
      score: hit.score,
      scale: s,
      method: "template",
    };
    if (!best || mapped.score > best.score) best = mapped;
  }
  if (!best || best.score < minScore) return best; // still return for ranking
  return best;
}

/** HSV helper, h in [0,360), s,v in [0,1]. */
export function rgbToHsv(r, g, b) {
  r /= 255;
  g /= 255;
  b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d > 1e-6) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  if (h < 0) h += 360;
  return { h, s: max === 0 ? 0 : d / max, v: max };
}

export function referenceHue(needle, { minS = 0.4, minV = 0.3 } = {}) {
  const hues = [];
  const { data } = needle;
  for (let i = 0; i < data.length; i += 4) {
    const hsv = rgbToHsv(data[i], data[i + 1], data[i + 2]);
    if (hsv.s >= minS && hsv.v >= minV) hues.push(hsv.h);
  }
  if (!hues.length) return null;
  hues.sort((a, b) => a - b);
  return hues[(hues.length / 2) | 0];
}

function hueDiff(a, b) {
  const d = Math.abs(a - b) % 360;
  return d > 180 ? 360 - d : d;
}

/**
 * Search for a window whose saturated pixels match the reference's hue.
 * Useful when the marking is a small orange/red/cyan tag on grass.
 */
export function matchColorSpot(haystack, needle, { maxHay = 360, hueTol = 18 } = {}) {
  const hue = referenceHue(needle);
  if (hue == null) return null;
  const hayScale = Math.min(1, maxHay / Math.max(haystack.width, haystack.height));
  const hayR = hayScale < 1
    ? resizeRgba(haystack, Math.round(haystack.width * hayScale), Math.round(haystack.height * hayScale))
    : haystack;
  const winW = Math.max(8, Math.round(needle.width * hayScale));
  const winH = Math.max(8, Math.round(needle.height * hayScale));
  if (winW >= hayR.width || winH >= hayR.height) return null;

  const { data, width, height } = hayR;
  const matchMap = new Uint8Array(width * height);
  for (let i = 0, p = 0; i < data.length; i += 4, p++) {
    const hsv = rgbToHsv(data[i], data[i + 1], data[i + 2]);
    if (hsv.s >= 0.35 && hsv.v >= 0.25 && hueDiff(hsv.h, hue) <= hueTol) matchMap[p] = 1;
  }
  // Integral image of matches
  const integ = new Uint32Array((width + 1) * (height + 1));
  for (let y = 1; y <= height; y++) {
    let row = 0;
    for (let x = 1; x <= width; x++) {
      row += matchMap[(y - 1) * width + (x - 1)];
      integ[y * (width + 1) + x] = integ[(y - 1) * (width + 1) + x] + row;
    }
  }
  const sum = (x0, y0, x1, y1) => {
    const W = width + 1;
    return integ[y1 * W + x1] - integ[y0 * W + x1] - integ[y1 * W + x0] + integ[y0 * W + x0];
  };

  let best = 0;
  let bestX = 0;
  let bestY = 0;
  const step = Math.max(1, Math.round(Math.min(winW, winH) / 4));
  const area = winW * winH;
  for (let y = 0; y + winH <= height; y += step) {
    for (let x = 0; x + winW <= width; x += step) {
      const s = sum(x, y, x + winW, y + winH) / area;
      if (s > best) {
        best = s;
        bestX = x;
        bestY = y;
      }
    }
  }
  if (best < 0.08) return null;
  // Color scores are capped so they cannot outrank a strong template/feature hit.
  return {
    x: bestX / hayScale,
    y: bestY / hayScale,
    width: winW / hayScale,
    height: winH / hayScale,
    score: Math.min(0.7, best),
    method: "color",
    hue,
  };
}

export function boxCenter(box) {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

export function boxesOverlap(a, b) {
  const x0 = Math.max(a.x, b.x);
  const y0 = Math.max(a.y, b.y);
  const x1 = Math.min(a.x + a.width, b.x + b.width);
  const y1 = Math.min(a.y + a.height, b.y + b.height);
  if (x1 <= x0 || y1 <= y0) return 0;
  const inter = (x1 - x0) * (y1 - y0);
  const union = a.width * a.height + b.width * b.height - inter;
  return union > 0 ? inter / union : 0;
}

/**
 * Combine template + color (and an optional feature match from OpenCV).
 * If two methods agree on a box, boost confidence.
 */
export function combineMatches(parts) {
  const hits = parts.filter(Boolean);
  if (!hits.length) return null;
  hits.sort((a, b) => b.score - a.score);
  let best = { ...hits[0] };
  const methods = new Set([best.method]);
  for (let i = 1; i < hits.length; i++) {
    if (boxesOverlap(best, hits[i]) > 0.2) {
      methods.add(hits[i].method);
      best.score = Math.min(0.99, best.score + 0.08);
    }
  }
  best.methods = [...methods];
  best.confidence = Math.max(0, Math.min(1, best.score));
  return best;
}

export function matchFallback(haystack, needle, options) {
  const tmpl = matchTemplate(haystack, needle, options);
  const color = matchColorSpot(haystack, needle, options);
  return combineMatches([tmpl, color]);
}

/**
 * Run OpenCV.js ORB (then AKAZE if present): knn + Lowe ratio + RANSAC
 * homography. `cv` is the OpenCV runtime. Returns a box in search-image
 * pixels or null.
 */
export function matchFeaturesCv(cv, refRgba, searchRgba, { ratio = 0.75, minInliers = 8 } = {}) {
  if (!cv || !cv.Mat) return null;
  const ref = rgbaToMat(cv, refRgba);
  const sea = rgbaToMat(cv, searchRgba);
  const refG = new cv.Mat();
  const seaG = new cv.Mat();
  cv.cvtColor(ref, refG, cv.COLOR_RGBA2GRAY);
  cv.cvtColor(sea, seaG, cv.COLOR_RGBA2GRAY);

  const tryDetect = (name) => {
    let detector = null;
    try {
      if (name === "AKAZE" && cv.AKAZE) detector = new cv.AKAZE();
      else if (name === "ORB" && cv.ORB) detector = new cv.ORB(2000);
    } catch {
      detector = null;
    }
    if (!detector) return null;
    const kp1 = new cv.KeyPointVector();
    const kp2 = new cv.KeyPointVector();
    const d1 = new cv.Mat();
    const d2 = new cv.Mat();
    try {
      detector.detectAndCompute(refG, new cv.Mat(), kp1, d1);
      detector.detectAndCompute(seaG, new cv.Mat(), kp2, d2);
      if (d1.rows < 8 || d2.rows < 8) return null;
      const knn = new cv.DMatchVectorVector();
      const matcher = new cv.BFMatcher(cv.NORM_HAMMING, false);
      matcher.knnMatch(d1, d2, knn, 2);
      const good = [];
      for (let i = 0; i < knn.size(); i++) {
        const pair = knn.get(i);
        if (pair.size() < 2) continue;
        const a = pair.get(0);
        const b = pair.get(1);
        if (a.distance < ratio * b.distance) good.push(a);
      }
      if (good.length < minInliers) return null;
      const src = cv.matFromArray(good.length, 1, cv.CV_32FC2, flatKp(kp1, good, "query"));
      const dst = cv.matFromArray(good.length, 1, cv.CV_32FC2, flatKp(kp2, good, "train"));
      const mask = new cv.Mat();
      const H = cv.findHomography(src, dst, cv.RANSAC, 5, mask);
      if (H.empty()) return null;
      let inliers = 0;
      for (let i = 0; i < mask.rows; i++) if (mask.data[i]) inliers += 1;
      if (inliers < minInliers) return null;
      const corners = cv.matFromArray(4, 1, cv.CV_32FC2, [
        0, 0,
        refRgba.width, 0,
        refRgba.width, refRgba.height,
        0, refRgba.height,
      ]);
      const proj = new cv.Mat();
      cv.perspectiveTransform(corners, proj, H);
      const xs = [], ys = [];
      for (let i = 0; i < 4; i++) {
        xs.push(proj.data32F[i * 2]);
        ys.push(proj.data32F[i * 2 + 1]);
      }
      const x = Math.min(...xs);
      const y = Math.min(...ys);
      const w = Math.max(...xs) - x;
      const h = Math.max(...ys) - y;
      if (!(w > 2 && h > 2)) return null;
      const inlierRatio = inliers / good.length;
      const score = Math.min(0.99, 0.35 + 0.5 * inlierRatio + 0.02 * Math.min(inliers, 20));
      return {
        x,
        y,
        width: w,
        height: h,
        score,
        method: name.toLowerCase(),
        inliers,
        matches: good.length,
        inlierRatio,
      };
    } catch {
      return null;
    } finally {
      try {
        detector.delete();
      } catch {
        /* ignore */
      }
    }
  };

  let hit = tryDetect("ORB");
  if (!hit || hit.inliers < 16) {
    const akaze = tryDetect("AKAZE");
    if (akaze && (!hit || akaze.score > hit.score)) hit = akaze;
  }

  ref.delete();
  sea.delete();
  refG.delete();
  seaG.delete();
  return hit;
}

function flatKp(kps, matches, side) {
  const out = [];
  for (const m of matches) {
    const idx = side === "query" ? m.queryIdx : m.trainIdx;
    const pt = kps.get(idx).pt;
    out.push(pt.x, pt.y);
  }
  return out;
}

function rgbaToMat(cv, image) {
  const mat = new cv.Mat(image.height, image.width, cv.CV_8UC4);
  mat.data.set(image.data);
  return mat;
}

export async function loadOpenCv(url) {
  if (typeof window === "undefined") return null;
  if (window.cv && window.cv.Mat) return window.cv;
  const src = url || "https://docs.opencv.org/4.10.0/opencv.js";
  await new Promise((resolve, reject) => {
    if (document.querySelector(`script[data-opencv]`)) {
      const t = setInterval(() => {
        if (window.cv && window.cv.Mat) {
          clearInterval(t);
          resolve();
        }
      }, 50);
      setTimeout(() => {
        clearInterval(t);
        resolve();
      }, 20000);
      return;
    }
    const s = document.createElement("script");
    s.async = true;
    s.dataset.opencv = "1";
    s.src = src;
    s.onerror = () => reject(new Error("Could not load OpenCV.js"));
    window.cv = window.cv || {};
    window.cv.onRuntimeInitialized = () => resolve();
    document.head.appendChild(s);
    setTimeout(() => {
      if (window.cv && window.cv.Mat) resolve();
    }, 20000);
  });
  return window.cv && window.cv.Mat ? window.cv : null;
}

/**
 * Full pipeline: features if OpenCV is ready, plus template/color fallback.
 */
export async function matchTarget(haystack, needle, { cv = null } = {}) {
  const fallback = matchFallback(haystack, needle);
  let feature = null;
  if (cv && cv.Mat) {
    try {
      feature = matchFeaturesCv(cv, needle, haystack);
    } catch {
      feature = null;
    }
  }
  return combineMatches([feature, fallback]);
}

/** Draw a match rectangle onto a copy of the search image. */
export function drawMatchBox(image, box, { color = [240, 162, 2, 255], label } = {}) {
  const w = image.width;
  const h = image.height;
  const out = { data: new Uint8ClampedArray(image.data), width: w, height: h };
  const x0 = Math.max(0, Math.round(box.x));
  const y0 = Math.max(0, Math.round(box.y));
  const x1 = Math.min(w - 1, Math.round(box.x + box.width));
  const y1 = Math.min(h - 1, Math.round(box.y + box.height));
  const thick = Math.max(2, Math.round(Math.min(w, h) / 200));
  const paint = (x, y) => {
    const i = (y * w + x) * 4;
    out.data[i] = color[0];
    out.data[i + 1] = color[1];
    out.data[i + 2] = color[2];
    out.data[i + 3] = 255;
  };
  for (let t = 0; t < thick; t++) {
    for (let x = x0; x <= x1; x++) {
      if (y0 + t < h) paint(x, y0 + t);
      if (y1 - t >= 0) paint(x, y1 - t);
    }
    for (let y = y0; y <= y1; y++) {
      if (x0 + t < w) paint(x0 + t, y);
      if (x1 - t >= 0) paint(x1 - t, y);
    }
  }
  void label;
  return out;
}
