/**
 * Account sync for missions, planner settings, flight progress, and
 * optional Find My Drone results. Uses the signed-in Piletally session.
 */
import { apiFetch, isAuthenticated } from "./auth.js";

export const POLL_MS = 12000;
export const CACHE_KEY = "drone-mapper-sync-cache";
export const QUEUE_KEY = "drone-mapper-sync-queue";

export function formatProgress(progress) {
  if (!progress || progress.deleted) return "";
  const status = progress.status || "";
  const linesDone = progress.lines_done;
  const linesTotal = progress.lines_total;
  const photosDone = progress.photos_done;
  const photosTotal = progress.photos_total;
  const parts = [];
  if (status === "completed") parts.push("Completed");
  else if (status === "paused") parts.push("Paused");
  else if (status === "aborted") parts.push("Stopped");
  else if (status === "in_progress") parts.push("In progress");
  else if (status === "flying") parts.push("Flying now");
  if (Number.isFinite(Number(linesDone))) {
    parts.push(
      Number.isFinite(Number(linesTotal))
        ? `${linesDone}/${linesTotal} lines`
        : `${linesDone} lines done`
    );
  }
  if (Number.isFinite(Number(photosDone))) {
    parts.push(
      Number.isFinite(Number(photosTotal))
        ? `${photosDone}/${photosTotal} photos`
        : `${photosDone} photos done`
    );
  }
  return parts.join(" · ");
}

export function emptyCache() {
  return { since: 0, missions: {}, settings: null, findResults: null, flyingNow: [] };
}

export function readCache(storage) {
  if (!storage) return emptyCache();
  try {
    const raw = storage.getItem(CACHE_KEY);
    if (!raw) return emptyCache();
    const parsed = JSON.parse(raw);
    return {
      since: Number(parsed.since) || 0,
      missions: parsed.missions && typeof parsed.missions === "object" ? parsed.missions : {},
      settings: parsed.settings || null,
      findResults: parsed.findResults || null,
      flyingNow: Array.isArray(parsed.flyingNow) ? parsed.flyingNow : [],
    };
  } catch {
    return emptyCache();
  }
}

export function writeCache(storage, cache) {
  if (!storage) return;
  storage.setItem(CACHE_KEY, JSON.stringify(cache));
}

export function readQueue(storage) {
  if (!storage) return [];
  try {
    const raw = storage.getItem(QUEUE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function writeQueue(storage, queue) {
  if (!storage) return;
  storage.setItem(QUEUE_KEY, JSON.stringify(queue));
}

export function enqueue(storage, item) {
  const queue = readQueue(storage);
  // Later writes to the same mission replace earlier queued writes.
  const next = queue.filter((entry) => {
    if (entry.type === item.type && entry.id && item.id && entry.id === item.id) {
      return false;
    }
    if (entry.type === item.type && (item.type === "settings" || item.type === "find-results")) {
      return false;
    }
    return true;
  });
  next.push({ ...item, queuedAt: item.queuedAt || new Date().toISOString() });
  writeQueue(storage, next);
  return next;
}

export function mergeChanges(cache, changes) {
  const next = {
    since: Math.max(Number(cache.since) || 0, Number(changes.server_time) || 0),
    missions: { ...(cache.missions || {}) },
    settings: cache.settings,
    findResults: cache.findResults,
  };
  for (const mission of changes.missions || []) {
    if (!mission || !mission.id) continue;
    if (mission.deleted) {
      delete next.missions[mission.id];
    } else {
      const prev = next.missions[mission.id] || {};
      next.missions[mission.id] = {
        ...prev,
        ...mission,
        mission: mission.mission || prev.mission,
        progress: mission.progress !== undefined ? mission.progress : prev.progress,
      };
    }
  }
  for (const progress of changes.progress || []) {
    if (!progress || !progress.mission_id) continue;
    const prev = next.missions[progress.mission_id];
    if (!prev) continue;
    if (progress.deleted) {
      prev.progress = null;
    } else {
      prev.progress = progress;
    }
  }
  if (changes.settings) {
    next.settings = changes.settings.deleted ? null : changes.settings;
  }
  if (changes.find_results) {
    next.findResults = changes.find_results.deleted ? null : changes.find_results;
  }
  if (Array.isArray(changes.flying_now)) {
    next.flyingNow = changes.flying_now;
  }
  return next;
}

export function listCachedMissions(cache) {
  return Object.values(cache.missions || {})
    .filter((item) => item && item.id && !item.deleted)
    .sort((a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0));
}

async function readError(res, fallback) {
  try {
    const body = await res.json();
    if (body && body.detail) return body.detail;
  } catch {}
  if (res.status === 401) return "Sign in to sync missions with your account.";
  return fallback || `HTTP ${res.status}`;
}

async function send(path, opts = {}) {
  const res = await apiFetch(path, opts);
  if (!res.ok) throw new Error(await readError(res, "Sync request failed."));
  if (res.status === 204) return {};
  return res.json();
}

export async function fetchChanges(since) {
  const q = since != null && since !== "" ? `?since=${encodeURIComponent(since)}` : "?since=0";
  return send(`/sync/changes${q}`);
}

export async function listMissions() {
  return send("/sync/missions");
}

export async function getMission(id) {
  return send(`/sync/missions/${encodeURIComponent(id)}`);
}

export async function saveMission(mission, { id, name } = {}) {
  const payload = { mission };
  if (name) payload.name = name;
  if (id) {
    payload.id = id;
    return send(`/sync/missions/${encodeURIComponent(id)}`, { method: "PUT", json: payload });
  }
  return send("/sync/missions", { method: "POST", json: payload });
}

export async function renameMission(id, name) {
  return send(`/sync/missions/${encodeURIComponent(id)}`, { method: "PATCH", json: { name } });
}

export async function deleteMission(id) {
  return send(`/sync/missions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function restoreMission(id) {
  return send(`/sync/missions/${encodeURIComponent(id)}`, { method: "PATCH", json: { restore: true } });
}

export async function putProgress(id, progress) {
  return send(`/sync/missions/${encodeURIComponent(id)}/progress`, { method: "PUT", json: progress });
}

export async function getSettings() {
  return send("/sync/settings");
}

export async function putSettings(settings) {
  return send("/sync/settings", { method: "PUT", json: { settings } });
}

export async function putFindResults(results) {
  return send("/sync/find-results", { method: "PUT", json: { results } });
}

export async function flushQueue(storage) {
  const queue = readQueue(storage);
  if (!queue.length) return [];
  const remaining = [];
  for (const item of queue) {
    try {
      if (item.type === "mission") {
        await saveMission(item.mission, { id: item.id, name: item.name });
      } else if (item.type === "delete") {
        await deleteMission(item.id);
      } else if (item.type === "rename") {
        await renameMission(item.id, item.name);
      } else if (item.type === "progress") {
        await putProgress(item.id, item.progress);
      } else if (item.type === "settings") {
        await putSettings(item.settings);
      } else if (item.type === "find-results") {
        await putFindResults(item.results);
      } else {
        remaining.push(item);
      }
    } catch (err) {
      // Leave the item queued if we are offline or the server rejected it temporarily.
      if (!err.message || /sign in/i.test(err.message) || /failed/i.test(err.message) || /HTTP 5/.test(err.message)) {
        remaining.push(item);
      }
      // 4xx other than auth: drop so a bad payload cannot block the queue forever.
      else if (/HTTP 4/.test(err.message)) {
        continue;
      } else {
        remaining.push(item);
      }
    }
  }
  writeQueue(storage, remaining);
  return remaining;
}

export async function pullAndCache(storage) {
  const cache = readCache(storage);
  const changes = await fetchChanges(cache.since || 0);
  const next = mergeChanges(cache, changes);
  writeCache(storage, next);
  return next;
}

export function startPolling(fn, { interval = POLL_MS, hiddenCheck = () => typeof document !== "undefined" && document.hidden } = {}) {
  let timer = null;
  const tick = async () => {
    if (hiddenCheck()) return;
    try {
      await fn();
    } catch {
      // Keep polling; the next tick can recover.
    }
  };
  timer = setInterval(tick, interval);
  return () => {
    if (timer) clearInterval(timer);
    timer = null;
  };
}

export function newMissionId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return `mission-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export { isAuthenticated };
