"""Drone Locator: turn search-flight photos into a search map.

Flow (all Drone Locator side; never touches survey-processing jobs or the
WebODM project):

  POST /searchmaps                      -> new job (status "collecting")
  POST /searchmaps/{id}/photos          -> add a batch of JPEGs (resized to
                                           SEARCHMAP_MAX_EDGE, EXIF/XMP kept)
  POST /searchmaps/{id}/start           -> submit to NodeODX as a light
                                           fast-orthophoto task
  GET  /searchmaps/{id}                 -> status + progress for the page
  POST /searchmaps/{id}/cancel          -> stop and discard the job

When the stitch finishes, the orthophoto is reprojected to lat/lon, capped at
SEARCHMAP_MAX_DIM px, saved as a normal finder mosaic (same 2 GiB cap and 12 h
TTL as uploaded maps) and the job's photos are deleted right away (Callin's
disk rule, Nov 14 2026). On failure the photos are kept so the user can retry;
unfinished or failed jobs expire after SEARCHMAP_TTL like other locator data.

Job rows live in the Drone Locator database (locator.sqlite), staging in
DATA_DIR/searchmaps/<user>/<job>/ (excluded from the mosaic cap and cleanup).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, UploadFile
from PIL import Image, ImageOps

import config
import locatorstore
import webodm  # only for assert_internal_base (no WebODM calls are made here)

NODE_URL = os.environ.get("SEARCHMAP_NODE_URL", "http://172.18.0.3:3000").rstrip("/")
MAX_PHOTOS = int(os.environ.get("SEARCHMAP_MAX_PHOTOS", "200"))
MIN_PHOTOS = int(os.environ.get("SEARCHMAP_MIN_PHOTOS", "5"))
MAX_EDGE = int(os.environ.get("SEARCHMAP_MAX_EDGE", "2048"))
MAX_INPUT_BYTES = int(os.environ.get("SEARCHMAP_MAX_INPUT", str(40 * 1024 * 1024)))
STAGING_CAP = int(os.environ.get("SEARCHMAP_STAGING_CAP", str(1536 * 1024 * 1024)))
MIN_FREE_DISK = int(os.environ.get("SEARCHMAP_MIN_FREE_DISK", str(6 * 1024 * 1024 * 1024)))
TTL_SECONDS = int(os.environ.get("SEARCHMAP_TTL", str(12 * 3600)))
MAX_DIM = int(os.environ.get("SEARCHMAP_MAX_DIM", "8000"))
MIN_RES_CM = float(os.environ.get("SEARCHMAP_MIN_RES_CM", "2.0"))
ODM_CONCURRENCY = int(os.environ.get("SEARCHMAP_CONCURRENCY", "4"))
JOB_TIMEOUT = int(os.environ.get("SEARCHMAP_TIMEOUT", str(2 * 3600)))
POLL_SECONDS = float(os.environ.get("SEARCHMAP_POLL", "5"))
UPLOAD_BATCH = 20

ACTIVE = ("collecting", "submitting", "queued", "processing", "finishing")
STAGE_TEXT = {
    "collecting": "Adding photos",
    "submitting": "Sending photos to the map builder",
    "queued": "Waiting for the map builder (another map is being made)",
    "matching_photos": "Matching photos",
    "making_map": "Stitching the map",
    "finishing": "Loading the map",
    "done": "Success",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
# NodeODX stage lines -> (our stage, percent at the END of that stage).
ODM_STAGES = [
    ("dataset", "matching_photos", 8),
    ("split", "matching_photos", 8),
    ("merge", "matching_photos", 8),
    ("opensfm", "matching_photos", 60),
    ("openmvs", "making_map", 62),
    ("odm_filterpoints", "making_map", 64),
    ("odm_meshing", "making_map", 72),
    ("mvs_texturing", "making_map", 80),
    ("odm_georeferencing", "making_map", 84),
    ("odm_dem", "making_map", 86),
    ("odm_orthophoto", "making_map", 95),
    ("odm_report", "making_map", 96),
    ("odm_postprocess", "making_map", 97),
]
_STAGE_RE = re.compile(r"Running\s+([A-Za-z0-9_]+)\s+stage", re.I)

_lock = threading.RLock()
_poller_stop = threading.Event()
_client_factory: Optional[Callable[[], httpx.Client]] = None


class SearchMapError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- storage

def _conn():
    return locatorstore._connect()


def _q(sql: str, params: tuple = (), many: bool = True):
    """Run one statement on the shared locator connection under its lock."""
    with locatorstore._lock:
        cur = _conn().execute(sql, params)
        return cur.fetchall() if many else cur.fetchone()


def init() -> None:
    with locatorstore._lock:
        _conn().executescript(
            """
        CREATE TABLE IF NOT EXISTS searchmap_jobs (
          id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          stage TEXT,
          progress_pct INTEGER NOT NULL DEFAULT 0,
          photo_count INTEGER NOT NULL DEFAULT 0,
          gps_count INTEGER NOT NULL DEFAULT 0,
          staged_bytes INTEGER NOT NULL DEFAULT 0,
          skipped_json TEXT,
          node_uuid TEXT,
          options_json TEXT,
          extent_json TEXT,
          mosaic_id TEXT,
          error TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          started_at REAL,
          finished_at REAL,
          photos_deleted_at REAL,
          stage_started_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_searchmap_user ON searchmap_jobs(user_id, created_at);
        """
    )
    root().mkdir(parents=True, exist_ok=True)


def root() -> Path:
    return Path(config.DATA_DIR) / "searchmaps"


def _job_dir(user_id: int, job_id: str) -> Path:
    return root() / str(int(user_id)) / job_id


def _photos_dir(user_id: int, job_id: str) -> Path:
    return _job_dir(user_id, job_id) / "photos"


def _now() -> float:
    return time.time()


def _row(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("skipped_json", "options_json", "extent_json"):
        try:
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        except Exception:
            d[k[:-5]] = None
    return d


def _get(job_id: str) -> dict[str, Any] | None:
    r = _q("SELECT * FROM searchmap_jobs WHERE id = ?", (job_id,), many=False)
    return _row(r) if r else None


def _update(job_id: str, **fields: Any) -> dict[str, Any]:
    fields["updated_at"] = _now()
    for k in list(fields):
        if k in ("skipped", "options", "extent"):
            fields[k + "_json"] = json.dumps(fields.pop(k))
    cols = ", ".join(f"{k} = ?" for k in fields)
    _q(f"UPDATE searchmap_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
    return _get(job_id) or {}


def _owned(user_id: int, job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        raise SearchMapError(404, "That map job was not found.")
    job = _get(job_id)
    if not job or int(job["user_id"]) != int(user_id):
        raise SearchMapError(404, "That map job was not found.")
    return job


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def staging_bytes() -> int:
    return _dir_bytes(root())


def delete_photos(job: dict[str, Any]) -> None:
    """Remove a job's staged photos (never anything outside its own folder)."""
    d = _photos_dir(job["user_id"], job["id"])
    base = root().resolve()
    if d.exists() and base in d.resolve().parents:
        shutil.rmtree(d, ignore_errors=True)


def public_view(job: dict[str, Any]) -> dict[str, Any]:
    stage = job.get("stage") or job.get("status")
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": stage,
        "stage_text": STAGE_TEXT.get(stage, STAGE_TEXT.get(job["status"], "Working")),
        "progress_pct": int(job.get("progress_pct") or 0),
        "photo_count": int(job.get("photo_count") or 0),
        "gps_count": int(job.get("gps_count") or 0),
        "skipped": job.get("skipped") or [],
        "mosaic_id": job.get("mosaic_id"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "elapsed_s": int(((job.get("finished_at") or _now()) - (job.get("started_at") or _now()))) if job.get("started_at") else 0,
        "photos_deleted": bool(job.get("photos_deleted_at")),
        "can_retry": job["status"] == "failed" and not job.get("photos_deleted_at") and int(job.get("gps_count") or 0) >= MIN_PHOTOS,
        "min_photos": MIN_PHOTOS,
        "max_photos": MAX_PHOTOS,
    }


# ---------------------------------------------------------------- photos

def _gps_of(img: Image.Image) -> tuple[float, float, float | None] | None:
    try:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)
    except Exception:
        return None
    if not gps or 2 not in gps or 4 not in gps:
        return None

    def dms(v) -> float:
        return float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0

    try:
        lat = dms(gps[2]) * (-1 if str(gps.get(1, "N")).upper().startswith("S") else 1)
        lon = dms(gps[4]) * (-1 if str(gps.get(3, "E")).upper().startswith("W") else 1)
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon, None


_REL_ALT = re.compile(rb'RelativeAltitude\s*=\s*"([-+0-9.]+)"|RelativeAltitude>\s*([-+0-9.]+)\s*<')
_PITCH = re.compile(rb'GimbalPitchDegree\s*=\s*"([-+0-9.]+)"|GimbalPitchDegree>\s*([-+0-9.]+)\s*<')


def _xmp_float(rx: re.Pattern, head: bytes) -> float | None:
    m = rx.search(head)
    if not m:
        return None
    try:
        return float(m.group(1) or m.group(2))
    except (TypeError, ValueError):
        return None


def prepare_photo(data: bytes, max_edge: int = MAX_EDGE) -> tuple[bytes, dict[str, Any]]:
    """Validate one drone JPEG; downscale to max_edge keeping EXIF (GPS, camera) and XMP."""
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("not a JPEG photo")
    head = data[:262144]
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        w, h = im.size
        if w * h > config.MAX_DECODE_PIXELS:
            raise ValueError("photo has too many pixels")
        gps = _gps_of(im)
        exif_bytes = im.info.get("exif")
        xmp = im.info.get("xmp")
        info = {
            "width": w,
            "height": h,
            "lat": gps[0] if gps else None,
            "lon": gps[1] if gps else None,
            "rel_alt_m": _xmp_float(_REL_ALT, head),
            "pitch_deg": _xmp_float(_PITCH, head),
        }
        if max(w, h) <= max_edge:
            return data, info
        scale = max_edge / float(max(w, h))
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        small = im.convert("RGB").resize((nw, nh), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        kwargs: dict[str, Any] = {"quality": 90, "subsampling": 0}
        if exif_bytes:
            kwargs["exif"] = exif_bytes
        if xmp:
            kwargs["xmp"] = xmp if isinstance(xmp, (bytes, bytearray)) else str(xmp).encode("utf-8")
        try:
            small.save(out, "JPEG", **kwargs)
        except TypeError:  # very old Pillow without xmp=
            kwargs.pop("xmp", None)
            small.save(out, "JPEG", **kwargs)
        info["width"], info["height"] = nw, nh
        return out.getvalue(), info


def _safe_name(name: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name or "").stem)[:60] or "photo"
    return f"{index:04d}_{stem}.jpg"


def _manifest_path(job: dict[str, Any]) -> Path:
    return _job_dir(job["user_id"], job["id"]) / "photos.json"


def _load_manifest(job: dict[str, Any]) -> list[dict[str, Any]]:
    p = _manifest_path(job)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_manifest(job: dict[str, Any], items: list[dict[str, Any]]) -> None:
    p = _manifest_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(p)


# ---------------------------------------------------------------- jobs

def create_job(user_id: int) -> dict[str, Any]:
    cleanup()
    with _lock:
        busy = _q(
            "SELECT id, status FROM searchmap_jobs WHERE user_id = ? AND status IN (%s)" % ",".join("?" * len(ACTIVE)),
            (int(user_id), *ACTIVE),
        )
        for r in busy:
            if r["status"] == "collecting":
                # An abandoned upload: start over instead of blocking the user.
                job = _get(r["id"])
                if job:
                    delete_photos(job)
                    _update(job["id"], status="cancelled", stage="cancelled", finished_at=_now(), photos_deleted_at=_now())
            else:
                raise SearchMapError(409, "A search map is already being made for your account. Wait for it to finish.")
        job_id = uuid.uuid4().hex
        now = _now()
        _q(
            "INSERT INTO searchmap_jobs (id, user_id, status, stage, created_at, updated_at) VALUES (?, ?, 'collecting', 'collecting', ?, ?)",
            (job_id, int(user_id), now, now),
        )
    _photos_dir(user_id, job_id).mkdir(parents=True, exist_ok=True)
    return _get(job_id)  # type: ignore[return-value]


def add_photos(user_id: int, job_id: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    job = _owned(user_id, job_id)
    if job["status"] not in ("collecting", "failed"):
        raise SearchMapError(409, "This map is already being made. Start a new one to add photos.")
    if job.get("photos_deleted_at"):
        raise SearchMapError(409, "This map job is closed. Start a new one.")
    items = _load_manifest(job)
    skipped = list(job.get("skipped") or [])
    if len(items) + len(files) > MAX_PHOTOS:
        raise SearchMapError(400, f"Up to {MAX_PHOTOS} photos per map. Pick fewer photos, or make two maps.")
    if shutil.disk_usage(str(root())).free < MIN_FREE_DISK or staging_bytes() > STAGING_CAP:
        raise SearchMapError(507, "The server is short on space for photos right now. Try again later.")
    pdir = _photos_dir(user_id, job_id)
    pdir.mkdir(parents=True, exist_ok=True)
    added = 0
    for name, data in files:
        if len(data) > MAX_INPUT_BYTES:
            skipped.append({"name": name, "reason": "too large"})
            continue
        try:
            out, info = prepare_photo(data)
        except Exception:
            skipped.append({"name": name, "reason": "not a drone photo (JPEG) we can read"})
            continue
        if info["lat"] is None:
            skipped.append({"name": name, "reason": "no GPS location in the photo"})
            continue
        fname = _safe_name(name, len(items) + 1)
        (pdir / fname).write_bytes(out)
        items.append({"file": fname, "name": name, **info, "bytes": len(out)})
        added += 1
    _save_manifest(job, items)
    job = _update(
        job_id,
        photo_count=len(items),
        gps_count=sum(1 for i in items if i.get("lat") is not None),
        staged_bytes=sum(int(i.get("bytes") or 0) for i in items),
        skipped=skipped[-50:],
    )
    view = public_view(job)
    view["added"] = added
    return view


def plan_options(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Light fast-orthophoto settings plus an output resolution that keeps the map <= MAX_DIM px."""
    lats = [i["lat"] for i in items if i.get("lat") is not None]
    lons = [i["lon"] for i in items if i.get("lon") is not None]
    alts = [i["rel_alt_m"] for i in items if i.get("rel_alt_m")]
    alt = float(np.median(alts)) if alts else 60.0
    alt = min(max(alt, 5.0), 500.0)
    lat0 = float(np.mean(lats))
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(0.05, float(np.cos(np.radians(lat0))))
    # Photo footprint margin: a wide drone lens sees about 1.4 x height across.
    margin = 0.75 * alt
    ext_ns = (max(lats) - min(lats)) * m_per_deg_lat + 2 * margin
    ext_ew = (max(lons) - min(lons)) * m_per_deg_lon + 2 * margin
    longest = max(ext_ns, ext_ew)
    widths = [max(int(i.get("width") or 0), int(i.get("height") or 0)) for i in items]
    px = float(np.median(widths)) if widths else float(MAX_EDGE)
    native_cm = (1.4 * alt / max(px, 1.0)) * 100.0
    res_cm = max(MIN_RES_CM, native_cm, longest * 100.0 / MAX_DIM)
    res_cm = round(res_cm + 0.05, 1)
    options = [
        {"name": "fast-orthophoto", "value": True},
        {"name": "skip-3dmodel", "value": True},
        {"name": "skip-report", "value": True},
        {"name": "feature-quality", "value": "high"},  # on 2048 px photos = 1024 px features
        {"name": "min-num-features", "value": 8000},
        {"name": "pc-quality", "value": "lowest"},
        {"name": "orthophoto-resolution", "value": res_cm},
        {"name": "max-concurrency", "value": ODM_CONCURRENCY},
        {"name": "optimize-disk-space", "value": True},
    ]
    extent = {
        "south": min(lats), "north": max(lats), "west": min(lons), "east": max(lons),
        "alt_m": round(alt, 1), "extent_m": round(longest, 1), "res_cm": res_cm,
        "expected_px": int(longest * 100.0 / res_cm),
    }
    return options, extent


def start_job(user_id: int, job_id: str) -> dict[str, Any]:
    job = _owned(user_id, job_id)
    if job["status"] not in ("collecting", "failed"):
        raise SearchMapError(409, "This map is already being made.")
    if job.get("photos_deleted_at"):
        raise SearchMapError(409, "This map job is closed. Start a new one.")
    items = [i for i in _load_manifest(job) if i.get("lat") is not None]
    if len(items) < MIN_PHOTOS:
        raise SearchMapError(
            400,
            f"Add at least {MIN_PHOTOS} photos from your search flight (with GPS). We have {len(items)} so far.",
        )
    import store

    store.cleanup()
    if store._mosaic_count(str(user_id)) >= config.MAX_MOSAICS:
        raise SearchMapError(
            409,
            f"You already have {config.MAX_MOSAICS} search maps on the server. Wait for one to expire (12 hours) before making another.",
        )
    options, extent = plan_options(items)
    job = _update(
        job_id, status="submitting", stage="submitting", progress_pct=1, error=None,
        options=options, extent=extent, started_at=_now(), finished_at=None, node_uuid=None,
        stage_started_at=_now(),
    )
    t = threading.Thread(target=_submit, args=(job_id,), name=f"searchmap-submit-{job_id[:8]}", daemon=True)
    t.start()
    return job


def cancel_job(user_id: int, job_id: str) -> dict[str, Any]:
    job = _owned(user_id, job_id)
    if job["status"] in ("done", "cancelled"):
        return job
    if job.get("node_uuid"):
        _node_remove(job["node_uuid"])
    delete_photos(job)
    return _update(job_id, status="cancelled", stage="cancelled", finished_at=_now(), photos_deleted_at=_now())


def get_job(user_id: int, job_id: str) -> dict[str, Any]:
    return _owned(user_id, job_id)


def latest_job(user_id: int) -> dict[str, Any] | None:
    r = _q("SELECT * FROM searchmap_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (int(user_id),), many=False)
    return _row(r) if r else None


# ---------------------------------------------------------------- NodeODX

def set_client_factory(factory: Optional[Callable[[], httpx.Client]]) -> None:
    global _client_factory
    _client_factory = factory


def _client() -> httpx.Client:
    if _client_factory is not None:
        return _client_factory()
    webodm.assert_internal_base(NODE_URL)
    return httpx.Client(base_url=NODE_URL, timeout=httpx.Timeout(60.0, connect=5.0))


def node_info() -> dict[str, Any]:
    with _client() as c:
        r = c.get("/info")
        r.raise_for_status()
        return r.json()


def _node_remove(node_uuid: str) -> None:
    try:
        with _client() as c:
            c.post("/task/cancel", data={"uuid": node_uuid})
            c.post("/task/remove", data={"uuid": node_uuid})
    except Exception:
        pass


def _submit(job_id: str) -> None:
    job = _get(job_id)
    if not job:
        return
    items = [i for i in _load_manifest(job) if i.get("lat") is not None]
    pdir = _photos_dir(job["user_id"], job_id)
    node_uuid = None
    try:
        with _client() as c:
            # NodeODX parses /task/new/init with multer: it must be multipart/form-data,
            # or the options are silently dropped and a full default reconstruction runs.
            r = c.post(
                "/task/new/init",
                files={
                    "name": (None, f"dl-searchmap-{job_id[:12]}"),
                    "options": (None, json.dumps(job.get("options") or [])),
                    "skipPostProcessing": (None, "true"),
                },
            )
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise SearchMapError(502, str(body["error"])[:200])
            node_uuid = body["uuid"]
            _update(job_id, node_uuid=node_uuid)
            sent = 0
            for k in range(0, len(items), UPLOAD_BATCH):
                batch = items[k : k + UPLOAD_BATCH]
                files = [("images", (i["file"], (pdir / i["file"]).read_bytes(), "image/jpeg")) for i in batch]
                r = c.post(f"/task/new/upload/{node_uuid}", files=files, timeout=300.0)
                r.raise_for_status()
                if "error" in (r.json() or {}):
                    raise SearchMapError(502, str(r.json()["error"])[:200])
                sent += len(batch)
                _update(job_id, progress_pct=1 + int(4 * sent / max(1, len(items))))
            r = c.post(f"/task/new/commit/{node_uuid}")
            r.raise_for_status()
            if "error" in (r.json() or {}):
                raise SearchMapError(502, str(r.json()["error"])[:200])
            # The task only exists after commit; make sure the light settings were applied.
            check = c.get(f"/task/{node_uuid}/info")
            applied = {o.get("name"): o.get("value") for o in (check.json() or {}).get("options", [])} if check.status_code == 200 else {}
            if applied.get("fast-orthophoto") not in (True, "true"):
                raise SearchMapError(502, "map builder ignored the light settings")
        _update(job_id, status="queued", stage="queued", progress_pct=5, stage_started_at=_now())
    except Exception as exc:
        if node_uuid:
            _node_remove(node_uuid)
        _fail(job_id, "The map builder could not take the photos. Try again in a few minutes.", exc)


def _fail(job_id: str, message: str, exc: Exception | None = None) -> None:
    detail = message
    _update(job_id, status="failed", stage="failed", error=detail, finished_at=_now(), node_uuid=None)
    if exc is not None:
        try:
            print(f"searchmap {job_id[:8]} failed: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
        except Exception:
            pass


def _friendly_failure(output: str) -> str:
    low = (output or "").lower()
    if "not enough" in low and ("image" in low or "photo" in low):
        return "Not enough of these photos overlap to build a map. Fly the search pattern again with more overlap, or add more photos."
    if "reconstruction" in low and ("0 " in low or "no " in low or "failed" in low):
        return "The photos could not be matched to each other. Photos should point straight down and overlap by about two thirds."
    if "memory" in low or "killed" in low:
        return "The map was too big for the server. Try fewer photos (for example, half the flight at a time)."
    return "The map could not be built from these photos. Photos should point straight down, overlap by about two thirds, and include GPS."


def tick() -> None:
    rows = _q("SELECT * FROM searchmap_jobs WHERE status IN ('queued', 'processing') AND node_uuid IS NOT NULL")
    for r in rows:
        job = _row(r)
        try:
            _tick_one(job)
        except Exception as exc:  # keep polling other jobs
            try:
                print(f"searchmap tick {job['id'][:8]}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            except Exception:
                pass


def _tick_one(job: dict[str, Any]) -> None:
    node_uuid = job["node_uuid"]
    with _client() as c:
        r = c.get(f"/task/{node_uuid}/info")
        if r.status_code == 404 or "error" in (r.json() if r.content else {}):
            _fail(job["id"], "The map builder lost this job. Tap Try again.")
            return
        info = r.json()
        code = int((info.get("status") or {}).get("code") or 0)
        out_lines: list[str] = []
        try:
            o = c.get(f"/task/{node_uuid}/output", params={"line": -60})
            if o.status_code == 200:
                out_lines = [str(x) for x in (o.json() or [])]
        except Exception:
            out_lines = []
    if job.get("started_at") and _now() - float(job["started_at"]) > JOB_TIMEOUT and code in (10, 20):
        _node_remove(node_uuid)
        _fail(job["id"], "Making the map took too long and was stopped. Try fewer photos.")
        return
    if code == 10:
        _update(job["id"], status="queued", stage="queued", progress_pct=5)
        return
    if code == 20:
        console = "\n".join(out_lines)
        odm_stage = None
        for m in _STAGE_RE.finditer(console):
            odm_stage = m.group(1)
        # The console tail may not include a stage line; keep the last known stage then.
        prev_stage = job.get("stage") if job.get("stage") in ("matching_photos", "making_map") else "matching_photos"
        stage, pct = prev_stage, 8
        for name, user_stage, end in ODM_STAGES:
            if name == odm_stage:
                stage, pct = user_stage, end
                break
        node_pct = int(float(info.get("progress") or 0))
        shown = max(int(job.get("progress_pct") or 5), min(97, max(node_pct, 5)))
        if odm_stage:
            # Never claim the end of a stage that is still running.
            prev_end = 5
            for name, _s, end in ODM_STAGES:
                if name == odm_stage:
                    break
                prev_end = end
            shown = max(shown, prev_end)
            shown = min(shown, max(prev_end, pct - 1))
        fields: dict[str, Any] = {"status": "processing", "stage": stage}
        started = job.get("stage_started_at") or _now()
        if stage != job.get("stage"):
            fields["stage_started_at"] = started = _now()
        # Long stages print little; creep toward (never past) the stage end so the bar keeps moving.
        lo, hi = (8, 59) if stage == "matching_photos" else (60, 96)
        frac = 1.0 - 2.0 ** (-max(0.0, _now() - float(started)) / 240.0)
        shown = max(shown, int(lo + (hi - lo) * frac))
        fields["progress_pct"] = min(97, shown)
        _update(job["id"], **fields)
        return
    if code == 30 or code == 50:
        console = "\n".join(out_lines)
        _node_remove(node_uuid)
        _fail(job["id"], _friendly_failure(console) if code == 30 else "The map was cancelled.")
        return
    if code == 40:
        _harvest(job)


def _harvest(job: dict[str, Any]) -> None:
    """Download the orthophoto, store it as a finder mosaic, then drop the photos."""
    import store

    _update(job["id"], status="finishing", stage="finishing", progress_pct=98, stage_started_at=_now())
    work = _job_dir(job["user_id"], job["id"]) / "result"
    work.mkdir(parents=True, exist_ok=True)
    tif = work / "orthophoto.tif"
    try:
        fetch_orthophoto(job["node_uuid"], work, tif)
        jpeg, bounds, size = ortho_to_search_jpeg(tif, MAX_DIM)
        mosaic = store.save_mosaic(
            jpeg,
            f"search-map-{time.strftime('%Y%m%d-%H%M')}.jpg",
            "image/jpeg",
            bounds,
            str(job["user_id"]),
        )
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        msg = "The map was built but could not be loaded. Tap Try again."
        if isinstance(exc, ValueError) and ("At most" in str(exc) or "store is full" in str(exc)):
            msg = "The map was built, but the server has no room for another map right now. Wait for an older map to expire (12 hours), then tap Try again."
        _node_remove(job["node_uuid"])
        _fail(job["id"], msg, exc)
        return
    # Success: photos go right away (users keep their own copies).
    shutil.rmtree(work, ignore_errors=True)
    delete_photos(job)
    _node_remove(job["node_uuid"])
    try:
        _manifest_path(job).unlink(missing_ok=True)
    except Exception:
        pass
    _update(
        job["id"], status="done", stage="done", progress_pct=100, mosaic_id=mosaic.id,
        finished_at=_now(), photos_deleted_at=_now(), node_uuid=None, staged_bytes=0,
    )


ORTHO_IN_ZIP = "odm_orthophoto/odm_orthophoto.tif"


def fetch_orthophoto(node_uuid: str, work: Path, dest: Path) -> None:
    """NodeODX only serves all.zip; stream it to disk and pull out the orthophoto."""
    import zipfile

    zpath = work / "all.zip"
    with _client() as c:
        with c.stream("GET", f"/task/{node_uuid}/download/all.zip", timeout=900.0) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                resp.read()
                raise ValueError(f"map builder said: {resp.text[:120]}")
            with zpath.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            name = ORTHO_IN_ZIP if ORTHO_IN_ZIP in names else next((n for n in names if n.endswith("odm_orthophoto.tif")), None)
            if not name:
                raise ValueError("The map builder did not make an orthophoto.")
            with zf.open(name) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    finally:
        zpath.unlink(missing_ok=True)


def ortho_to_search_jpeg(tif: Path, max_dim: int) -> tuple[bytes, tuple[float, float, float, float], tuple[int, int]]:
    """Reproject an ODM orthophoto to EPSG:4326, crop to the photographed area, cap the size, JPEG it."""
    import cv2
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window

    with rasterio.open(tif) as src:
        with WarpedVRT(src, crs="EPSG:4326", resampling=Resampling.bilinear) as vrt:
            W, H = vrt.width, vrt.height
            # Find the photographed area on a small overview first.
            ov = max(1, int(np.ceil(max(W, H) / 2000)))
            ow, oh = max(1, W // ov), max(1, H // ov)
            if vrt.count >= 4:
                mask = vrt.read(4, out_shape=(oh, ow), resampling=Resampling.nearest) > 0
            else:
                mask = vrt.read_masks(1, out_shape=(oh, ow)) > 0
            ys, xs = np.nonzero(mask)
            if len(xs) == 0:
                raise ValueError("The orthophoto is empty.")
            x0 = max(0, xs.min() * W // ow - 2)
            x1 = min(W, (xs.max() + 1) * W // ow + 2)
            y0 = max(0, ys.min() * H // oh - 2)
            y1 = min(H, (ys.max() + 1) * H // oh + 2)
            win = Window(x0, y0, x1 - x0, y1 - y0)
            cw, ch = int(win.width), int(win.height)
            scale = min(1.0, max_dim / float(max(cw, ch)))
            tw, th = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
            rgb = vrt.read([1, 2, 3], window=win, out_shape=(3, th, tw), resampling=Resampling.average)
            if vrt.count >= 4:
                alpha = vrt.read(4, window=win, out_shape=(th, tw), resampling=Resampling.nearest)
            else:
                alpha = vrt.read_masks(1, window=win, out_shape=(th, tw))
            left, bottom, right, top = rasterio.windows.bounds(win, vrt.transform)
    img = np.transpose(rgb, (1, 2, 0)).astype(np.uint8, copy=False)
    img = np.ascontiguousarray(img)
    img[alpha == 0] = (60, 60, 60)  # neutral gray outside the photographed area
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("Could not encode the map.")
    return buf.tobytes(), (float(left), float(bottom), float(right), float(top)), (tw, th)


# ---------------------------------------------------------------- housekeeping

def cleanup(now: float | None = None) -> int:
    """Expire unfinished or failed jobs after TTL. Done jobs have no photos left."""
    now = now or _now()
    removed = 0
    rows = _q(
        "SELECT * FROM searchmap_jobs WHERE photos_deleted_at IS NULL AND status IN ('collecting', 'failed', 'cancelled') AND updated_at < ?",
        (now - TTL_SECONDS,),
    )
    for r in rows:
        job = _row(r)
        delete_photos(job)
        try:
            _manifest_path(job).unlink(missing_ok=True)
        except Exception:
            pass
        _update(job["id"], photos_deleted_at=now, status="failed" if job["status"] != "cancelled" else "cancelled",
                error=job.get("error") or "Expired before the map was made.")
        removed += 1
    return removed


def recover_on_start() -> None:
    """A restart mid-submit cannot resume an upload; mark it failed so the user can retry."""
    rows = _q("SELECT id FROM searchmap_jobs WHERE status IN ('submitting', 'finishing')")
    for r in rows:
        job = _get(r["id"])
        if job and job.get("node_uuid") and job["status"] == "finishing":
            # Result is still on the node; let the poller harvest it again.
            _update(job["id"], status="processing", stage="making_map")
        else:
            _fail(r["id"], "The server restarted while sending your photos. Tap Try again.")


def start_poller() -> Callable[[], None]:
    if os.environ.get("SEARCHMAP_DISABLE_POLLER") == "1":
        return lambda: None
    _poller_stop.clear()
    try:
        recover_on_start()
    except Exception:
        pass

    def loop():
        last_clean = 0.0
        while not _poller_stop.wait(POLL_SECONDS):
            try:
                tick()
                if _now() - last_clean > 600:
                    cleanup()
                    last_clean = _now()
            except Exception:
                continue

    t = threading.Thread(target=loop, name="searchmap-poller", daemon=True)
    t.start()
    return lambda: _poller_stop.set()


# ---------------------------------------------------------------- routes

def register(app: FastAPI, require_mosaic: Callable[[Request], tuple[Any, Any]]) -> None:
    def _http(exc: SearchMapError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.detail)

    @app.post("/searchmaps", status_code=201)
    def api_create(request: Request):
        user, _s = require_mosaic(request)
        try:
            return public_view(create_job(user.id))
        except SearchMapError as exc:
            raise _http(exc) from exc

    @app.get("/searchmaps/latest")
    def api_latest(request: Request):
        user, _s = require_mosaic(request)
        job = latest_job(user.id)
        return {"job": public_view(job) if job else None}

    @app.get("/searchmaps/{job_id}")
    def api_get(job_id: str, request: Request):
        user, _s = require_mosaic(request)
        try:
            return public_view(get_job(user.id, job_id))
        except SearchMapError as exc:
            raise _http(exc) from exc

    @app.post("/searchmaps/{job_id}/photos")
    async def api_photos(job_id: str, request: Request):
        user, _s = require_mosaic(request)
        form = await request.form(max_files=UPLOAD_BATCH * 2 + 5, max_fields=20)
        uploads = [v for k, v in form.multi_items() if k == "photos" and hasattr(v, "read")]
        if not uploads:
            raise HTTPException(status_code=400, detail="Pick the photos from your search flight.")
        if len(uploads) > UPLOAD_BATCH * 2:
            raise HTTPException(status_code=400, detail=f"Send at most {UPLOAD_BATCH * 2} photos per request.")
        files: list[tuple[str, bytes]] = []
        for up in uploads:
            data = await up.read(MAX_INPUT_BYTES + 1)
            files.append((getattr(up, "filename", None) or "photo.jpg", data))
        import asyncio

        try:
            return await asyncio.to_thread(add_photos, user.id, job_id, files)
        except SearchMapError as exc:
            raise _http(exc) from exc

    @app.post("/searchmaps/{job_id}/start")
    def api_start(job_id: str, request: Request):
        user, _s = require_mosaic(request)
        try:
            return public_view(start_job(user.id, job_id))
        except SearchMapError as exc:
            raise _http(exc) from exc

    @app.post("/searchmaps/{job_id}/cancel")
    def api_cancel(job_id: str, request: Request):
        user, _s = require_mosaic(request)
        try:
            return public_view(cancel_job(user.id, job_id))
        except SearchMapError as exc:
            raise _http(exc) from exc
