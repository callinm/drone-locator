"""User feedback messages, stored in the existing accounts database."""

from __future__ import annotations

import time
from typing import Any

import accounts

MAX_LENGTH = 2000
MIN_LENGTH = 1
RATE_LIMIT = 5
RATE_WINDOW = 3600  # 5 messages per user (or IP) per hour

_hits_user: dict[int, list[float]] = {}
_hits_ip: dict[str, list[float]] = {}


def init() -> None:
    conn = accounts._connect()
    with accounts._lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              username TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at REAL NOT NULL,
              is_read INTEGER NOT NULL DEFAULT 0,
              read_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_feedback_unread ON feedback(is_read, created_at DESC);
            """
        )


def _trim(bucket: dict, key, window: float) -> list[float]:
    now = time.time()
    hits = [t for t in bucket.get(key, []) if now - t < window]
    bucket[key] = hits
    return hits


def allow_submit(user_id: int, ip: str | None) -> bool:
    with accounts._lock:
        user_hits = _trim(_hits_user, user_id, RATE_WINDOW)
        if len(user_hits) >= RATE_LIMIT:
            return False
        if ip:
            ip_hits = _trim(_hits_ip, ip, RATE_WINDOW)
            if len(ip_hits) >= RATE_LIMIT:
                return False
            ip_hits.append(time.time())
            _hits_ip[ip] = ip_hits
        user_hits.append(time.time())
        _hits_user[user_id] = user_hits
        return True


def reset_rate_limits() -> None:
    with accounts._lock:
        _hits_user.clear()
        _hits_ip.clear()


def _clean_message(raw: Any) -> str:
    text = "" if raw is None else str(raw)
    text = text.replace("\x00", "").strip()
    return text


def submit(user: accounts.User, message: Any, ip: str | None = None) -> dict[str, Any]:
    text = _clean_message(message)
    if len(text) < MIN_LENGTH:
        raise accounts.AuthError(400, "Write a short message before sending.")
    if len(text) > MAX_LENGTH:
        raise accounts.AuthError(400, f"Keep feedback to {MAX_LENGTH} characters or less.")
    if not allow_submit(user.id, ip):
        raise accounts.AuthError(429, "You can send up to 5 feedback messages per hour. Try again later.")
    now = time.time()
    conn = accounts._connect()
    with accounts._lock:
        cur = conn.execute(
            """
            INSERT INTO feedback (user_id, username, message, created_at, is_read)
            VALUES (?, ?, ?, ?, 0)
            """,
            (user.id, user.username, text, now),
        )
        row_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (row_id,)).fetchone()
    return public_item(row)


def public_item(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "message": row["message"],
        "created_at": row["created_at"],
        "is_read": bool(row["is_read"]),
        "read_at": row["read_at"],
    }


def list_for_admin(*, unread_first: bool = True, limit: int = 200) -> list[dict[str, Any]]:
    conn = accounts._connect()
    order = "is_read ASC, created_at DESC" if unread_first else "created_at DESC"
    cap = max(1, min(int(limit), 500))
    with accounts._lock:
        rows = conn.execute(
            f"SELECT * FROM feedback ORDER BY {order} LIMIT ?",
            (cap,),
        ).fetchall()
    return [public_item(r) for r in rows]


def set_read(item_id: int, is_read: bool) -> dict[str, Any] | None:
    conn = accounts._connect()
    now = time.time() if is_read else None
    with accounts._lock:
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (int(item_id),)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE feedback SET is_read = ?, read_at = ? WHERE id = ?",
            (1 if is_read else 0, now, int(item_id)),
        )
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (int(item_id),)).fetchone()
    return public_item(row)
