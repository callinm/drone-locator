"""Per-user dashboard/app sync store.

Missions, planner settings, flight progress, and optional Find My Drone
results are keyed by Drone Mapper user id. Deletes are tombstones so they
can be recovered. Last write wins; the server always stamps updated_at.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

import accounts

MISSION_FORMAT = "drone-mapper-mission"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
MAX_NAME_LEN = 200
MAX_MISSION_BYTES = 2 * 1024 * 1024
MAX_SETTINGS_BYTES = 64 * 1024
MAX_PROGRESS_BYTES = 16 * 1024
MAX_FIND_BYTES = 256 * 1024
MAX_MISSIONS_PER_USER = 200
ALLOWED_PROGRESS_STATUS = {"idle", "in_progress", "paused", "completed", "aborted"}


class SyncError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _conn():
    return accounts._connect()


def init() -> None:
    """Create sync tables on the accounts database."""
    conn = _conn()
    with accounts._lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_missions (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              mission_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              deleted_at REAL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sync_missions_user_updated
              ON sync_missions(user_id, updated_at);

            CREATE TABLE IF NOT EXISTS sync_settings (
              user_id INTEGER PRIMARY KEY,
              settings_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at REAL NOT NULL,
              deleted_at REAL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sync_progress (
              mission_id TEXT NOT NULL,
              user_id INTEGER NOT NULL,
              progress_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at REAL NOT NULL,
              deleted_at REAL,
              PRIMARY KEY (mission_id, user_id),
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sync_progress_user_updated
              ON sync_progress(user_id, updated_at);

            CREATE TABLE IF NOT EXISTS sync_find_results (
              user_id INTEGER PRIMARY KEY,
              results_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at REAL NOT NULL,
              deleted_at REAL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


def delete_user_data(user_id: int) -> None:
    """Remove sync rows when the Drone Mapper account itself is deleted."""
    conn = _conn()
    with accounts._lock:
        conn.execute("DELETE FROM sync_progress WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sync_missions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sync_settings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sync_find_results WHERE user_id = ?", (user_id,))


def now() -> float:
    return time.time()


def new_id() -> str:
    return str(uuid.uuid4())


def validate_id(value: str | None) -> str:
    text = (value or "").strip()
    if not ID_RE.match(text):
        raise SyncError(400, "Mission id must be 8-80 letters, numbers, dots, underscores, colons, or dashes.")
    return text


def _parse_json_object(raw: Any, label: str, max_bytes: int) -> dict[str, Any]:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > max_bytes:
            raise SyncError(400, f"{label} is too large.")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SyncError(400, f"{label} is not valid JSON.") from exc
    if not isinstance(raw, dict):
        raise SyncError(400, f"{label} must be a JSON object.")
    encoded = json.dumps(raw, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise SyncError(400, f"{label} is too large.")
    return raw


def validate_mission(raw: Any) -> dict[str, Any]:
    mission = _parse_json_object(raw, "Mission", MAX_MISSION_BYTES)
    if mission.get("format") != MISSION_FORMAT:
        raise SyncError(400, "Not a drone-mapper mission file.")
    return mission


def mission_name(mission: dict[str, Any], override: str | None = None) -> str:
    name = (override if override is not None else None) or mission.get("name") or "Untitled survey"
    name = str(name).strip() or "Untitled survey"
    if len(name) > MAX_NAME_LEN:
        raise SyncError(400, f"Mission name must be at most {MAX_NAME_LEN} characters.")
    return name


def _row_mission(row, include_body: bool = True, progress: dict | None = None) -> dict[str, Any]:
    body = json.loads(row["mission_json"]) if include_body else None
    out = {
        "id": row["id"],
        "name": row["name"],
        "version": int(row["version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "deleted": row["deleted_at"] is not None,
    }
    if include_body and row["deleted_at"] is None:
        out["mission"] = body
    if progress is not None:
        out["progress"] = progress
    elif include_body:
        out["progress"] = None
    return out


def _count_active(conn, user_id: int, exclude_id: str | None = None) -> int:
    if exclude_id:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sync_missions WHERE user_id = ? AND deleted_at IS NULL AND id != ?",
            (user_id, exclude_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sync_missions WHERE user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(row["n"] if row else 0)


def get_mission(user_id: int, mission_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    init()
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mission_id, user_id),
        ).fetchone()
        if row is None:
            return None
        if row["deleted_at"] is not None and not include_deleted:
            return None
        progress = _progress_locked(conn, user_id, mission_id)
        return _row_mission(row, include_body=True, progress=progress)


def list_missions(user_id: int, include_deleted: bool = False) -> list[dict[str, Any]]:
    init()
    conn = _conn()
    with accounts._lock:
        if include_deleted:
            rows = conn.execute(
                "SELECT * FROM sync_missions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sync_missions WHERE user_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        progress_map = _progress_map_locked(conn, user_id)
        return [
            _row_mission(row, include_body=False, progress=progress_map.get(row["id"]))
            for row in rows
        ]



def _enforce_stockpile_limits(user_id: int, mission: dict[str, Any]) -> None:
    import photolimits

    if not photolimits.is_stockpile_mission(mission):
        return
    count = photolimits.photo_count_of(mission)
    if count is None:
        return
    user = accounts.get_user_by_id(user_id)
    cap = accounts.effective_photo_cap(user) if user else photolimits.DEFAULT_PHOTO_CAP
    try:
        photolimits.enforce(count, cap)
    except photolimits.PhotoLimitError as exc:
        raise SyncError(exc.status, exc.detail) from exc


def upsert_mission(
    user_id: int,
    mission: dict[str, Any],
    mission_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    init()
    mission = validate_mission(mission)
    _enforce_stockpile_limits(user_id, mission)
    label = mission_name(mission, name)
    conn = _conn()
    stamp = now()
    with accounts._lock:
        if mission_id:
            mid = validate_id(mission_id)
            existing = conn.execute(
                "SELECT * FROM sync_missions WHERE id = ?",
                (mid,),
            ).fetchone()
            if existing is not None and int(existing["user_id"]) != int(user_id):
                raise SyncError(404, "Unknown mission.")
            if existing is None:
                if _count_active(conn, user_id) >= MAX_MISSIONS_PER_USER:
                    raise SyncError(400, "Mission limit reached for this account.")
                conn.execute(
                    """
                    INSERT INTO sync_missions (id, user_id, name, mission_json, version, created_at, updated_at, deleted_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (mid, user_id, label, json.dumps(mission, separators=(",", ":")), stamp, stamp),
                )
            else:
                # Recover a tombstone by writing again (last write wins).
                if existing["deleted_at"] is not None and _count_active(conn, user_id, exclude_id=mid) >= MAX_MISSIONS_PER_USER:
                    raise SyncError(400, "Mission limit reached for this account.")
                conn.execute(
                    """
                    UPDATE sync_missions
                    SET name = ?, mission_json = ?, version = version + 1, updated_at = ?, deleted_at = NULL
                    WHERE id = ? AND user_id = ?
                    """,
                    (label, json.dumps(mission, separators=(",", ":")), stamp, mid, user_id),
                )
        else:
            if _count_active(conn, user_id) >= MAX_MISSIONS_PER_USER:
                raise SyncError(400, "Mission limit reached for this account.")
            mid = new_id()
            conn.execute(
                """
                INSERT INTO sync_missions (id, user_id, name, mission_json, version, created_at, updated_at, deleted_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, NULL)
                """,
                (mid, user_id, label, json.dumps(mission, separators=(",", ":")), stamp, stamp),
            )
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        progress = _progress_locked(conn, user_id, mid)
        return _row_mission(row, include_body=True, progress=progress)


def rename_mission(user_id: int, mission_id: str, name: str) -> dict[str, Any]:
    init()
    mid = validate_id(mission_id)
    label = mission_name({"name": name}, name)
    conn = _conn()
    stamp = now()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise SyncError(404, "Unknown mission.")
        mission = json.loads(row["mission_json"])
        mission["name"] = label
        conn.execute(
            """
            UPDATE sync_missions
            SET name = ?, mission_json = ?, version = version + 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (label, json.dumps(mission, separators=(",", ":")), stamp, mid, user_id),
        )
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        progress = _progress_locked(conn, user_id, mid)
        return _row_mission(row, include_body=True, progress=progress)


def soft_delete_mission(user_id: int, mission_id: str) -> dict[str, Any]:
    init()
    mid = validate_id(mission_id)
    conn = _conn()
    stamp = now()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        if row is None:
            raise SyncError(404, "Unknown mission.")
        if row["deleted_at"] is None:
            conn.execute(
                """
                UPDATE sync_missions
                SET deleted_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND user_id = ?
                """,
                (stamp, stamp, mid, user_id),
            )
            conn.execute(
                """
                UPDATE sync_progress
                SET deleted_at = ?, updated_at = ?, version = version + 1
                WHERE mission_id = ? AND user_id = ? AND deleted_at IS NULL
                """,
                (stamp, stamp, mid, user_id),
            )
            row = conn.execute(
                "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
                (mid, user_id),
            ).fetchone()
        return _row_mission(row, include_body=False, progress=None)


def restore_mission(user_id: int, mission_id: str) -> dict[str, Any]:
    init()
    mid = validate_id(mission_id)
    conn = _conn()
    stamp = now()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        if row is None:
            raise SyncError(404, "Unknown mission.")
        if row["deleted_at"] is None:
            progress = _progress_locked(conn, user_id, mid)
            return _row_mission(row, include_body=True, progress=progress)
        if _count_active(conn, user_id) >= MAX_MISSIONS_PER_USER:
            raise SyncError(400, "Mission limit reached for this account.")
        conn.execute(
            """
            UPDATE sync_missions
            SET deleted_at = NULL, updated_at = ?, version = version + 1
            WHERE id = ? AND user_id = ?
            """,
            (stamp, mid, user_id),
        )
        row = conn.execute(
            "SELECT * FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        progress = _progress_locked(conn, user_id, mid)
        return _row_mission(row, include_body=True, progress=progress)


def validate_progress(raw: Any) -> dict[str, Any]:
    data = _parse_json_object(raw, "Progress", MAX_PROGRESS_BYTES)
    status = str(data.get("status") or "in_progress")
    if status not in ALLOWED_PROGRESS_STATUS:
        raise SyncError(400, "Unknown flight status.")
    def _num(key: str) -> int | None:
        if key not in data or data[key] is None or data[key] == "":
            return None
        try:
            return int(data[key])
        except (TypeError, ValueError) as exc:
            raise SyncError(400, f"{key} must be a number.") from exc

    def _float(key: str) -> float | None:
        if key not in data or data[key] is None or data[key] == "":
            return None
        try:
            return float(data[key])
        except (TypeError, ValueError) as exc:
            raise SyncError(400, f"{key} must be a number.") from exc

    return {
        "status": status,
        "lines_done": _num("lines_done"),
        "lines_total": _num("lines_total"),
        "photos_done": _num("photos_done"),
        "photos_total": _num("photos_total"),
        "last_lat": _float("last_lat"),
        "last_lon": _float("last_lon"),
        "note": str(data["note"])[:200] if data.get("note") else None,
    }


def _progress_locked(conn, user_id: int, mission_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sync_progress WHERE user_id = ? AND mission_id = ? AND deleted_at IS NULL",
        (user_id, mission_id),
    ).fetchone()
    if row is None:
        return None
    return _row_progress(row)


def _progress_map_locked(conn, user_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sync_progress WHERE user_id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchall()
    return {row["mission_id"]: _row_progress(row) for row in rows}


def _row_progress(row) -> dict[str, Any]:
    body = json.loads(row["progress_json"])
    return {
        "mission_id": row["mission_id"],
        "status": body.get("status"),
        "lines_done": body.get("lines_done"),
        "lines_total": body.get("lines_total"),
        "photos_done": body.get("photos_done"),
        "photos_total": body.get("photos_total"),
        "last_lat": body.get("last_lat"),
        "last_lon": body.get("last_lon"),
        "note": body.get("note"),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "deleted": row["deleted_at"] is not None,
    }


def set_progress(user_id: int, mission_id: str, raw: Any) -> dict[str, Any]:
    init()
    mid = validate_id(mission_id)
    progress = validate_progress(raw)
    conn = _conn()
    stamp = now()
    with accounts._lock:
        mission = conn.execute(
            "SELECT id, deleted_at FROM sync_missions WHERE id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        if mission is None or mission["deleted_at"] is not None:
            raise SyncError(404, "Unknown mission.")
        existing = conn.execute(
            "SELECT * FROM sync_progress WHERE mission_id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        payload = json.dumps(progress, separators=(",", ":"))
        if existing is None:
            conn.execute(
                """
                INSERT INTO sync_progress (mission_id, user_id, progress_json, version, updated_at, deleted_at)
                VALUES (?, ?, ?, 1, ?, NULL)
                """,
                (mid, user_id, payload, stamp),
            )
        else:
            conn.execute(
                """
                UPDATE sync_progress
                SET progress_json = ?, version = version + 1, updated_at = ?, deleted_at = NULL
                WHERE mission_id = ? AND user_id = ?
                """,
                (payload, stamp, mid, user_id),
            )
        row = conn.execute(
            "SELECT * FROM sync_progress WHERE mission_id = ? AND user_id = ?",
            (mid, user_id),
        ).fetchone()
        return _row_progress(row)


def get_settings(user_id: int) -> dict[str, Any] | None:
    init()
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None or row["deleted_at"] is not None:
            return None
        return {
            "settings": json.loads(row["settings_json"]),
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
            "deleted_at": None,
            "deleted": False,
        }


def set_settings(user_id: int, raw: Any) -> dict[str, Any]:
    init()
    settings = _parse_json_object(raw, "Settings", MAX_SETTINGS_BYTES)
    conn = _conn()
    stamp = now()
    payload = json.dumps(settings, separators=(",", ":"))
    with accounts._lock:
        existing = conn.execute(
            "SELECT * FROM sync_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO sync_settings (user_id, settings_json, version, updated_at, deleted_at)
                VALUES (?, ?, 1, ?, NULL)
                """,
                (user_id, payload, stamp),
            )
        else:
            conn.execute(
                """
                UPDATE sync_settings
                SET settings_json = ?, version = version + 1, updated_at = ?, deleted_at = NULL
                WHERE user_id = ?
                """,
                (payload, stamp, user_id),
            )
        return get_settings(user_id)


def get_find_results(user_id: int) -> dict[str, Any] | None:
    init()
    conn = _conn()
    with accounts._lock:
        row = conn.execute(
            "SELECT * FROM sync_find_results WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None or row["deleted_at"] is not None:
            return None
        return {
            "results": json.loads(row["results_json"]),
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
            "deleted_at": None,
            "deleted": False,
        }


def set_find_results(user_id: int, raw: Any) -> dict[str, Any]:
    init()
    results = _parse_json_object(raw, "Find results", MAX_FIND_BYTES)
    conn = _conn()
    stamp = now()
    payload = json.dumps(results, separators=(",", ":"))
    with accounts._lock:
        existing = conn.execute(
            "SELECT user_id FROM sync_find_results WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO sync_find_results (user_id, results_json, version, updated_at, deleted_at)
                VALUES (?, ?, 1, ?, NULL)
                """,
                (user_id, payload, stamp),
            )
        else:
            conn.execute(
                """
                UPDATE sync_find_results
                SET results_json = ?, version = version + 1, updated_at = ?, deleted_at = NULL
                WHERE user_id = ?
                """,
                (payload, stamp, user_id),
            )
    return get_find_results(user_id)


def changes_since(user_id: int, since: float | None) -> dict[str, Any]:
    """Return records updated at or after `since` (inclusive), including tombstones."""
    init()
    conn = _conn()
    stamp = now()
    threshold = float(since) if since is not None else 0.0
    with accounts._lock:
        missions = conn.execute(
            "SELECT * FROM sync_missions WHERE user_id = ? AND updated_at >= ? ORDER BY updated_at ASC",
            (user_id, threshold),
        ).fetchall()
        progress_rows = conn.execute(
            "SELECT * FROM sync_progress WHERE user_id = ? AND updated_at >= ? ORDER BY updated_at ASC",
            (user_id, threshold),
        ).fetchall()
        settings_row = conn.execute(
            "SELECT * FROM sync_settings WHERE user_id = ? AND updated_at >= ?",
            (user_id, threshold),
        ).fetchone()
        find_row = conn.execute(
            "SELECT * FROM sync_find_results WHERE user_id = ? AND updated_at >= ?",
            (user_id, threshold),
        ).fetchone()
        progress_by_id = {row["mission_id"]: _row_progress(row) for row in progress_rows}
        mission_payloads = []
        for row in missions:
            item = _row_mission(
                row,
                include_body=row["deleted_at"] is None,
                progress=progress_by_id.get(row["id"]) if row["deleted_at"] is None else None,
            )
            mission_payloads.append(item)
        settings = None
        if settings_row is not None:
            settings = {
                "settings": json.loads(settings_row["settings_json"]) if settings_row["deleted_at"] is None else None,
                "version": int(settings_row["version"]),
                "updated_at": settings_row["updated_at"],
                "deleted_at": settings_row["deleted_at"],
                "deleted": settings_row["deleted_at"] is not None,
            }
        find_results = None
        if find_row is not None:
            find_results = {
                "results": json.loads(find_row["results_json"]) if find_row["deleted_at"] is None else None,
                "version": int(find_row["version"]),
                "updated_at": find_row["updated_at"],
                "deleted_at": find_row["deleted_at"],
                "deleted": find_row["deleted_at"] is not None,
            }
        return {
            "server_time": stamp,
            "since": threshold,
            "missions": mission_payloads,
            "progress": [_row_progress(row) for row in progress_rows],
            "settings": settings,
            "find_results": find_results,
        }


def latest_update(user_id: int) -> float:
    init()
    conn = _conn()
    with accounts._lock:
        times = []
        for sql in (
            "SELECT MAX(updated_at) AS t FROM sync_missions WHERE user_id = ?",
            "SELECT MAX(updated_at) AS t FROM sync_progress WHERE user_id = ?",
            "SELECT MAX(updated_at) AS t FROM sync_settings WHERE user_id = ?",
            "SELECT MAX(updated_at) AS t FROM sync_find_results WHERE user_id = ?",
            "SELECT MAX(updated_at) AS t FROM flight_sessions WHERE user_id = ?",
        ):
            try:
                row = conn.execute(sql, (user_id,)).fetchone()
            except Exception:
                row = None
            if row and row["t"] is not None:
                times.append(float(row["t"]))
        return max(times) if times else 0.0
