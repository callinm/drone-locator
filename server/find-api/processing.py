"""Hidden survey-quality processing. Users talk only to find-api.

One internal WebODM service account and project, owned by find-api.
Each Drone Mapper mission or stockpile job maps to a WebODM task id
here. Account isolation is enforced on every read and write. WebODM
URLs and task ids are never returned to clients.
"""

from __future__ import annotations

import json
import re
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

import accounts
import config
import numpy as np

import photolimits
import stockpile
import volume_calc
import webodm

RequireLogin = Callable[[Request], tuple[Any, Any]]
RequireCsrf = Callable[[Request, Any], None]
RequireMosaic = Callable[[Request], tuple[Any, Any]]

POLL_SECONDS = float(os.environ.get("FIND_API_WEBODM_POLL", "8"))
PUBLIC_STATUSES = ("queued", "uploading", "processing", "done", "failed")
STAGE_LABELS = {
    "uploading": "Uploading",
    "queued": "Queued",
    "matching_photos": "Matching photos",
    "building_surface": "Building 3D surface",
    "making_map": "Making map",
    "computing_volume": "Computing volume",
    "processing": "Processing",
    "building_map": "Making map",
    "done": "Done",
    "failed": "Failed",
}

# Official ODM stage end percentages from stages/odm_app.py (normal, non-split jobs).
# split/merge advertise 75/100 only on large split datasets; treat them as no-ops here.
ODM_STAGE_END = {
    "dataset": 5,
    "split": 5,
    "merge": 5,
    "opensfm": 25,
    "openmvs": 50,
    "odm_filterpoints": 52,
    "odm_meshing": 60,
    "mvs_texturing": 70,
    "odm_georeferencing": 80,
    "odm_dem": 90,
    "odm_orthophoto": 98,
    "odm_report": 99,
    "odm_postprocess": 100,
}
ODM_STAGE_TO_USER = {
    "dataset": "matching_photos",
    "split": "matching_photos",
    "merge": "matching_photos",
    "opensfm": "matching_photos",
    "openmvs": "building_surface",
    "odm_filterpoints": "building_surface",
    "odm_meshing": "building_surface",
    "mvs_texturing": "making_map",
    "odm_georeferencing": "making_map",
    "odm_dem": "making_map",
    "odm_orthophoto": "making_map",
    "odm_report": "making_map",
    "odm_postprocess": "making_map",
}
# Fallback ranges when the console cannot be read.
USER_STAGE_END = {
    "uploading": 100,
    "queued": 0,
    "matching_photos": 25,
    "building_surface": 60,
    "making_map": 99,
    "computing_volume": 100,
    "processing": 85,
    "building_map": 99,
    "done": 100,
    "failed": 100,
}
CREEP_HALFLIFE_SEC = 480.0
_RUNNING_STAGE_RE = re.compile(r"Running\s+([A-Za-z0-9_]+)\s+stage", re.I)


def parse_odm_stage(console: str | None) -> str | None:
    found = None
    for match in _RUNNING_STAGE_RE.finditer(console or ""):
        name = match.group(1)
        if name in ODM_STAGE_END or name in ODM_STAGE_TO_USER:
            found = name
    return found


def infer_stage_from_pct(progress_pct: int | None) -> str:
    try:
        pct = int(progress_pct or 0)
    except (TypeError, ValueError):
        pct = 0
    if pct < 25:
        return "matching_photos"
    if pct < 60:
        return "building_surface"
    if pct < 100:
        return "making_map"
    return "making_map"


def derive_stage(status: str | None, progress_pct: int | None = None, stored: str | None = None) -> str:
    st = status or "queued"
    if st in {"uploading", "queued", "done", "failed", "computing_volume"}:
        return st
    if stored in STAGE_LABELS and stored not in {"processing", "building_map"}:
        return stored
    if st == "processing":
        if stored in STAGE_LABELS:
            return "making_map" if stored == "building_map" else stored
        return infer_stage_from_pct(progress_pct)
    return st


def stage_end_pct(user_stage: str, odm_stage: str | None = None) -> int:
    if odm_stage and odm_stage in ODM_STAGE_END:
        return int(ODM_STAGE_END[odm_stage])
    return int(USER_STAGE_END.get(user_stage or "", 100))


def display_progress(
    webodm_pct: int,
    stage_end: int,
    stage_started_at: float | None,
    now: float,
    last_pct: int | None = 0,
) -> int:
    """Creep toward, but never past, the current ODM stage end while WebODM is stuck."""
    try:
        base = max(0, int(webodm_pct or 0))
    except (TypeError, ValueError):
        base = 0
    try:
        last = max(0, int(last_pct or 0))
    except (TypeError, ValueError):
        last = 0
    end = max(0, min(100, int(stage_end)))
    if base >= end:
        return min(100, max(base, last))
    cap = max(base, end - 1)
    started = now if stage_started_at is None else stage_started_at
    elapsed = max(0.0, float(now) - float(started))
    frac = 1.0 - (2.0 ** (-elapsed / CREEP_HALFLIFE_SEC))
    crept = base + (cap - base) * frac
    shown = int(crept)  # floor so we never round up to the stage end
    shown = max(base, last if last <= cap else cap, shown)
    return max(0, min(cap, shown))


def format_elapsed(seconds: int | float | None) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes} min"
    return f"{secs}s"


def elapsed_of(job: dict[str, Any], now: float | None = None) -> int:
    start = job.get("started_at") or job.get("created_at")
    if not start:
        return 0
    if job.get("status") in {"done", "failed"} and job.get("finished_at"):
        end = job["finished_at"]
    else:
        end = now if now is not None else _now()
    try:
        return max(0, int(end - start))
    except (TypeError, ValueError):
        return 0



class ProcessingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _conn():
    return accounts._connect()


def _now() -> float:
    return time.time()


def _jobs_dir(user_id: int, job_id: str) -> Path:
    path = config.DATA_DIR / "processing" / str(user_id) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def init() -> None:
    conn = _conn()
    with accounts._lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processing_jobs (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              mission_id TEXT,
              stockpile_job_id TEXT,
              name TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              progress TEXT,
              progress_pct INTEGER,
              error TEXT,
              webodm_project_id INTEGER,
              webodm_task_id TEXT,
              options_json TEXT,
              photo_count INTEGER NOT NULL DEFAULT 0,
              result_meta TEXT,
              volumes_json TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              started_at REAL,
              finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_processing_user ON processing_jobs(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_processing_task ON processing_jobs(webodm_task_id);
            CREATE INDEX IF NOT EXISTS idx_processing_stockpile ON processing_jobs(stockpile_job_id);
            CREATE INDEX IF NOT EXISTS idx_processing_mission ON processing_jobs(user_id, mission_id);
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(processing_jobs)")}
        if "pipeline_stage" not in existing:
            conn.execute("ALTER TABLE processing_jobs ADD COLUMN pipeline_stage TEXT")
        if "stage_started_at" not in existing:
            conn.execute("ALTER TABLE processing_jobs ADD COLUMN stage_started_at REAL")


def _parse(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "mission_id": row["mission_id"],
        "stockpile_job_id": row["stockpile_job_id"],
        "name": row["name"],
        "status": row["status"],
        "progress": row["progress"],
        "progress_pct": row["progress_pct"],
        "error": row["error"],
        "webodm_project_id": row["webodm_project_id"],
        "webodm_task_id": row["webodm_task_id"],
        "options": _parse(row["options_json"], webodm.public_options()),
        "photo_count": int(row["photo_count"] or 0),
        "result": _parse(row["result_meta"], None),
        "volumes": _parse(row["volumes_json"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "pipeline_stage": row["pipeline_stage"] if "pipeline_stage" in row.keys() else None,
        "stage_started_at": row["stage_started_at"] if "stage_started_at" in row.keys() else None,
    }


def _live_dsm_frame(job: dict[str, Any]) -> dict[str, Any] | None:
    """Read the harvested DSM so older jobs also report a local vs geographic frame."""
    try:
        import volume_calc
        import volume_crs
        candidates = []
        if job.get("id") and job.get("user_id") is not None:
            candidates.append(config.DATA_DIR / "processing" / str(job["user_id"]) / job["id"] / "dsm.tif")
        if job.get("stockpile_job_id") and job.get("user_id") is not None:
            candidates.append(config.DATA_DIR / "stockpile" / str(job["user_id"]) / job["stockpile_job_id"] / "dsm.tif")
        for path in candidates:
            if path.is_file():
                return volume_crs.frame_info(volume_calc.load_dsm_geotiff(path.read_bytes()))
    except Exception:
        return None
    return None


def public_view(job: dict[str, Any]) -> dict[str, Any]:
    """Client payload: no WebODM hostnames, task ids, or credentials."""
    result = job.get("result") or {}
    frame = result.get("dsm_frame") or _live_dsm_frame(job)
    safe_result = None
    if result or frame:
        bounds = result.get("bounds")
        if frame and frame.get("local_frame"):
            bounds = {
                "west": frame["west"],
                "south": frame["south"],
                "east": frame["east"],
                "north": frame["north"],
            }
        safe_result = {
            "kind": result.get("kind") or "survey",
            "has_orthophoto": bool(result.get("has_orthophoto")),
            "has_dsm": bool(result.get("has_dsm") or frame),
            "bounds": bounds,
            "quality": "survey",
            "dsm_frame": frame,
            "local_frame": bool(frame and frame.get("local_frame")),
        }
    return {
        "id": job["id"],
        "mission_id": job.get("mission_id"),
        "stockpile_job_id": job.get("stockpile_job_id"),
        "name": job["name"],
        "status": job["status"],
        "stage": derive_stage(job.get("status"), job.get("progress_pct"), job.get("pipeline_stage")),
        "stage_label": STAGE_LABELS.get(derive_stage(job.get("status"), job.get("progress_pct"), job.get("pipeline_stage")), job.get("status")),
        "progress": job.get("progress"),
        "progress_pct": int(job.get("progress_pct") or 0),
        "elapsed_sec": elapsed_of(job),
        "error": job.get("error") if job["status"] == "failed" else None,
        "photo_count": job.get("photo_count") or 0,
        "result": safe_result,
        "volumes": job.get("volumes"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "orthophoto_url": f"/process/{job['id']}/orthophoto.png" if safe_result and safe_result.get("has_orthophoto") else None,
        "dsm_url": f"/process/{job['id']}/dsm.tif" if safe_result and safe_result.get("has_dsm") else None,
    }


def get_job(user_id: int, job_id: str) -> dict[str, Any] | None:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    return _row(row) if row is not None else None


def _require_job(user_id: int, job_id: str) -> dict[str, Any]:
    job = get_job(user_id, job_id)
    if job is None:
        raise ProcessingError(404, "Unknown processing job.")
    return job


def list_jobs(user_id: int) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM processing_jobs WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def list_active_jobs() -> list[dict[str, Any]]:
    """All in-flight /process jobs (and stockpile-backed ones), oldest first."""
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            """
            SELECT * FROM processing_jobs
            WHERE status IN ('queued', 'uploading', 'processing')
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [_row(r) for r in rows]


def latest_for_stockpile(user_id: int, stockpile_job_id: str) -> dict[str, Any] | None:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            """
            SELECT * FROM processing_jobs
            WHERE user_id = ? AND stockpile_job_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, stockpile_job_id),
        ).fetchone()
    return _row(row) if row is not None else None


def latest_for_mission(user_id: int, mission_id: str) -> dict[str, Any] | None:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            """
            SELECT * FROM processing_jobs
            WHERE user_id = ? AND mission_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, mission_id),
        ).fetchone()
    return _row(row) if row is not None else None


def changes_since(user_id: int, since: float | None) -> list[dict[str, Any]]:
    init()
    threshold = float(since) if since is not None else 0.0
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM processing_jobs WHERE user_id = ? AND updated_at >= ? ORDER BY updated_at ASC",
            (user_id, threshold),
        ).fetchall()
    return [public_view(_row(r)) for r in rows]


def latest_update(user_id: int) -> float:
    init()
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT MAX(updated_at) AS t FROM processing_jobs WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return float(row["t"]) if row and row["t"] is not None else 0.0


def _update(job_id: str, user_id: int, **fields: Any) -> dict[str, Any]:
    fields["updated_at"] = _now()
    keys = list(fields.keys())
    assignments = ", ".join(f"{k} = ?" for k in keys)
    values = []
    for k in keys:
        val = fields[k]
        if k in {"options_json", "result_meta", "volumes_json"} and not isinstance(val, str) and val is not None:
            values.append(json.dumps(val))
        else:
            values.append(val)
    conn = _conn()
    with accounts._lock:
        conn.execute(
            f"UPDATE processing_jobs SET {assignments} WHERE id = ? AND user_id = ?",
            (*values, job_id, user_id),
        )
    job = get_job(user_id, job_id)
    assert job is not None
    return job


def credentials_or_raise() -> webodm.Credentials:
    creds = webodm.load_credentials()
    if creds is None:
        raise ProcessingError(
            503,
            "Survey-quality processing is not configured on this server yet.",
        )
    return creds


def _client(creds: webodm.Credentials | None = None, transport=None) -> webodm.WebOdmClient:
    return webodm.WebOdmClient(creds or credentials_or_raise(), transport=transport)


# Optional inject for tests.
_client_factory: Callable[[], webodm.WebOdmClient] | None = None


def set_client_factory(factory: Callable[[], webodm.WebOdmClient] | None) -> None:
    global _client_factory
    _client_factory = factory


def get_client() -> webodm.WebOdmClient:
    if _client_factory is not None:
        return _client_factory()
    return _client()


def _collect_stockpile_photos(user_id: int, stockpile_job_id: str) -> list[tuple[str, Path]]:
    job = stockpile.get_job(user_id, stockpile_job_id, include_photos=True)
    if job is None:
        raise ProcessingError(404, "Unknown pile job.")
    folder = stockpile._jobs_dir(user_id, stockpile_job_id) / "photos"
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT filename, stored_name FROM stockpile_photos WHERE job_id = ? AND user_id = ? ORDER BY created_at",
            (stockpile_job_id, user_id),
        ).fetchall()
    out = []
    for row in rows:
        path = folder / row["stored_name"]
        if path.is_file():
            out.append((row["filename"] or path.name, path))
    return out


def _store_uploaded_photos(user_id: int, job_id: str, files: list[tuple[str, bytes]]) -> list[tuple[str, Path]]:
    folder = _jobs_dir(user_id, job_id) / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (filename, data) in enumerate(files):
        if not data:
            continue
        if len(data) > stockpile.MAX_PHOTO_BYTES:
            raise ProcessingError(400, f"{filename} is over the 25 MiB photo size limit.")
        ext = Path(filename).suffix.lower() or ".jpg"
        if ext not in stockpile.ALLOWED_PHOTO_TYPES:
            ext = ".jpg"
        stored = folder / f"{i:04d}-{uuid.uuid4().hex}{ext}"
        stored.write_bytes(data)
        out.append((filename or stored.name, stored))
    return out


def _uploads_dir(user_id: int, upload_id: str) -> Path:
    path = config.DATA_DIR / "processing-uploads" / str(user_id) / upload_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_manifest_path(user_id: int, upload_id: str) -> Path:
    return _uploads_dir(user_id, upload_id) / "manifest.json"


def _load_upload(user_id: int, upload_id: str) -> dict[str, Any]:
    path = _upload_manifest_path(user_id, upload_id)
    if not path.is_file():
        raise ProcessingError(404, "Upload session not found.")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(500, "Upload session could not be read.") from exc
    if data.get("user_id") != user_id:
        raise ProcessingError(404, "Upload session not found.")
    return data


def _save_upload(user_id: int, manifest: dict[str, Any]) -> None:
    path = _upload_manifest_path(user_id, manifest["id"])
    path.write_text(json.dumps(manifest))


def _public_upload(manifest: dict[str, Any]) -> dict[str, Any]:
    received = [{"name": f["name"], "size": int(f["size"])} for f in manifest.get("files") or []]
    return {"id": manifest["id"], "received": received, "count": len(received)}


def create_upload_session(user: accounts.User) -> dict[str, Any]:
    upload_id = str(uuid.uuid4())
    (_uploads_dir(user.id, upload_id) / "photos").mkdir(parents=True, exist_ok=True)
    manifest = {"id": upload_id, "user_id": user.id, "files": [], "consumed": False}
    _save_upload(user.id, manifest)
    return _public_upload(manifest)


def get_upload_session(user: accounts.User, upload_id: str) -> dict[str, Any]:
    return _public_upload(_load_upload(user.id, upload_id))


def add_files_to_upload(
    user: accounts.User,
    upload_id: str,
    files: list[tuple[str, bytes]],
) -> dict[str, Any]:
    manifest = _load_upload(user.id, upload_id)
    if manifest.get("consumed"):
        raise ProcessingError(409, "This upload session has already been submitted.")
    known = {(f["name"], int(f["size"])) for f in manifest.get("files") or []}
    photos = _uploads_dir(user.id, upload_id) / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    cap = accounts.effective_photo_cap(user)
    incoming_new = 0
    for name, data in files:
        if not data:
            continue
        if (name, len(data)) not in known:
            incoming_new += 1
    projected = len(manifest.get("files") or []) + incoming_new
    if projected > cap:
        raise photolimits.PhotoLimitError(400, photolimits.evaluate(projected, cap)["warning"], "too_many")
    for name, data in files:
        if not data:
            continue
        if len(data) > stockpile.MAX_PHOTO_BYTES:
            raise ProcessingError(400, f"{name} is over the 25 MiB photo size limit.")
        size = len(data)
        if (name, size) in known:
            skipped.append({"name": name, "size": size})
            continue
        ext = Path(name).suffix.lower() or ".jpg"
        if ext not in stockpile.ALLOWED_PHOTO_TYPES:
            ext = ".jpg"
        stored = f"{uuid.uuid4().hex}{ext}"
        (photos / stored).write_bytes(data)
        manifest.setdefault("files", []).append({"name": name, "size": size, "stored": stored})
        known.add((name, size))
        added.append({"name": name, "size": size})
    _save_upload(user.id, manifest)
    out = _public_upload(manifest)
    out["added"] = added
    out["skipped"] = skipped
    return out


def _adopt_upload_photos(user: accounts.User, upload_id: str, dest: Path) -> list[tuple[str, Path]]:
    manifest = _load_upload(user.id, upload_id)
    if manifest.get("consumed"):
        raise ProcessingError(409, "This upload session has already been submitted.")
    recs = list(manifest.get("files") or [])
    if not recs:
        raise ProcessingError(400, "Upload session has no photos.")
    src = _uploads_dir(user.id, upload_id) / "photos"
    dest.mkdir(parents=True, exist_ok=True)
    paths: list[tuple[str, Path]] = []
    for rec in recs:
        stored = rec.get("stored") or ""
        srcp = src / stored
        if not stored or not srcp.is_file():
            raise ProcessingError(400, f"Missing uploaded photo: {rec.get('name') or 'photo'}.")
        target = dest / stored
        srcp.replace(target)
        paths.append((rec["name"], target))
    manifest["consumed"] = True
    _save_upload(user.id, manifest)
    return paths


def _staged_upload_count(user: accounts.User, upload_id: str) -> int:
    return len(_load_upload(user.id, upload_id).get("files") or [])


def submit(
    user: accounts.User,
    *,
    name: str = "Survey processing",
    mission_id: str | None = None,
    stockpile_job_id: str | None = None,
    files: list[tuple[str, bytes]] | None = None,
    upload_id: str | None = None,
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    init()
    credentials_or_raise()
    photo_paths: list[tuple[str, Path]] = []
    if stockpile_job_id:
        photo_paths = _collect_stockpile_photos(user.id, stockpile_job_id)
    if stockpile_job_id and not photo_paths and not files and not upload_id:
        raise ProcessingError(400, "Upload photos to the pile job before processing.")

    # Count photos we will send. Staged upload files are counted before adopt
    # so a short session is rejected without consuming it.
    incoming = [(n, d) for n, d in (files or []) if d]
    staged = _staged_upload_count(user, upload_id) if upload_id else 0
    count = len(photo_paths) + len(incoming) + staged
    cap = accounts.effective_photo_cap(user)
    photolimits.enforce(count, cap)

    job_id = str(uuid.uuid4())
    now = _now()
    title = (name or "Survey processing").strip()[:200] or "Survey processing"
    opts = webodm.public_options(options)
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            INSERT INTO processing_jobs (
              id, user_id, mission_id, stockpile_job_id, name, status, progress,
              progress_pct, options_json, photo_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)
            """,
            (
                job_id,
                user.id,
                mission_id,
                stockpile_job_id,
                title,
                "Queued for survey-quality processing.",
                json.dumps(opts),
                count,
                now,
                now,
            ),
        )
    dest_photos = _jobs_dir(user.id, job_id) / "photos"
    if upload_id:
        photo_paths = list(photo_paths) + _adopt_upload_photos(user, upload_id, dest_photos)
    if incoming:
        stored = _store_uploaded_photos(user.id, job_id, incoming)
        photo_paths = list(photo_paths) + stored
    _sync_stockpile_progress(
        user.id,
        stockpile_job_id,
        "queued",
        "Queued for survey-quality processing.",
        job_id,
    )
    if os.environ.get("FIND_API_WEBODM_SYNC_SUBMIT") == "1":
        _run_submit(user.id, job_id, photo_paths, opts)
    else:
        thread = threading.Thread(
            target=_run_submit,
            args=(user.id, job_id, photo_paths, opts),
            name=f"webodm-submit-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
    job = get_job(user.id, job_id)
    assert job is not None
    return job


def _run_submit(user_id: int, job_id: str, photo_paths: list[tuple[str, Path]], options: list[dict[str, str]]) -> None:
    try:
        _update(job_id, user_id, status="uploading", progress="Uploading photos for survey-quality processing.", started_at=_now())
        client = get_client()
        try:
            client.authenticate()
            project_id = client.ensure_project()
            task = client.create_task(f"dm-{user_id}-{job_id}", options=options, project_id=project_id)
            task_id = str(task["id"])
            _update(
                job_id,
                user_id,
                webodm_project_id=int(task.get("_project_id") or project_id),
                webodm_task_id=task_id,
                progress=f"Uploading {len(photo_paths)} photos…",
            )

            def iter_files():
                for filename, path in photo_paths:
                    yield filename, path.read_bytes()

            total = len(photo_paths)

            def on_progress(done, expected):
                pct = int(round(100 * done / expected)) if expected else 0
                _update(
                    job_id,
                    user_id,
                    status="uploading",
                    progress=f"Uploading photos {done} of {expected} ({pct}%).",
                    progress_pct=pct,
                )
                _sync_stockpile_progress(user_id, None, "uploading", f"Uploading {pct}%", job_id)

            uploaded = client.upload_images(
                project_id, task_id, iter_files(), on_progress=on_progress, total=total
            )
            client.commit(project_id, task_id)
        finally:
            client.close()
        _update(
            job_id,
            user_id,
            status="queued",
            progress=f"Uploaded {uploaded} photos. Waiting in the processing queue.",
            photo_count=uploaded,
            progress_pct=0,
        )
        _sync_stockpile_progress(user_id, None, "queued", "Waiting in the processing queue.", job_id)
    except (webodm.WebOdmError, ProcessingError, OSError, Exception) as exc:
        detail = exc.detail if isinstance(exc, (webodm.WebOdmError, ProcessingError)) else "Processing could not be started."
        _update(job_id, user_id, status="failed", error=str(detail), progress=str(detail), finished_at=_now())
        job = get_job(user_id, job_id)
        if job:
            _sync_stockpile_progress(user_id, job.get("stockpile_job_id"), "failed", str(detail), job_id)


def _sync_stockpile_progress(user_id: int, stockpile_job_id: str | None, status: str, progress: str, process_id: str) -> None:
    if not stockpile_job_id:
        job = get_job(user_id, process_id)
        if job:
            stockpile_job_id = job.get("stockpile_job_id")
    if not stockpile_job_id:
        return
    try:
        existing = stockpile.get_job(user_id, stockpile_job_id)
        if existing is None:
            return
        # Keep the GPS preview (mosaic step) if it already exists.
        step = existing.get("step") or "mosaic"
        if step in {"plan", "pick", "fly"}:
            step = "mosaic"
        if status == "done":
            step = "volume"
        stockpile.update_job(user_id, stockpile_job_id, progress=progress, step=step)
    except Exception:
        return


def tick(job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Poll in-flight WebODM tasks and harvest results when they finish."""
    init()
    conn = _conn()
    with accounts._lock:
        if job is not None:
            rows = [job]
        else:
            raw = conn.execute(
                "SELECT * FROM processing_jobs WHERE status IN ('queued', 'uploading', 'processing') AND webodm_task_id IS NOT NULL"
            ).fetchall()
            rows = [_row(r) for r in raw]
    updated = []
    if not rows:
        return updated
    try:
        client = get_client()
    except ProcessingError:
        return updated
    try:
        for item in rows:
            try:
                refreshed = _tick_one(client, item)
            except (webodm.WebOdmError, ProcessingError, OSError, Exception) as exc:
                detail = exc.detail if isinstance(exc, (webodm.WebOdmError, ProcessingError)) else "Processing failed."
                refreshed = _update(
                    item["id"],
                    item["user_id"],
                    status="failed",
                    error=str(detail),
                    progress=str(detail),
                    finished_at=_now(),
                )
                _sync_stockpile_progress(item["user_id"], item.get("stockpile_job_id"), "failed", str(detail), item["id"])
            updated.append(refreshed)
    finally:
        client.close()
    return updated


def _tick_one(client: webodm.WebOdmClient, job: dict[str, Any]) -> dict[str, Any]:
    if not job.get("webodm_task_id") or not job.get("webodm_project_id"):
        return job
    task = client.get_task(int(job["webodm_project_id"]), str(job["webodm_task_id"]))
    raw_status = task.get("status")
    code, label = webodm.normalize_status(raw_status)
    # A just-committed task can report status=null until a processing node
    # is assigned. That is still queued, not a failure.
    if raw_status is None or label in {"unknown", ""}:
        if task.get("last_error") or task.get("error"):
            label = "failed"
        else:
            label = "queued"
    running_progress = task.get("running_progress") or task.get("progress") or 0
    try:
        pct = int(float(running_progress) * (100 if float(running_progress) <= 1 else 1))
    except (TypeError, ValueError):
        pct = job.get("progress_pct") or 0
    pct = max(0, min(100, pct))
    if label == "processing" or label == "queued":
        odm_stage = None
        if label == "processing":
            try:
                odm_stage = parse_odm_stage(client.get_task_output(int(job["webodm_project_id"]), str(job["webodm_task_id"])))
            except (webodm.WebOdmError, OSError, ValueError, TypeError):
                odm_stage = None
        user_stage = ODM_STAGE_TO_USER.get(odm_stage or "", "") or derive_stage(label, pct, job.get("pipeline_stage"))
        if label == "queued":
            user_stage = "queued"
        end = stage_end_pct(user_stage, odm_stage)
        now = _now()
        stage_started = job.get("stage_started_at")
        if user_stage != (job.get("pipeline_stage") or "") or not stage_started:
            stage_started = now
        shown = display_progress(pct, end, stage_started, now, job.get("progress_pct"))
        elapsed = elapsed_of({**job, "status": label, "started_at": job.get("started_at") or job.get("created_at")}, now)
        if user_stage == "queued":
            msg = "Waiting in the processing queue."
        else:
            msg = f"{STAGE_LABELS.get(user_stage, 'Processing')}… {shown}% · {format_elapsed(elapsed)}"
        updated = _update(
            job["id"],
            job["user_id"],
            status=label,
            progress=msg,
            progress_pct=shown,
            pipeline_stage=user_stage,
            stage_started_at=stage_started,
        )
        _sync_stockpile_progress(job["user_id"], job.get("stockpile_job_id"), label, msg, job["id"])
        return updated
    if label != "done":
        detail = _public_failure(task)
        updated = _update(
            job["id"],
            job["user_id"],
            status="failed",
            error=detail,
            progress=detail,
            progress_pct=pct,
            finished_at=_now(),
        )
        _sync_stockpile_progress(job["user_id"], job.get("stockpile_job_id"), "failed", detail, job["id"])
        return updated
    return _harvest(client, job, task)



def _write_preview_png(tif_path: Path, dest: Path, max_edge: int = 1600) -> bool:
    """Build a PNG preview when WebODM does not offer orthophoto.png."""
    try:
        import tifffile
        from PIL import Image
        arr = tifffile.imread(str(tif_path))
        if arr is None or getattr(arr, "size", 0) == 0:
            return False
        if arr.ndim == 3 and arr.shape[0] in {3, 4} and arr.shape[0] < arr.shape[-1]:
            arr = np.moveaxis(arr, 0, -1)
        if arr.ndim == 2:
            arr = np.stack([arr, arr, arr], axis=-1)
        if arr.dtype != np.uint8:
            a = arr.astype(np.float64)
            finite = a[np.isfinite(a)]
            if finite.size == 0:
                return False
            lo, hi = np.percentile(finite, [2, 98])
            if hi <= lo:
                hi = lo + 1
            a = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
            arr = a
        if arr.ndim == 3 and arr.shape[-1] > 3:
            arr = arr[..., :3]
        img = Image.fromarray(arr)
        img.thumbnail((max_edge, max_edge))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "PNG", optimize=True)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _public_failure(task: dict[str, Any]) -> str:
    # Do not forward raw WebODM logs.
    return "Survey-quality processing failed. You can try again after checking the photos and overlap."


def _harvest(client: webodm.WebOdmClient, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    folder = _jobs_dir(job["user_id"], job["id"])
    has_ortho = False
    has_dsm = False
    for asset, dest in (("orthophoto.png", "orthophoto.png"), ("orthophoto.tif", "orthophoto.tif"), ("dsm.tif", "dsm.tif")):
        try:
            data, _ctype = client.download_asset(int(job["webodm_project_id"]), str(job["webodm_task_id"]), asset)
        except webodm.WebOdmError:
            continue
        if not data:
            continue
        (folder / dest).write_bytes(data)
        if dest.startswith("orthophoto"):
            has_ortho = True
        if dest == "dsm.tif":
            has_dsm = True
    png_path = folder / "orthophoto.png"
    tif_path = folder / "orthophoto.tif"
    if not png_path.is_file() and tif_path.is_file():
        if _write_preview_png(tif_path, png_path):
            has_ortho = True
    bounds = webodm.bounds_from_task(task)
    result = {
        "kind": "survey",
        "has_orthophoto": has_ortho,
        "has_dsm": has_dsm,
        "bounds": bounds,
        "quality": "survey",
    }
    dsm_path = folder / "dsm.tif"
    if dsm_path.is_file():
        try:
            import volume_calc
            import volume_crs
            dsm = volume_calc.load_dsm_geotiff(dsm_path.read_bytes())
            frame = volume_crs.frame_info(dsm)
            result["dsm_frame"] = frame
            result["local_frame"] = bool(frame.get("local_frame"))
            if frame.get("local_frame"):
                # Do not present a misleading lon/lat extent for a local-meter DSM.
                result["bounds_reported"] = bounds
                result["bounds"] = {
                    "west": frame["west"],
                    "south": frame["south"],
                    "east": frame["east"],
                    "north": frame["north"],
                }
                result["crs"] = "local-meters"
        except Exception:
            pass
    _update(
        job["id"],
        job["user_id"],
        status="processing",
        progress="Computing volume… 99%",
        progress_pct=99,
        result_meta=result,
        pipeline_stage="computing_volume",
        stage_started_at=_now(),
    )
    upgraded = get_job(job["user_id"], job["id"]) or job
    upgraded["result"] = result
    _apply_stockpile_upgrade(upgraded)
    updated = _update(
        job["id"],
        job["user_id"],
        status="done",
        progress="Survey-quality map is ready.",
        progress_pct=100,
        result_meta=result,
        pipeline_stage="done",
        finished_at=_now(),
    )
    return get_job(job["user_id"], job["id"]) or updated


def _apply_stockpile_upgrade(job: dict[str, Any]) -> None:
    stockpile_job_id = job.get("stockpile_job_id")
    if not stockpile_job_id:
        return
    user_id = job["user_id"]
    dest_dir = stockpile._jobs_dir(user_id, stockpile_job_id)
    src_dir = _jobs_dir(user_id, job["id"])
    # Keep the GPS preview.jpg as-is; copy survey products alongside it.
    for name in ("orthophoto.png", "orthophoto.tif", "dsm.tif"):
        src = src_dir / name
        if src.is_file():
            (dest_dir / name).write_bytes(src.read_bytes())
    existing = stockpile.get_job(user_id, stockpile_job_id)
    meta = dict(existing.get("mosaic") or {}) if existing else {}
    bounds = (job.get("result") or {}).get("bounds") or {}
    if bounds:
        meta.update(bounds)
    meta["kind"] = "survey"
    meta["quality"] = "survey"
    meta["processing_id"] = job["id"]
    # preview endpoint stays on the GPS stitch; a new survey preview is served separately.
    now = _now()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            UPDATE stockpile_jobs
            SET mosaic_meta = ?, step = 'volume', progress = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                json.dumps(meta),
                "Survey-quality map is ready. Volumes will use the processed DSM.",
                now,
                stockpile_job_id,
                user_id,
            ),
        )
    if (src_dir / "dsm.tif").is_file():
        try:
            # Use a dummy user-like object only for id; compute_volumes looks up by user.id
            class _U:
                def __init__(self, i):
                    self.id = i

            stockpile.compute_volumes(_U(user_id), stockpile_job_id, None)
        except Exception:
            # Volumes can be computed later once piles/rings are marked.
            pass
    _sync_stockpile_progress(user_id, stockpile_job_id, "done", "Survey-quality map is ready.", job["id"])


def asset_bytes(user_id: int, job_id: str, name: str) -> tuple[bytes, str]:
    if name not in {"orthophoto.png", "orthophoto.tif", "dsm.tif"}:
        raise ProcessingError(404, "Unknown asset.")
    job = _require_job(user_id, job_id)
    path = _jobs_dir(user_id, job_id) / name
    if path.is_file():
        ctype = "image/png" if name.endswith(".png") else "image/tiff"
        return path.read_bytes(), ctype
    # Fall back to a live proxy if harvest has not written the file yet.
    if not job.get("webodm_task_id") or job.get("status") != "done":
        raise ProcessingError(404, "That result is not ready yet.")
    try:
        client = get_client()
        try:
            data, ctype = client.download_asset(int(job["webodm_project_id"]), str(job["webodm_task_id"]), name)
        finally:
            client.close()
    except webodm.WebOdmError as exc:
        raise ProcessingError(exc.status, exc.detail) from exc
    path.write_bytes(data)
    return data, ctype


def tile_png(user_id: int, job_id: str, z: int, x: int, y: int) -> bytes:
    job = _require_job(user_id, job_id)
    if not job.get("webodm_task_id") or job.get("status") not in {"done", "processing"}:
        raise ProcessingError(404, "Tiles are not ready yet.")
    try:
        client = get_client()
        try:
            return client.tile_png(int(job["webodm_project_id"]), str(job["webodm_task_id"]), z, x, y)
        finally:
            client.close()
    except webodm.WebOdmError as exc:
        raise ProcessingError(exc.status, exc.detail) from exc


def compute_volumes(user: accounts.User, job_id: str, density: float | None = None, rings: list[dict] | None = None) -> dict[str, Any]:
    job = _require_job(user.id, job_id)
    dsm_path = _jobs_dir(user.id, job_id) / "dsm.tif"
    if not dsm_path.is_file():
        # Maybe harvest just finished on the stockpile side.
        if job.get("stockpile_job_id"):
            alt = stockpile._jobs_dir(user.id, job["stockpile_job_id"]) / "dsm.tif"
            if alt.is_file():
                dsm_path = alt
    if not dsm_path.is_file():
        raise ProcessingError(400, "The survey DSM is not ready yet.")
    dsm = volume_calc.load_dsm_geotiff(dsm_path.read_bytes())
    dens = density if density and density > 0 else volume_calc.DEFAULT_DENSITY_T_PER_M3
    piles = rings
    if not piles and job.get("stockpile_job_id"):
        sp = stockpile.get_job(user.id, job["stockpile_job_id"])
        piles = (sp or {}).get("piles") or []
    if not piles:
        raise ProcessingError(400, "Mark at least one pile (or draw a base ring) before computing volume.")
    results = []
    import volume_crs
    frame = volume_crs.frame_info(dsm)
    crs_errors = 0
    for i, pile in enumerate(piles):
        try:
            metric = volume_crs.prepare_ring(dsm, pile)
        except volume_crs.RingError as exc:
            crs_errors += 1
            results.append(
                {
                    "id": (pile.get("id") if isinstance(pile, dict) else None) or f"pile-{i+1}",
                    "name": (pile.get("name") if isinstance(pile, dict) else None) or f"Pile {i+1}",
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue
        try:
            vol = volume_calc.measure_polygon(dsm, metric, dens)
            if vol.get("area_m2", 0) <= 0:
                crs_errors += 1
                results.append(
                    {
                        "id": (pile.get("id") if isinstance(pile, dict) else None) or f"pile-{i+1}",
                        "name": (pile.get("name") if isinstance(pile, dict) else None) or f"Pile {i+1}",
                        "ok": False,
                        "error": volume_crs.NO_OVERLAP_MESSAGE,
                    }
                )
                continue
            results.append(
                {
                    "id": pile.get("id") or f"pile-{i+1}",
                    "name": pile.get("name") or f"Pile {i+1}",
                    "fill_m3": vol["fill_m3"],
                    "cut_m3": vol["cut_m3"],
                    "net_m3": vol["net_m3"],
                    "area_m2": vol["area_m2"],
                    "tons": vol["tons"],
                    "density_t_per_m3": vol["density_t_per_m3"],
                    "ok": True,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": pile.get("id") or f"pile-{i+1}",
                    "name": pile.get("name") or f"Pile {i+1}",
                    "ok": False,
                    "error": "Could not measure that pile on the survey DSM.",
                }
            )
    if piles and crs_errors == len(piles) and not any(r.get("ok") for r in results):
        raise ProcessingError(400, results[0].get("error") or volume_crs.NO_OVERLAP_MESSAGE)
    payload = {"density_t_per_m3": dens, "piles": results, "computed_at": _now(), "source": "survey", "dsm_frame": frame}
    _update(job_id, user.id, volumes_json=payload, progress="Computed survey-quality volumes.")
    if job.get("stockpile_job_id"):
        conn = _conn()
        with accounts._lock:
            conn.execute(
                "UPDATE stockpile_jobs SET volumes_json = ?, step = 'volume', progress = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (
                    json.dumps(payload),
                    f"Computed survey-quality volume for {sum(1 for r in results if r.get('ok'))} of {len(results)} piles.",
                    _now(),
                    job["stockpile_job_id"],
                    user.id,
                ),
            )
    return get_job(user.id, job_id)


_poller_stop = threading.Event()
_poller_thread: threading.Thread | None = None


def start_poller() -> Callable[[], None]:
    global _poller_thread
    if os.environ.get("FIND_API_DISABLE_WEBODM_POLLER") == "1":
        return lambda: None
    if webodm.load_credentials() is None:
        return lambda: None
    _poller_stop.clear()

    def loop():
        while not _poller_stop.wait(POLL_SECONDS):
            try:
                tick()
            except Exception:
                continue

    _poller_thread = threading.Thread(target=loop, name="webodm-poller", daemon=True)
    _poller_thread.start()

    def stop() -> None:
        _poller_stop.set()

    return stop


def register(app: FastAPI, require_mosaic: RequireMosaic, require_csrf: RequireCsrf) -> None:
    def _err(exc: Exception) -> HTTPException:
        if isinstance(exc, (ProcessingError, webodm.WebOdmError, photolimits.PhotoLimitError)):
            return HTTPException(status_code=exc.status, detail=exc.detail)
        return HTTPException(status_code=500, detail="Processing failed.")

    def _user(request: Request):
        user, session = require_mosaic(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, session)
        return user

    @app.get("/process")
    def api_list(request: Request):
        user = _user(request)
        return {"jobs": [public_view(j) for j in list_jobs(user.id)]}

    @app.post("/process", status_code=201)
    async def api_submit(request: Request):
        user = _user(request)
        content_type = request.headers.get("content-type") or ""
        files: list[tuple[str, bytes]] = []
        mission_id = None
        stockpile_job_id = None
        name = "Survey processing"
        upload_id = None
        if "multipart/form-data" in content_type:
            form = await request.form()
            mission_id = _opt_str(form.get("mission_id"))
            stockpile_job_id = _opt_str(form.get("stockpile_job_id"))
            name = _opt_str(form.get("name")) or name
            upload_id = _opt_str(form.get("upload_id"))
            for item in form.getlist("files") + form.getlist("images"):
                if hasattr(item, "read"):
                    data = await item.read()
                    files.append((getattr(item, "filename", None) or "photo.jpg", data))
        else:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            mission_id = _opt_str(payload.get("mission_id"))
            stockpile_job_id = _opt_str(payload.get("stockpile_job_id"))
            name = _opt_str(payload.get("name")) or name
            upload_id = _opt_str(payload.get("upload_id"))
        try:
            job = submit(
                user,
                name=name,
                mission_id=mission_id,
                stockpile_job_id=stockpile_job_id,
                files=files or None,
                upload_id=upload_id,
            )
        except (ProcessingError, photolimits.PhotoLimitError, webodm.WebOdmError) as exc:
            raise _err(exc) from exc
        return public_view(job)

    @app.post("/process/uploads", status_code=201)
    def api_create_upload(request: Request):
        user = _user(request)
        try:
            return create_upload_session(user)
        except (ProcessingError, photolimits.PhotoLimitError, webodm.WebOdmError) as exc:
            raise _err(exc) from exc

    @app.get("/process/uploads/{upload_id}")
    def api_get_upload(upload_id: str, request: Request):
        user = _user(request)
        try:
            return get_upload_session(user, upload_id)
        except ProcessingError as exc:
            raise _err(exc) from exc

    @app.post("/process/uploads/{upload_id}/files")
    async def api_add_upload_files(upload_id: str, request: Request):
        user = _user(request)
        incoming: list[tuple[str, bytes]] = []
        try:
            form = await request.form()
        except Exception:
            form = None
        if form is not None:
            for item in form.getlist("files") + form.getlist("file") + form.getlist("images"):
                if hasattr(item, "read"):
                    data = await item.read()
                    incoming.append((getattr(item, "filename", None) or "photo.jpg", data))
        if not incoming:
            raise HTTPException(status_code=400, detail="Choose a photo to upload.")
        try:
            return add_files_to_upload(user, upload_id, incoming)
        except (ProcessingError, photolimits.PhotoLimitError) as exc:
            raise _err(exc) from exc

    @app.get("/process/{job_id}")
    def api_get(job_id: str, request: Request):
        user = _user(request)
        try:
            job = _require_job(user.id, job_id)
        except ProcessingError as exc:
            raise _err(exc) from exc
        return public_view(job)

    @app.post("/process/{job_id}/refresh")
    def api_refresh(job_id: str, request: Request):
        user = _user(request)
        try:
            job = _require_job(user.id, job_id)
            tick(job)
            return public_view(_require_job(user.id, job_id))
        except (ProcessingError, webodm.WebOdmError) as exc:
            raise _err(exc) from exc

    @app.get("/process/{job_id}/orthophoto.png")
    def api_ortho_png(job_id: str, request: Request):
        user = _user(request)
        try:
            data, ctype = asset_bytes(user.id, job_id, "orthophoto.png")
        except ProcessingError as exc:
            raise _err(exc) from exc
        return Response(data, media_type=ctype)

    @app.get("/process/{job_id}/orthophoto.tif")
    def api_ortho_tif(job_id: str, request: Request):
        user = _user(request)
        try:
            data, ctype = asset_bytes(user.id, job_id, "orthophoto.tif")
        except ProcessingError as exc:
            raise _err(exc) from exc
        return Response(data, media_type=ctype, headers={"Content-Disposition": 'attachment; filename="orthophoto.tif"'})

    @app.get("/process/{job_id}/dsm.tif")
    def api_dsm(job_id: str, request: Request):
        user = _user(request)
        try:
            data, ctype = asset_bytes(user.id, job_id, "dsm.tif")
        except ProcessingError as exc:
            raise _err(exc) from exc
        return Response(data, media_type=ctype, headers={"Content-Disposition": 'attachment; filename="dsm.tif"'})

    @app.get("/process/{job_id}/tiles/{z}/{x}/{y}.png")
    def api_tile(job_id: str, z: int, x: int, y: int, request: Request):
        user = _user(request)
        try:
            data = tile_png(user.id, job_id, z, x, y)
        except ProcessingError as exc:
            raise _err(exc) from exc
        return Response(data, media_type="image/png")

    @app.post("/process/{job_id}/volume")
    async def api_volume(job_id: str, request: Request):
        user = _user(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        density = payload.get("density_t_per_m3") or payload.get("density")
        rings = payload.get("piles") or payload.get("rings")
        try:
            job = compute_volumes(
                user,
                job_id,
                float(density) if density is not None else None,
                rings if isinstance(rings, list) else None,
            )
        except (ProcessingError, TypeError, ValueError) as exc:
            raise _err(exc) from exc
        return public_view(job)

    @app.post("/stockpile/jobs/{job_id}/process", status_code=201)
    async def api_stockpile_process(job_id: str, request: Request):
        user = _user(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        sp = stockpile.get_job(user.id, job_id)
        if sp is None:
            raise HTTPException(status_code=404, detail="Unknown pile job.")
        try:
            job = submit(
                user,
                name=payload.get("name") or sp.get("name") or "Stockpile processing",
                mission_id=sp.get("mission_id"),
                stockpile_job_id=job_id,
            )
        except (ProcessingError, photolimits.PhotoLimitError, webodm.WebOdmError) as exc:
            raise _err(exc) from exc
        out = stockpile.get_job(user.id, job_id, include_photos=True) or {}
        out["processing"] = public_view(job)
        return out


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
