"""User accounts, sessions, CSRF, and signup rate limits."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import config

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,31}$")
SESSION_TTL = 30 * 24 * 3600
SIGNUP_LIMIT = 5
SIGNUP_WINDOW = 3600
LOGIN_LIMIT = 10
LOGIN_WINDOW = 900
# Password reset: emailed one-time links, with admin-made links as the fallback.
RESET_TTL = 30 * 60
RESET_LIMIT = 10
RESET_WINDOW = 900
RESET_REQUEST_LIMIT = 5
RESET_REQUEST_WINDOW = 3600
RESET_EMAIL_COOLDOWN = 5 * 60  # at most one auto email per account per 5 minutes
EMAIL_RE = re.compile(r"^[^@\s<>,;\"]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$")
RESET_GENERIC_ERROR = "This reset link is invalid, already used, or expired. Tap Forgot password on the login page to get a new one."
SESSION_COOKIE = "dm_session"
CSRF_COOKIE = "dm_csrf"
COOKIE_PATH = "/drone-mapper"
HONEYPOT_FIELD = "website"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None
_signup_hits: dict[str, list[float]] = {}
_login_hits: dict[str, list[float]] = {}
_reset_hits: dict[str, list[float]] = {}
_reset_request_hits: dict[str, list[float]] = {}


class AuthError(Exception):
    def __init__(self, status: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


@dataclass
class User:
    id: int
    username: str
    is_admin: bool
    mosaic_enabled: bool
    created_at: float
    photo_cap: int | None = None
    plan: str = "default"
    email: str | None = None
    last_login: float | None = None

    def public(self) -> dict[str, Any]:
        cap = effective_photo_cap(self)
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "mosaic_enabled": self.mosaic_enabled,
            "created_at": self.created_at,
            "photo_cap": cap,
            "photo_cap_override": self.photo_cap,
            "plan": self.plan or "default",
            "min_photos": 30,
            "email": self.email,
            "last_login": self.last_login,
            "status": "active",
            "features": {"mosaic_enabled": self.mosaic_enabled},
        }


@dataclass
class Session:
    token: str
    token_hash: str
    user_id: int
    csrf: str
    expires_at: float


def _db_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "accounts.sqlite"


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = _db_path()
    with _lock:
        if _conn is not None and _conn_path == path:
            return _conn
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              username TEXT NOT NULL,
              username_lower TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 0,
              mosaic_enabled INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              csrf TEXT NOT NULL,
              expires_at REAL NOT NULL,
              created_at REAL NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS password_resets (
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              used_at REAL,
              created_by INTEGER,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS password_reset_requests (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at REAL NOT NULL,
              handled_at REAL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS plans (
              id TEXT PRIMARY KEY,
              label TEXT NOT NULL DEFAULT '',
              photo_cap INTEGER NOT NULL DEFAULT 500
            );
            """
        )
        _ensure_column(conn, "users", "photo_cap", "photo_cap INTEGER")
        _ensure_column(conn, "users", "plan", "plan TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "users", "email", "email TEXT")
        _ensure_column(conn, "users", "last_login", "last_login REAL")
        _ensure_column(conn, "password_reset_requests", "status", "status TEXT")
        _ensure_column(conn, "password_reset_requests", "emailed_at", "emailed_at REAL")
        conn.execute(
            "INSERT OR IGNORE INTO plans (id, label, photo_cap) VALUES (?, ?, ?)",
            ("default", "Default", 500),
        )
        _conn = conn
        _conn_path = path
        return conn


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _row_get(row: sqlite3.Row, key: str, default=None):
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    return default


def list_plans() -> list[dict[str, Any]]:
    conn = _connect()
    with _lock:
        rows = conn.execute("SELECT id, label, photo_cap FROM plans ORDER BY id").fetchall()
    return [{"id": r["id"], "label": r["label"], "photo_cap": int(r["photo_cap"])} for r in rows]


def get_plan(plan_id: str | None) -> dict[str, Any]:
    key = (plan_id or "default").strip() or "default"
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT id, label, photo_cap FROM plans WHERE id = ?", (key,)).fetchone()
        if row is None and key != "default":
            row = conn.execute("SELECT id, label, photo_cap FROM plans WHERE id = ?", ("default",)).fetchone()
    if row is None:
        return {"id": "default", "label": "Default", "photo_cap": 500}
    return {"id": row["id"], "label": row["label"], "photo_cap": int(row["photo_cap"])}


def upsert_plan(plan_id: str, photo_cap: int, label: str | None = None) -> dict[str, Any]:
    import photolimits

    key = (plan_id or "").strip() or "default"
    cap = photolimits.clamp_cap(photo_cap)
    conn = _connect()
    with _lock:
        existing = conn.execute("SELECT id, label FROM plans WHERE id = ?", (key,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO plans (id, label, photo_cap) VALUES (?, ?, ?)",
                (key, (label or key).strip() or key, cap),
            )
        else:
            conn.execute(
                "UPDATE plans SET photo_cap = ?, label = COALESCE(?, label) WHERE id = ?",
                (cap, (label or "").strip() or None, key),
            )
    return get_plan(key)


def effective_photo_cap(user: "User | None" = None, plan_id: str | None = None, override: int | None = None) -> int:
    import photolimits

    if user is not None:
        if user.photo_cap is not None:
            return photolimits.clamp_cap(user.photo_cap)
        plan_id = user.plan
    if override is not None:
        return photolimits.clamp_cap(override)
    return photolimits.clamp_cap(get_plan(plan_id)["photo_cap"])


def reset_connection() -> None:
    """Used by tests when DATA_DIR changes."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _conn_path = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$%s$%s" % (salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt_hex, dk_hex = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(dk_hex)),
        )
        return hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    except Exception:
        return False


def normalize_username(username: str) -> str:
    return (username or "").strip()


def validate_username(username: str) -> str:
    name = normalize_username(username)
    if not USERNAME_RE.match(name):
        raise AuthError(
            400,
            "Username must be 3–32 characters, start with a letter, and use only letters, numbers, _ or -.",
        )
    return name


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise AuthError(400, "Password is required.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(400, "Password is too long.")
    return password


def _row_user(row: sqlite3.Row) -> User:
    raw_cap = _row_get(row, "photo_cap")
    cap = int(raw_cap) if raw_cap is not None else None
    plan = _row_get(row, "plan") or "default"
    return User(
        id=int(row["id"]),
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        mosaic_enabled=bool(row["mosaic_enabled"]),
        created_at=float(row["created_at"]),
        photo_cap=cap,
        plan=str(plan),
        email=(_row_get(row, "email") or None),
        last_login=(float(v) if (v := _row_get(row, "last_login")) is not None else None),
    )


def get_user_by_id(user_id: int) -> User | None:
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_user(row) if row else None


def get_user_by_username(username: str) -> User | None:
    key = normalize_username(username).lower()
    if not key:
        return None
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT * FROM users WHERE username_lower = ?", (key,)).fetchone()
    return _row_user(row) if row else None


def _job_counts_by_user() -> dict[int, int]:
    """Stockpile + processing job counts per user id. Missing tables count as zero."""
    conn = _connect()
    counts: dict[int, int] = {}
    with _lock:
        for table in ("stockpile_jobs", "processing_jobs"):
            try:
                rows = conn.execute(f"SELECT user_id, COUNT(*) FROM {table} GROUP BY user_id").fetchall()
            except Exception:
                continue
            for row in rows:
                uid = int(row[0])
                counts[uid] = counts.get(uid, 0) + int(row[1])
    return counts


def list_users_for_admin(*, q: str | None = None, sort: str = "username", direction: str = "asc") -> list[dict]:
    users = list_users()
    counts = _job_counts_by_user()
    query = (q or "").strip().lower()
    items = []
    for u in users:
        item = u.public()
        item["job_count"] = int(counts.get(u.id, 0))
        item["credits"] = None  # placeholder for a future credit system
        if query:
            hay = " ".join(
                str(item.get(k) or "")
                for k in ("username", "email", "plan", "status")
            ).lower()
            if query not in hay:
                continue
        items.append(item)
    key = sort if sort in {"username", "email", "created_at", "last_login", "job_count", "status", "plan"} else "username"
    reverse = str(direction).lower() == "desc"

    def sort_val(row):
        v = row.get(key)
        if v is None:
            return "" if key in {"username", "email", "status", "plan"} else -1
        return v

    items.sort(key=sort_val, reverse=reverse)
    return items


def list_users() -> list[User]:
    conn = _connect()
    with _lock:
        rows = conn.execute("SELECT * FROM users ORDER BY username_lower").fetchall()
    return [_row_user(r) for r in rows]


def create_user(
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    mosaic_enabled: bool = False,
    email: str | None = None,
) -> User:
    name = validate_username(username)
    validate_password(password)
    clean_email = validate_email(email)
    conn = _connect()
    now = time.time()
    with _lock:
        existing = conn.execute(
            "SELECT id FROM users WHERE username_lower = ?", (name.lower(),)
        ).fetchone()
        if existing:
            raise AuthError(409, "That username is already taken.")
        cur = conn.execute(
            """
            INSERT INTO users (username, username_lower, password_hash, is_admin, mosaic_enabled, created_at, email)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, name.lower(), hash_password(password), int(is_admin), int(mosaic_enabled), now, clean_email),
        )
        return get_user_by_id(int(cur.lastrowid))  # type: ignore[arg-type]


def ensure_user(
    username: str,
    password: str,
    *,
    is_admin: bool,
    mosaic_enabled: bool,
) -> tuple[User, bool]:
    """Create the user if missing. Never rotates an existing password.

    Returns (user, created).
    """
    existing = get_user_by_username(username)
    if existing:
        conn = _connect()
        with _lock:
            conn.execute(
                "UPDATE users SET is_admin = ?, mosaic_enabled = ? WHERE id = ?",
                (int(is_admin), int(mosaic_enabled), existing.id),
            )
        updated = get_user_by_username(username)
        assert updated is not None
        return updated, False
    return create_user(username, password, is_admin=is_admin, mosaic_enabled=mosaic_enabled), True


def set_mosaic_enabled(username: str, enabled: bool) -> User:
    user = get_user_by_username(username)
    if user is None:
        raise AuthError(404, "User not found.")
    conn = _connect()
    with _lock:
        conn.execute(
            "UPDATE users SET mosaic_enabled = ? WHERE id = ?",
            (int(enabled), user.id),
        )
    updated = get_user_by_id(user.id)
    assert updated is not None
    return updated


def set_photo_limits(
    username: str,
    *,
    photo_cap: int | None = None,
    clear_override: bool = False,
    plan: str | None = None,
) -> User:
    """Update a user's photo cap and/or plan. clear_override reverts to the plan default."""
    import photolimits

    user = get_user_by_username(username)
    if user is None:
        raise AuthError(404, "User not found.")
    plan_id = (plan if plan is not None else user.plan) or "default"
    plan_id = str(plan_id).strip() or "default"
    conn = _connect()
    with _lock:
        if clear_override:
            conn.execute(
                "UPDATE users SET photo_cap = NULL, plan = ? WHERE id = ?",
                (plan_id, user.id),
            )
        elif photo_cap is not None:
            conn.execute(
                "UPDATE users SET photo_cap = ?, plan = ? WHERE id = ?",
                (photolimits.clamp_cap(photo_cap), plan_id, user.id),
            )
        else:
            conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan_id, user.id))
    updated = get_user_by_id(user.id)
    assert updated is not None
    return updated


def change_password(user: User, current: str, new: str) -> None:
    validate_password(new)
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
        if not row or not verify_password(current, row["password_hash"]):
            raise AuthError(403, "Current password is incorrect.")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new), user.id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))


def check_password(user: User, password: str) -> bool:
    conn = _connect()
    with _lock:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    return bool(row) and verify_password(password, row["password_hash"])


def delete_user(user: User) -> None:
    from store import delete_user_mosaics
    import syncstore

    delete_user_mosaics(str(user.id))
    syncstore.delete_user_data(user.id)
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user.id,))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


def create_session(user: User) -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + SESSION_TTL
    token_hash = _hash_token(token)
    conn = _connect()
    with _lock:
        _purge_sessions(conn)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user.id, csrf, expires, now),
        )
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user.id))
    return Session(token=token, token_hash=token_hash, user_id=user.id, csrf=csrf, expires_at=expires)


def load_session(token: str | None) -> tuple[User, Session] | None:
    if not token:
        return None
    token_hash = _hash_token(token)
    conn = _connect()
    with _lock:
        _purge_sessions(conn)
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    if not row:
        return None
    user = get_user_by_id(int(row["user_id"]))
    if user is None:
        return None
    sess = Session(
        token=token,
        token_hash=token_hash,
        user_id=user.id,
        csrf=row["csrf"],
        expires_at=float(row["expires_at"]),
    )
    return user, sess


def destroy_session(token: str | None) -> None:
    if not token:
        return
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def rotate_csrf(session: Session) -> str:
    csrf = secrets.token_urlsafe(32)
    conn = _connect()
    with _lock:
        conn.execute(
            "UPDATE sessions SET csrf = ? WHERE token_hash = ?",
            (csrf, session.token_hash),
        )
    session.csrf = csrf
    return csrf


def _trim_hits(bucket: dict[str, list[float]], key: str, window: float) -> list[float]:
    now = time.time()
    hits = [t for t in bucket.get(key, []) if now - t < window]
    bucket[key] = hits
    return hits


def allow_signup(ip: str) -> bool:
    with _lock:
        hits = _trim_hits(_signup_hits, ip, SIGNUP_WINDOW)
        if len(hits) >= SIGNUP_LIMIT:
            return False
        hits.append(time.time())
        _signup_hits[ip] = hits
        return True


def allow_login(ip: str) -> bool:
    with _lock:
        hits = _trim_hits(_login_hits, ip, LOGIN_WINDOW)
        if len(hits) >= LOGIN_LIMIT:
            return False
        hits.append(time.time())
        _login_hits[ip] = hits
        return True


def allow_reset(ip: str) -> bool:
    with _lock:
        hits = _trim_hits(_reset_hits, ip, RESET_WINDOW)
        if len(hits) >= RESET_LIMIT:
            return False
        hits.append(time.time())
        _reset_hits[ip] = hits
        return True


def allow_reset_request(ip: str) -> bool:
    with _lock:
        hits = _trim_hits(_reset_request_hits, ip, RESET_REQUEST_WINDOW)
        if len(hits) >= RESET_REQUEST_LIMIT:
            return False
        hits.append(time.time())
        _reset_request_hits[ip] = hits
        return True


def create_reset_token(user: User, created_by: User | None = None) -> tuple[str, float]:
    """Make a single-use reset token. Only its SHA-256 is stored.

    Any older unused token for the same user stops working. The current
    password is left alone until the reset is completed.
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + RESET_TTL
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM password_resets WHERE expires_at < ? OR used_at IS NOT NULL", (now - 7 * 86400,))
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now, user.id),
        )
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, created_by) VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), user.id, now, expires, created_by.id if created_by else None),
        )
        if created_by is not None:
            conn.execute(
                "UPDATE password_reset_requests SET handled_at = ?, status = ? WHERE user_id = ? AND handled_at IS NULL",
                (now, f"admin made a link ({created_by.username})", user.id),
            )
    return token, expires


def _valid_reset_row(conn: sqlite3.Connection, token: str):
    if not token or not isinstance(token, str) or len(token) > 200:
        return None
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token_hash = ?", (_hash_token(token),)
    ).fetchone()
    if not row or row["used_at"] is not None or float(row["expires_at"]) < time.time():
        return None
    return row


def reset_token_valid(token: str) -> bool:
    conn = _connect()
    with _lock:
        return _valid_reset_row(conn, token) is not None


def complete_password_reset(token: str, new_password: str) -> User:
    """Use a reset token: set the new password, burn the token, end all sessions."""
    conn = _connect()
    with _lock:
        row = _valid_reset_row(conn, token)
        if row is None:
            raise AuthError(400, RESET_GENERIC_ERROR, code="reset_invalid")
        validate_password(new_password)
        now = time.time()
        # Mark used first, guarded, so two simultaneous submits can't both win.
        cur = conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
            (now, row["token_hash"]),
        )
        if cur.rowcount != 1:
            raise AuthError(400, RESET_GENERIC_ERROR, code="reset_invalid")
        user_id = int(row["user_id"])
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now, user_id),
        )
    user = get_user_by_id(user_id)
    if user is None:
        raise AuthError(400, RESET_GENERIC_ERROR, code="reset_invalid")
    return user


def validate_email(email: str | None) -> str | None:
    """Optional email: blank means none. Returns the trimmed address or raises."""
    value = (email or "").strip()
    if not value:
        return None
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise AuthError(400, "That email address doesn't look right.")
    return value


def set_email(user: User, email: str | None) -> User:
    clean = validate_email(email)
    conn = _connect()
    with _lock:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (clean, user.id))
    updated = get_user_by_id(user.id)
    assert updated is not None
    return updated


def record_reset_request(username: str) -> tuple[int, User] | None:
    """Note that someone asked for a reset. Silent (None) when the user doesn't exist.

    Returns (request_id, user) so the caller can try to email a link.
    """
    user = get_user_by_username(username or "")
    if user is None:
        return None
    conn = _connect()
    now = time.time()
    with _lock:
        open_row = conn.execute(
            "SELECT id FROM password_reset_requests WHERE user_id = ? AND handled_at IS NULL", (user.id,)
        ).fetchone()
        if open_row:
            conn.execute(
                "UPDATE password_reset_requests SET created_at = ?, status = ? WHERE id = ?",
                (now, "asked", open_row["id"]),
            )
            return int(open_row["id"]), user
        cur = conn.execute(
            "INSERT INTO password_reset_requests (user_id, created_at, status) VALUES (?, ?, ?)",
            (user.id, now, "asked"),
        )
        return int(cur.lastrowid), user  # type: ignore[arg-type]


def reset_email_recently_sent(user: User) -> bool:
    conn = _connect()
    with _lock:
        row = conn.execute(
            "SELECT MAX(emailed_at) AS t FROM password_reset_requests WHERE user_id = ?", (user.id,)
        ).fetchone()
    last = row["t"] if row else None
    return last is not None and time.time() - float(last) < RESET_EMAIL_COOLDOWN


def revoke_reset_token(token: str) -> None:
    conn = _connect()
    with _lock:
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
            (time.time(), _hash_token(token)),
        )


def finish_reset_request(request_id: int, status: str, *, emailed: bool) -> None:
    """Write the outcome to the admin log. Emailed requests leave the to-do box;
    failed/unsent ones stay open so the admin can make a link by hand."""
    conn = _connect()
    now = time.time()
    with _lock:
        if emailed:
            conn.execute(
                "UPDATE password_reset_requests SET status = ?, emailed_at = ?, handled_at = ? WHERE id = ?",
                (status[:200], now, now, int(request_id)),
            )
        else:
            conn.execute(
                "UPDATE password_reset_requests SET status = ?, handled_at = NULL WHERE id = ?",
                (status[:200], int(request_id)),
            )


def _request_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "username": r["username"],
        "created_at": r["created_at"],
        "status": r["status"] or "asked",
        "emailed_at": r["emailed_at"],
        "handled_at": r["handled_at"],
        "has_email": bool(r["email"]),
    }


def list_reset_requests() -> list[dict[str, Any]]:
    conn = _connect()
    with _lock:
        rows = conn.execute(
            """SELECT r.id, r.created_at, r.status, r.emailed_at, r.handled_at, u.username, u.email
               FROM password_reset_requests r
               JOIN users u ON u.id = r.user_id WHERE r.handled_at IS NULL ORDER BY r.created_at DESC"""
        ).fetchall()
    return [_request_row(r) for r in rows]


def reset_request_log(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    with _lock:
        rows = conn.execute(
            """SELECT r.id, r.created_at, r.status, r.emailed_at, r.handled_at, u.username, u.email
               FROM password_reset_requests r
               JOIN users u ON u.id = r.user_id ORDER BY r.created_at DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [_request_row(r) for r in rows]


def dismiss_reset_request(request_id: int) -> None:
    conn = _connect()
    with _lock:
        conn.execute(
            "UPDATE password_reset_requests SET handled_at = ? WHERE id = ? AND handled_at IS NULL",
            (time.time(), int(request_id)),
        )


def reset_rate_limits() -> None:
    with _lock:
        _signup_hits.clear()
        _login_hits.clear()
        _reset_hits.clear()
        _reset_request_hits.clear()


def signup_honeypot_triggered(payload: dict) -> bool:
    value = payload.get(HONEYPOT_FIELD)
    if value is None:
        return False
    return str(value).strip() != ""


def generate_password() -> str:
    # 24+ url-safe characters, always longer than the minimum.
    return secrets.token_urlsafe(18)


def write_login_file(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lines = [
        "# Drone Mapper logins",
        f"# Written {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "# File mode 0600. Do not share or commit this file.",
        "",
    ]
    for item in entries:
        lines.extend(
            [
                f"username: {item['username']}",
                f"password: {item['password']}",
                f"role: {item['role']}",
                f"mosaic_enabled: {item['mosaic_enabled']}",
                "",
            ]
        )
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines))
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    os.chmod(path, 0o600)


def cookie_secure(request) -> bool:
    env = os.environ.get("FIND_API_SECURE_COOKIES")
    if env is not None:
        return env not in {"0", "false", "no"}
    proto = request.headers.get("x-forwarded-proto", "")
    if proto.lower() == "https":
        return True
    return request.url.scheme == "https"


def cookie_path() -> str:
    # Must be "/" now that the dashboard is served at the site root. A path of
    # /drone-mapper hid the CSRF cookie from pages at / and could drop the session.
    value = os.environ.get("FIND_API_COOKIE_PATH")
    if value in {None, "", "/drone-mapper", "/drone-mapper/"}:
        return "/"
    return value
