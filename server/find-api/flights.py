"""Flight sessions linked to a Drone Mapper account and mission.

Live web streaming of the camera feed is intentionally out of scope
(an optional later step that needs a streaming relay on the server).
This module records start/stop, a "flying now" flag, and snapshot/clip
uploads attached to the mission.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import accounts
import config
import syncstore

RequireLogin = Callable[[Request], tuple[Any, Any]]
RequireCsrf = Callable[[Request, Any], None]

MAX_MEDIA_BYTES = 80 * 1024 * 1024
MAX_MEDIA_PER_FLIGHT = 40
ALLOWED_MEDIA = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".m4v", ".webm"}


class FlightError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _conn():
    return accounts._connect()


def init() -> None:
    conn = _conn()
    with accounts._lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS flight_sessions (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              mission_id TEXT NOT NULL,
              started_at REAL NOT NULL,
              stopped_at REAL,
              status TEXT NOT NULL DEFAULT 'flying',
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flight_media (
              id TEXT PRIMARY KEY,
              flight_id TEXT NOT NULL,
              user_id INTEGER NOT NULL,
              mission_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              filename TEXT NOT NULL,
              stored_name TEXT NOT NULL,
              content_type TEXT,
              size INTEGER NOT NULL,
              created_at REAL NOT NULL,
              FOREIGN KEY(flight_id) REFERENCES flight_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_flights_user ON flight_sessions(user_id, status, started_at);
            CREATE INDEX IF NOT EXISTS idx_flights_mission ON flight_sessions(user_id, mission_id);
            CREATE INDEX IF NOT EXISTS idx_flight_media_flight ON flight_media(flight_id);
            """
        )


def _media_dir(user_id: int, flight_id: str) -> Path:
    path = config.DATA_DIR / "flights" / str(user_id) / flight_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _row_flight(row, media: list[dict] | None = None) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "mission_id": row["mission_id"],
        "started_at": row["started_at"],
        "stopped_at": row["stopped_at"],
        "status": row["status"],
        "updated_at": row["updated_at"],
        "flying": row["status"] == "flying" and row["stopped_at"] is None,
    }
    if media is not None:
        item["media"] = media
    return item


def _row_media(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "flight_id": row["flight_id"],
        "mission_id": row["mission_id"],
        "kind": row["kind"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": int(row["size"]),
        "created_at": row["created_at"],
    }


def _media_for(flight_id: str) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM flight_media WHERE flight_id = ? ORDER BY created_at",
            (flight_id,),
        ).fetchall()
    return [_row_media(r) for r in rows]


def list_flights(user_id: int, mission_id: str | None = None, include_media: bool = False) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        if mission_id:
            rows = conn.execute(
                "SELECT * FROM flight_sessions WHERE user_id = ? AND mission_id = ? ORDER BY started_at DESC",
                (user_id, mission_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flight_sessions WHERE user_id = ? ORDER BY started_at DESC",
                (user_id,),
            ).fetchall()
    out = []
    for row in rows:
        out.append(_row_flight(row, _media_for(row["id"]) if include_media else None))
    return out


def active_flights(user_id: int) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        rows = conn.execute(
            "SELECT * FROM flight_sessions WHERE user_id = ? AND status = 'flying' AND stopped_at IS NULL ORDER BY started_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_flight(r) for r in rows]


def get_flight(user_id: int, flight_id: str, include_media: bool = True) -> dict[str, Any] | None:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM flight_sessions WHERE id = ? AND user_id = ?",
            (flight_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return _row_flight(row, _media_for(flight_id) if include_media else None)


def start_flight(user_id: int, mission_id: str) -> dict[str, Any]:
    mid = syncstore.validate_id(mission_id)
    mission = syncstore.get_mission(user_id, mid, include_deleted=False)
    if mission is None:
        raise FlightError(404, "Unknown mission.")
    now = time.time()
    fid = str(uuid.uuid4())
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            INSERT INTO flight_sessions (id, user_id, mission_id, started_at, stopped_at, status, updated_at)
            VALUES (?, ?, ?, ?, NULL, 'flying', ?)
            """,
            (fid, user_id, mid, now, now),
        )
    item = get_flight(user_id, fid)
    assert item is not None
    return item


def stop_flight(user_id: int, flight_id: str) -> dict[str, Any]:
    item = get_flight(user_id, flight_id)
    if item is None:
        raise FlightError(404, "Unknown flight session.")
    now = time.time()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            "UPDATE flight_sessions SET stopped_at = COALESCE(stopped_at, ?), status = 'ended', updated_at = ? WHERE id = ? AND user_id = ?",
            (now, now, flight_id, user_id),
        )
    updated = get_flight(user_id, flight_id)
    assert updated is not None
    return updated


def add_media(user_id: int, flight_id: str, filename: str, data: bytes, content_type: str | None, kind: str | None = None) -> dict[str, Any]:
    item = get_flight(user_id, flight_id, include_media=True)
    if item is None:
        raise FlightError(404, "Unknown flight session.")
    if not data:
        raise FlightError(400, "A snapshot or clip file is required.")
    if len(data) > MAX_MEDIA_BYTES:
        raise FlightError(400, "That file is over the 80 MiB media size limit.")
    if len(item.get("media") or []) >= MAX_MEDIA_PER_FLIGHT:
        raise FlightError(400, f"At most {MAX_MEDIA_PER_FLIGHT} snapshots or clips can be attached to one flight.")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_MEDIA:
        if (content_type or "").startswith("image/"):
            ext = ".jpg"
        elif (content_type or "").startswith("video/"):
            ext = ".mp4"
        else:
            raise FlightError(400, "Attach a JPEG, PNG, or a short MP4 / MOV clip.")
    guessed_kind = kind or ("clip" if ext in {".mp4", ".mov", ".m4v", ".webm"} else "snapshot")
    mid = str(uuid.uuid4())
    stored = f"{mid}{ext}"
    folder = _media_dir(user_id, flight_id)
    (folder / stored).write_bytes(data)
    now = time.time()
    conn = _conn()
    with accounts._lock:
        conn.execute(
            """
            INSERT INTO flight_media (id, flight_id, user_id, mission_id, kind, filename, stored_name, content_type, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                flight_id,
                user_id,
                item["mission_id"],
                guessed_kind,
                (filename or stored)[:200],
                stored,
                (content_type or "")[:120],
                len(data),
                now,
            ),
        )
        conn.execute(
            "UPDATE flight_sessions SET updated_at = ? WHERE id = ?",
            (now, flight_id),
        )
    updated = get_flight(user_id, flight_id)
    assert updated is not None
    return updated


def media_bytes(user_id: int, media_id: str) -> tuple[bytes, str, str]:
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM flight_media WHERE id = ? AND user_id = ?",
            (media_id, user_id),
        ).fetchone()
    if row is None:
        raise FlightError(404, "Unknown flight media.")
    path = _media_dir(user_id, row["flight_id"]) / row["stored_name"]
    if not path.is_file():
        raise FlightError(404, "That file is no longer on the server.")
    ctype = row["content_type"] or ("video/mp4" if row["kind"] == "clip" else "image/jpeg")
    return path.read_bytes(), ctype, row["filename"]


def changes_since(user_id: int, since: float | None) -> list[dict[str, Any]]:
    conn = _conn()
    with accounts._lock:
        if since is None:
            rows = conn.execute(
                "SELECT * FROM flight_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flight_sessions WHERE user_id = ? AND updated_at > ? ORDER BY updated_at DESC",
                (user_id, since),
            ).fetchall()
    return [_row_flight(r) for r in rows]


def register(app: FastAPI, require_login: RequireLogin, require_csrf: RequireCsrf) -> None:
    def _user(request: Request):
        user, session = require_login(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, session)
        return user

    def _err(exc: FlightError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.detail)

    @app.get("/sync/flights")
    def api_list(request: Request, mission_id: str | None = None):
        user = _user(request)
        return {
            "flights": list_flights(user.id, mission_id=mission_id, include_media=True),
            "flying_now": active_flights(user.id),
        }

    @app.get("/sync/flights/active")
    def api_active(request: Request):
        user = _user(request)
        return {"flying_now": active_flights(user.id)}

    @app.post("/sync/missions/{mission_id}/flights")
    def api_start(mission_id: str, request: Request):
        user = _user(request)
        try:
            item = start_flight(user.id, mission_id)
        except (FlightError, syncstore.SyncError) as exc:
            if isinstance(exc, syncstore.SyncError):
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            raise _err(exc) from exc
        return JSONResponse(item, status_code=201)

    @app.get("/sync/flights/{flight_id}")
    def api_get(flight_id: str, request: Request):
        user = _user(request)
        item = get_flight(user.id, flight_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Unknown flight session.")
        return item

    @app.post("/sync/flights/{flight_id}/stop")
    def api_stop(flight_id: str, request: Request):
        user = _user(request)
        try:
            return stop_flight(user.id, flight_id)
        except FlightError as exc:
            raise _err(exc) from exc

    @app.post("/sync/flights/{flight_id}/media")
    async def api_media(
        flight_id: str,
        request: Request,
        file: UploadFile = File(...),
        kind: str | None = None,
    ):
        user = _user(request)
        data = await file.read()
        try:
            return add_media(user.id, flight_id, file.filename or "media", data, file.content_type, kind)
        except FlightError as exc:
            raise _err(exc) from exc

    @app.get("/sync/media/{media_id}")
    def api_media_get(media_id: str, request: Request):
        user = _user(request)
        try:
            data, ctype, filename = media_bytes(user.id, media_id)
        except FlightError as exc:
            raise _err(exc) from exc
        return Response(
            data,
            media_type=ctype,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
