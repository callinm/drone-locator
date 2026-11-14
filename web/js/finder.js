/**
 * Find My Drone — upload a mosaic once, send a query (region or photo)
 * to the server matcher, and draw the returned boxes / GPS points.
 */
import { getLatestSurveyMap } from "./survey-map.js";
import { attachBasemap, refreshMapSize, locateOrFallback } from "./basemap.js";
import { setPageTab, onPageTab } from "./tabs.js";
import { apiFetch, apiBase as authApiBase, isMosaicEnabled, isAuthenticated, lockMessage } from "./auth.js";
import { enqueue, putFindResults } from "./sync.js";

const MAX_PREVIEW = 2048;

const state = {
  apiBase: "",
  apiOk: null,
  mosaicId: null,
  mosaicMeta: null, // {width,height,georeferenced,bounds,...}
  display: { scale: 1, width: 0, height: 0 },
  queryRect: null, // mosaic-pixel {x,y,w,h}
  queryFile: null,
  queryUrl: null,
  drag: null,
  matches: [],
  map: null,
  orthoLayer: null,
  matchLayer: null,
  queryLayer: null,
  locateMarker: null,
};

function el(id) {
  return document.getElementById(id);
}

function apiBase() {
  return authApiBase();
}

async function readApiError(res, fallback) {
  try {
    const body = await res.json();
    if (body && body.detail) return body.detail;
  } catch {}
  if (res.status === 403) return lockMessage().body;
  if (res.status === 401) return "Sign in to use Find My Drone.";
  return fallback || `HTTP ${res.status}`;
}

function setBox(id, msg) {
  const box = el(id);
  if (!box) return;
  box.hidden = !msg;
  box.textContent = msg || "";
}

function setError(msg) { setBox("findError", msg); }
function setWarn(msg) { setBox("findWarn", msg); }

function setProgress(msg) {
  const n = el("findProgress");
  if (n) n.textContent = msg;
}

function setMosaicStatus(msg) {
  const n = el("mosaicStatus");
  if (n) n.textContent = msg;
}

function setRefStatus(msg) {
  const n = el("refStatus");
  if (n) n.textContent = msg;
}

async function probeApi() {
  state.apiBase = apiBase();
  try {
    const r = await fetch(`${state.apiBase}/health`, { method: "GET" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    state.apiOk = !!body.ok;
  } catch (err) {
    state.apiOk = false;
    setWarn(
      "The matcher service is not running. Matching happens on the server " +
      `(${state.apiBase}), not in the browser, so this tab will work once ` +
      "the Find API is live. Nothing has been changed on the public site yet."
    );
  }
  return state.apiOk;
}

function parseBounds(text) {
  if (!text || !text.trim()) return null;
  const parts = text.split(/[, ]+/).map((s) => s.trim()).filter(Boolean).map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n))) {
    throw new Error("Bounds must be four numbers: west, south, east, north.");
  }
  const [west, south, east, north] = parts;
  return { west, south, east, north };
}

function boundsFromInput() {
  const raw = el("findBounds")?.value || "";
  return parseBounds(raw);
}

function fillBoundsInput(b) {
  if (!b || !el("findBounds")) return;
  el("findBounds").value = [b.west, b.south, b.east, b.north]
    .map((n) => Number(n).toFixed(6).replace(/0+$/, "").replace(/\.$/, ""))
    .join(", ");
}

function canvasToBlob(canvas, type = "image/png", quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Could not encode the image."))), type, quality);
  });
}

function displaySize(srcW, srcH, maxDim = MAX_PREVIEW) {
  const scale = Math.min(1, maxDim / Math.max(srcW, srcH));
  return {
    scale,
    width: Math.max(1, Math.round(srcW * scale)),
    height: Math.max(1, Math.round(srcH * scale)),
  };
}

function layoutCanvases(w, h) {
  const canvas = el("findCanvas");
  const overlay = el("findOverlay");
  const inner = el("findStageInner");
  canvas.width = w;
  canvas.height = h;
  overlay.width = w;
  overlay.height = h;
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
  overlay.style.width = `${w}px`;
  overlay.style.height = `${h}px`;
  if (inner) {
    inner.style.width = `${w}px`;
    inner.style.height = `${h}px`;
  }
}

async function drawFilePreview(file, width, height) {
  const disp = displaySize(width, height);
  state.display = disp;
  layoutCanvases(disp.width, disp.height);
  const ctx = el("findCanvas").getContext("2d");
  ctx.fillStyle = "#0b1014";
  ctx.fillRect(0, 0, disp.width, disp.height);
  if (!file.type || /tiff/i.test(file.type) || /\.ti[f]+$/i.test(file.name || "")) {
    // Browsers often cannot paint TIFF; a server preview is fetched after upload.
    ctx.fillStyle = "#9aa4ad";
    ctx.font = "14px sans-serif";
    ctx.fillText("Uploading TIFF… a preview will appear after the server receives it.", 16, 32);
    return;
  }
  const url = URL.createObjectURL(file);
  try {
    const img = await loadImage(url);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(img, 0, 0, disp.width, disp.height);
  } finally {
    URL.revokeObjectURL(url);
  }
  syncFindMap();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Could not decode that image in the browser."));
    img.src = url;
  });
}

async function fetchServerPreview() {
  if (!state.mosaicId || !state.mosaicMeta) return;
  const { width, height } = state.mosaicMeta;
  const disp = displaySize(width, height);
  state.display = disp;
  layoutCanvases(disp.width, disp.height);
  const r = await apiFetch(`/mosaics/${state.mosaicId}/preview?max_dim=${MAX_PREVIEW}`);
  if (!r.ok) throw new Error(await readApiError(r, "The server could not build a preview."));
  const blob = await r.blob();
  const obj = URL.createObjectURL(blob);
  try {
    const img = await loadImage(obj);
    const ctx = el("findCanvas").getContext("2d");
    ctx.drawImage(img, 0, 0, disp.width, disp.height);
  } finally {
    URL.revokeObjectURL(obj);
  }
  redrawOverlay();
}

function eventToMosaicPx(ev) {
  const canvas = el("findCanvas");
  const rect = canvas.getBoundingClientRect();
  const sx = (ev.clientX - rect.left) / rect.width;
  const sy = (ev.clientY - rect.top) / rect.height;
  const W = state.mosaicMeta?.width || canvas.width;
  const H = state.mosaicMeta?.height || canvas.height;
  const x = Math.min(W, Math.max(0, sx * W));
  const y = Math.min(H, Math.max(0, sy * H));
  return { x, y };
}

function normRect(x0, y0, x1, y1) {
  const x = Math.min(x0, x1);
  const y = Math.min(y0, y1);
  return { x, y, w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
}

function mosaicToDisplay(r) {
  const s = state.display.scale || 1;
  return { x: r.x * s, y: r.y * s, w: r.w * s, h: r.h * s };
}

function redrawOverlay() {
  const canvas = el("findOverlay");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (state.queryRect && state.queryRect.w > 1 && state.queryRect.h > 1) {
    const r = mosaicToDisplay(state.queryRect);
    ctx.save();
    ctx.strokeStyle = "#7ee8ff";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w, r.h);
    ctx.restore();
  }
  state.matches.forEach((m, i) => {
    const r = mosaicToDisplay(m);
    const top = i === 0;
    ctx.save();
    ctx.strokeStyle = top ? "#f5c542" : "#4cc9f0";
    ctx.lineWidth = top ? 3 : 2;
    ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w, r.h);
    const label = `${i + 1}  ${(m.score * 100).toFixed(0)}%  ${m.method || ""}`.trim();
    ctx.font = "bold 12px ui-sans-serif, system-ui, sans-serif";
    const tw = ctx.measureText(label).width + 10;
    const ty = Math.max(14, r.y - 2);
    ctx.fillStyle = "rgba(10,14,18,0.85)";
    ctx.fillRect(r.x, ty - 12, tw, 16);
    ctx.fillStyle = top ? "#f5c542" : "#e8eef4";
    ctx.fillText(label, r.x + 4, ty);
    ctx.restore();
  });
}

function enableSearch() {
  const ready = !!(state.mosaicId && (state.queryFile || (state.queryRect && state.queryRect.w >= 8 && state.queryRect.h >= 8)));
  const btn = el("btnFindSearch");
  if (btn) btn.disabled = !ready || state.apiOk === false;
}

function clearQuery({ keepStatus = false } = {}) {
  state.queryRect = null;
  state.queryFile = null;
  if (state.queryUrl) URL.revokeObjectURL(state.queryUrl);
  state.queryUrl = null;
  const preview = el("refPreview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  if (!keepStatus) {
    setRefStatus("Click and drag a region on the mosaic, or upload a close-up of the aircraft or a marking.");
  }
  redrawOverlay();
  enableSearch();
  if (document.getElementById("viewFind") && !document.getElementById("viewFind").hidden) {
    ensureFindMap();
  }
}

function showRefPreview(src, caption) {
  const preview = el("refPreview");
  if (!preview) return;
  preview.hidden = false;
  preview.innerHTML = "";
  const img = document.createElement("img");
  img.alt = "Query";
  img.src = src;
  preview.appendChild(img);
  if (caption) setRefStatus(caption);
}

async function uploadMosaic(file, bounds) {
  if (!isMosaicEnabled()) {
    throw new Error(lockMessage().body);
  }
  if (state.apiOk === false) {
    throw new Error("The matcher service is not running.");
  }
  setProgress("Uploading mosaic to the matcher…");
  const fd = new FormData();
  fd.append("file", file, file.name || "mosaic.jpg");
  if (bounds) {
    fd.append("west", String(bounds.west));
    fd.append("south", String(bounds.south));
    fd.append("east", String(bounds.east));
    fd.append("north", String(bounds.north));
  }
  const r = await apiFetch("/mosaics", { method: "POST", body: fd });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `Upload failed (${r.status})`);
  state.mosaicId = body.id;
  state.mosaicMeta = body;
  state.matches = [];
  const geo = body.georeferenced
    ? `georeferenced` + (body.bounds ? ` (${body.bounds.west.toFixed(5)}, ${body.bounds.south.toFixed(5)})–(${body.bounds.east.toFixed(5)}, ${body.bounds.north.toFixed(5)})` : "")
    : "not georeferenced — add bounds above and re-upload if you want GPS";
  setMosaicStatus(
    `Mosaic “${body.filename}” uploaded — ${body.width}×${body.height} px, ${geo}. ` +
    `It will expire automatically; later searches reuse this id so the file is not sent again.`
  );
  if (body.bounds) fillBoundsInput(body.bounds);
  setProgress("Mosaic is on the server. Draw a query region or upload a reference, then search.");
  enableSearch();
  syncFindMap();
  return body;
}

async function onMosaicFile(file) {
  setError("");
  setWarn("");
  let bounds = null;
  try {
    bounds = boundsFromInput();
  } catch (err) {
    setError(err.message);
    return;
  }
  // Peek dimensions for JPEG/PNG so the canvas can paint immediately.
  let w = 0, h = 0;
  try {
    if (!/\.ti[f]+$/i.test(file.name || "") && !/tiff/i.test(file.type || "")) {
      const url = URL.createObjectURL(file);
      try {
        const img = await loadImage(url);
        w = img.naturalWidth;
        h = img.naturalHeight;
      } finally {
        URL.revokeObjectURL(url);
      }
    }
  } catch {
    /* preview will come from the server */
  }
  if (w && h) {
    state.mosaicMeta = { ...(state.mosaicMeta || {}), width: w, height: h };
    await drawFilePreview(file, w, h);
  }
  try {
    await uploadMosaic(file, bounds);
    // Prefer the server preview so TIFF / large files still appear.
    try {
      await fetchServerPreview();
    } catch (err) {
      if (!w) setWarn(err.message);
    }
  } catch (err) {
    setError(err.message);
    setProgress("Upload failed.");
  }
  redrawOverlay();
}

async function useLatestSurveyMap() {
  setError("");
  const packed = getLatestSurveyMap();
  if (!packed || !packed.canvas) {
    setError("No survey map is ready yet. Upload a mosaic here, or use a survey map already on this account.");
    return;
  }
  const blob = await canvasToBlob(packed.canvas, "image/png");
  const file = new File([blob], packed.name || "mosaic-ortho.png", { type: "image/png" });
  if (packed.bounds) fillBoundsInput(packed.bounds);
  state.mosaicMeta = { width: packed.width, height: packed.height };
  await drawFilePreview(file, packed.width, packed.height);
  try {
    await uploadMosaic(file, packed.bounds || boundsFromInput());
  } catch (err) {
    setError(err.message);
  }
  redrawOverlay();
}

async function onRefFile(file) {
  setError("");
  clearQuery({ keepStatus: true });
  state.queryFile = file;
  state.queryUrl = URL.createObjectURL(file);
  showRefPreview(state.queryUrl, `Reference “${file.name}” will be sent with the next search. The mosaic stays on the server.`);
  enableSearch();
  if (document.getElementById("viewFind") && !document.getElementById("viewFind").hidden) {
    ensureFindMap();
  }
}

async function runSearch() {
  setError("");
  if (!state.apiOk) {
    setError("The matcher service is not running, so the search cannot start.");
    return;
  }
  if (!state.mosaicId) {
    setError("Load a mosaic first.");
    return;
  }
  const btn = el("btnFindSearch");
  if (btn) btn.disabled = true;
  setProgress("Searching the mosaic on the server… this can take a while on a large orthophoto.");
  const fd = new FormData();
  fd.append("top_n", "8");
  const useColor = el("findMatchColor") ? el("findMatchColor").checked : true;
  const small = el("findSmallMarking") && el("findSmallMarking").checked;
  // Small marking: color-only by default so low-score shape leftovers do not pad the list.
  fd.append("method", useColor ? (small ? "color" : "color+shape") : "shape");
  const preset = el("findColorPreset") ? el("findColorPreset").value : "auto";
  if (useColor && preset && preset !== "auto" && preset !== "custom") {
    fd.append("color_preset", preset);
  }
  if (useColor && preset === "custom" && el("findColorPicker")) {
    fd.append("color", el("findColorPicker").value);
  }
  const sizeCm = el("findTargetSizeCm") ? Number(el("findTargetSizeCm").value) : NaN;
  if (small || (Number.isFinite(sizeCm) && sizeCm > 0)) {
    fd.append("size_mode", "small_marking");
  }
  if (Number.isFinite(sizeCm) && sizeCm > 0) {
    fd.append("target_size_m", String(sizeCm / 100));
  }
  const gsdCm = el("findGsdCm") ? Number(el("findGsdCm").value) : NaN;
  if (Number.isFinite(gsdCm) && gsdCm > 0) {
    fd.append("gsd_m", String(gsdCm / 100));
  }
  const altM = el("findAltitudeM") ? Number(el("findAltitudeM").value) : NaN;
  if (Number.isFinite(altM) && altM > 0) {
    fd.append("altitude_m", String(altM));
  }
  if (state.queryFile) {
    fd.append("query", state.queryFile, state.queryFile.name || "query.png");
  } else if (state.queryRect) {
    const r = state.queryRect;
    fd.append("x", String(r.x));
    fd.append("y", String(r.y));
    fd.append("w", String(r.w));
    fd.append("h", String(r.h));
  } else {
    setError("Draw a region or upload a reference photo first.");
    enableSearch();
    return;
  }
  const t0 = performance.now();
  try {
    const r = await apiFetch(`/mosaics/${state.mosaicId}/match`, { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `Match failed (${r.status})`);
    let matches = Array.isArray(body.matches) ? body.matches : [];
    // If the query was a crop of this mosaic, hide the exact region itself.
    if (state.queryRect && !state.queryFile) {
      const q = state.queryRect;
      matches = matches.filter((m) => iou(m, q) < 0.5);
    }
    state.matches = matches;
    syncFindMap();
    renderNavigate();
    const dt = ((performance.now() - t0) / 1000).toFixed(1);
    if (!matches.length) {
      setProgress(`No matches above the threshold (${dt}s). Try a tighter region or a clearer reference.`);
    } else {
      setProgress(`Found ${matches.length} match${matches.length === 1 ? "" : "es"} in ${dt}s. Boxes are drawn on the mosaic.`);
    }
    renderResults();
    redrawOverlay();
    if (isAuthenticated()) {
      const results = {
        mosaic_id: state.mosaicId,
        matches: matches.map((m) => ({
          x: m.x, y: m.y, w: m.w, h: m.h,
          score: m.score, lon: m.lon, lat: m.lat,
        })),
        found_at: new Date().toISOString(),
      };
      putFindResults(results).catch(() => {
        enqueue(typeof localStorage !== "undefined" ? localStorage : null, { type: "find-results", results });
      });
    }
  } catch (err) {
    setError(err.message);
    setProgress("Search failed.");
  } finally {
    enableSearch();
  }
}

function iou(a, b) {
  const ax2 = a.x + a.w, ay2 = a.y + a.h, bx2 = b.x + b.w, by2 = b.y + b.h;
  const ix = Math.max(0, Math.min(ax2, bx2) - Math.max(a.x, b.x));
  const iy = Math.max(0, Math.min(ay2, by2) - Math.max(a.y, b.y));
  const inter = ix * iy;
  const union = a.w * a.h + b.w * b.h - inter;
  return union <= 0 ? 0 : inter / union;
}

function mapsUrl(lat, lon) {
  return `https://www.google.com/maps?q=${lat.toFixed(6)},${lon.toFixed(6)}`;
}

function renderResults() {
  const box = el("findResults");
  if (!box) return;
  box.innerHTML = "";
  if (!state.matches.length) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = state.queryRect && !state.queryFile
      ? "Only the region you selected matched. That usually means the marking is not repeated elsewhere on this mosaic."
      : "No matches yet.";
    box.appendChild(p);
    return;
  }
  state.matches.forEach((m, i) => {
    const card = document.createElement("article");
    card.className = "candidate" + (i === 0 ? " is-top" : "");
    const thumb = document.createElement("canvas");
    thumb.width = 96;
    thumb.height = 72;
    paintThumb(thumb, m);
    const meta = document.createElement("div");
    meta.className = "meta";
    const scoreCls = m.score >= 0.6 ? "score" : "score is-low";
    const gps = (Number.isFinite(m.lat) && Number.isFinite(m.lon))
      ? `<div>${m.lat.toFixed(6)}, ${m.lon.toFixed(6)} · <a href="${mapsUrl(m.lat, m.lon)}" target="_blank" rel="noopener">Google Maps</a></div>`
      : `<div>No GPS — the mosaic is not georeferenced.</div>`;
    meta.innerHTML =
      `<strong>#${i + 1} <span class="${scoreCls}">${(m.score * 100).toFixed(0)}%</span> ${escapeHtml(m.method || "")}</strong>` +
      `<div>${m.w.toFixed(0)}×${m.h.toFixed(0)} px at (${m.x.toFixed(0)}, ${m.y.toFixed(0)})</div>` +
      gps;
    card.appendChild(thumb);
    card.appendChild(meta);
    card.addEventListener("click", () => highlightMatch(i));
    box.appendChild(card);
  });
  renderNavigate();
  plotMatchesOnMap();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function paintThumb(canvas, m) {
  const src = el("findCanvas");
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!src || !state.display.scale) return;
  const r = mosaicToDisplay(m);
  const pad = 10;
  const sx = Math.max(0, r.x - pad);
  const sy = Math.max(0, r.y - pad);
  const sw = Math.min(src.width - sx, r.w + pad * 2);
  const sh = Math.min(src.height - sy, r.h + pad * 2);
  if (sw < 1 || sh < 1) return;
  ctx.drawImage(src, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
}

function highlightMatch(i) {
  if (!state.matches[i]) return;
  const m = state.matches[i];
  if (state.map && Number.isFinite(m.lat) && Number.isFinite(m.lon)) {
    state.map.setView([m.lat, m.lon], Math.max(state.map.getZoom(), 18));
  }
  document.querySelectorAll("#findResults .candidate").forEach((c, n) => {
    c.classList.toggle("is-active", n === i);
  });
  // Re-order overlay emphasis by swapping to front visually: just redraw with a ring.
  const r = mosaicToDisplay(m);
  const overlay = el("findOverlay");
  const ctx = overlay.getContext("2d");
  redrawOverlay();
  ctx.save();
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(r.x - 2, r.y - 2, r.w + 4, r.h + 4);
  ctx.restore();
  const stage = el("findStage");
  if (stage) {
    const inner = el("findStageInner");
    const sx = inner ? inner.offsetLeft + r.x + r.w / 2 - stage.clientWidth / 2 : r.x;
    const sy = inner ? inner.offsetTop + r.y + r.h / 2 - stage.clientHeight / 2 : r.y;
    stage.scrollTo({ left: Math.max(0, sx), top: Math.max(0, sy), behavior: "smooth" });
  }
}

function bindStageDrag() {
  const canvas = el("findCanvas");
  if (!canvas) return;
  canvas.addEventListener("mousedown", (ev) => {
    if (!state.mosaicMeta) return;
    ev.preventDefault();
    const p = eventToMosaicPx(ev);
    state.drag = { x: p.x, y: p.y };
  });
  window.addEventListener("mousemove", (ev) => {
    if (!state.drag) return;
    const p = eventToMosaicPx(ev);
    state.queryRect = normRect(state.drag.x, state.drag.y, p.x, p.y);
    // A new region query replaces a previously uploaded reference.
    if (state.queryFile) {
      state.queryFile = null;
      if (state.queryUrl) URL.revokeObjectURL(state.queryUrl);
      state.queryUrl = null;
      const preview = el("refPreview");
      if (preview) { preview.hidden = true; preview.innerHTML = ""; }
    }
    redrawOverlay();
  });
  window.addEventListener("mouseup", () => {
    if (!state.drag) return;
    state.drag = null;
    const r = state.queryRect;
    if (!r || r.w < 8 || r.h < 8) {
      state.queryRect = null;
      setRefStatus("Drag a larger region (at least 8×8 px) or upload a reference photo.");
    } else {
      setRefStatus(`Query region ${r.w.toFixed(0)}×${r.h.toFixed(0)} px at (${r.x.toFixed(0)}, ${r.y.toFixed(0)}). Other instances of this patch will be ranked.`);
    }
    enableSearch();
    redrawOverlay();
  });
}

function makeDemoTarget(ctx, x0, y0, s) {
  ctx.fillStyle = "#f5c542";
  ctx.fillRect(x0, y0, s, s);
  ctx.fillStyle = "#1a1400";
  ctx.fillRect(x0 + 8, y0 + 8, s - 16, s - 16);
  ctx.strokeStyle = "#ff4d4d";
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(x0 + 6, y0 + s - 6);
  ctx.lineTo(x0 + s / 2, y0 + 6);
  ctx.lineTo(x0 + s - 6, y0 + s - 6);
  ctx.stroke();
}

function makeDemoMosaic() {
  const w = 900, h = 700, s = 48;
  const x0 = 430, y0 = 180;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  const img = ctx.createImageData(w, h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = ((y * 131 + x * 17 + Math.floor(y / 64) * 9 + Math.floor(x / 64) * 13) & 63) + 40;
      const i = (y * w + x) * 4;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  makeDemoTarget(ctx, x0, y0, s);
  // A second, slightly smaller copy so the bbox-query demo can find something other than the crop.
  const x1 = 120, y1 = 420, s1 = 40;
  ctx.save();
  ctx.translate(x1 + s1 / 2, y1 + s1 / 2);
  ctx.rotate((12 * Math.PI) / 180);
  ctx.translate(-s1 / 2, -s1 / 2);
  makeDemoTarget(ctx, 0, 0, s1);
  ctx.restore();
  return {
    canvas: c,
    bounds: { west: -88.2264, south: 40.0830, east: -88.2240, north: 40.0850 },
    query: { x: x0 - 4, y: y0 - 4, w: s + 8, h: s + 8 },
  };
}

async function loadDemo() {
  setError("");
  setWarn("");
  if (state.apiOk === false) {
    const still = await probeApi();
    if (!still) {
      setError("Demo needs the matcher service running on this machine (default http://127.0.0.1:8091).");
      return;
    }
  }
  setPageTab("find");
  const demo = makeDemoMosaic();
  fillBoundsInput(demo.bounds);
  const blob = await canvasToBlob(demo.canvas, "image/png");
  const file = new File([blob], "demo-mosaic.png", { type: "image/png" });
  state.mosaicMeta = { width: demo.canvas.width, height: demo.canvas.height };
  state.matches = [];
  clearQuery({ keepStatus: true });
  await drawFilePreview(file, demo.canvas.width, demo.canvas.height);
  try {
    await uploadMosaic(file, demo.bounds);
    state.queryRect = demo.query;
    setRefStatus(
      `Demo query is a padded crop of the yellow marking at (${demo.query.x}, ${demo.query.y}). ` +
        "A second, slightly rotated copy sits elsewhere on the mosaic."
    );
    enableSearch();
    redrawOverlay();
    await runSearch();
  } catch (err) {
    setError(err.message);
  }
}


function mosaicBounds() {
  const b = state.mosaicMeta?.bounds || boundsFromInput();
  if (!b) return null;
  const west = Number(b.west), south = Number(b.south), east = Number(b.east), north = Number(b.north);
  if (![west, south, east, north].every(Number.isFinite)) return null;
  return { west, south, east, north };
}

function latLngToMosaicPx(latlng) {
  const b = mosaicBounds();
  const W = state.mosaicMeta?.width;
  const H = state.mosaicMeta?.height;
  if (!b || !W || !H) return null;
  const x = ((latlng.lng - b.west) / (b.east - b.west)) * W;
  const y = ((b.north - latlng.lat) / (b.north - b.south)) * H;
  return { x, y };
}

function mosaicPxToLatLng(x, y) {
  const b = mosaicBounds();
  const W = state.mosaicMeta?.width;
  const H = state.mosaicMeta?.height;
  if (!b || !W || !H) return null;
  const lon = b.west + (x / W) * (b.east - b.west);
  const lat = b.north - (y / H) * (b.north - b.south);
  return [lat, lon];
}

function ensureFindMap() {
  if (state.map) {
    refreshMapSize(state.map);
    return state.map;
  }
  const host = el("findMap");
  if (!host) return null;
  const map = L.map("findMap", { zoomControl: true, worldCopyJump: true, maxZoom: 22 });
  attachBasemap(map, { locate: false });
  state.matchLayer = L.layerGroup().addTo(map);
  state.queryLayer = L.layerGroup().addTo(map);
  let dragStart = null;
  map.on("mousedown", (ev) => {
    if (!mosaicBounds() || ev.originalEvent.shiftKey || ev.originalEvent.button !== 0) return;
    // Draw a query region only when the pointer is on the ortho.
    dragStart = ev.latlng;
  });
  map.on("mousemove", (ev) => {
    if (!dragStart) return;
    const a = dragStart;
    const b = ev.latlng;
    const sw = [Math.min(a.lat, b.lat), Math.min(a.lng, b.lng)];
    const ne = [Math.max(a.lat, b.lat), Math.max(a.lng, b.lng)];
    state.queryLayer.clearLayers();
    L.rectangle([sw, ne], { color: "#f0a202", weight: 2, fillOpacity: 0.08 }).addTo(state.queryLayer);
  });
  map.on("mouseup", (ev) => {
    if (!dragStart) return;
    const a = dragStart;
    dragStart = null;
    const p0 = latLngToMosaicPx(a);
    const p1 = latLngToMosaicPx(ev.latlng);
    if (!p0 || !p1) return;
    const x = Math.min(p0.x, p1.x), y = Math.min(p0.y, p1.y);
    const w = Math.abs(p1.x - p0.x), h = Math.abs(p1.y - p0.y);
    if (w < 4 || h < 4) return;
    state.queryRect = { x, y, w, h };
    enableSearch();
    redrawOverlay();
    setRefStatus("Query region set on the map. Press Search mosaic.");
  });
  state.map = map;
  locateOrFallback(map, {
    onLocated: ({ lat, lon }) => {
      if (state.locateMarker) state.map.removeLayer(state.locateMarker);
      state.locateMarker = L.circleMarker([lat, lon], {
        radius: 7, color: "#4cc3ff", weight: 2, fillColor: "#4cc3ff", fillOpacity: 0.9,
      }).bindTooltip("Your location").addTo(state.map);
    },
  });
  refreshMapSize(map);
  return map;
}

function syncFindMap() {
  const map = ensureFindMap();
  if (!map) return;
  refreshMapSize(map);
  const b = mosaicBounds();
  if (state.orthoLayer) {
    try { map.removeLayer(state.orthoLayer); } catch { /* */ }
    state.orthoLayer = null;
  }
  const canvas = el("findCanvas");
  if (b && canvas && canvas.width) {
    try {
      const url = canvas.toDataURL("image/png");
      state.orthoLayer = L.imageOverlay(url, [[b.south, b.west], [b.north, b.east]], {
        opacity: 0.85,
        className: "survey-ortho",
        interactive: false,
      }).addTo(map);
      map.fitBounds([[b.south, b.west], [b.north, b.east]], { padding: [20, 20] });
    } catch {
      map.fitBounds([[b.south, b.west], [b.north, b.east]]);
    }
  }
  plotMatchesOnMap();
  renderNavigate();
}

function plotMatchesOnMap() {
  const map = state.map;
  if (!map || !state.matchLayer) return;
  state.matchLayer.clearLayers();
  state.matches.forEach((m, i) => {
    let lat = Number(m.lat), lon = Number(m.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      const mid = mosaicPxToLatLng((m.x || 0) + (m.w || 0) / 2, (m.y || 0) + (m.h || 0) / 2);
      if (mid) {
        lat = mid[0];
        lon = mid[1];
      }
    }
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const pct = Number.isFinite(m.score) ? Math.round(m.score * 100) : null;
    const label = pct == null ? String(i + 1) : `${i + 1} · ${pct}%`;
    const icon = L.divIcon({
      className: "find-pin-wrap",
      html: `<div class="find-pin" title="Match ${i + 1}${pct != null ? ", " + pct + "% confidence" : ""}">${label}</div>`,
      iconSize: [56, 24],
      iconAnchor: [28, 12],
    });
    L.marker([lat, lon], { icon, zIndexOffset: 1000 - i })
      .bindTooltip(`Match ${i + 1}${pct != null ? " · " + pct + "% confidence" : ""}`)
      .on("click", () => highlightMatch(i))
      .addTo(state.matchLayer);
  });
}

function renderNavigate() {
  const bar = el("findNavigate");
  const link = el("findNavigateLink");
  const meta = el("findNavigateMeta");
  if (!bar || !link) return;
  const top = state.matches.find((m) => Number.isFinite(m.lat) && Number.isFinite(m.lon)) || null;
  if (!top) {
    bar.hidden = true;
    return;
  }
  const pct = Number.isFinite(top.score) ? `${Math.round(top.score * 100)}% confidence` : "top match";
  const idx = state.matches.indexOf(top) + 1;
  link.href = mapsUrl(top.lat, top.lon);
  link.textContent = "Navigate here";
  if (meta) meta.textContent = `Match ${idx} · ${pct} · ${top.lat.toFixed(6)}, ${top.lon.toFixed(6)}`;
  bar.hidden = false;
}

export async function initFinder() {
  state.apiBase = apiBase();
  onPageTab((name) => {
    if (name === "find") {
      const stage = el("findStage");
      if (stage) stage.scrollTop = 0;
      ensureFindMap();
      syncFindMap();
    }
  });
  el("btnLoadMosaic")?.addEventListener("click", () => el("fileMosaic")?.click());
  el("fileMosaic")?.addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (file) onMosaicFile(file);
  });
  el("btnUseOrtho")?.addEventListener("click", () => useLatestSurveyMap());
  el("btnFindDemo")?.addEventListener("click", () => loadDemo());
  el("btnLoadRef")?.addEventListener("click", () => el("fileRef")?.click());
  el("fileRef")?.addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (file) onRefFile(file);
  });
  el("btnClearQuery")?.addEventListener("click", () => clearQuery());
  el("btnFindSearch")?.addEventListener("click", () => runSearch());
  const syncColorPicker = () => {
    const wrap = el("findColorPickerWrap");
    const preset = el("findColorPreset");
    if (wrap) wrap.hidden = !(preset && preset.value === "custom");
  };
  el("findColorPreset")?.addEventListener("change", syncColorPicker);
  syncColorPicker();
  bindStageDrag();
  await probeApi();
  const params = new URLSearchParams(location.search);
  if (params.has("find") && (params.get("find") === "demo" || params.get("find") === "")) {
    await loadDemo();
  }
  enableSearch();
  if (document.getElementById("viewFind") && !document.getElementById("viewFind").hidden) {
    ensureFindMap();
  }
}
