import { generateGrid, defaultSettings } from "./grid.js?v=20261114dlpm2";
import { FAA_MAX_ALTITUDE_M, CAMERAS, getCamera, MAVIC_AIR_2 } from "./camera.js?v=20261114dlpm2";
import { PRESETS, presetSettings, presetStats } from "./presets.js?v=20261114dlpm2";

const DRONE_KEY = "piletally_drone_model";

function selectedCamera() {
  const id = document.getElementById("droneModel")?.value || localStorage.getItem(DRONE_KEY) || "mavic-air-2";
  return getCamera(id);
}

function syncDroneSelect() {
  const sel = document.getElementById("droneModel");
  if (!sel) return selectedCamera();
  const saved = localStorage.getItem(DRONE_KEY);
  if (saved && CAMERAS[saved] && !sel.dataset.ready) {
    sel.value = saved;
    sel.dataset.ready = "1";
  }
  const cam = getCamera(sel.value);
  try {
    localStorage.setItem(DRONE_KEY, cam.id);
  } catch {
    /* private mode */
  }
  const note = document.getElementById("simpleModeNote");
  if (note) {
    note.textContent =
      cam.id === "phantom-4-pro"
        ? "Simple greys out the sliders and applies a preset built for the Phantom 4 Pro (1\" 20 MP, 8.8 mm, mechanical shutter). Switch to Advanced to edit every value yourself. Export a Litchi CSV to fly this plan."
        : "Simple greys out the sliders and applies a preset built for the Mavic Air 2. Switch to Advanced to edit every value yourself.";
  }
  const exportNote = document.getElementById("exportHint");
  if (exportNote) {
    exportNote.textContent =
      cam.id === "phantom-4-pro"
        ? "Load the JSON in the Android flight app, or save to my list while signed in and it will show up in the app for the same account. KML opens in Google Earth. Litchi CSV is the usual way to fly a Phantom 4 Pro plan if you do not use the Drone Mapper app."
        : "Load the JSON in the Android flight app, or save to my list while signed in and it will show up in the app for the same account. KML opens in Google Earth. Litchi CSV is a bonus — Air 2 has no DJI waypoint API, and Litchi also flies it with virtual sticks.";
  }
  return cam;
}

import {
  altitudeFtFromM,
  formatAltitudeFt,
  formatArea,
  formatDistance,
  formatGsd,
  formatSpeedMph,
  ftToM,
  mphToMps,
  mpsToMph,
} from "./units.js?v=20261114dlpm2";
import { initTutorial } from "./tutorial.js?v=20261114dlpm2";
import { attachBasemap, refreshMapSize } from "./basemap.js?v=20261114dlpm2";
import { evaluatePhotoCount, isStockpileMission } from "./photolimits.js?v=20261114dlpm2";
import { initPageTabs, setPageTab, onPageTab } from "./tabs.js?v=20261114dlpm2";
import { applyProductChrome, isLocator } from "./product.js?v=20261114dlpm2";
import {
  buildMission,
  toMissionJson,
  toKml,
  toLitchiCsv,
  parseMissionJson,
  planFromMission,
  suggestedFilename,
} from "./export.js?v=20261114dlpm2";
import { initSavedMissions } from "./saved-missions.js?v=20261114dlpm2";
import { initAuthChrome, isAuthenticated, onAuthChange, currentUser } from "./auth.js?v=20261114dlpm2";
import { enqueue, getSettings, putSettings, readCache, writeCache } from "./sync.js?v=20261114dlpm2";

// Public University of Illinois South Farms — demo only, not a real mission.
const DEMO_POLYGON = [
  { lat: 40.08470, lon: -88.22640 },
  { lat: 40.08470, lon: -88.22390 },
  { lat: 40.08310, lon: -88.22390 },
  { lat: 40.08310, lon: -88.22640 },
];

const state = {
  drawing: false,
  vertices: [],
  markers: [],
  plan: null,
  mode: "simple", // "simple" | "advanced"
  preset: "stockpile",
};

const map = L.map("map", { zoomControl: true, worldCopyJump: true, maxZoom: 19 });
attachBasemap(map, { locate: true });

const fieldLayer = L.layerGroup().addTo(map);
const planLayer = L.layerGroup().addTo(map);

const el = (id) => document.getElementById(id);

function formatPresetMeta(id) {
  const s = presetStats(PRESETS[id]);
  return (
    `${formatAltitudeFt(s.altitudeM)} AGL · ${formatGsd(s.gsdCmPerPx)} · ` +
    `${Math.round(s.frontOverlap * 100)}/${Math.round(s.sideOverlap * 100)} overlap · ` +
    `${formatSpeedMph(s.speedMps)} · heading ${s.gridAngleDeg}°`
  );
}

function fillPresetCards() {
  const pileMeta = el("presetStockpileMeta");
  if (pileMeta) pileMeta.textContent = formatPresetMeta("stockpile");
  const findMeta = el("presetFindDroneMeta");
  if (findMeta) findMeta.textContent = formatPresetMeta("findDrone");
}

function applyPresetToForm(id) {
  const preset = PRESETS[id];
  if (!preset) return;
  const s = presetSettings(preset);
  state.preset = id;
  el("altitude").value = String(altitudeFtFromM(s.altitudeM));
  el("frontOverlap").value = String(Math.round(s.frontOverlap * 100));
  el("sideOverlap").value = String(Math.round(s.sideOverlap * 100));
  el("speed").value = String(Math.round(mpsToMph(s.speedMps)));
  el("gridAngle").value = String(s.gridAngleDeg);
  el("crosshatch").checked = !!s.crosshatch;
}

function setMode(mode, opts = {}) {
  state.mode = mode === "advanced" ? "advanced" : "simple";
  const simple = state.mode === "simple";
  el("btnModeSimple").setAttribute("aria-pressed", simple ? "true" : "false");
  el("btnModeAdvanced").setAttribute("aria-pressed", simple ? "false" : "true");
  el("simplePresets").hidden = !simple;
  el("flightControls").classList.toggle("is-locked", simple);
  const controls = el("flightControls").querySelectorAll("input, select, textarea, button");
  controls.forEach((node) => {
    node.disabled = simple;
  });
  if (simple) applyPresetToForm(state.preset);
  if (!opts.skipRebuild) rebuildPlan();
}

function settingsFromForm() {
  // Simple mode uses the preset's exact SI values so 165 ft / 130 ft cards
  // stay at 50 m / 40 m internally instead of the slider's rounded feet.
  if (state.mode === "simple" && PRESETS[state.preset]) {
    const cam = selectedCamera();
    return defaultSettings({
      name: el("missionName").value.trim() || "Untitled survey",
      ...presetSettings(PRESETS[state.preset], cam),
      preset: state.preset,
      kind: state.preset === "stockpile" ? "stockpile_counting" : state.preset,
      camera: cam,
    });
  }
  const cam = selectedCamera();
  return defaultSettings({
    name: el("missionName").value.trim() || "Untitled survey",
    altitudeM: ftToM(Number(el("altitude").value)),
    frontOverlap: Number(el("frontOverlap").value) / 100,
    sideOverlap: Number(el("sideOverlap").value) / 100,
    speedMps: mphToMps(Number(el("speed").value)),
    gridAngleDeg: Number(el("gridAngle").value),
    crosshatch: el("crosshatch").checked,
    camera: cam,
  });
}

function fmt(n, digits = 1, unit = "") {
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}${unit}`;
}

function fmtTime(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m} min ${String(s).padStart(2, "0")}s`;
}

function updateLabels() {
  el("altVal").textContent = `${el("altitude").value} ft`;
  el("frontVal").textContent = `${el("frontOverlap").value}%`;
  el("sideVal").textContent = `${el("sideOverlap").value}%`;
  el("speedVal").textContent = `${Number(el("speed").value).toFixed(0)} mph`;
  el("angleVal").textContent = `${el("gridAngle").value}°`;
}

function renderField() {
  fieldLayer.clearLayers();
  state.markers = [];
  if (!state.vertices.length) return;
  if (state.vertices.length >= 3 && !state.drawing) {
    L.polygon(
      state.vertices.map((p) => [p.lat, p.lon]),
      { color: "#4cc9f0", weight: 2, fillColor: "#4cc9f0", fillOpacity: 0.15 }
    ).addTo(fieldLayer);
  } else {
    L.polyline(
      state.vertices.map((p) => [p.lat, p.lon]),
      { color: "#4cc9f0", weight: 2, dashArray: "6 4" }
    ).addTo(fieldLayer);
  }
  state.vertices.forEach((p, i) => {
    const marker = L.marker([p.lat, p.lon], {
      draggable: !state.drawing,
      icon: L.divIcon({
        className: "vertex-icon",
        iconSize: [12, 12],
        iconAnchor: [6, 6],
      }),
      title: `Corner ${i + 1}`,
    }).addTo(fieldLayer);
    marker.on("drag", (ev) => {
      const ll = ev.target.getLatLng();
      state.vertices[i] = { lat: ll.lat, lon: ll.lng };
    });
    marker.on("dragend", () => {
      renderField();
      rebuildPlan();
    });
    state.markers.push(marker);
  });
}

function renderPlan(plan) {
  planLayer.clearLayers();
  if (!plan || !plan.ok) return;
  const pts = plan.waypoints.map((w) => [w.lat, w.lon]);
  if (pts.length >= 2) {
    L.polyline(pts, { color: "#ff9f1c", weight: 3, opacity: 0.95 }).addTo(planLayer);
    const start = plan.waypoints[0];
    L.circleMarker([start.lat, start.lon], {
      radius: 7,
      color: "#fff",
      weight: 2,
      fillColor: "#ff9f1c",
      fillOpacity: 1,
    })
      .bindTooltip("Start")
      .addTo(planLayer);
  }
  for (const p of plan.photos) {
    L.circleMarker([p.lat, p.lon], {
      radius: 3.5,
      color: "#5ee0b1",
      weight: 1,
      fillColor: "#5ee0b1",
      fillOpacity: 0.95,
    }).addTo(planLayer);
  }
}

function rebuildPlan() {
  updateLabels();
  const settings = settingsFromForm();
  const plan = generateGrid(state.vertices, settings, selectedCamera());
  state.plan = plan;
  showPlan(plan);
}

function showPlan(plan) {
  const warn = el("planWarn");
  const err = el("planError");
  warn.hidden = true;
  err.hidden = true;
  renderPlan(plan);

  const s = plan.stats || {};
  const settings = plan.settings || {};
  el("statGsd").textContent = s.gsdCmPerPx ? formatGsd(s.gsdCmPerPx) : "—";
  el("statSpacing").textContent = s.lineSpacingM ? formatDistance(s.lineSpacingM) : "—";
  el("statTrigger").textContent = s.triggerDistanceM ? formatDistance(s.triggerDistanceM) : "—";
  el("statPhotos").textContent = plan.ok ? String(s.photoCount) : "—";
  const batCount = s.batteryCount || (plan.segments ? plan.segments.length : 0);
  if (plan.ok) {
    const packs = batCount === 1 ? "1 battery" : `${batCount} batteries`;
    el("statTime").textContent = `${fmtTime(s.estimatedFlightTimeSec)} · ${packs}`;
  } else {
    el("statTime").textContent = "—";
  }
  el("statPath").textContent = plan.ok
    ? `${formatDistance(s.pathLengthM)} · ${formatArea(s.areaM2)}`
    : "—";

  const timeStat = el("statTime").parentElement;
  timeStat.classList.remove("warn", "bad", "ok");
  const split = el("batterySplit");
  split.hidden = true;
  if (plan.ok) {
    if (s.segmentOverBudget) timeStat.classList.add("bad");
    else if (batCount > 1) timeStat.classList.add("warn");
    else timeStat.classList.add("ok");
    const usable = s.usableMissionMin ?? 19.6;
    const parts = (plan.segments || []).map((seg, i) => {
      const lines = seg.lineStart === seg.lineEnd
        ? `line ${seg.lineStart + 1}`
        : `lines ${seg.lineStart + 1}–${seg.lineEnd + 1}`;
      const flag = seg.overBudget ? " — over one pack" : "";
      return `Battery ${i + 1}: ${lines} (${fmtTime(seg.estimatedFlightTimeSec)})${flag}`;
    });
    split.hidden = false;
    split.textContent =
      parts.join(" · ") +
      `. Each pack has ${usable.toFixed(1)} min of climb, survey, and RTH after a 30% reserve (28 min realistic empty from the Air 2's 34 min / 3500 mAh hover rating). Auto-RTH at 30%.`;
  }

  const warnings = [];
  if (settings.altitudeM > FAA_MAX_ALTITUDE_M || s.aboveFaaLimit) {
    warnings.push("Altitude is capped at 400 ft (FAA Part 107).");
  }
  if (s.speedCapped) {
    warnings.push(
      `Speed reduced to ${formatSpeedMph(s.speedMps, 1)} so the 2 s camera interval can keep up.`
    );
  }
  if (plan.ok && batCount > 1) {
    warnings.push(
      `This survey needs ${batCount} batteries. The app will return to home at 30% and you can resume the next segment on a fresh pack.`
    );
  }
  if (plan.ok && s.segmentOverBudget) {
    warnings.push(
      "At least one battery segment is longer than the 19.6 min usable budget. Raise altitude, fly a smaller field, or accept a long last line."
    );
  }
  const stockpileLike = state.preset === "stockpile" || isStockpileMission({ settings: settings, kind: settings.kind });
  if (plan.ok && stockpileLike) {
    const cap = Number(currentUser()?.photo_cap) || 500;
    const limits = evaluatePhotoCount(s.photoCount, cap);
    if (limits.warning) warnings.push(limits.warning);
    if (!limits.ok) {
      err.hidden = false;
      err.textContent = limits.warning;
    }
  }
  if (warnings.length) {
    warn.hidden = false;
    warn.textContent = warnings.join(" ");
  }
  if (!plan.ok && state.vertices.length >= 3) {
    err.hidden = false;
    err.textContent = plan.error || "Could not build a plan.";
  }

  let limitsBlock = false;
  if (plan && plan.ok && (state.preset === "stockpile" || isStockpileMission({ settings, kind: settings.kind }))) {
    const cap = Number(currentUser()?.photo_cap) || 500;
    limitsBlock = !evaluatePhotoCount(s.photoCount, cap).ok;
  }
  const ready = !!(plan && plan.ok) && !limitsBlock;
  el("btnJson").disabled = !ready;
  el("btnKml").disabled = !ready;
  el("btnCsv").disabled = !ready;
  if (el("btnSaveLocal")) el("btnSaveLocal").disabled = !ready;
}

function startDrawing() {
  state.drawing = true;
  state.vertices = [];
  state.plan = null;
  planLayer.clearLayers();
  renderField();
  rebuildPlan();
  el("btnClose").disabled = true;
  el("btnUndo").disabled = true;
  el("btnDraw").textContent = "Drawing…";
  el("drawHint").textContent =
    "Click corners on the map. Click Close shape (or the first corner) when you are done.";
  map.getContainer().style.cursor = "crosshair";
}

function finishDrawing() {
  if (state.vertices.length < 3) return;
  state.drawing = false;
  el("btnDraw").textContent = "Redraw field";
  el("btnClose").disabled = true;
  el("btnUndo").disabled = true;
  map.getContainer().style.cursor = "";
  el("drawHint").textContent = "Drag corners to edit the field. Export when the plan looks right.";
  renderField();
  rebuildPlan();
}

function clearAll() {
  state.drawing = false;
  state.vertices = [];
  state.plan = null;
  fieldLayer.clearLayers();
  planLayer.clearLayers();
  el("btnDraw").textContent = "Draw field";
  el("btnClose").disabled = true;
  el("btnUndo").disabled = true;
  map.getContainer().style.cursor = "";
  rebuildPlan();
}

map.on("click", (ev) => {
  if (!state.drawing) return;
  const pt = { lat: ev.latlng.lat, lon: ev.latlng.lng };
  if (state.vertices.length >= 3) {
    const first = state.vertices[0];
    const firstPx = map.latLngToLayerPoint([first.lat, first.lon]);
    const clickPx = map.latLngToLayerPoint(ev.latlng);
    if (firstPx.distanceTo(clickPx) < 14) {
      finishDrawing();
      return;
    }
  }
  state.vertices.push(pt);
  el("btnClose").disabled = state.vertices.length < 3;
  el("btnUndo").disabled = state.vertices.length === 0;
  renderField();
});

el("btnDraw").addEventListener("click", startDrawing);
el("btnClose").addEventListener("click", finishDrawing);
el("btnUndo").addEventListener("click", () => {
  if (!state.drawing || !state.vertices.length) return;
  state.vertices.pop();
  el("btnClose").disabled = state.vertices.length < 3;
  el("btnUndo").disabled = state.vertices.length === 0;
  renderField();
});
el("btnClear").addEventListener("click", clearAll);

el("btnDemo").addEventListener("click", loadDemo);
function loadDemo() {
  state.drawing = false;
  state.vertices = DEMO_POLYGON.map((p) => ({ ...p }));
  el("missionName").value = "Demo field";
  el("btnDraw").textContent = "Redraw field";
  map.getContainer().style.cursor = "";
  renderField();
  rebuildPlan();
  map.fitBounds(
    state.vertices.map((p) => [p.lat, p.lon]),
    { padding: [40, 40], maxZoom: 18 }
  );
}

function loadMission(mission) {
  const parsed = typeof mission === "string" ? parseMissionJson(mission) : mission;
  el("missionName").value = parsed.name || "Imported survey";
  const s = parsed.settings || {};
  if (s.altitudeM) el("altitude").value = altitudeFtFromM(s.altitudeM);
  if (s.frontOverlap) el("frontOverlap").value = Math.round(s.frontOverlap * 100);
  if (s.sideOverlap) el("sideOverlap").value = Math.round(s.sideOverlap * 100);
  if (s.speedMps) el("speed").value = Math.round(mpsToMph(s.speedMps));
  if (s.gridAngleDeg != null) el("gridAngle").value = s.gridAngleDeg;
  el("crosshatch").checked = !!s.crosshatch;
  // Imported missions keep their own numbers, so leave Simple mode.
  // Skip rebuild so rounded slider values do not rewrite a saved 10 m grid.
  setMode("advanced", { skipRebuild: true });
  state.drawing = false;
  state.vertices = (parsed.polygon || []).map((p) => ({ lat: p.lat, lon: p.lon }));
  el("btnDraw").textContent = "Redraw field";
  const plan = planFromMission(parsed);
  state.plan = plan;
  updateLabels();
  renderField();
  showPlan(plan);
  const fit = () => {
    if (state.vertices.length) {
      map.invalidateSize();
      map.fitBounds(state.vertices.map((p) => [p.lat, p.lon]), { padding: [40, 40] });
    }
  };
  fit();
  setTimeout(fit, 80);
}

el("btnImport").addEventListener("click", () => el("fileImport").click());
el("fileImport").addEventListener("change", async (ev) => {
  const file = ev.target.files?.[0];
  ev.target.value = "";
  if (!file) return;
  try {
    const text = await file.text();
    loadMission(parseMissionJson(text));
  } catch (e) {
    el("planError").hidden = false;
    el("planError").textContent = e.message || "Could not import that file.";
  }
});

for (const id of ["altitude", "frontOverlap", "sideOverlap", "speed", "gridAngle"]) {
  el(id).addEventListener("input", rebuildPlan);
}
el("crosshatch").addEventListener("change", rebuildPlan);
el("missionName").addEventListener("input", () => {
  if (state.plan) state.plan.settings.name = el("missionName").value;
});

function download(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

el("btnJson").addEventListener("click", () => {
  if (!state.plan?.ok) return;
  download(suggestedFilename(state.plan, "json"), toMissionJson(state.plan), "application/json");
});
el("btnKml").addEventListener("click", () => {
  if (!state.plan?.ok) return;
  download(suggestedFilename(state.plan, "kml"), toKml(state.plan), "application/vnd.google-earth.kml+xml");
});
el("btnCsv").addEventListener("click", () => {
  if (!state.plan?.ok) return;
  download(suggestedFilename(state.plan, "csv"), toLitchiCsv(state.plan), "text/csv");
});

async function searchPlace(query) {
  const q = (query || "").trim();
  if (!q) return;
  const coord = q.match(/^\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*$/);
  if (coord) {
    map.setView([Number(coord[1]), Number(coord[3])], 17);
    return;
  }
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`;
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  const data = await res.json();
  if (data?.[0]) {
    map.setView([Number(data[0].lat), Number(data[0].lon)], 16);
  } else {
    el("planError").hidden = false;
    el("planError").textContent = "No place found for that search.";
  }
}

el("btnSearch").addEventListener("click", () => searchPlace(el("search").value));
el("search").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") searchPlace(el("search").value);
});
el("btnLocate").addEventListener("click", () => {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => map.setView([pos.coords.latitude, pos.coords.longitude], 16),
    () => {
      el("planError").hidden = false;
      el("planError").textContent = "Could not read your location.";
    }
  );
});

el("btnModeSimple").addEventListener("click", () => setMode("simple"));
el("btnModeAdvanced").addEventListener("click", () => setMode("advanced"));
el("presetStockpile")?.addEventListener("change", () => {
  if (el("presetStockpile").checked) {
    applyPresetToForm("stockpile");
    rebuildPlan();
  }
});
el("presetFindDrone")?.addEventListener("change", () => {
  if (el("presetFindDrone").checked) {
    applyPresetToForm("findDrone");
    rebuildPlan();
  }
});

fillPresetCards();
applyPresetToForm(isLocator() ? "findDrone" : "stockpile");
if (isLocator() && el("presetFindDrone")) el("presetFindDrone").checked = true;

const params = new URLSearchParams(location.search);
if (params.has("demo") && !params.has("mosaic") && !params.has("find")) loadDemo();
updateLabels();
setMode("simple");

applyProductChrome();
initPageTabs();
document.getElementById("droneModel")?.addEventListener("change", () => {
  syncDroneSelect();
  rebuildPlan();
});
syncDroneSelect();
initAuthChrome();
(async () => {
  if (isLocator()) {
    const { initFinder } = await import("./finder.js?v=20261114dlpm2");
    initFinder();
  } else {
    const { initStockpile } = await import("./stockpile.js?v=20261114dlpm2");
    initStockpile();
  }
})();

let applyingRemoteSettings = false;
let settingsTimer = null;
let lastSettingsPayload = "";

function plannerSettingsPayload() {
  const s = settingsFromForm();
  return {
    mode: state.mode,
    preset: state.preset,
    name: s.name,
    altitudeM: s.altitudeM,
    frontOverlap: s.frontOverlap,
    sideOverlap: s.sideOverlap,
    speedMps: s.speedMps,
    gridAngleDeg: s.gridAngleDeg,
    crosshatch: !!s.crosshatch,
  };
}

function applySyncedSettings(payload) {
  if (!payload || typeof payload !== "object") return;
  applyingRemoteSettings = true;
  try {
    if (payload.name) el("missionName").value = payload.name;
    if (payload.preset && PRESETS[payload.preset]) {
      const radio = payload.preset === "findDrone" ? el("presetFindDrone") : el("presetStockpile");
      if (radio) radio.checked = true;
      state.preset = payload.preset;
    }
    if (payload.altitudeM != null) el("altitude").value = String(altitudeFtFromM(payload.altitudeM));
    if (payload.frontOverlap != null) el("frontOverlap").value = String(Math.round(payload.frontOverlap * 100));
    if (payload.sideOverlap != null) el("sideOverlap").value = String(Math.round(payload.sideOverlap * 100));
    if (payload.speedMps != null) el("speed").value = String(Math.round(mpsToMph(payload.speedMps)));
    if (payload.gridAngleDeg != null) el("gridAngle").value = String(payload.gridAngleDeg);
    if (payload.crosshatch != null) el("crosshatch").checked = !!payload.crosshatch;
    if (payload.mode) setMode(payload.mode);
    else rebuildPlan();
    lastSettingsPayload = JSON.stringify(plannerSettingsPayload());
  } finally {
    applyingRemoteSettings = false;
  }
}

async function pullPlannerSettings() {
  if (!isAuthenticated()) return;
  try {
    const body = await getSettings();
    const settings = body && body.settings;
    if (!settings) return;
    const cache = readCache(typeof localStorage !== "undefined" ? localStorage : null);
    cache.settings = body;
    writeCache(typeof localStorage !== "undefined" ? localStorage : null, cache);
    applySyncedSettings(settings);
  } catch {
    const cache = readCache(typeof localStorage !== "undefined" ? localStorage : null);
    if (cache.settings && cache.settings.settings) applySyncedSettings(cache.settings.settings);
  }
}

function queuePlannerSettings() {
  if (applyingRemoteSettings || !isAuthenticated()) return;
  const payload = plannerSettingsPayload();
  const encoded = JSON.stringify(payload);
  if (encoded === lastSettingsPayload) return;
  lastSettingsPayload = encoded;
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(async () => {
    try {
      const saved = await putSettings(payload);
      const cache = readCache(typeof localStorage !== "undefined" ? localStorage : null);
      cache.settings = saved;
      writeCache(typeof localStorage !== "undefined" ? localStorage : null, cache);
    } catch {
      enqueue(typeof localStorage !== "undefined" ? localStorage : null, { type: "settings", settings: payload });
    }
  }, 1500);
}

initSavedMissions({
  loadMission,
  getPlan: () => state.plan,
  planFromMission,
  buildMission,
  toMissionJson,
  toKml,
  toLitchiCsv,
  suggestedFilename,
  setPageTab,
});
const tutorial = initTutorial({
  setPageTab,
  getSettings: async () => {
    try {
      const item = await getSettings();
      return item?.settings || item || {};
    } catch {
      return {};
    }
  },
  saveSettings: async (settings) => {
    await putSettings(settings);
  },
  autoStart: true,
});
window.piletallyTutorial = tutorial;
el("btnShowTutorial")?.addEventListener("click", () => tutorial.replay());

// Phones: account links live behind a Menu button so the map gets the screen.
(function bindHeaderMenu() {
  const btn = el("btnHeaderMenu");
  const bar = btn ? btn.closest(".page-tabs") : null;
  if (!btn || !bar) return;
  const set = (open) => {
    bar.classList.toggle("menu-open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    set(!bar.classList.contains("menu-open"));
  });
  document.addEventListener("click", (ev) => {
    if (bar.classList.contains("menu-open") && !bar.contains(ev.target)) set(false);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") set(false);
  });
  el("btnShowTutorial")?.addEventListener("click", () => set(false));
})();


onAuthChange(() => {
  if (isAuthenticated()) pullPlannerSettings();
});
if (isAuthenticated()) pullPlannerSettings();
["altitude", "frontOverlap", "sideOverlap", "speed", "gridAngle", "missionName"].forEach((id) => {
  el(id)?.addEventListener("change", queuePlannerSettings);
  el(id)?.addEventListener("input", queuePlannerSettings);
});
el("crosshatch")?.addEventListener("change", queuePlannerSettings);
el("btnModeSimple")?.addEventListener("click", queuePlannerSettings);
el("btnModeAdvanced")?.addEventListener("click", queuePlannerSettings);
el("presetStockpile")?.addEventListener("change", queuePlannerSettings);
el("presetFindDrone")?.addEventListener("change", queuePlannerSettings);
onPageTab((name) => {
  if (name === "planner") refreshMapSize(map);

  if (name === "planner") setTimeout(() => map.invalidateSize(), 50);
});

const tabParam = params.get("tab") || (params.has("saved") ? "saved" : null);
const missionParam = params.get("mission");
if (tabParam === "saved" && !missionParam) setPageTab("saved");
if (missionParam) {
  (async () => {
    try {
      const index = await (await fetch(new URL("missions/index.json", document.baseURI))).json();
      const item = (index.missions || []).find(
        (m) => m.id === missionParam || m.files?.json === missionParam
      );
      if (!item) throw new Error("That saved mission was not found.");
      const res = await fetch(new URL(`missions/${item.files.json}`, document.baseURI));
      if (!res.ok) throw new Error(`Could not open that mission (${res.status}).`);
      loadMission(parseMissionJson(await res.text()));
      setPageTab("planner");
    } catch (e) {
      el("planError").hidden = false;
      el("planError").textContent = e.message || "Could not open that saved mission.";
    }
  })();
}
