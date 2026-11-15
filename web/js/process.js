/** Survey-quality processing UI helpers. Status comes from find-api only. */

import { apiFetch, apiBase, requestAuthHeaders } from "./auth.js?v=20261114dlpm1";
import {
  registerXhr,
  clearXhr,
  setProgress,
  pendingFiles,
  markReceived,
  snapshot,
  setFiles,
} from "./upload-store.js?v=20261114dlpm1";

export const STATUS_LABELS = {
  uploading: "Uploading",
  queued: "Queued",
  matching_photos: "Matching photos",
  building_surface: "Building 3D surface",
  making_map: "Making map",
  computing_volume: "Computing volume",
  processing: "Processing",
  building_map: "Making map",
  done: "Done",
  failed: "Failed",
};

export function formatElapsed(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return "";
  const total = Math.floor(n);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(secs).padStart(2, "0")}s`;
  if (minutes) return `${minutes} min ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

export function stageOf(job) {
  if (!job) return "";
  return job.stage || job.status || "";
}

export function stageLabel(job) {
  if (!job) return "";
  return job.stage_label || STATUS_LABELS[stageOf(job)] || stageOf(job);
}

export function progressPct(job) {
  const n = Number(job?.progress_pct);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n)));
}

export function statusText(job) {
  if (!job) return "";
  const label = stageLabel(job);
  const pct = `${progressPct(job)}%`;
  const elapsed = formatElapsed(job.elapsed_sec);
  const clock = elapsed ? ` · ${elapsed}` : "";
  return job.progress ? `${label} ${pct}${clock}: ${job.progress}` : `${label} ${pct}${clock}`;
}

export function renderProcessBar(prefix, job) {
  const wrap = document.getElementById(`${prefix}ProcessBarWrap`);
  if (!wrap) return;
  if (!job) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  wrap.classList.toggle("is-failed", stageOf(job) === "failed");
  wrap.classList.toggle("is-done", stageOf(job) === "done");
  const pct = progressPct(job);
  const stage = document.getElementById(`${prefix}ProcessStage`);
  const fill = document.getElementById(`${prefix}ProcessBarFill`);
  const bar = document.getElementById(`${prefix}ProcessBar`);
  const pctEl = document.getElementById(`${prefix}ProcessPct`);
  if (stage) stage.textContent = stageLabel(job);
  if (fill) fill.style.width = `${pct}%`;
  const elapsed = formatElapsed(job.elapsed_sec);
  if (pctEl) pctEl.textContent = elapsed ? `${pct}% · ${elapsed}` : `${pct}%`;
  if (bar) bar.setAttribute("aria-valuenow", String(pct));
}

export async function startStockpileProcess(stockpileJobId, name) {
  const res = await apiFetch(`/stockpile/jobs/${stockpileJobId}/process`, {
    method: "POST",
    json: name ? { name } : {},
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not start survey-quality processing.");
  return body;
}

function postFormWithProgress(path, form, onUploadProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${apiBase()}${path}`);
    xhr.withCredentials = true;
    const headers = requestAuthHeaders("POST");
    Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    registerXhr(xhr);
    xhr.upload.onprogress = (ev) => {
      if (!onUploadProgress) return;
      if (ev.lengthComputable && ev.total) {
        onUploadProgress(Math.max(0, Math.min(100, Math.round((100 * ev.loaded) / ev.total))));
      }
    };
    const finish = (fn) => (ev) => {
      clearXhr();
      fn(ev);
    };
    xhr.onload = finish(() => {
      let json = {};
      try { json = JSON.parse(xhr.responseText || "{}"); } catch { json = {}; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(json);
      else reject(new Error(json.detail || "Could not start survey-quality processing."));
    });
    xhr.onerror = finish(() => reject(new Error("Could not start survey-quality processing.")));
    xhr.onabort = finish(() => reject(new Error("Upload was interrupted.")));
    xhr.send(form);
  });
}

async function tryCreateUploadSession() {
  try {
    const res = await apiFetch("/process/uploads", { method: "POST", json: {} });
    if (res.status === 404 || res.status === 405) return null;
    const body = await res.json().catch(() => ({}));
    if (res.status === 401 || res.status === 403) {
      throw new Error(body.detail || "Sign in to start survey-quality processing.");
    }
    if (!res.ok) return null;
    return body.id || body.upload_id || null;
  } catch (err) {
    if (err && err.message && /sign in/i.test(err.message)) throw err;
    return null;
  }
}

async function startProcessIncremental({ missionId, name, files, onUploadProgress }) {
  const list = Array.from(files || []);
  if (!list.length) return null;
  if (!snapshot().files.length) setFiles(list);
  const uploadId = snapshot().uploadId || (await tryCreateUploadSession());
  if (!uploadId) return null;
  setProgress({ phase: "uploading", uploadId, message: `Uploading 0% of ${list.length} photos…`, pct: 0 });

  try {
    const existing = await apiFetch(`/process/uploads/${encodeURIComponent(uploadId)}`);
    if (existing.ok) {
      const body = await existing.json().catch(() => ({}));
      for (const f of body.received || body.files || []) {
        if (f && f.name != null && f.size != null) markReceived(f.name, f.size);
      }
    }
  } catch {
    /* continue; we will send every file */
  }

  const total = list.length;
  let done = total - pendingFiles().length;
  const report = (pct, msg) => {
    setProgress({ phase: "uploading", pct, message: msg });
    if (onUploadProgress) onUploadProgress(pct);
  };
  report(total ? Math.round((100 * done) / total) : 0, `Uploading ${done} of ${total} photos…`);

  for (const file of pendingFiles()) {
    const body = new FormData();
    body.append("files", file, file.name);
    await postFormWithProgress(`/process/uploads/${encodeURIComponent(uploadId)}/files`, body, (filePct) => {
      const blended = Math.round((100 * (done + filePct / 100)) / total);
      report(blended, `Uploading ${done + 1} of ${total} photos…`);
    });
    markReceived(file.name, file.size);
    done += 1;
    report(Math.round((100 * done) / total), `Uploading ${done} of ${total} photos…`);
  }

  const startBody = new FormData();
  startBody.append("upload_id", uploadId);
  if (missionId) startBody.append("mission_id", missionId);
  if (name) startBody.append("name", name);
  const job = await postFormWithProgress("/process", startBody, null);
  setProgress({
    phase: job && (job.status === "failed" ? "failed" : "processing"),
    jobId: job && job.id,
    pct: job && job.progress_pct != null ? Number(job.progress_pct) : 100,
    message: (job && job.progress) || "Upload complete. Processing…",
  });
  return job;
}

/**
 * Upload photos to a resumable session and stop when the last file is in.
 * Does not start Stitch — the caller should show "Upload complete" and wait
 * for an explicit Stitch tap (a 100-photo job can fill the disk).
 */
export async function uploadPhotosIncremental({ files, onUploadProgress, onFile } = {}) {
  const list = Array.from(files || []);
  if (!list.length) return null;
  if (!snapshot().files.length) setFiles(list);
  const uploadId = snapshot().uploadId || (await tryCreateUploadSession());
  if (!uploadId) {
    // Fall back: no per-file API. Caller can still post all files later.
    setProgress({
      phase: "selected",
      message: `${list.length} photos selected. They will upload when you stay on this page.`,
    });
    return { uploadId: null, fileCount: list.length, incremental: false };
  }
  setProgress({ phase: "uploading", uploadId, message: `Uploading 0% of ${list.length} photos…`, pct: 0 });

  try {
    const existing = await apiFetch(`/process/uploads/${encodeURIComponent(uploadId)}`);
    if (existing.ok) {
      const body = await existing.json().catch(() => ({}));
      for (const f of body.received || body.files || []) {
        if (f && f.name != null && f.size != null) markReceived(f.name, f.size);
      }
    }
  } catch {
    /* continue; we will send every file */
  }

  const total = list.length;
  let done = total - pendingFiles().length;
  const report = (pct, msg) => {
    setProgress({ phase: "uploading", pct, message: msg });
    if (onUploadProgress) onUploadProgress(pct);
  };
  report(total ? Math.round((100 * done) / total) : 0, `Uploading ${done} of ${total} photos…`);

  for (const file of pendingFiles()) {
    const body = new FormData();
    body.append("files", file, file.name);
    await postFormWithProgress(`/process/uploads/${encodeURIComponent(uploadId)}/files`, body, (filePct) => {
      const blended = Math.round((100 * (done + filePct / 100)) / total);
      report(blended, `Uploading ${done + 1} of ${total} photos…`);
    });
    markReceived(file.name, file.size);
    done += 1;
    report(Math.round((100 * done) / total), `Uploading ${done} of ${total} photos…`);
    if (onFile) onFile(file, done, total);
  }

  setProgress({
    phase: "uploaded",
    uploadId,
    pct: 100,
    message: "Upload complete",
  });
  return { uploadId, fileCount: total, incremental: true };
}

export async function startProcessFromUpload({ uploadId, missionId, name } = {}) {
  if (!uploadId) throw new Error("Upload the photos first, then tap Stitch.");
  const startBody = new FormData();
  startBody.append("upload_id", uploadId);
  if (missionId) startBody.append("mission_id", missionId);
  if (name) startBody.append("name", name);
  setProgress({ phase: "processing", message: "Starting Stitch…", uploadId });
  const job = await postFormWithProgress("/process", startBody, null);
  setProgress({
    phase: job && (job.status === "failed" ? "failed" : "processing"),
    jobId: job && job.id,
    pct: job && job.progress_pct != null ? Number(job.progress_pct) : 0,
    message: (job && job.progress) || "Stitching…",
  });
  return job;
}

export async function startProcess({ missionId, name, files, onUploadProgress } = {}) {
  const hasFiles = files && files.length;
  if (hasFiles) {
    const incremental = await startProcessIncremental({ missionId, name, files, onUploadProgress });
    if (incremental) return incremental;
    // Live find-api may not have per-file upload yet; send one request and
    // keep the XHR registered on the app-level store so tab switches do not abort it.
    const body = new FormData();
    if (missionId) body.append("mission_id", missionId);
    if (name) body.append("name", name);
    [...files].forEach((f) => body.append("files", f, f.name));
    setProgress({ phase: "uploading", pct: 0, message: `Uploading 0% of ${files.length} photos…` });
    try {
      const job = await postFormWithProgress("/process", body, (pct) => {
        setProgress({ phase: "uploading", pct, message: `Uploading ${pct}% of ${files.length} photos…` });
        if (onUploadProgress) onUploadProgress(pct);
      });
      setProgress({
        phase: job && (job.status === "failed" ? "failed" : "processing"),
        jobId: job && job.id,
        pct: job && job.progress_pct != null ? Number(job.progress_pct) : 0,
        message: (job && job.progress) || "Upload complete. Processing…",
      });
      return job;
    } catch (err) {
      setProgress({ phase: "failed", message: err.message || String(err) });
      throw err;
    }
  }
  const res = await apiFetch("/process", {
    method: "POST",
    json: { mission_id: missionId, name },
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || "Could not start survey-quality processing.");
  return json;
}

export async function getProcess(id) {
  const res = await apiFetch(`/process/${id}`);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || "Could not read processing status.");
  return json;
}

export async function refreshProcess(id) {
  const res = await apiFetch(`/process/${id}/refresh`, { method: "POST", json: {} });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || "Could not refresh processing status.");
  return json;
}

export function orthoUrl(job) {
  if (!job?.id) return null;
  if (job.status !== "done") return null;
  return apiBase() + `/process/${job.id}/orthophoto.png`;
}

export function startPolling(getId, onTick, ms = 1000) {
  let timer = null;
  let clock = null;
  let lastJob = null;
  const stop = () => {
    if (timer) clearInterval(timer);
    if (clock) clearInterval(clock);
    timer = null;
    clock = null;
  };
  const withLiveElapsed = (job) => {
    if (!job) return job;
    const start = Number(job.started_at || job.created_at);
    if (!Number.isFinite(start) || start <= 0) return job;
    const nowSec = Date.now() / 1000;
    const elapsed = nowSec - (start > 1e12 ? start / 1000 : start);
    if (!Number.isFinite(elapsed) || elapsed < 0) return job;
    return { ...job, elapsed_sec: elapsed };
  };
  const tick = async () => {
    const id = getId();
    if (!id) return;
    try {
      const job = await getProcess(id);
      lastJob = job;
      onTick(withLiveElapsed(job));
      if (job.status === "done" || job.status === "failed") stop();
    } catch {
      // Keep polling; a single failed tick should not cancel the wait.
    }
  };
  const tickClock = () => {
    if (!lastJob) return;
    if (lastJob.status === "done" || lastJob.status === "failed") return;
    onTick(withLiveElapsed(lastJob));
  };
  timer = setInterval(tick, ms);
  clock = setInterval(tickClock, 1000);
  tick();
  return stop;
}
