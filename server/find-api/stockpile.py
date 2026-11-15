"""Stockpile counting jobs: plan, piles, photo upload, preview mosaic, volume."""

from __future__ import annotations

import io
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import accounts
import config
import photolimits
import volume_calc

RequireLogin = Callable[[Request], tuple[Any, Any]]
RequireCsrf = Callable[[Request, Any], None]
RequireMosaic = Callable[[Request], tuple[Any, Any]]

STEPS = ("plan", "pick", "fly", "mosaic", "volume", "done")
MAX_PILES = 50
MAX_PHOTOS_HARD = photolimits.MAX_PHOTO_CAP
MAX_PHOTO_BYTES = 25 * 1024 * 1024
MAX_DSM_BYTES = 250 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}


class StockpileError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _conn():
    return accounts._connect()


def _jobs_dir(user_id: int, job_id: str) -> Path:
    path = config.DATA_DIR / "stockpile" / str(user_id) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def init() -> None:
    conn = _conn()
    with accounts._lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stockpile_jobs (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              mission_id TEXT,
              name TEXT NOT NULL,
              step TEXT NOT NULL DEFAULT 'plan',
              plan_json TEXT,
              piles_json TEXT NOT NULL DEFAULT '[]',
              photo_count INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'open',
              progress TEXT,
              mosaic_meta TEXT,
              volumes_json TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stockpile_photos (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              user_id INTEGER NOT NULL,
              filename TEXT NOT NULL,
              stored_name TEXT NOT NULL,
              size INTEGER NOT NULL,
              lat REAL,
              lon REAL,
              alt_m REAL,
              created_at REAL NOT NULL,
              FOREIGN KEY(job_id) REFERENCES stockpile_jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_stockpile_jobs_user ON stockpile_jobs(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_stockpile_photos_job ON stockpile_photos(job_id);
            """
        )


def _now() -> float:
    return time.time()


def _row_job(row, photos: list[dict] | None = None) -> dict[str, Any]:
    def parse(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    item = {
        "id": row["id"],
        "mission_id": row["mission_id"],
        "name": row["name"],
        "step": row["step"],
        "plan": parse(row["plan_json"], None),
        "piles": parse(row["piles_json"], []),
        "photo_count": int(row["photo_count"]),
        "status": row["status"],
        "progress": row["progress"],
        "mosaic": parse(row["mosaic_meta"], None),
        "volumes": parse(row["volumes_json"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if photos is not None:
        item["photos"] = photos
    try:
        user_id = int(row["user_id"])
        dsm_path = config.DATA_DIR / "stockpile" / str(user_id) / row["id"] / "dsm.tif"
        if dsm_path.is_file():
            import volume_crs
            dsm = volume_calc.load_dsm_geotiff(dsm_path.read_bytes())
            item["dsm_frame"] = volume_crs.frame_info(dsm)
    except Exception:
        pass
    return item


def get_job(user_id: int, job_id: str, include_photos: bool = False) -> dict[str, Any] | None:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM stockpile_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
        if row is None:
            return None
        photos = None
        if include_photos:
            photos = [
                _row_photo(p)
                for p in conn.execute(
                    "SELECT * FROM stockpile_photos WHERE job_id = ? ORDER BY created_at",
                    (job_id,),
                ).fetchall()
            ]
    return _row_job(row, photos)


def list_jobs(user_id: int) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM stockpile_jobs WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_job(r) for r in rows]


def create_job(user_id: int, name: str = "Pile job", mission_id: str | None = None, plan: dict | None = None) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = _now()
    title = (name or "Pile job").strip()[:200] or "Pile job"
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            INSERT INTO stockpile_jobs (id, user_id, mission_id, name, step, plan_json, piles_json, photo_count, status, progress, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'plan', ?, '[]', 0, 'open', ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                mission_id,
                title,
                json.dumps(plan) if plan is not None else None,
                "Plan the pile area.",
                now,
                now,
            ),
        )
    _jobs_dir(user_id, job_id)
    job = get_job(user_id, job_id)
    assert job is not None
    return job


def _require_job(user_id: int, job_id: str, include_photos: bool = False) -> dict[str, Any]:
    job = get_job(user_id, job_id, include_photos=include_photos)
    if job is None:
        raise StockpileError(404, "Unknown pile job.")
    return job


def update_job(
    user_id: int,
    job_id: str,
    *,
    name: str | None = None,
    step: str | None = None,
    plan: Any = None,
    piles: Any = None,
    mission_id: str | None = None,
    progress: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    job = _require_job(user_id, job_id)
    if step is not None and step not in STEPS:
        raise StockpileError(400, "Unknown step.")
    if piles is not None:
        piles = _validate_piles(piles)
    if name is not None:
        name = (name or "").strip()[:200] or job["name"]
    now = _now()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            UPDATE stockpile_jobs SET
              name = COALESCE(?, name),
              step = COALESCE(?, step),
              plan_json = CASE WHEN ? THEN ? ELSE plan_json END,
              piles_json = COALESCE(?, piles_json),
              mission_id = COALESCE(?, mission_id),
              progress = COALESCE(?, progress),
              status = COALESCE(?, status),
              updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                name,
                step,
                1 if plan is not None else 0,
                json.dumps(plan) if plan is not None else None,
                json.dumps(piles) if piles is not None else None,
                mission_id,
                progress,
                status,
                now,
                job_id,
                user_id,
            ),
        )
    return _require_job(user_id, job_id, include_photos=True)


def _validate_piles(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise StockpileError(400, "piles must be a list.")
    if len(raw) > MAX_PILES:
        raise StockpileError(400, f"At most {MAX_PILES} piles can be marked on one job.")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise StockpileError(400, "Each pile must be an object.")
        try:
            if "lat" in item and "lon" in item:
                lat = float(item["lat"])
                lon = float(item["lon"])
            else:
                lat = float(item["y"])
                lon = float(item["x"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StockpileError(400, "Each pile needs a lat and lon.") from exc
        if not all(map(lambda v: v == v and abs(v) < 1e12, (lat, lon))):
            raise StockpileError(400, "Pile coordinates are out of range.")
        frame = item.get("frame")
        geographic = -90 <= lat <= 90 and -180 <= lon <= 180
        if not geographic:
            frame = frame or "local"
        pile = {
            "id": str(item.get("id") or f"pile-{i+1}"),
            "name": str(item.get("name") or f"Pile {i+1}")[:80],
            "lat": lat,
            "lon": lon,
        }
        if frame:
            pile["frame"] = str(frame)[:16]
        if isinstance(item.get("ring"), list) and len(item["ring"]) >= 3:
            ring = []
            for pt in item["ring"]:
                if not isinstance(pt, dict):
                    continue
                try:
                    if "lat" in pt and "lon" in pt:
                        ring.append({"lat": float(pt["lat"]), "lon": float(pt["lon"])})
                    else:
                        ring.append({"lat": float(pt["y"]), "lon": float(pt["x"])})
                except (KeyError, TypeError, ValueError):
                    continue
            if len(ring) >= 3:
                pile["ring"] = ring
        out.append(pile)
    return out


def _row_photo(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "size": int(row["size"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "alt_m": row["alt_m"],
        "created_at": row["created_at"],
    }


def photo_limits_for(user: accounts.User, photo_count: int) -> dict[str, Any]:
    cap = accounts.effective_photo_cap(user)
    return photolimits.evaluate(photo_count, cap)


def add_photos(user: accounts.User, job_id: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    job = _require_job(user.id, job_id, include_photos=True)
    cap = accounts.effective_photo_cap(user)
    incoming = [f for f in files if f[1]]
    if not incoming:
        raise StockpileError(400, "At least one photo file is required.")
    if job["photo_count"] + len(incoming) > cap:
        raise photolimits.PhotoLimitError(
            400,
            f"This job already has {job['photo_count']} photos. Adding {len(incoming)} would go over the cap of {cap}.",
            "too_many",
        )
    saved = []
    folder = _jobs_dir(user.id, job_id) / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    now = _now()
    with accounts._lock:
        for filename, data in incoming:
            if len(data) > MAX_PHOTO_BYTES:
                raise StockpileError(400, f"{filename} is over the 25 MiB photo size limit.")
            ext = Path(filename).suffix.lower() or ".jpg"
            if ext not in ALLOWED_PHOTO_TYPES:
                ext = ".jpg"
            pid = str(uuid.uuid4())
            stored = f"{pid}{ext}"
            (folder / stored).write_bytes(data)
            gps = read_exif_gps(data)
            conn.execute(
                """
                INSERT INTO stockpile_photos (id, job_id, user_id, filename, stored_name, size, lat, lon, alt_m, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    job_id,
                    user.id,
                    (filename or stored)[:200],
                    stored,
                    len(data),
                    gps["lat"] if gps else None,
                    gps["lon"] if gps else None,
                    gps.get("alt_m") if gps else None,
                    now,
                ),
            )
            saved.append(pid)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM stockpile_photos WHERE job_id = ?", (job_id,)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE stockpile_jobs SET photo_count = ?, step = CASE WHEN step IN ('plan','pick') THEN 'fly' ELSE step END, progress = ?, updated_at = ? WHERE id = ?",
            (int(count), f"Uploaded {count} photos.", now, job_id),
        )
    job = _require_job(user.id, job_id, include_photos=True)
    job["limits"] = photo_limits_for(user, job["photo_count"])
    job["added"] = len(saved)
    return job


def read_exif_gps(data: bytes) -> dict[str, float] | None:
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS
    except Exception:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        exif = im.getexif()
        if not exif:
            return None
        gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else None
        if not gps_ifd:
            return None
        tagged = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
        lat = _gps_to_deg(tagged.get("GPSLatitude"), tagged.get("GPSLatitudeRef"))
        lon = _gps_to_deg(tagged.get("GPSLongitude"), tagged.get("GPSLongitudeRef"))
        if lat is None or lon is None:
            return None
        alt = tagged.get("GPSAltitude")
        alt_m = None
        if alt is not None:
            try:
                alt_m = float(alt[0]) / float(alt[1]) if isinstance(alt, tuple) else float(alt)
                ref = tagged.get("GPSAltitudeRef")
                if ref in (1, b"\x01", "1"):
                    alt_m = -alt_m
            except Exception:
                alt_m = None
        return {"lat": lat, "lon": lon, "alt_m": alt_m}
    except Exception:
        return None


def _gps_to_deg(values, ref) -> float | None:
    if not values:
        return None
    try:
        def rat(x):
            if isinstance(x, tuple) and len(x) == 2:
                return float(x[0]) / float(x[1] or 1)
            return float(x)

        parts = [rat(v) for v in values]
        if len(parts) < 3:
            return None
        deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
        if ref in ("S", "W", b"S", b"W"):
            deg = -deg
        return deg
    except Exception:
        return None


def _listed_photos(user_id: int, job_id: str) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM stockpile_photos WHERE job_id = ? AND user_id = ? ORDER BY created_at",
            (job_id, user_id),
        ).fetchall()
    return [dict(r) for r in rows]


def build_mosaic(user: accounts.User, job_id: str) -> dict[str, Any]:
    job = _require_job(user.id, job_id, include_photos=True)
    limits = photo_limits_for(user, job["photo_count"])
    photolimits.enforce(job["photo_count"], limits["photo_cap"])
    rows = _listed_photos(user.id, job_id)
    folder = _jobs_dir(user.id, job_id) / "photos"
    located = []
    for row in rows:
        path = folder / row["stored_name"]
        if not path.is_file():
            continue
        if row["lat"] is None or row["lon"] is None:
            continue
        located.append({"path": path, "lat": float(row["lat"]), "lon": float(row["lon"]), "filename": row["filename"]})
    if len(located) < 2:
        raise StockpileError(
            400,
            "Need at least two photos with EXIF GPS to build a preview mosaic. Volume still needs a DSM GeoTIFF.",
        )
    meta = _stitch_preview(user.id, job_id, located)
    now = _now()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            "UPDATE stockpile_jobs SET mosaic_meta = ?, step = 'mosaic', progress = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (
                json.dumps(meta),
                f"Preview mosaic built from {len(located)} geotagged photos. Upload a DSM to measure volume.",
                now,
                job_id,
                user.id,
            ),
        )
    return _require_job(user.id, job_id, include_photos=True)


def _stitch_preview(user_id: int, job_id: str, located: list[dict]) -> dict[str, Any]:
    from PIL import Image

    lats = [p["lat"] for p in located]
    lons = [p["lon"] for p in located]
    pad = 0.00015
    south, north = min(lats) - pad, max(lats) + pad
    west, east = min(lons) - pad, max(lons) + pad
    if east <= west:
        east = west + 0.0002
    if north <= south:
        north = south + 0.0002
    width = 2048
    aspect = max(0.3, (north - south) / (east - west))
    height = max(512, min(2048, int(width * aspect)))
    canvas = Image.new("RGB", (width, height), (18, 28, 34))
    thumb = 160
    for p in located:
        try:
            im = Image.open(p["path"]).convert("RGB")
            im.thumbnail((thumb, thumb))
        except Exception:
            continue
        x = int((p["lon"] - west) / (east - west) * (width - thumb))
        y = int((north - p["lat"]) / (north - south) * (height - thumb))
        canvas.paste(im, (max(0, x), max(0, y)))
    out = _jobs_dir(user_id, job_id) / "preview.jpg"
    canvas.save(out, "JPEG", quality=85)
    return {
        "kind": "gps_preview",
        "west": west,
        "south": south,
        "east": east,
        "north": north,
        "width": width,
        "height": height,
        "photo_count": len(located),
        "note": "Preview mosaic placed by EXIF GPS. Volume needs a DSM GeoTIFF from a photogrammetry export.",
    }


def preview_bytes(user_id: int, job_id: str) -> bytes:
    job = _require_job(user_id, job_id)
    path = _jobs_dir(user_id, job_id) / "preview.jpg"
    if not path.is_file():
        raise StockpileError(404, "No preview mosaic has been built yet.")
    return path.read_bytes()


def save_dsm(user: accounts.User, job_id: str, filename: str, data: bytes) -> dict[str, Any]:
    _require_job(user.id, job_id)
    if not data:
        raise StockpileError(400, "A DSM GeoTIFF file is required.")
    if len(data) > MAX_DSM_BYTES:
        raise StockpileError(400, "DSM is over the 250 MiB size limit.")
    path = _jobs_dir(user.id, job_id) / "dsm.tif"
    path.write_bytes(data)
    # Validate it opens.
    try:
        volume_calc.load_dsm_geotiff(data)
    except Exception as exc:
        raise StockpileError(400, f"Could not read that GeoTIFF as a DSM: {exc}") from exc
    now = _now()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            "UPDATE stockpile_jobs SET step = 'volume', progress = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (f"DSM saved ({filename or 'dsm.tif'}). Ready to compute pile volumes.", now, job_id, user.id),
        )
    return _require_job(user.id, job_id, include_photos=True)


def compute_volumes(user: accounts.User, job_id: str, density: float | None = None) -> dict[str, Any]:
    job = _require_job(user.id, job_id, include_photos=True)
    path = _jobs_dir(user.id, job_id) / "dsm.tif"
    if not path.is_file():
        raise StockpileError(400, "Upload a DSM GeoTIFF before computing volume. The preview mosaic alone is not enough.")
    dsm = volume_calc.load_dsm_geotiff(path.read_bytes())
    dens = density if density and density > 0 else volume_calc.DEFAULT_DENSITY_T_PER_M3
    results = []
    piles = job["piles"] or []
    if not piles:
        raise StockpileError(400, "Mark at least one pile before computing volume.")
    import volume_crs
    frame = volume_crs.frame_info(dsm)
    crs_errors = 0
    for pile in piles:
        try:
            metric = volume_crs.prepare_ring(dsm, pile)
        except volume_crs.RingError as exc:
            crs_errors += 1
            results.append({"id": pile["id"], "name": pile["name"], "ok": False, "error": str(exc)})
            continue
        try:
            vol = volume_calc.measure_polygon(dsm, metric, dens)
            # A successful call that measured nothing usually means the ring missed the raster.
            if vol.get("area_m2", 0) <= 0:
                crs_errors += 1
                results.append({"id": pile["id"], "name": pile["name"], "ok": False, "error": volume_crs.NO_OVERLAP_MESSAGE})
                continue
            results.append(
                {
                    "id": pile["id"],
                    "name": pile["name"],
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
            results.append({"id": pile["id"], "name": pile["name"], "ok": False, "error": str(exc)})
    if piles and crs_errors == len(piles) and not any(r.get("ok") for r in results):
        # Surface a single clear error instead of a list of silent-looking zeros.
        detail = results[0].get("error") or volume_crs.NO_OVERLAP_MESSAGE
        raise StockpileError(400, detail)
    payload = {"density_t_per_m3": dens, "piles": results, "computed_at": _now(), "dsm_frame": frame}
    now = _now()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            "UPDATE stockpile_jobs SET volumes_json = ?, step = 'volume', progress = ?, status = 'open', updated_at = ? WHERE id = ? AND user_id = ?",
            (json.dumps(payload), f"Computed volume for {sum(1 for r in results if r.get('ok'))} of {len(results)} piles.", now, job_id, user.id),
        )
    job = _require_job(user.id, job_id, include_photos=True)
    job["volumes"] = payload
    return job


def enforce_mission_limits(user: accounts.User, mission: dict | None) -> None:
    if not photolimits.is_stockpile_mission(mission):
        return
    count = photolimits.photo_count_of(mission)
    if count is None:
        return
    photolimits.enforce(count, accounts.effective_photo_cap(user))



def _with_processing(user_id: int, job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return job
    try:
        import processing as _processing
        proc = _processing.latest_for_stockpile(user_id, job["id"])
        if proc:
            job["processing"] = _processing.public_view(proc)
    except Exception:
        return job
    return job


def register(app: FastAPI, require_mosaic: RequireMosaic) -> None:
    def _err(exc):
        if isinstance(exc, photolimits.PhotoLimitError):
            return HTTPException(status_code=exc.status, detail=exc.detail)
        if isinstance(exc, StockpileError):
            return HTTPException(status_code=exc.status, detail=exc.detail)
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/stockpile/limits")
    def get_limits(request: Request):
        user, _session = require_mosaic(request)
        cap = accounts.effective_photo_cap(user)
        return {
            "min_photos": photolimits.MIN_PHOTOS,
            "photo_cap": cap,
            "photo_cap_override": user.photo_cap,
            "plan": user.plan,
            "default_front_overlap": photolimits.DEFAULT_FRONT_OVERLAP,
            "default_side_overlap": photolimits.DEFAULT_SIDE_OVERLAP,
            "height_multiplier_min": photolimits.HEIGHT_MULTIPLIER_MIN,
            "height_multiplier_max": photolimits.HEIGHT_MULTIPLIER_MAX,
        }

    @app.get("/stockpile/jobs")
    def api_list(request: Request):
        user, _session = require_mosaic(request)
        return {"jobs": [_with_processing(user.id, j) for j in list_jobs(user.id)], "limits": photo_limits_for(user, 0) | {"photo_count": None}}

    @app.post("/stockpile/jobs")
    async def api_create(request: Request):
        user, _session = require_mosaic(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            if payload.get("plan") or payload.get("mission"):
                mission = payload.get("mission") or payload.get("plan")
                if isinstance(mission, dict):
                    enforce_mission_limits(user, mission)
            job = create_job(
                user.id,
                name=payload.get("name") or "Pile job",
                mission_id=payload.get("mission_id"),
                plan=payload.get("plan") or payload.get("mission"),
            )
        except (StockpileError, photolimits.PhotoLimitError) as exc:
            raise _err(exc) from exc
        return JSONResponse(job, status_code=201)

    @app.get("/stockpile/jobs/{job_id}")
    def api_get(job_id: str, request: Request):
        user, _session = require_mosaic(request)
        try:
            return _with_processing(user.id, _require_job(user.id, job_id, include_photos=True))
        except StockpileError as exc:
            raise _err(exc) from exc

    @app.patch("/stockpile/jobs/{job_id}")
    async def api_patch(job_id: str, request: Request):
        user, _session = require_mosaic(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object.")
        try:
            if payload.get("plan") or payload.get("mission"):
                enforce_mission_limits(user, payload.get("mission") or payload.get("plan"))
            return update_job(
                user.id,
                job_id,
                name=payload.get("name"),
                step=payload.get("step"),
                plan=payload.get("plan") or payload.get("mission"),
                piles=payload.get("piles"),
                mission_id=payload.get("mission_id"),
                progress=payload.get("progress"),
                status=payload.get("status"),
            )
        except (StockpileError, photolimits.PhotoLimitError) as exc:
            raise _err(exc) from exc

    @app.post("/stockpile/jobs/{job_id}/photos")
    async def api_photos(job_id: str, request: Request, files: list[UploadFile] = File(default=[])):
        user, _session = require_mosaic(request)
        blobs: list[tuple[str, bytes]] = []
        for f in files:
            blobs.append((f.filename or "photo.jpg", await f.read()))
        try:
            return add_photos(user, job_id, blobs)
        except (StockpileError, photolimits.PhotoLimitError) as exc:
            raise _err(exc) from exc

    @app.post("/stockpile/jobs/{job_id}/mosaic")
    def api_mosaic(job_id: str, request: Request):
        user, _session = require_mosaic(request)
        try:
            return build_mosaic(user, job_id)
        except (StockpileError, photolimits.PhotoLimitError) as exc:
            raise _err(exc) from exc

    @app.get("/stockpile/jobs/{job_id}/preview")
    def api_preview(job_id: str, request: Request):
        user, _session = require_mosaic(request)
        try:
            data = preview_bytes(user.id, job_id)
        except StockpileError as exc:
            raise _err(exc) from exc
        return Response(data, media_type="image/jpeg")

    @app.post("/stockpile/jobs/{job_id}/dsm")
    async def api_dsm(job_id: str, request: Request, file: UploadFile = File(...)):
        user, _session = require_mosaic(request)
        data = await file.read()
        try:
            return save_dsm(user, job_id, file.filename or "dsm.tif", data)
        except StockpileError as exc:
            raise _err(exc) from exc

    @app.post("/stockpile/jobs/{job_id}/volume")
    async def api_volume(job_id: str, request: Request):
        user, _session = require_mosaic(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        density = payload.get("density_t_per_m3") or payload.get("density")
        try:
            return compute_volumes(user, job_id, float(density) if density is not None else None)
        except (StockpileError, TypeError, ValueError) as exc:
            raise _err(exc) from exc
