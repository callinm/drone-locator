"""Drone Locator data, kept in its own SQLite file.

Finder mosaics (index/metadata) and saved search results live here.
Account login stays in accounts.sqlite. Stockpile and survey-processing
jobs stay in that same accounts database (and on-disk stockpile/processing
folders). This module never deletes legacy rows or mosaic folders.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import accounts
import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def db_path() -> Path:
    override = getattr(config, "LOCATOR_DB", None)
    if override:
        return Path(override)
    return Path(config.DATA_DIR) / "locator.sqlite"


def reset_connection() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _conn_path = None


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path()
    with _lock:
        if _conn is not None and _conn_path == path:
            return _conn
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _conn = conn
        _conn_path = path
        _init_schema(conn)
        return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mosaics (
          id TEXT PRIMARY KEY,
          owner_id TEXT,
          filename TEXT,
          folder TEXT NOT NULL,
          meta_json TEXT NOT NULL,
          created_at REAL,
          expires_at REAL,
          bytes INTEGER,
          source TEXT NOT NULL DEFAULT 'new',
          copied_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_locator_mosaics_owner ON mosaics(owner_id);

        CREATE TABLE IF NOT EXISTS find_results (
          user_id INTEGER PRIMARY KEY,
          results_json TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          updated_at REAL NOT NULL,
          deleted_at REAL,
          copied_at REAL
        );

        CREATE TABLE IF NOT EXISTS locator_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )


def init() -> None:
    _connect()


def _now() -> float:
    return time.time()


def migrate_from_legacy(*, data_dir: Path | None = None) -> dict[str, Any]:
    """COPY finder records into locator.sqlite. Delete nothing from the old stores."""
    init()
    root = Path(data_dir or config.DATA_DIR)
    conn = _connect()
    mosaics_copied = 0
    mosaics_skipped = 0
    results_copied = 0
    results_skipped = 0
    stamp = _now()

    skip_dirs = {"stockpile", "processing", "locator", "accounts"}
    with _lock:
        if root.is_dir():
            for child in root.iterdir():
                if not child.is_dir() or child.name in skip_dirs:
                    continue
                meta_path = child / "meta.json"
                if not meta_path.is_file():
                    continue
                try:
                    data = json.loads(meta_path.read_text())
                except Exception:
                    continue
                existing = conn.execute("SELECT id FROM mosaics WHERE id = ?", (child.name,)).fetchone()
                if existing:
                    mosaics_skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO mosaics (id, owner_id, filename, folder, meta_json, created_at, expires_at, bytes, source, copied_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'legacy-copy', ?)
                    """,
                    (
                        child.name,
                        str(data["owner_id"]) if data.get("owner_id") is not None else None,
                        data.get("filename") or data.get("filename_stored") or "mosaic",
                        str(child.resolve()),
                        json.dumps(data, separators=(",", ":")),
                        data.get("created"),
                        data.get("expires"),
                        data.get("bytes"),
                        stamp,
                    ),
                )
                mosaics_copied += 1

        # Copy synced find results from the accounts/sync database. Leave originals.
        try:
            accounts._connect()
            src = accounts._connect()
            rows = src.execute("SELECT * FROM sync_find_results").fetchall()
        except Exception:
            rows = []
        for row in rows:
            uid = int(row["user_id"])
            exists = conn.execute("SELECT user_id FROM find_results WHERE user_id = ?", (uid,)).fetchone()
            if exists:
                results_skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO find_results (user_id, results_json, version, updated_at, deleted_at, copied_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    row["results_json"],
                    int(row["version"] or 1),
                    row["updated_at"],
                    row["deleted_at"] if "deleted_at" in row.keys() else None,
                    stamp,
                ),
            )
            results_copied += 1

        conn.execute(
            """
            INSERT INTO locator_meta(key, value) VALUES('last_migration', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps({"at": stamp, "mosaics_copied": mosaics_copied, "results_copied": results_copied}),),
        )

    return {
        "db": str(db_path()),
        "mosaics_copied": mosaics_copied,
        "mosaics_already_present": mosaics_skipped,
        "results_copied": results_copied,
        "results_already_present": results_skipped,
        "legacy_deleted": False,
    }


def register_mosaic(mosaic_id: str, folder: Path, meta: dict[str, Any], *, source: str = "new") -> None:
    init()
    conn = _connect()
    with _lock:
        conn.execute(
            """
            INSERT INTO mosaics (id, owner_id, filename, folder, meta_json, created_at, expires_at, bytes, source, copied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
              owner_id = excluded.owner_id,
              filename = excluded.filename,
              folder = excluded.folder,
              meta_json = excluded.meta_json,
              created_at = excluded.created_at,
              expires_at = excluded.expires_at,
              bytes = excluded.bytes
            """,
            (
                mosaic_id,
                str(meta["owner_id"]) if meta.get("owner_id") is not None else None,
                meta.get("filename") or meta.get("filename_stored") or "mosaic",
                str(Path(folder).resolve()),
                json.dumps(meta, separators=(",", ":")),
                meta.get("created"),
                meta.get("expires"),
                meta.get("bytes"),
                source,
            ),
        )


def get_find_results(user_id: int) -> dict[str, Any] | None:
    init()
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT * FROM find_results WHERE user_id = ?", (int(user_id),)).fetchone()
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
    import syncstore

    # Reuse the same payload limits/shape as the legacy sync store.
    results = syncstore._parse_json_object(raw, "Find results", syncstore.MAX_FIND_BYTES)
    conn = _connect()
    stamp = _now()
    payload = json.dumps(results, separators=(",", ":"))
    uid = int(user_id)
    with _lock:
        existing = conn.execute("SELECT user_id FROM find_results WHERE user_id = ?", (uid,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO find_results (user_id, results_json, version, updated_at, deleted_at, copied_at)
                VALUES (?, ?, 1, ?, NULL, NULL)
                """,
                (uid, payload, stamp),
            )
        else:
            conn.execute(
                """
                UPDATE find_results
                SET results_json = ?, version = version + 1, updated_at = ?, deleted_at = NULL
                WHERE user_id = ?
                """,
                (payload, stamp, uid),
            )
    return get_find_results(uid)
