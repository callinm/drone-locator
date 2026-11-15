/**
 * Drone Locator: make a search map from search-flight photos.
 * Photos are shrunk on the device (EXIF/GPS kept), sent in small batches,
 * stitched on the server, and the result is loaded as the search map.
 */
import { apiFetch } from "./auth.js?v=20261114dlpm1";

export const MAX_EDGE = 2048;
export const BATCH = 8;
export const MIN_PHOTOS = 5;
export const MAX_PHOTOS = 200;

/** Return the APP1 (Exif + XMP) segments of a JPEG, or [] if it is not a JPEG. */
export function jpegApp1Segments(bytes) {
  const segs = [];
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return segs;
  let i = 2;
  while (i + 4 <= bytes.length) {
    if (bytes[i] !== 0xff) break;
    const marker = bytes[i + 1];
    if (marker === 0xda || marker === 0xd9) break; // start of scan / end
    const len = (bytes[i + 2] << 8) | bytes[i + 3];
    if (len < 2 || i + 2 + len > bytes.length) break;
    if (marker === 0xe1) segs.push(bytes.subarray(i, i + 2 + len));
    i += 2 + len;
  }
  return segs;
}

/** Put the original APP1 segments right after SOI of a freshly encoded JPEG (dropping its own APP0/APP1). */
export function spliceApp1(encoded, app1s) {
  if (!app1s.length) return encoded;
  let i = 2;
  while (i + 4 <= encoded.length && encoded[i] === 0xff && (encoded[i + 1] === 0xe0 || encoded[i + 1] === 0xe1)) {
    i += 2 + ((encoded[i + 2] << 8) | encoded[i + 3]);
  }
  const extra = app1s.reduce((n, s) => n + s.length, 0);
  const out = new Uint8Array(2 + extra + (encoded.length - i));
  out.set([0xff, 0xd8], 0);
  let o = 2;
  for (const s of app1s) {
    out.set(s, o);
    o += s.length;
  }
  out.set(encoded.subarray(i), o);
  return out;
}

/** Shrink one photo to MAX_EDGE on the device. Falls back to the original file (the server shrinks it). */
export async function shrinkJpeg(file, maxEdge = MAX_EDGE) {
  try {
    if (!/jpe?g$/i.test(file.type || "") && !/\.jpe?g$/i.test(file.name || "")) return file;
    if (typeof createImageBitmap !== "function") return file;
    const buf = new Uint8Array(await file.arrayBuffer());
    const app1 = jpegApp1Segments(buf);
    if (!app1.length) return file; // no EXIF: let the server decide (it needs GPS)
    // Keep the pixels as stored; the EXIF orientation tag travels with them.
    const bmp = await createImageBitmap(file, { imageOrientation: "none" });
    const { width: w, height: h } = bmp;
    if (Math.max(w, h) <= maxEdge) {
      bmp.close?.();
      return file;
    }
    const s = maxEdge / Math.max(w, h);
    const tw = Math.round(w * s);
    const th = Math.round(h * s);
    let blob;
    if (typeof OffscreenCanvas !== "undefined") {
      const c = new OffscreenCanvas(tw, th);
      c.getContext("2d").drawImage(bmp, 0, 0, tw, th);
      blob = await c.convertToBlob({ type: "image/jpeg", quality: 0.9 });
    } else {
      const c = document.createElement("canvas");
      c.width = tw;
      c.height = th;
      c.getContext("2d").drawImage(bmp, 0, 0, tw, th);
      blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.9));
    }
    bmp.close?.();
    if (!blob) return file;
    const out = spliceApp1(new Uint8Array(await blob.arrayBuffer()), app1);
    return new File([out], file.name || "photo.jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

async function json(res) {
  return res.json().catch(() => ({}));
}

async function call(path, opts = {}) {
  const res = await apiFetch(path, opts);
  const body = await json(res);
  if (!res.ok) {
    const err = new Error(body.detail || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

export async function latestJob() {
  try {
    const body = await call("/searchmaps/latest");
    return body.job || null;
  } catch {
    return null;
  }
}

export function getJob(id) {
  return call(`/searchmaps/${encodeURIComponent(id)}`);
}

export function cancelJob(id) {
  return call(`/searchmaps/${encodeURIComponent(id)}/cancel`, { method: "POST", json: {} });
}

export function startJob(id) {
  return call(`/searchmaps/${encodeURIComponent(id)}/start`, { method: "POST", json: {} });
}

/**
 * Shrink + upload all photos, then start the stitch.
 * onUpdate({phase:"upload", done, total, skipped}) is called as it goes.
 */
export async function uploadAndStart(files, onUpdate, isCancelled = () => false) {
  const photos = Array.from(files || []).filter((f) => /jpe?g$/i.test(f.type || "") || /\.jpe?g$/i.test(f.name || ""));
  const ignored = (files?.length || 0) - photos.length;
  if (photos.length < MIN_PHOTOS) {
    throw new Error(`Pick at least ${MIN_PHOTOS} photos (JPG) from your search flight. You picked ${photos.length}.`);
  }
  if (photos.length > MAX_PHOTOS) {
    throw new Error(`Pick up to ${MAX_PHOTOS} photos at a time. You picked ${photos.length}. Try half of the flight first.`);
  }
  let job = await call("/searchmaps", { method: "POST", json: {} });
  onUpdate?.({ phase: "upload", done: 0, total: photos.length, job, ignored });
  for (let k = 0; k < photos.length; k += BATCH) {
    if (isCancelled()) {
      await cancelJob(job.id).catch(() => {});
      throw new Error("Cancelled.");
    }
    const fd = new FormData();
    const slice = photos.slice(k, k + BATCH);
    for (const f of slice) {
      const small = await shrinkJpeg(f);
      fd.append("photos", small, f.name || "photo.jpg");
    }
    let tries = 0;
    for (;;) {
      try {
        job = await call(`/searchmaps/${job.id}/photos`, { method: "POST", body: fd });
        break;
      } catch (err) {
        tries += 1;
        if (tries >= 3 || (err.status && err.status < 500 && err.status !== 408 && err.status !== 429)) throw err;
        await new Promise((r) => setTimeout(r, 1500 * tries));
      }
    }
    onUpdate?.({ phase: "upload", done: Math.min(photos.length, k + slice.length), total: photos.length, job, ignored });
  }
  return startJob(job.id);
}
