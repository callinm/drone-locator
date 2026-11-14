/**
 * App-level Pile Count upload state.
 * Lives outside the tab view so an in-flight XHR and the selected
 * File objects survive switching Planner / Saved / Pile Count / Find tabs.
 * Metadata is also kept in sessionStorage so a reload can show status and
 * resume server-side per-file uploads when that API is available.
 */

const META_KEY = "piletally_mosaic_upload_v1";

const store = {
  files: [], // File objects copied out of the <input> FileList
  received: {}, // name+size -> true when the server already has the file
  uploadId: null,
  jobId: null,
  phase: "idle", // idle | selected | uploading | processing | failed | done
  pct: 0,
  message: "",
  xhr: null,
};

const listeners = new Set();
let beforeUnloadBound = false;

function keyFor(name, size) {
  return `${name}::${size}`;
}

function emit() {
  persistMeta();
  const snap = snapshot();
  listeners.forEach((fn) => {
    try {
      fn(snap);
    } catch {
      /* listener errors must not stop the upload */
    }
  });
  syncTabBadge(snap);
}

export function snapshot() {
  return {
    files: store.files.slice(),
    fileCount: store.files.length,
    received: { ...store.received },
    uploadId: store.uploadId,
    jobId: store.jobId,
    phase: store.phase,
    pct: store.pct,
    message: store.message,
    busy: isBusy(),
  };
}

export function isBusy() {
  const xhr = store.xhr;
  const xhrActive = !!(xhr && xhr.readyState !== 0 && xhr.readyState !== 4);
  // After a full reload File objects and the XHR are gone, so do not treat
  // a leftover "uploading" phase as an active in-flight upload.
  return xhrActive || (store.phase === "uploading" && store.files.length > 0);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function persistMeta() {
  try {
    sessionStorage.setItem(
      META_KEY,
      JSON.stringify({
        uploadId: store.uploadId,
        jobId: store.jobId,
        phase: store.phase,
        pct: store.pct,
        message: store.message,
        names: store.files.map((f) => ({
          name: f.name,
          size: f.size,
          received: !!store.received[keyFor(f.name, f.size)],
        })),
        savedAt: Date.now(),
      })
    );
  } catch {
    /* quota / private mode */
  }
}

export function readPersistedMeta() {
  try {
    const raw = sessionStorage.getItem(META_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function setFiles(fileList) {
  const next = Array.from(fileList || []);
  const meta = readPersistedMeta();
  const sameSession = !!(
    meta &&
    meta.uploadId &&
    next.length &&
    Array.isArray(meta.names) &&
    meta.names.some((n) =>
      next.some((f) => f.name === n.name && f.size === n.size && n.received)
    )
  );
  store.files = next;
  if (sameSession) {
    store.uploadId = meta.uploadId;
    store.jobId = meta.jobId || null;
    store.received = {};
    for (const n of meta.names) {
      if (n && n.received) store.received[keyFor(n.name, n.size)] = true;
    }
    const pending = pendingFiles().length;
    const already = next.length - pending;
    store.phase = "selected";
    store.pct = next.length ? Math.round((100 * already) / next.length) : 0;
    store.message =
      already > 0
        ? `${already} of ${next.length} photos already uploaded. Remaining files will be sent next.`
        : `${next.length} photos selected.`;
  } else {
    store.received = {};
    store.uploadId = null;
    store.jobId = null;
    store.phase = next.length ? "selected" : "idle";
    store.pct = 0;
    store.message = next.length
      ? `${next.length} photos selected.`
      : "";
  }
  emit();
  return store.files;
}

export function setProgress({ phase, pct, message, uploadId, jobId, received } = {}) {
  if (phase) store.phase = phase;
  if (Number.isFinite(pct)) store.pct = Math.max(0, Math.min(100, pct));
  if (message != null) store.message = message;
  if (uploadId !== undefined) store.uploadId = uploadId;
  if (jobId !== undefined) store.jobId = jobId;
  if (received && typeof received === "object") {
    store.received = { ...store.received, ...received };
  }
  emit();
}

export function markReceived(name, size) {
  store.received[keyFor(name, size)] = true;
  persistMeta();
}

export function pendingFiles() {
  return store.files.filter((f) => !store.received[keyFor(f.name, f.size)]);
}

export function registerXhr(xhr) {
  store.xhr = xhr || null;
}

export function clearXhr() {
  store.xhr = null;
}

export function attachBeforeUnload() {
  if (beforeUnloadBound) return;
  beforeUnloadBound = true;
  window.addEventListener("beforeunload", (event) => {
    if (!isBusy()) return;
    event.preventDefault();
    event.returnValue = "Photos are still uploading. If you leave, the upload may stop.";
    return event.returnValue;
  });
}

function syncTabBadge(snap) {
  const tab = document.getElementById("tabStockpile");
  if (mosaic && mosaic !== tab) {
    mosaic.classList.remove("tab-busy");
    mosaic.title = "";
  }
  if (!tab) return;
  if (snap.busy || snap.phase === "uploading") {
    tab.classList.add("tab-busy");
    tab.title = snap.message || "Photos are still uploading";
  } else {
    tab.classList.remove("tab-busy");
    if (snap.phase === "processing") {
      tab.title = snap.message || "Survey processing is running";
    } else {
      tab.title = "";
    }
  }
}

export function hydrateFromSession() {
  const meta = readPersistedMeta();
  if (!meta) return null;
  // File objects cannot be restored after a full reload. Keep ids/progress
  // so the UI can resume a server-side session or show the running job.
  if (meta.uploadId) store.uploadId = meta.uploadId;
  if (meta.jobId) store.jobId = meta.jobId;
  if (meta.phase) store.phase = meta.phase;
  if (Number.isFinite(meta.pct)) store.pct = meta.pct;
  if (meta.message) store.message = meta.message;
  if (Array.isArray(meta.names)) {
    for (const n of meta.names) {
      if (n && n.received) store.received[keyFor(n.name, n.size)] = true;
    }
  }
  if (
    meta.uploadId &&
    !store.files.length &&
    (store.phase === "uploading" || store.phase === "selected")
  ) {
    store.phase = "selected";
    store.message =
      "Upload was interrupted. Choose the same photos to resume; files already received will not be sent again.";
  }
  return meta;
}
