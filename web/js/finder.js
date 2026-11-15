/**
 * Find My Drone — upload a mosaic once, send a query (region or photo)
 * to the server matcher, and draw the returned boxes / GPS points.
 */
import { attachBasemap, refreshMapSize, locateOrFallback } from "./basemap.js?v=20261114dlpm1";
import { setPageTab, onPageTab } from "./tabs.js?v=20261114dlpm1";
import { apiFetch, apiBase as authApiBase, isMosaicEnabled, isAuthenticated, lockMessage, onAuthChange } from "./auth.js?v=20261114dlpm1";
import { uploadAndStart, latestJob, getJob, cancelJob, startJob, MIN_PHOTOS } from "./photomap.js?v=20261114dlpm1";
import { enqueue, putFindResults } from "./sync.js?v=20261114dlpm1";

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
  drawMode: false,
};

function el(id) {
  return document.getElementById(id);
}

function apiBase() {
  return authApiBase();
}

const FINDER_DOWN = "Something went wrong on our side, so the finder could not answer. Your files are fine. Wait a few minutes and try again; if it keeps happening, send us a note from Account Settings.";
const SIGNED_OUT = "You are not signed in. Log in at the top of the page, then try again.";
const SESSION_EXPIRED = "Your sign-in has expired. Log out and log in again, then try again.";
function signInMessage() {
  return isAuthenticated() ? SESSION_EXPIRED : SIGNED_OUT;
}
const DEFAULT_REF_STATUS = "On a phone, tap Draw box, then drag on the map. On a computer, tap Draw box or hold Shift and drag. Or upload a close-up photo of your drone or a marking on it.";

function logTechnical(err, context) {
  try {
    console.error(context || "Drone Locator", err);
  } catch {
    /* ignore */
  }
}

function friendlyError(err, fallback = FINDER_DOWN) {
  const raw = err && (err.message || String(err));
  logTechnical(err, "Drone Locator");
  if (!raw) return fallback;
  const lower = raw.toLowerCase();
  if (/not authenticated|401|^log in or create/.test(lower)) return signInMessage();
  if (/locked|403|enable mapping|enable drone locator/.test(lower)) return lockMessage().body;
  if (/four numbers|west, south/.test(lower)) {
    return "The area corners need four numbers: west, south, east, and north.";
  }
  if (/upload failed|could not encode|cannot decode|could not decode|unsupported|too large|too big/.test(lower)) {
    return "We could not read that file as a map. Use a JPG, PNG or TIFF picture of the ground under 250 MB.";
  }
  if (/match failed|search cannot start|matcher service/.test(lower)) return FINDER_DOWN;
  if (/http \d+/.test(lower) || /127\.0\.0\.1|localhost|\/drone-mapper\/api/.test(lower)) return fallback;
  if (raw.length < 160 && !/https?:\/\//i.test(raw) && !/\b\d{3}\b/.test(raw)) return raw;
  return fallback;
}

async function readApiError(res, fallback) {
  let detail = "";
  try {
    const body = await res.json();
    if (body && body.detail) detail = String(body.detail);
  } catch {}
  const technical = detail || `HTTP ${res.status}`;
  logTechnical(technical, "Drone Locator API");
  if (res.status === 403) return lockMessage().body;
  if (res.status === 401) return signInMessage();
  return friendlyError(new Error(technical), fallback || FINDER_DOWN);
}

function setBox(id, msg) {
  const box = el(id);
  if (!box) return;
  box.hidden = !msg;
  box.textContent = msg || "";
  if (msg && typeof box.scrollIntoView === "function") {
    try { box.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch { /* old browsers */ }
  }
}

/** Phones: keep the map short until a search map exists, then give it most of the screen. */
function markMapLoaded(on) {
  const app = el("findApp");
  if (!app) return;
  app.classList.toggle("has-search-map", !!on);
  if (state.map) setTimeout(() => refreshMapSize(state.map), 60);
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
    logTechnical(err, `Finder health check (${state.apiBase})`);
    setWarn(FINDER_DOWN);
  }
  return state.apiOk;
}

function parseBounds(text) {
  if (!text || !text.trim()) return null;
  const parts = text.split(/[, ]+/).map((s) => s.trim()).filter(Boolean).map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n))) {
    throw new Error("The area corners need four numbers: west, south, east, and north.");
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

export function searchBlocker(s) {
  const hasQuery = !!(s.queryFile || (s.queryRect && s.queryRect.w >= 8 && s.queryRect.h >= 8));
  if (s.apiOk === false) return "Search is off because the finder is not answering right now. Wait a few minutes, then refresh the page.";
  if (!s.mosaicId && !hasQuery) return "Search turns on after step 1 (load a search map) and step 2 (draw a box or upload a photo of your drone).";
  if (!s.mosaicId) return "Search turns on after step 1: load a search map.";
  if (!hasQuery) return "Search turns on after step 2: draw a box around your drone on the map, or upload a photo of it.";
  return "";
}

function enableSearch() {
  const reason = searchBlocker(state);
  const btn = el("btnFindSearch");
  if (btn) btn.disabled = !!reason;
  const hint = el("findSearchHint");
  if (hint) {
    hint.hidden = !reason;
    hint.textContent = reason;
  }
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
    setRefStatus(DEFAULT_REF_STATUS);
  }
  redrawOverlay();
  enableSearch();
  if (document.getElementById("viewFind") && !document.getElementById("viewFind").hidden) {
    ensureFindMap();
  }
  pmResume().catch((err) => logTechnical(err, "resume search map"));
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

function applyLoadedMosaic(body, sourceNote) {
  state.mosaicId = body.id;
  state.mosaicMeta = body;
  markMapLoaded(true);
  state.matches = [];
  const geo = body.georeferenced
    ? "location data included" + (body.bounds ? ` (${body.bounds.west.toFixed(5)}, ${body.bounds.south.toFixed(5)})–(${body.bounds.east.toFixed(5)}, ${body.bounds.north.toFixed(5)})` : "")
    : "no location data — add the area corners under More options and upload again if you want walking directions";
  setMosaicStatus(sourceNote || (
    `“${body.filename}” is on the server — ${body.width}×${body.height} px, ${geo}. ` +
    `Later searches reuse this map so the file is not sent again.`
  ));
  if (body.bounds) fillBoundsInput(body.bounds);
  setProgress("The map is on the server. Draw a box or upload a photo of your drone, then search.");
  enableSearch();
  syncFindMap();
}

async function uploadMosaic(file, bounds) {
  if (!isAuthenticated()) {
    throw new Error(SIGNED_OUT);
  }
  if (!isMosaicEnabled()) {
    throw new Error(lockMessage().body);
  }
  if (state.apiOk === false) {
    throw new Error(FINDER_DOWN);
  }
  setProgress("Uploading your search map…");
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
  if (!r.ok) {
    logTechnical(body.detail || `Upload failed (${r.status})`, "map upload");
    if (r.status === 401) throw new Error(signInMessage());
    if (r.status === 403) throw new Error(lockMessage().body);
    if (r.status >= 500 || r.status === 0) throw new Error(FINDER_DOWN);
    throw new Error(body.detail || "We could not read that file as a map. Use a JPG, PNG or TIFF picture of the ground under 250 MB.");
  }
  applyLoadedMosaic(body);
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
    setError(friendlyError(err, FINDER_DOWN));
    setProgress("Step 1 is not done: no search map is loaded yet. Fix the problem shown below, then load the map again.");
  }
  redrawOverlay();
}

async function onRefFile(file) {
  setError("");
  clearQuery({ keepStatus: true });
  state.queryFile = file;
  state.queryUrl = URL.createObjectURL(file);
  showRefPreview(state.queryUrl, state.mosaicId
    ? `“${file.name}” will be used in the next search. Tap Search in step 3.`
    : `“${file.name}” is ready. Next, load a search map in step 1, then tap Search.`);
  enableSearch();
  if (document.getElementById("viewFind") && !document.getElementById("viewFind").hidden) {
    ensureFindMap();
  }
  pmResume().catch((err) => logTechnical(err, "resume search map"));
}

async function runSearch() {
  setError("");
  if (!state.apiOk) {
    setError(FINDER_DOWN);
    return;
  }
  if (!state.mosaicId) {
    setError("Load a search map first.");
    return;
  }
  const btn = el("btnFindSearch");
  if (btn) btn.disabled = true;
  setProgress("Searching the map on the server… a large map can take a little while.");
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
    setError("Draw a box on the map or upload a photo of your drone first.");
    enableSearch();
    return;
  }
  const t0 = performance.now();
  try {
    const r = await apiFetch(`/mosaics/${state.mosaicId}/match`, { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      logTechnical(body.detail || `Match failed (${r.status})`, "search");
      throw new Error(body.detail || FINDER_DOWN);
    }
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
      setProgress(`No close matches (${dt}s). Try drawing a tighter box or using a clearer photo of your drone.`);
    } else {
      setProgress(`Found ${matches.length} match${matches.length === 1 ? "" : "es"} in ${dt}s. Boxes are drawn on the map.`);
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
    setError(friendlyError(err, FINDER_DOWN));
    setProgress("The search did not finish. The reason is shown below.");
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
      ? "Only the area you marked matched. That usually means the marking is not repeated elsewhere on this map."
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
      : `<div>No walking directions — this map has no location data.</div>`;
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

function finishRegionDrag() {
  const r = state.queryRect;
  state.drag = null;
  if (!r || r.w < 8 || r.h < 8) {
    state.queryRect = null;
    setRefStatus("Draw a larger box, or upload a photo of your drone.");
  } else {
    setRefStatus("Area marked. Tap Search to look for similar spots on the map.");
  }
  enableSearch();
  redrawOverlay();
}

function applyRegionFromPoints(x0, y0, x1, y1) {
  state.queryRect = normRect(x0, y0, x1, y1);
  if (state.queryFile) {
    state.queryFile = null;
    if (state.queryUrl) URL.revokeObjectURL(state.queryUrl);
    state.queryUrl = null;
    const preview = el("refPreview");
    if (preview) { preview.hidden = true; preview.innerHTML = ""; }
  }
  redrawOverlay();
}

function bindStageDrag() {
  const canvas = el("findCanvas");
  if (!canvas || canvas.dataset.drawBound) return;
  canvas.dataset.drawBound = "1";
  const startDrag = (ev) => {
    if (!state.mosaicMeta) return;
    if (ev.pointerType === "mouse" && ev.button !== 0) return;
    ev.preventDefault();
    try { canvas.setPointerCapture(ev.pointerId); } catch { /* */ }
    const p = eventToMosaicPx(ev);
    state.drag = { x: p.x, y: p.y, pointerId: ev.pointerId };
  };
  const moveDrag = (ev) => {
    if (!state.drag) return;
    ev.preventDefault();
    const p = eventToMosaicPx(ev);
    applyRegionFromPoints(state.drag.x, state.drag.y, p.x, p.y);
  };
  const endDrag = (ev) => {
    if (!state.drag) return;
    if (ev && state.drag.pointerId != null && ev.pointerId !== state.drag.pointerId) return;
    finishRegionDrag();
  };
  canvas.addEventListener("pointerdown", startDrag);
  canvas.addEventListener("pointermove", moveDrag);
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  // Mouse fallback for older browsers that still fire mouse events only.
  canvas.addEventListener("mousedown", (ev) => {
    if (window.PointerEvent) return;
    if (!state.mosaicMeta) return;
    ev.preventDefault();
    const p = eventToMosaicPx(ev);
    state.drag = { x: p.x, y: p.y };
  });
  window.addEventListener("mousemove", (ev) => {
    if (window.PointerEvent || !state.drag) return;
    const p = eventToMosaicPx(ev);
    applyRegionFromPoints(state.drag.x, state.drag.y, p.x, p.y);
  });
  window.addEventListener("mouseup", () => {
    if (window.PointerEvent || !state.drag) return;
    finishRegionDrag();
  });
}

function setDrawMode(on) {
  state.drawMode = !!on;
  const btn = el("btnDrawQuery");
  if (btn) {
    btn.setAttribute("aria-pressed", state.drawMode ? "true" : "false");
  }
  const host = el("findMap");
  const wrap = host && host.closest(".find-map-wrap");
  if (host) host.classList.toggle("is-draw-mode", state.drawMode);
  if (wrap) wrap.classList.toggle("is-draw-mode", state.drawMode);
  const map = state.map;
  if (!map) return;
  if (state.drawMode) {
    map.dragging.disable();
    if (map.boxZoom) map.boxZoom.disable();
    if (map.tap && map.tap.disable) map.tap.disable();
    if (map.touchZoom) map.touchZoom.disable();
    if (map.doubleClickZoom) map.doubleClickZoom.disable();
  } else {
    map.dragging.enable();
    if (map.boxZoom) map.boxZoom.enable();
    if (map.tap && map.tap.enable) map.tap.enable();
    if (map.touchZoom) map.touchZoom.enable();
    if (map.doubleClickZoom) map.doubleClickZoom.enable();
  }
}

function bindMapDraw(map) {
  const host = el("findMap");
  if (!host || host.dataset.drawBound) return;
  host.dataset.drawBound = "1";
  let startLatLng = null;

  const eventLatLng = (ev) => {
    if (!state.map) return null;
    return state.map.mouseEventToLatLng(ev);
  };

  const paintBox = (a, b) => {
    if (!state.queryLayer || !a || !b) return;
    const sw = [Math.min(a.lat, b.lat), Math.min(a.lng, b.lng)];
    const ne = [Math.max(a.lat, b.lat), Math.max(a.lng, b.lng)];
    state.queryLayer.clearLayers();
    L.rectangle([sw, ne], { color: "#e05a5a", weight: 2, fillOpacity: 0.08 }).addTo(state.queryLayer);
  };

  const commitBox = (a, b) => {
    startLatLng = null;
    const p0 = latLngToMosaicPx(a);
    const p1 = latLngToMosaicPx(b);
    if (!p0 || !p1) return;
    const x = Math.min(p0.x, p1.x), y = Math.min(p0.y, p1.y);
    const w = Math.abs(p1.x - p0.x), h = Math.abs(p1.y - p0.y);
    if (w < 4 || h < 4) return;
    state.queryRect = { x, y, w, h };
    if (state.queryFile) {
      state.queryFile = null;
      if (state.queryUrl) URL.revokeObjectURL(state.queryUrl);
      state.queryUrl = null;
      const preview = el("refPreview");
      if (preview) { preview.hidden = true; preview.innerHTML = ""; }
    }
    enableSearch();
    redrawOverlay();
    setRefStatus("Area marked. Tap Search to look for similar spots on the map.");
    setDrawMode(false);
  };

  host.addEventListener("pointerdown", (ev) => {
    const isMouse = ev.pointerType === "mouse" || ev.pointerType === "";
    const allow = state.drawMode || (isMouse && ev.shiftKey && ev.button === 0);
    if (!allow) return;
    if (!mosaicBounds()) return;
    if (ev.target && ev.target.closest && ev.target.closest(".leaflet-control")) return;
    ev.preventDefault();
    ev.stopPropagation();
    try { host.setPointerCapture(ev.pointerId); } catch { /* */ }
    startLatLng = eventLatLng(ev);
    if (state.drawMode && state.map) state.map.dragging.disable();
  });
  host.addEventListener("pointermove", (ev) => {
    if (!startLatLng) return;
    ev.preventDefault();
    paintBox(startLatLng, eventLatLng(ev));
  });
  host.addEventListener("pointerup", (ev) => {
    if (!startLatLng) return;
    ev.preventDefault();
    commitBox(startLatLng, eventLatLng(ev));
  });
  host.addEventListener("pointercancel", () => { startLatLng = null; });

  // Keep the original Leaflet mouse path as a backup when PointerEvent is missing.
  let dragStart = null;
  map.on("mousedown", (ev) => {
    if (window.PointerEvent) return;
    if (!mosaicBounds() || ev.originalEvent.shiftKey || ev.originalEvent.button !== 0) return;
    dragStart = ev.latlng;
  });
  map.on("mousemove", (ev) => {
    if (window.PointerEvent || !dragStart) return;
    paintBox(dragStart, ev.latlng);
  });
  map.on("mouseup", (ev) => {
    if (window.PointerEvent || !dragStart) return;
    const a = dragStart;
    dragStart = null;
    commitBox(a, ev.latlng);
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
      setError(FINDER_DOWN);
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
      "Demo is ready: a yellow marking is boxed on the map. A second, slightly rotated copy sits elsewhere so you can try Search."
    );
    enableSearch();
    redrawOverlay();
    await runSearch();
  } catch (err) {
    setError(friendlyError(err, FINDER_DOWN));
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
  state.map = map;
  bindMapDraw(map);
  locateOrFallback(map, {
    keepView: () => !!state.orthoLayer,
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


/* ---------- Make a map from search-flight photos ---------- */

const pm = { job: null, timer: null, cancelled: false, busy: false, loadedJob: null, resumed: false };
const PM_STEPS = ["upload", "match", "stitch", "load"];

function pmPanel(show) {
  const panel = el("photoMapPanel");
  if (!panel) return;
  const wasHidden = panel.hidden;
  panel.hidden = !show;
  if (show && wasHidden && typeof panel.scrollIntoView === "function") {
    try { panel.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch { /* old browsers */ }
  }
}

/** states: {upload:"done"|"active"|"failed"|"", ...} */
function pmRender(states, details, pct, text, kind) {
  pmPanel(true);
  const panel = el("photoMapPanel");
  for (const step of PM_STEPS) {
    const li = panel?.querySelector(`[data-pm-step="${step}"]`);
    if (!li) continue;
    li.className = states[step] ? `is-${states[step]}` : "";
    const d = li.querySelector("[data-pm-detail]");
    if (d) d.textContent = states[step] === "done" ? "Success" : (details[step] || "");
  }
  const fill = el("photoMapFill");
  if (fill) {
    fill.style.width = `${Math.max(2, Math.min(100, pct || 0))}%`;
    fill.className = "pm-fill" + (kind === "success" ? " is-success" : kind === "error" ? " is-error" : "");
  }
  const st = el("photoMapStatus");
  if (st) {
    st.textContent = text || "";
    st.className = kind === "success" ? "okbox success-box" : kind === "error" ? "errorbox" : "note";
  }
  if (panel) panel.classList.toggle("is-success", kind === "success");
  const btn = el("btnPhotoMap");
  if (btn) {
    btn.classList.toggle("is-success", kind === "success");
    btn.textContent = kind === "success" ? "\u2713 Map made \u2014 make another" : "Make a map from my flight photos";
  }
}

function pmButtons({ retry = false, cancel = false } = {}) {
  const r = el("btnPhotoMapRetry");
  const c = el("btnPhotoMapCancel");
  if (r) r.hidden = !retry;
  if (c) c.hidden = !cancel;
  const make = el("btnPhotoMap");
  if (make) make.disabled = pm.busy;
}

function minutes(sec) {
  const m = Math.floor((sec || 0) / 60);
  return m < 1 ? "under a minute" : m === 1 ? "1 minute" : `${m} minutes`;
}

function pmRenderJob(job) {
  const s = job.status;
  const stage = job.stage;
  const elapsed = job.elapsed_s ? ` (${minutes(job.elapsed_s)} so far)` : "";
  const photos = `${job.photo_count} photo${job.photo_count === 1 ? "" : "s"}`;
  if (s === "collecting" || s === "submitting") {
    pmRender({ upload: "active" }, { upload: "sending to the map builder\u2026" }, job.progress_pct, `Sending ${photos} to the map builder\u2026`);
  } else if (s === "queued") {
    pmRender({ upload: "done", match: "active" }, { match: "waiting in line" }, job.progress_pct,
      `Your ${photos} are on our server, waiting for the map builder. Another map is being made first.${elapsed} You can leave this page open, or come back later \u2014 the map will be here.`);
  } else if ((s === "processing" && stage === "matching_photos")) {
    pmRender({ upload: "done", match: "active" }, { match: `${job.progress_pct}%` }, job.progress_pct,
      `Matching ${photos} to each other${elapsed}. A map usually takes 5 to 20 minutes. You can leave this page open, or come back later.`);
  } else if (s === "processing") {
    pmRender({ upload: "done", match: "done", stitch: "active" }, { stitch: `${job.progress_pct}%` }, job.progress_pct,
      `Stitching the map${elapsed}. Almost there.`);
  } else if (s === "finishing") {
    pmRender({ upload: "done", match: "done", stitch: "done", load: "active" }, { load: "loading\u2026" }, 98, "Loading your map\u2026");
  } else if (s === "done") {
    pmRender({ upload: "done", match: "done", stitch: "done", load: "active" }, { load: "loading\u2026" }, 99, "Loading your map\u2026");
  } else if (s === "failed") {
    const failedStep = !job.started_at ? "upload" : (stage === "making_map" ? "stitch" : (stage === "finishing" ? "load" : "match"));
    const states = {};
    for (const step of PM_STEPS) {
      if (step === failedStep) { states[step] = "failed"; break; }
      states[step] = "done";
    }
    const extra = job.can_retry
      ? " Your photos are still on our server, so Try again will not upload them again."
      : " Tap Make a map from my flight photos to start over.";
    pmRender(states, { [failedStep]: "failed" }, job.progress_pct, `${job.error || "The map could not be made."}${extra}`, "error");
  } else if (s === "cancelled") {
    pmPanel(false);
  }
  pmButtons({
    retry: s === "failed" && !!job.can_retry,
    cancel: ["collecting", "submitting", "queued", "processing"].includes(s) || (s === "failed" && !!job.can_retry),
  });
}

function pmStopPolling() {
  if (pm.timer) clearTimeout(pm.timer);
  pm.timer = null;
}

function pmPoll(delay = 4000) {
  pmStopPolling();
  pm.timer = setTimeout(async () => {
    if (!pm.job) return;
    try {
      const job = await getJob(pm.job.id);
      pm.job = job;
      await pmHandleJob(job);
    } catch (err) {
      logTechnical(err, "search map status");
      // Network hiccup: keep the last state and try again a bit later.
      pmPoll(10000);
    }
  }, delay);
}

async function pmHandleJob(job, { fromResume = false } = {}) {
  pmRenderJob(job);
  if (job.status === "done" && job.mosaic_id) {
    pm.busy = false;
    pmStopPolling();
    if (pm.loadedJob === job.id && state.mosaicId === job.mosaic_id) {
      pmShowSuccess(job);
      return;
    }
    await pmLoadResult(job, fromResume);
    return;
  }
  if (["collecting", "submitting", "queued", "processing", "finishing"].includes(job.status)) {
    if (job.status === "collecting" && fromResume) {
      // An upload from another visit never finished; it cannot be resumed from here.
      pmPanel(false);
      return;
    }
    pm.busy = true;
    pmButtons({ cancel: true });
    pmPoll();
    return;
  }
  pm.busy = false;
  pmButtons({ retry: job.status === "failed" && !!job.can_retry, cancel: job.status === "failed" && !!job.can_retry });
}

function pmShowSuccess(job) {
  const took = job.elapsed_s ? ` in ${minutes(job.elapsed_s)}` : "";
  pmRender({ upload: "done", match: "done", stitch: "done", load: "done" }, {}, 100,
    `\u2713 Success \u2014 your search map is loaded (made from ${job.photo_count} photos${took}). Your flight photos were deleted from our server. Next: step 2, show us your drone.`,
    "success");
  pmButtons({});
}

async function pmLoadResult(job, quiet) {
  try {
    const r = await apiFetch(`/mosaics/${job.mosaic_id}`);
    if (r.status === 404) {
      if (quiet) { pmPanel(false); return; }
      throw new Error("Your map from this flight has expired (maps are kept for 12 hours). Make a new one from your photos.");
    }
    if (!r.ok) throw new Error(await readApiError(r, FINDER_DOWN));
    const body = await r.json();
    setPageTab("find");
    clearQuery({ keepStatus: true });
    applyLoadedMosaic(body, `Your search map is loaded: made from ${job.photo_count} flight photos, ${body.width}\u00d7${body.height} px, with GPS so matches get walking directions. It stays on the server for 12 hours.`);
    try {
      await fetchServerPreview();
    } catch (err) {
      logTechnical(err, "search map preview");
    }
    syncFindMap();
    pm.loadedJob = job.id;
    pmShowSuccess(job);
    setProgress("Your map is loaded. Next, draw a box around your drone on the map or upload a photo of it, then tap Search.");
  } catch (err) {
    pmRender({ upload: "done", match: "done", stitch: "done", load: "failed" }, { load: "failed" }, 99,
      friendlyError(err, FINDER_DOWN), "error");
    pmButtons({});
  }
}

async function onPhotoMapFiles(files) {
  setError("");
  setWarn("");
  if (!isAuthenticated()) { setError(SIGNED_OUT); return; }
  if (!isMosaicEnabled()) { setError(lockMessage().body); return; }
  if (pm.busy) return;
  pm.busy = true;
  pm.cancelled = false;
  pmStopPolling();
  pmButtons({ cancel: true });
  const total = files.length;
  pmRender({ upload: "active" }, { upload: `0 of ${total}` }, 1, `Getting ${total} photos ready\u2026 Keep this page open until the upload finishes.`);
  try {
    const job = await uploadAndStart(files, (u) => {
      pm.job = u.job;
      const pct = Math.round(1 + (4 * u.done) / Math.max(1, u.total));
      const skipped = (u.job && u.job.skipped && u.job.skipped.length) ? ` ${u.job.skipped.length} skipped (no GPS or not a drone photo).` : "";
      pmRender({ upload: "active" }, { upload: `${u.done} of ${u.total}` }, pct,
        `Uploading photos: ${u.done} of ${u.total}.${skipped} Keep this page open until the upload finishes.`);
    }, () => pm.cancelled);
    pm.job = job;
    await pmHandleJob(job);
  } catch (err) {
    pm.busy = false;
    if (pm.cancelled) { pmPanel(false); pmButtons({}); return; }
    const plain = !err.status ? !/HTTP \d|fetch|network/i.test(err.message || "") : [400, 409, 413, 507].includes(err.status);
    const msg = plain && err.message ? err.message : friendlyError(err, FINDER_DOWN);
    const job = pm.job;
    if (job && job.id) {
      try {
        const fresh = await getJob(job.id);
        if (fresh.status === "collecting" && fresh.gps_count < MIN_PHOTOS) {
          await cancelJob(job.id).catch(() => {});
        }
      } catch { /* ignore */ }
    }
    pmRender({ upload: "failed" }, { upload: "failed" }, 2, msg, "error");
    pmButtons({});
  }
}

async function onPhotoMapRetry() {
  if (!pm.job || pm.busy) return;
  pm.busy = true;
  pmButtons({ cancel: true });
  try {
    const job = await startJob(pm.job.id);
    pm.job = job;
    await pmHandleJob(job);
  } catch (err) {
    pm.busy = false;
    pmRender({ upload: "done", match: "failed" }, {}, pm.job.progress_pct || 5, friendlyError(err, FINDER_DOWN), "error");
    pmButtons({ retry: true, cancel: true });
  }
}

async function onPhotoMapCancel() {
  pm.cancelled = true;
  pmStopPolling();
  if (pm.job && pm.job.id) {
    try { await cancelJob(pm.job.id); } catch (err) { logTechnical(err, "cancel search map"); }
  }
  pm.busy = false;
  pm.job = null;
  pmPanel(false);
  pmButtons({});
  setProgress("Cancelled. Your photos were removed from our server.");
}

async function pmResume() {
  if (pm.resumed || !isMosaicEnabled()) return;
  pm.resumed = true;
  const job = await latestJob();
  if (!job) return;
  pm.job = job;
  if (job.status === "done") {
    // Reload a map finished while the page was closed (it stays 12 hours).
    if (!state.mosaicId && job.mosaic_id) await pmHandleJob(job, { fromResume: true });
    return;
  }
  if (job.status === "cancelled") return;
  if (job.status === "failed" && !job.can_retry) return;
  await pmHandleJob(job, { fromResume: true });
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
  el("btnPhotoMap")?.addEventListener("click", () => el("filePhotoMap")?.click());
  el("filePhotoMap")?.addEventListener("change", (ev) => {
    const files = ev.target.files ? Array.from(ev.target.files) : [];
    ev.target.value = "";
    if (files.length) onPhotoMapFiles(files);
  });
  el("btnPhotoMapRetry")?.addEventListener("click", () => onPhotoMapRetry());
  el("btnPhotoMapCancel")?.addEventListener("click", () => onPhotoMapCancel());
  onAuthChange(() => { pmResume().catch((err) => logTechnical(err, "resume search map")); });
  el("fileMosaic")?.addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (file) onMosaicFile(file);
  });
  el("btnDrawQuery")?.addEventListener("click", () => setDrawMode(!state.drawMode));
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
  pmResume().catch((err) => logTechnical(err, "resume search map"));
}
