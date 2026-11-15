import {
  formatAltitudeFtExact,
  formatDistance,
} from "./units.js?v=20261114dlpm2";
import { parseMissionJson } from "./export.js?v=20261114dlpm2";
import { isAuthenticated, onAuthChange } from "./auth.js?v=20261114dlpm2";
import {
  deleteMission as syncDeleteMission,
  enqueue,
  flushQueue,
  formatProgress,
  getMission,
  listCachedMissions,
  newMissionId,
  pullAndCache,
  readCache,
  readQueue,
  renameMission as syncRenameMission,
  saveMission as syncSaveMission,
  startPolling,
  writeCache,
} from "./sync.js?v=20261114dlpm2";

export const INDEX_FORMAT = "drone-mapper-missions-index";
export const INDEX_URL = "missions/index.json";
export const LOCAL_STORAGE_KEY = "drone-mapper-saved-missions";

export function createMemoryStore(initial = {}) {
  const data = { ...initial };
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      data[key] = String(value);
    },
    removeItem(key) {
      delete data[key];
    },
  };
}

export function summarizeMission(mission, extra = {}) {
  if (!mission || typeof mission !== "object") {
    throw new Error("Not a drone-mapper mission file.");
  }
  const computed = mission.computed || {};
  const settings = mission.settings || {};
  const lineCount = computed.lineCount ?? extra.lineCount ?? 0;
  const photoCount = computed.photoCount ?? mission.photos?.length ?? extra.photoCount ?? 0;
  return {
    id: extra.id || null,
    name: extra.name || mission.name || "Untitled survey",
    createdAt: extra.createdAt || mission.createdAt || null,
    notes: extra.notes || mission.notes || "",
    lineCount,
    lineSpacingM:
      computed.lineSpacingM ?? extra.lineSpacingM ?? null,
    altitudeM: settings.altitudeM ?? extra.altitudeM ?? null,
    photoCount,
    source: extra.source || "catalog",
    files: extra.files || null,
  };
}

/** Format a mission timestamp in America/Chicago. */
export function formatMissionDate(iso, timeZone = "America/Chicago") {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short",
  }).format(date);
}

export function formatMissionSummary(summary) {
  return {
    name: summary.name || "Untitled survey",
    date: formatMissionDate(summary.createdAt),
    lineCount: Number.isFinite(Number(summary.lineCount))
      ? String(summary.lineCount)
      : "—",
    spacing: formatDistance(summary.lineSpacingM),
    altitude: formatAltitudeFtExact(summary.altitudeM, 0),
    photoCount: Number.isFinite(Number(summary.photoCount))
      ? String(summary.photoCount)
      : "—",
  };
}

export function parseMissionsIndex(data) {
  const obj = typeof data === "string" ? JSON.parse(data) : data;
  if (!obj || obj.format !== INDEX_FORMAT) {
    throw new Error("Not a drone-mapper missions index.");
  }
  const missions = Array.isArray(obj.missions) ? obj.missions : [];
  return missions
    .filter((m) => m && m.id && m.files && m.files.json)
    .map((m) => ({
      id: m.id,
      name: m.name || "Untitled survey",
      createdAt: m.createdAt || null,
      notes: m.notes || "",
      lineCount: m.lineCount ?? 0,
      lineSpacingM: m.lineSpacingM ?? null,
      altitudeM: m.altitudeM ?? null,
      photoCount: m.photoCount ?? 0,
      source: "catalog",
      files: {
        json: m.files.json,
        kml: m.files.kml || null,
        csv: m.files.csv || null,
      },
    }));
}

function readLocalList(storage) {
  const raw = storage?.getItem?.(LOCAL_STORAGE_KEY);
  if (!raw) return [];
  const parsed = JSON.parse(raw);
  return Array.isArray(parsed) ? parsed : [];
}

export function listLocalMissions(storage) {
  if (!storage) return [];
  try {
    return readLocalList(storage)
      .filter((entry) => entry && entry.id && entry.mission)
      .map((entry) => ({
        ...summarizeMission(entry.mission, {
          id: entry.id,
          name: entry.mission.name,
          createdAt: entry.savedAt || entry.mission.createdAt,
          source: "local",
        }),
        mission: entry.mission,
      }));
  } catch {
    return [];
  }
}

export function saveLocalMission(mission, storage, extra = {}) {
  if (!storage) {
    throw new Error("Saving to My list needs a browser on this device.");
  }
  const parsed = parseMissionJson(mission);
  const list = readLocalList(storage);
  const id = extra.id || `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const savedAt = extra.savedAt || new Date().toISOString();
  if (parsed.name && extra.name) parsed.name = extra.name;
  list.unshift({ id, savedAt, mission: parsed });
  storage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(list));
  return summarizeMission(parsed, { id, createdAt: savedAt, source: "local" });
}

export function deleteLocalMission(id, storage) {
  if (!storage) return false;
  const list = readLocalList(storage);
  const next = list.filter((entry) => entry.id !== id);
  storage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(next));
  return next.length !== list.length;
}

export function getLocalMission(id, storage) {
  if (!storage) return null;
  const entry = readLocalList(storage).find((item) => item.id === id);
  return entry ? entry.mission : null;
}

export function fileUrl(filename) {
  if (!filename) return null;
  return new URL(`missions/${filename}`, document.baseURI).href;
}

function cardHtml(summary) {
  const meta = formatMissionSummary(summary);
  const notes = summary.notes
    ? `<p class="mission-notes">${escapeHtml(summary.notes)}</p>`
    : "";
  const progressText = formatProgress(summary.progress);
  const progress = progressText
    ? `<p class="mission-progress">${escapeHtml(progressText)}</p>`
    : "";
  let downloads = "";
  if (summary.source === "catalog" && summary.files) {
    const links = [];
    if (summary.files.json) {
      links.push(
        `<a href="${escapeAttr(fileUrl(summary.files.json))}" download>JSON</a>`
      );
    }
    if (summary.files.kml) {
      links.push(
        `<a href="${escapeAttr(fileUrl(summary.files.kml))}" download>KML</a>`
      );
    }
    if (summary.files.csv) {
      links.push(
        `<a href="${escapeAttr(fileUrl(summary.files.csv))}" download>Litchi CSV</a>`
      );
    }
    downloads = `<div class="mission-downloads">${links.join("")}</div>`;
  } else if (summary.source === "local" || summary.source === "account") {
    downloads = `<div class="mission-downloads">
      <button type="button" class="linkish" data-download="json">JSON</button>
      <button type="button" class="linkish" data-download="kml">KML</button>
      <button type="button" class="linkish" data-download="csv">Litchi CSV</button>
    </div>`;
  }
  const remove =
    summary.source === "local" || summary.source === "account"
      ? `<button type="button" class="ghost mission-remove" data-remove="${escapeAttr(summary.id)}">Remove</button>`
      : "";
  const rename =
    summary.source === "account"
      ? `<button type="button" class="ghost" data-rename="${escapeAttr(summary.id)}">Rename</button>`
      : "";
  return `<article class="mission-card" data-id="${escapeAttr(summary.id)}" data-source="${escapeAttr(summary.source)}">
    <div class="mission-head">
      <strong>${escapeHtml(meta.name)}</strong>
      <span class="mission-date">${escapeHtml(meta.date)}</span>
    </div>
    ${notes}
    ${progress}
    <dl class="mission-meta">
      <div><dt>Lines</dt><dd>${escapeHtml(meta.lineCount)}</dd></div>
      <div><dt>Spacing</dt><dd>${escapeHtml(meta.spacing)}</dd></div>
      <div><dt>Altitude</dt><dd>${escapeHtml(meta.altitude)}</dd></div>
      <div><dt>Photos</dt><dd>${escapeHtml(meta.photoCount)}</dd></div>
    </dl>
    <div class="mission-actions">
      <button type="button" class="primary" data-open="${escapeAttr(summary.id)}">Open on map</button>
      ${rename}
      ${remove}
    </div>
    ${downloads}
  </article>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s);
}

async function loadCatalog() {
  const res = await fetch(new URL(INDEX_URL, document.baseURI), {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`Could not load saved missions (${res.status}).`);
  }
  return parseMissionsIndex(await res.json());
}

function renderList(node, items, emptyText) {
  if (!node) return;
  if (!items.length) {
    node.innerHTML = `<p class="note">${escapeHtml(emptyText)}</p>`;
    return;
  }
  node.innerHTML = items.map(cardHtml).join("");
}

export function accountMissionSummary(item) {
  const mission = item.mission || {};
  return {
    ...summarizeMission(mission, {
      id: item.id,
      name: item.name || mission.name,
      createdAt: mission.createdAt,
      source: "account",
    }),
    updatedAt: item.updated_at,
    progress: item.progress || null,
    mission,
  };
}

export function initSavedMissions({
  loadMission,
  getPlan,
  planFromMission,
  buildMission,
  toMissionJson,
  toKml,
  toLitchiCsv,
  suggestedFilename,
  setPageTab,
  storage = typeof localStorage !== "undefined" ? localStorage : null,
  isSignedIn = isAuthenticated,
} = {}) {
  const catalogEl = document.getElementById("savedCatalog");
  const localEl = document.getElementById("savedLocal");
  const catalogStatus = document.getElementById("savedCatalogStatus");
  const localNote = document.getElementById("savedLocalNote");
  const syncStatus = document.getElementById("savedSyncStatus");
  const saveBtn = document.getElementById("btnSaveLocal");
  const saveNote = document.getElementById("saveLocalStatus");

  let catalog = [];
  let stopPoll = null;

  const showFlyingNow = (cache) => {
    const box = document.getElementById("flyingNow");
    if (!box) return;
    const flights = cache?.flyingNow || [];
    const live = flights.filter((f) => f.flying);
    if (!live.length) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    const names = live.map((f) => f.mission_id).join(", ");
    box.textContent = live.length === 1
      ? "Flying now — a mapping flight is in progress on the Android app for this account."
      : `Flying now — ${live.length} mapping flights are in progress on the Android app.`;
  };

  const setSyncNote = (msg, isError = false) => {
    if (!syncStatus) return;
    if (!msg) {
      syncStatus.hidden = true;
      syncStatus.textContent = "";
      return;
    }
    syncStatus.hidden = false;
    syncStatus.textContent = msg;
    syncStatus.classList.toggle("errorbox", isError);
    syncStatus.classList.toggle("note", !isError);
  };

  const updateLocalNote = () => {
    if (!localNote) return;
    if (isSignedIn()) {
      localNote.textContent =
        "Missions in your account show up here and in the Android app. Changes usually appear within about 15 seconds while this page is open.";
    } else {
      localNote.textContent =
        "Sign in to keep this list in sync with the Android app. Until then, items stay on this device only.";
    }
  };

  const accountItems = () => listCachedMissions(readCache(storage)).map(accountMissionSummary);

  const refreshLocal = () => {
    updateLocalNote();
    const items = isSignedIn()
      ? accountItems()
      : listLocalMissions(storage);
    const empty = isSignedIn()
      ? "Nothing in your account yet. Plan a survey, then tap Save to my list."
      : "Nothing in My list yet. Plan a survey, then tap Save to my list.";
    renderList(localEl, items, empty);
  };

  const refreshCatalog = async () => {
    if (!catalogEl) return;
    try {
      catalog = await loadCatalog();
      if (catalogStatus) catalogStatus.hidden = true;
      renderList(catalogEl, catalog, "No shared missions are published yet.");
    } catch (err) {
      catalog = [];
      if (catalogStatus) {
        catalogStatus.hidden = false;
        catalogStatus.textContent = err.message || "Could not load shared missions.";
      }
      catalogEl.innerHTML = "";
    }
  };

  const pullAccount = async ({ quiet = false } = {}) => {
    if (!isSignedIn()) {
      refreshLocal();
      return;
    }
    try {
      await flushQueue(storage);
      await pullAndCache(storage);
      if (!quiet) setSyncNote("");
      const pending = readQueue(storage).length;
      if (pending) setSyncNote(`Waiting to upload ${pending} change${pending === 1 ? "" : "s"}.`);
    } catch (err) {
      if (!quiet) setSyncNote(err.message || "Could not refresh your missions. Showing the last copy saved on this device.", true);
    }
    showFlyingNow(readCache(storage));
    refreshLocal();
  };

  const openCatalog = async (id) => {
    const item = catalog.find((m) => m.id === id);
    if (!item) return;
    const url = fileUrl(item.files.json);
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`Could not open that mission (${res.status}).`);
    loadMission(parseMissionJson(await res.text()));
    setPageTab?.("planner");
  };

  const openLocal = async (id, source) => {
    if (source === "account") {
      let item = readCache(storage).missions[id];
      if (!item || !item.mission) {
        item = await getMission(id);
        if (item && item.mission) {
          const cache = readCache(storage);
          cache.missions[id] = item;
          writeCache(storage, cache);
        }
      }
      if (!item || !item.mission) throw new Error("That mission is no longer in your account.");
      loadMission(parseMissionJson(item.mission));
      setPageTab?.("planner");
      return;
    }
    const mission = getLocalMission(id, storage);
    if (!mission) throw new Error("That saved mission is no longer on this device.");
    loadMission(parseMissionJson(mission));
    setPageTab?.("planner");
  };

  const downloadAccountOrLocal = (id, kind, source) => {
    let mission = null;
    if (source === "account") {
      mission = readCache(storage).missions[id]?.mission;
    } else {
      mission = getLocalMission(id, storage);
    }
    if (!mission || !planFromMission) return;
    const plan = planFromMission(mission);
    if (!plan) return;
    const text =
      kind === "kml"
        ? toKml(plan)
        : kind === "csv"
          ? toLitchiCsv(plan)
          : toMissionJson(plan);
    const ext = kind === "kml" ? "kml" : kind === "csv" ? "csv" : "json";
    const type =
      kind === "kml"
        ? "application/vnd.google-earth.kml+xml"
        : kind === "csv"
          ? "text/csv"
          : "application/json";
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = suggestedFilename(plan, ext);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const bindList = (root, source) => {
    root?.addEventListener("click", async (ev) => {
      const openBtn = ev.target.closest("[data-open]");
      const removeBtn = ev.target.closest("[data-remove]");
      const renameBtn = ev.target.closest("[data-rename]");
      const dlBtn = ev.target.closest("[data-download]");
      const card = ev.target.closest(".mission-card");
      const cardSource = card?.dataset.source || source;
      try {
        if (openBtn) {
          ev.preventDefault();
          if (cardSource === "catalog") await openCatalog(openBtn.getAttribute("data-open"));
          else await openLocal(openBtn.getAttribute("data-open"), cardSource);
        } else if (renameBtn) {
          ev.preventDefault();
          const id = renameBtn.getAttribute("data-rename");
          const current = accountItems().find((item) => item.id === id);
          const nextName = typeof prompt === "function" ? prompt("Rename this mission", current?.name || "") : null;
          if (!nextName || !nextName.trim()) return;
          try {
            const updated = await syncRenameMission(id, nextName.trim());
            const cache = readCache(storage);
            cache.missions[id] = { ...(cache.missions[id] || {}), ...updated };
            writeCache(storage, cache);
          } catch {
            enqueue(storage, { type: "rename", id, name: nextName.trim() });
            const cache = readCache(storage);
            if (cache.missions[id]) {
              cache.missions[id].name = nextName.trim();
              if (cache.missions[id].mission) cache.missions[id].mission.name = nextName.trim();
              writeCache(storage, cache);
            }
          }
          refreshLocal();
        } else if (removeBtn) {
          ev.preventDefault();
          const id = removeBtn.getAttribute("data-remove");
          if (cardSource === "account") {
            try {
              await syncDeleteMission(id);
            } catch {
              enqueue(storage, { type: "delete", id });
            }
            const cache = readCache(storage);
            delete cache.missions[id];
            writeCache(storage, cache);
          } else {
            deleteLocalMission(id, storage);
          }
          refreshLocal();
        } else if (dlBtn && cardSource !== "catalog") {
          ev.preventDefault();
          downloadAccountOrLocal(card?.dataset.id, dlBtn.getAttribute("data-download"), cardSource);
        }
      } catch (err) {
        if (catalogStatus) {
          catalogStatus.hidden = false;
          catalogStatus.textContent = err.message || "Could not open that mission.";
        }
      }
    });
  };

  bindList(catalogEl, "catalog");
  bindList(localEl, "local");

  saveBtn?.addEventListener("click", async () => {
    try {
      const plan = getPlan?.();
      if (!plan?.ok) {
        throw new Error("Plan a field first, then save it to My list.");
      }
      const mission = buildMission(plan);
      if (isSignedIn()) {
        const id = newMissionId();
        const cache = readCache(storage);
        cache.missions[id] = {
          id,
          name: mission.name,
          mission,
          updated_at: Date.now() / 1000,
          progress: null,
        };
        writeCache(storage, cache);
        refreshLocal();
        try {
          const saved = await syncSaveMission(mission, { id, name: mission.name });
          cache.missions[id] = { ...cache.missions[id], ...saved };
          writeCache(storage, cache);
          if (saveNote) {
            saveNote.hidden = false;
            saveNote.textContent = "Saved to your account. It will show up in the app for this login.";
          }
        } catch (err) {
          enqueue(storage, { type: "mission", id, name: mission.name, mission });
          if (saveNote) {
            saveNote.hidden = false;
            saveNote.textContent = (err.message || "Could not reach the server.") + " Saved on this device and queued to upload.";
          }
        }
        refreshLocal();
      } else {
        saveLocalMission(mission, storage);
        refreshLocal();
        if (saveNote) {
          saveNote.hidden = false;
          saveNote.textContent =
            "Saved on this device only. Sign in to sync it with the Android app and other computers.";
        }
      }
    } catch (err) {
      if (saveNote) {
        saveNote.hidden = false;
        saveNote.textContent = err.message || "Could not save that mission.";
      }
    }
  });

  const start = () => {
    if (stopPoll) stopPoll();
    stopPoll = startPolling(() => pullAccount({ quiet: true }));
  };

  onAuthChange(() => {
    updateLocalNote();
    refreshLocal();
    if (isSignedIn()) pullAccount();
  });

  refreshLocal();
  refreshCatalog();
  if (isSignedIn()) pullAccount();
  start();

  return { refreshCatalog, refreshLocal, pullAccount };
}
