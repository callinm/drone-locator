"""
Drone Mapper API — accounts, admin, and Find My Drone matcher.

Bound to 127.0.0.1. Caddy reverse-proxies /drone-mapper/api/* here with
handle_path so these routes are /health, /auth/*, /admin/*, /sync/*, /mosaics.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import accounts
import config
import dashsync
import feedback
import locatorstore
import mailer
import flights
import admin_status
import processing
import stockpile
import syncstore
from georef import GeoRef
from color_match import expected_target_px, gsd_meters, hex_to_bgr, is_small_marking, match_color, ranges_for_request, search_with_color
from matcher import search, to_gray
from store import (
    cleanup,
    crop_bbox,
    crop_bbox_color,
    decode_query,
    decode_query_color,
    load_mosaic,
    open_color,
    open_gray,
    preview_jpeg,
    save_mosaic,
)

_match_sema: asyncio.Semaphore | None = None
_match_pool: ThreadPoolExecutor | None = None

SESSION_COOKIE = accounts.SESSION_COOKIE
CSRF_COOKIE = accounts.CSRF_COOKIE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _match_sema, _match_pool
    _match_sema = asyncio.Semaphore(max(1, int(config.MATCH_CONCURRENCY)))
    _match_pool = ThreadPoolExecutor(
        max_workers=max(2, int(config.MATCH_CONCURRENCY)),
        thread_name_prefix="match",
    )
    accounts._connect()
    syncstore.init()
    stockpile.init()
    flights.init()
    processing.init()
    feedback.init()
    locatorstore.init()
    locatorstore.migrate_from_legacy()
    admin_status.start_sampler()
    cleanup()
    stop_poller = processing.start_poller()
    try:
        yield
    finally:
        stop_poller()
        if _match_pool is not None:
            _match_pool.shutdown(wait=False, cancel_futures=False)


app = FastAPI(
    title="Drone Mapper API",
    version="0.6.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wheresthepelican.com",
        "http://127.0.0.1:8766",
        "http://localhost:8766",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
    ],
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost):\d+",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*", "Authorization", "X-CSRF-Token"],
    allow_credentials=True,
)


@app.get("/health")
def health():
    return {"ok": True, "service": "drone-mapper-find", "version": "0.6.0", "auth": True}


def _http_400(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and request.client and request.client.host in {"127.0.0.1", "::1"}:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    return request.cookies.get(SESSION_COOKIE)


def _is_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization") or ""
    return auth.lower().startswith("bearer ") and bool(auth[7:].strip())


# After the dashboard moved to the site root, leftover cookies at /drone-mapper
# would not be visible to pages at / (and could fight a new Path=/ cookie).
_STALE_COOKIE_PATHS = ("/drone-mapper", "/drone-mapper/")


def _delete_auth_cookies_at(response: Response, path: str, secure: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path=path, secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path=path, secure=secure, httponly=False, samesite="lax")


def _set_auth_cookies(response: Response, request: Request, session: accounts.Session) -> None:
    secure = accounts.cookie_secure(request)
    path = accounts.cookie_path()
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=accounts.SESSION_TTL,
        path=path,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf,
        max_age=accounts.SESSION_TTL,
        path=path,
        httponly=False,
        secure=secure,
        samesite="lax",
    )
    for stale in _STALE_COOKIE_PATHS:
        if stale != path:
            _delete_auth_cookies_at(response, stale, secure)


def _clear_auth_cookies(response: Response, request: Request) -> None:
    secure = accounts.cookie_secure(request)
    paths = {accounts.cookie_path(), *_STALE_COOKIE_PATHS}
    for path in paths:
        _delete_auth_cookies_at(response, path, secure)


def _current(request: Request) -> tuple[accounts.User, accounts.Session] | None:
    return accounts.load_session(_extract_token(request))


def _require_login(request: Request) -> tuple[accounts.User, accounts.Session]:
    pair = _current(request)
    if not pair:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return pair


def _require_csrf(request: Request, session: accounts.Session) -> None:
    # Cookie-authenticated browser calls must present the CSRF token.
    # Bearer (Android / API) tokens are not vulnerable to cookie CSRF.
    if _is_bearer(request):
        return
    header = request.headers.get("x-csrf-token") or request.headers.get("x-csrftoken") or ""
    # Cookie alone is not enough — a cross-site POST would send it automatically.
    if not header or not hmac_eq(header, session.csrf):
        raise HTTPException(status_code=403, detail="CSRF check failed.")


def hmac_eq(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _require_mosaic_user(request: Request) -> tuple[accounts.User, accounts.Session]:
    user, session = _require_login(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_csrf(request, session)
    if not user.mosaic_enabled:
        raise HTTPException(
            status_code=403,
            detail="Photo Mosaic is locked. Contact an admin to enable mapping for your account.",
        )
    return user, session


def _require_admin(request: Request) -> tuple[accounts.User, accounts.Session]:
    user, session = _require_login(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_csrf(request, session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only.")
    return user, session


dashsync.register(app, require_login=_require_login, require_csrf=_require_csrf)
flights.register(app, require_login=_require_login, require_csrf=_require_csrf)
stockpile.register(app, require_mosaic=_require_mosaic_user)
processing.register(app, require_mosaic=_require_mosaic_user, require_csrf=_require_csrf)


def _owned_mosaic(mosaic_id: str, user: accounts.User):
    try:
        mosaic = load_mosaic(mosaic_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if mosaic.owner_id != str(user.id):
        raise HTTPException(status_code=404, detail="Unknown or expired mosaic id.")
    return mosaic


def _auth_payload(user: accounts.User, session: accounts.Session, include_token: bool = False) -> dict:
    body = {"user": user.public(), "csrf": session.csrf, "authenticated": True}
    if include_token:
        body["token"] = session.token
    return body


@app.get("/auth/me")
def auth_me(request: Request):
    pair = _current(request)
    if not pair:
        return {"authenticated": False, "user": None, "csrf": None}
    user, session = pair
    return {"authenticated": True, "user": user.public(), "csrf": session.csrf}


@app.post("/auth/signup")
async def auth_signup(request: Request):
    ip = _client_ip(request)
    if not accounts.allow_signup(ip):
        raise HTTPException(status_code=429, detail="Too many signup attempts. Try again later.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if accounts.signup_honeypot_triggered(payload):
        # Pretend nothing special happened; do not create an account.
        raise HTTPException(status_code=400, detail="Could not create that account.")
    username = payload.get("username") or ""
    password = payload.get("password") or ""
    email = str(payload.get("email") or "")
    try:
        user = accounts.create_user(username, password, is_admin=False, mosaic_enabled=False, email=email)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    session = accounts.create_session(user)
    response = JSONResponse(_auth_payload(user, session, include_token=True), status_code=201)
    _set_auth_cookies(response, request, session)
    return response


@app.post("/auth/login")
async def auth_login(request: Request):
    ip = _client_ip(request)
    if not accounts.allow_login(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    user = accounts.get_user_by_username(username)
    # Constant-ish failure message; still verify when the user exists.
    if user is None or not accounts.check_password(user, password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    session = accounts.create_session(user)
    response = JSONResponse(_auth_payload(user, session, include_token=True))
    _set_auth_cookies(response, request, session)
    return response


@app.post("/auth/logout")
def auth_logout(request: Request):
    token = _extract_token(request)
    pair = accounts.load_session(token)
    if pair:
        _require_csrf(request, pair[1])
    accounts.destroy_session(token)
    response = JSONResponse({"ok": True, "authenticated": False})
    _clear_auth_cookies(response, request)
    return response


@app.post("/auth/password")
async def auth_password(request: Request):
    user, session = _require_login(request)
    _require_csrf(request, session)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    current = (payload or {}).get("current_password") or ""
    new = (payload or {}).get("new_password") or ""
    try:
        accounts.change_password(user, current, new)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    # Previous sessions were dropped; issue a fresh one.
    session = accounts.create_session(user)
    response = JSONResponse(_auth_payload(user, session, include_token=True))
    _set_auth_cookies(response, request, session)
    return response


@app.post("/auth/delete")
async def auth_delete(request: Request):
    user, session = _require_login(request)
    _require_csrf(request, session)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    password = (payload or {}).get("password") or ""
    if not accounts.check_password(user, password):
        raise HTTPException(status_code=403, detail="Password is incorrect.")
    accounts.delete_user(user)
    response = JSONResponse({"ok": True, "deleted": user.username})
    _clear_auth_cookies(response, request)
    return response


async def _json_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


RESET_REQUEST_MESSAGE = (
    "Thanks. If that account exists, we'll email a reset link to the address on it "
    "(or your Piletally admin will send you one). Your current password still works until then."
)


@app.post("/auth/email")
async def auth_email(request: Request):
    """Set or clear the signed-in user's optional email (used for reset links)."""
    user, session = _require_login(request)
    _require_csrf(request, session)
    payload = await _json_payload(request)
    try:
        user = accounts.set_email(user, str(payload.get("email") or ""))
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"ok": True, "user": user.public()}


def _auto_send_reset(request_id: int, user_id: int) -> None:
    """Background: email a one-time link and write the outcome to the admin log.

    Runs after the response is sent, so timing never reveals whether the account exists.
    The token and link never go in the log.
    """
    try:
        user = accounts.get_user_by_id(user_id)
        if user is None:
            return
        if not user.email:
            accounts.finish_reset_request(request_id, "no email on account", emailed=False)
            return
        if not mailer.is_configured():
            accounts.finish_reset_request(request_id, mailer.NOT_CONFIGURED, emailed=False)
            return
        if accounts.reset_email_recently_sent(user):
            accounts.finish_reset_request(
                request_id, "skipped: a link was emailed less than 5 minutes ago", emailed=True
            )
            return
        token, _expires = accounts.create_reset_token(user)
        url = f"{config.PUBLIC_URL}/reset/#t={token}"
        sent, status = mailer.send_reset_email(user.email, user.username, url, accounts.RESET_TTL // 60)
        if not sent:
            accounts.revoke_reset_token(token)
        accounts.finish_reset_request(request_id, status, emailed=sent)
    except Exception as exc:  # never let a mail problem break the request
        try:
            accounts.finish_reset_request(request_id, f"email failed: {type(exc).__name__}", emailed=False)
        except Exception:
            pass


@app.post("/auth/reset/request")
async def auth_reset_request(request: Request, background: BackgroundTasks):
    """Self-serve "Forgot password": log the request and email a link in the background.

    Same answer whether or not the account exists (no user enumeration).
    """
    ip = _client_ip(request)
    if not accounts.allow_reset_request(ip):
        raise HTTPException(status_code=429, detail="Too many tries. Wait a while and try again.")
    payload = await _json_payload(request)
    username = str(payload.get("username") or "").strip()[:64]
    if username and not accounts.signup_honeypot_triggered(payload):
        try:
            found = accounts.record_reset_request(username)
        except Exception:
            found = None
        if found is not None:
            request_id, user = found
            background.add_task(_auto_send_reset, request_id, user.id)
    return {"ok": True, "message": RESET_REQUEST_MESSAGE}


@app.post("/auth/reset/check")
async def auth_reset_check(request: Request):
    ip = _client_ip(request)
    if not accounts.allow_reset(ip):
        raise HTTPException(status_code=429, detail="Too many tries. Wait a while and try again.")
    payload = await _json_payload(request)
    return {"valid": accounts.reset_token_valid(str(payload.get("token") or ""))}


@app.post("/auth/reset")
async def auth_reset(request: Request):
    ip = _client_ip(request)
    if not accounts.allow_reset(ip):
        raise HTTPException(status_code=429, detail="Too many tries. Wait a while and try again.")
    payload = await _json_payload(request)
    token = str(payload.get("token") or "")
    new = payload.get("new_password") or ""
    try:
        user = accounts.complete_password_reset(token, new)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    # All of the user's sessions were ended. They log in with the new password.
    response = JSONResponse({"ok": True, "username": user.username})
    _clear_auth_cookies(response, request)
    return response


@app.post("/admin/users/{username}/reset-link")
def admin_reset_link(username: str, request: Request):
    admin, _ = _require_admin(request)
    user = accounts.get_user_by_username(username)
    if user is None:
        raise HTTPException(status_code=404, detail="Unknown user.")
    token, expires = accounts.create_reset_token(user, created_by=admin)
    # Token rides in the URL fragment so it never reaches server logs or Referer headers.
    return {"username": user.username, "url": f"{config.PUBLIC_URL}/reset/#t={token}", "expires_at": expires}


@app.get("/admin/reset-requests")
def admin_reset_requests(request: Request):
    _require_admin(request)
    return {
        "requests": accounts.list_reset_requests(),
        "log": accounts.reset_request_log(),
        "email_configured": mailer.is_configured(),
    }


@app.post("/admin/reset-requests/{request_id}/dismiss")
def admin_reset_request_dismiss(request_id: int, request: Request):
    _require_admin(request)
    accounts.dismiss_reset_request(request_id)
    return {"ok": True}


@app.get("/admin/users")
def admin_list_users(request: Request, q: str | None = None, sort: str = "username", direction: str = "asc"):
    _require_admin(request)
    return {"users": accounts.list_users_for_admin(q=q, sort=sort, direction=direction)}


@app.post("/admin/users/{username}/mosaic")
async def admin_toggle_mosaic(username: str, request: Request):
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if "mosaic_enabled" not in (payload or {}):
        raise HTTPException(status_code=400, detail="mosaic_enabled is required.")
    enabled = bool(payload["mosaic_enabled"])
    try:
        user = accounts.set_mosaic_enabled(username, enabled)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"user": user.public()}


@app.post("/admin/users/{username}/limits")
async def admin_set_limits(username: str, request: Request):
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if "photo_cap" not in payload and "plan" not in payload and not payload.get("clear_photo_cap"):
        raise HTTPException(status_code=400, detail="Provide photo_cap, plan, or clear_photo_cap.")
    try:
        user = accounts.set_photo_limits(
            username,
            photo_cap=payload.get("photo_cap"),
            clear_override=bool(payload.get("clear_photo_cap")),
            plan=payload.get("plan"),
        )
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"user": user.public()}


@app.get("/admin/plans")
def admin_list_plans(request: Request):
    _require_admin(request)
    return {"plans": accounts.list_plans()}


@app.post("/feedback")
async def post_feedback(request: Request):
    """Signed-in users can send a short message to the admin."""
    user, session = _require_login(request)
    _require_csrf(request, session)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        item = feedback.submit(user, payload.get("message"), ip=_client_ip(request))
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"ok": True, "feedback": item}


@app.get("/admin/feedback")
def admin_list_feedback(request: Request):
    _require_admin(request)
    return {"feedback": feedback.list_for_admin()}


@app.post("/admin/feedback/{item_id}/read")
async def admin_mark_feedback(request: Request, item_id: int):
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    is_read = payload.get("is_read")
    if is_read is None:
        is_read = payload.get("read", True)
    item = feedback.set_read(item_id, bool(is_read))
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown feedback message.")
    return {"feedback": item}


@app.get("/admin/processing")
def admin_processing(request: Request):
    """Running job, queue, WebODM/NodeODM, and host RAM/swap/disk/CPU. Admin only."""
    _require_admin(request)
    return admin_status.processing_overview()


@app.post("/mosaics")
async def upload_mosaic(
    request: Request,
    file: Optional[UploadFile] = File(None),
    west: Optional[float] = Form(None),
    south: Optional[float] = Form(None),
    east: Optional[float] = Form(None),
    north: Optional[float] = Form(None),
):
    user, _session = _require_mosaic_user(request)
    if file is None:
        raise HTTPException(status_code=400, detail="A mosaic image file is required.")
    raw = await file.read()
    bounds = None
    if all(v is not None for v in (west, south, east, north)):
        bounds = (float(west), float(south), float(east), float(north))
    try:
        mosaic = await asyncio.to_thread(
            save_mosaic,
            raw,
            file.filename or "mosaic",
            file.content_type or "",
            bounds,
            str(user.id),
        )
    except ValueError as exc:
        raise _http_400(exc) from exc
    return _mosaic_payload(mosaic)


@app.get("/mosaics/{mosaic_id}/preview")
def preview_mosaic(mosaic_id: str, request: Request, max_dim: int = 2048):
    user, _session = _require_mosaic_user(request)
    mosaic = _owned_mosaic(mosaic_id, user)
    try:
        data = preview_jpeg(mosaic, max_dim=max_dim)
    except ValueError as exc:
        raise _http_400(exc) from exc
    return Response(content=data, media_type="image/jpeg")


@app.get("/mosaics/{mosaic_id}")
def get_mosaic(mosaic_id: str, request: Request):
    user, _session = _require_mosaic_user(request)
    mosaic = _owned_mosaic(mosaic_id, user)
    return _mosaic_payload(mosaic)


@app.post("/mosaics/{mosaic_id}/match")
async def match_mosaic(
    mosaic_id: str,
    request: Request,
    query: Optional[UploadFile] = File(None),
    x: Optional[float] = Form(None),
    y: Optional[float] = Form(None),
    w: Optional[float] = Form(None),
    h: Optional[float] = Form(None),
    top_n: int = Form(config.TOP_N_DEFAULT),
    method: str = Form("color+shape"),
    color: Optional[str] = Form(None),
    color_preset: Optional[str] = Form(None),
    size_mode: Optional[str] = Form(None),
    target_size_m: Optional[float] = Form(None),
    target_size_px: Optional[float] = Form(None),
    gsd_m: Optional[float] = Form(None),
    altitude_m: Optional[float] = Form(None),
    min_altitude_m: Optional[float] = Form(None),
    offset: int = Form(0),
    gimbal_pitch_deg: Optional[float] = Form(None),
    focal_px: Optional[float] = Form(None),
    timeout_s: Optional[float] = Form(None),
):
    user, _session = _require_mosaic_user(request)
    mosaic = _owned_mosaic(mosaic_id, user)

    top_n = max(1, min(int(top_n), config.TOP_N_MAX))
    mode_size = (size_mode or "aircraft").strip().lower() or "aircraft"
    if mode_size not in {"aircraft", "small_marking", "marking", "fin"}:
        mode_size = "aircraft"
    tgt_m = _opt_float(target_size_m)
    tgt_px = _opt_float(target_size_px)
    gsd_in = _opt_float(gsd_m)
    alt_in = _opt_float(altitude_m)
    min_alt_in = _opt_float_allow_zero(min_altitude_m)
    pitch_in = _opt_signed_float(gimbal_pitch_deg)
    focal_in = _opt_float(focal_px)
    off = max(0, int(offset or 0))
    fetch_n = max(1, min(off + top_n, config.TOP_N_MAX))
    budget_s = float(timeout_s) if timeout_s else float(config.MATCH_TIMEOUT_S)
    budget_s = max(2.0, min(budget_s, 120.0))
    qgray = None
    qcolor = None
    if query is not None and query.filename:
        raw = await query.read()
        try:
            qgray = decode_query(raw)
            try:
                qcolor = decode_query_color(raw)
            except Exception:
                qcolor = None
        except ValueError as exc:
            raise _http_400(exc) from exc
    elif all(v is not None for v in (x, y, w, h)):
        try:
            qgray = crop_bbox(mosaic, float(x), float(y), float(w), float(h))
            try:
                qcolor = crop_bbox_color(mosaic, float(x), float(y), float(w), float(h))
            except Exception:
                qcolor = None
        except ValueError as exc:
            raise _http_400(exc) from exc
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide a query image or a bbox (x, y, w, h) on the mosaic.",
        )

    qgray = to_gray(qgray)
    mode = (method or "color+shape").strip().lower()
    sema = _match_sema or asyncio.Semaphore(max(1, int(config.MATCH_CONCURRENCY)))
    loop = asyncio.get_running_loop()
    pool = _match_pool
    async with sema:
        try:
            hits = await asyncio.wait_for(
                loop.run_in_executor(
                    pool,
                    lambda: _run_search(
                        mosaic, qgray, qcolor, fetch_n, mode, color, color_preset,
                        size_mode=mode_size, target_size_m=tgt_m, target_size_px=tgt_px, gsd_m=gsd_in, altitude_m=alt_in,
                        min_altitude_m=min_alt_in, gimbal_pitch_deg=pitch_in, focal_px=focal_in, timeout_s=budget_s,
                    ),
                ),
                timeout=budget_s + 8.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="Match timed out. Try a tighter region, Small marking mode, or a shorter timeout.",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Match failed: {exc}") from exc
    result = hits if isinstance(hits, dict) else {"matches": hits[0], "timed_out": hits[1], "gsd_m": hits[2]}
    matches = list(result.get("matches") or [])
    total = len(matches)
    page = matches[off: off + top_n]
    out = {
        "mosaic_id": mosaic.id,
        "matches": page,
        "query_size": [int(qgray.shape[1]), int(qgray.shape[0])],
        "method": mode,
        "size_mode": mode_size,
        "timed_out": bool(result.get("timed_out")),
        "gsd_m": result.get("gsd_m"),
        "gsd_m_used": result.get("gsd_m_used"),
        "altitude_m": result.get("altitude_m"),
        "horizon_row": result.get("horizon_row"),
        "horizon_source": result.get("horizon_source"),
        "skipped": bool(result.get("skipped")),
        "skip_reason": result.get("skip_reason"),
        "gimbal_pitch_deg": result.get("gimbal_pitch_deg"),
        "offset": off,
        "top_n": top_n,
        "total": total,
    }
    return out




def _parse_id_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        import json
        try:
            data = json.loads(text)
        except ValueError:
            data = []
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


@app.post("/mosaics/batch-match")
async def batch_match(
    request: Request,
    mosaic_ids: Optional[str] = Form(None),
    query: Optional[UploadFile] = File(None),
    top_n: int = Form(config.TOP_N_DEFAULT),
    method: str = Form("color"),
    color: Optional[str] = Form(None),
    color_preset: Optional[str] = Form(None),
    size_mode: Optional[str] = Form("small_marking"),
    target_size_m: Optional[float] = Form(None),
    target_size_px: Optional[float] = Form(None),
    gsd_m: Optional[float] = Form(None),
    altitude_m: Optional[float] = Form(None),
    min_altitude_m: Optional[float] = Form(None),
    gimbal_pitch_deg: Optional[float] = Form(None),
    focal_px: Optional[float] = Form(None),
    timeout_s: Optional[float] = Form(None),
):
    """Run the same search across many already-uploaded mosaics and return a summary.

    `mosaic_ids` is a JSON list or a comma-separated string (max 500). Each
    frame uses its own EXIF/XMP altitude and gimbal pitch when present; optional
    form values apply as defaults. Intended for color-only small-marking sweeps.
    """
    user, _session = _require_mosaic_user(request)
    ids = _parse_id_list(mosaic_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="Provide mosaic_ids (JSON list or comma-separated).")
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="Batch is limited to 500 mosaics.")
    top_n = max(1, min(int(top_n), config.TOP_N_MAX))
    mode_size = (size_mode or "small_marking").strip().lower() or "small_marking"
    if mode_size not in {"aircraft", "small_marking", "marking", "fin"}:
        mode_size = "small_marking"
    mode = (method or "color").strip().lower()
    tgt_m = _opt_float(target_size_m)
    tgt_px = _opt_float(target_size_px)
    gsd_in = _opt_float(gsd_m)
    alt_in = _opt_float(altitude_m)
    min_alt_in = _opt_float_allow_zero(min_altitude_m)
    pitch_in = _opt_signed_float(gimbal_pitch_deg)
    focal_in = _opt_float(focal_px)
    budget_s = float(timeout_s) if timeout_s else float(config.MATCH_TIMEOUT_S)
    budget_s = max(2.0, min(budget_s, 120.0))
    qgray = None
    qcolor = None
    if query is not None and query.filename:
        raw = await query.read()
        try:
            qgray = decode_query(raw)
            try:
                qcolor = decode_query_color(raw)
            except Exception:
                qcolor = None
        except ValueError as exc:
            raise _http_400(exc) from exc
    if qgray is None:
        raise HTTPException(status_code=400, detail="Provide a query image for a batch search.")
    qgray = to_gray(qgray)

    sema = _match_sema or asyncio.Semaphore(max(1, int(config.MATCH_CONCURRENCY)))
    loop = asyncio.get_running_loop()
    pool = _match_pool
    results = []
    all_hits = []
    skipped = 0
    failed = 0
    for mid in ids:
        try:
            mosaic = _owned_mosaic(mid, user)
        except HTTPException as exc:
            failed += 1
            results.append({"mosaic_id": mid, "ok": False, "error": exc.detail, "matches": []})
            continue
        try:
            async with sema:
                rec = await asyncio.wait_for(
                    loop.run_in_executor(
                        pool,
                        lambda mo=mosaic: _run_search(
                            mo, qgray, qcolor, top_n, mode, color, color_preset,
                            size_mode=mode_size, target_size_m=tgt_m, target_size_px=tgt_px,
                            gsd_m=gsd_in, altitude_m=alt_in, min_altitude_m=min_alt_in,
                            gimbal_pitch_deg=pitch_in, focal_px=focal_in, timeout_s=budget_s,
                        ),
                    ),
                    timeout=budget_s + 8.0,
                )
        except Exception as exc:
            failed += 1
            results.append({"mosaic_id": mid, "ok": False, "error": str(exc)[:200], "matches": []})
            continue
        matches = list(rec.get("matches") or [])
        if rec.get("skipped"):
            skipped += 1
        for m in matches:
            hit = dict(m)
            hit["mosaic_id"] = mid
            all_hits.append(hit)
        results.append({
            "mosaic_id": mid,
            "ok": True,
            "skipped": bool(rec.get("skipped")),
            "skip_reason": rec.get("skip_reason"),
            "altitude_m": rec.get("altitude_m"),
            "gsd_m": rec.get("gsd_m"),
            "gsd_m_used": rec.get("gsd_m_used"),
            "horizon_row": rec.get("horizon_row"),
            "horizon_source": rec.get("horizon_source"),
            "timed_out": bool(rec.get("timed_out")),
            "matches": matches,
        })
    all_hits.sort(key=lambda m: m.get("score") or 0, reverse=True)
    frames_with = sum(1 for r in results if r.get("ok") and not r.get("skipped") and r.get("matches"))
    summary = {
        "frames": len(ids),
        "processed": len(ids) - failed,
        "failed": failed,
        "skipped_low_altitude": skipped,
        "frames_with_hits": frames_with,
        "total_hits": len(all_hits),
        "top": all_hits[: min(20, len(all_hits))],
    }
    return {"summary": summary, "results": results, "method": mode, "size_mode": mode_size, "top_n": top_n}


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _opt_float_allow_zero(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _opt_signed_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _camera_info(mosaic) -> dict:
    try:
        from gsd_exif import extract_camera_info
        path = getattr(mosaic, "path", None)
        data = Path(path).read_bytes() if path and Path(path).is_file() else b""
        return extract_camera_info(data, image_width_px=getattr(mosaic, "width", None)) or {}
    except Exception:
        return {}


def _resolve_gsd(mosaic, gsd_m: float | None, altitude_m: float | None) -> float | None:
    if gsd_m and gsd_m > 0:
        return float(gsd_m)
    geo = gsd_meters(getattr(mosaic, "georef", None))
    if geo and geo > 0:
        return geo
    # Fall back to EXIF / DJI XMP on the original file, plus an optional altitude hint.
    try:
        from gsd_exif import gsd_from_image_bytes
        path = getattr(mosaic, "path", None)
        data = Path(path).read_bytes() if path and Path(path).is_file() else b""
        return gsd_from_image_bytes(
            data,
            image_width_px=getattr(mosaic, "width", None),
            altitude_m=altitude_m,
        )
    except Exception:
        return None


def _run_search(
    mosaic,
    qgray,
    qcolor,
    top_n: int,
    mode: str = "shape",
    color=None,
    color_preset=None,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
    gsd_m: float | None = None,
    altitude_m: float | None = None,
    min_altitude_m: float | None = None,
    gimbal_pitch_deg: float | None = None,
    focal_px: float | None = None,
    timeout_s: float | None = None,
):
    from matcher import MatchBudget, search as shape_search
    from color_match import (
        extract_dominant_colors,
        fuse_matches,
        hex_to_bgr,
        is_small_marking,
        match_color,
        range_sets_for_request,
        expected_target_px,
        gsd_for_size_gate,
        should_skip_low_altitude,
    )
    from horizon import detect_horizon
    from gsd_exif import extract_camera_info, focal_px_from_info

    budget = MatchBudget(timeout_s)
    gray = open_gray(mosaic)
    mosaic_bgr = None
    scale = 1.0
    use_color = (mode or "shape") != "shape"
    if use_color:
        try:
            mosaic_bgr, scale = open_color(mosaic)
        except Exception:
            mosaic_bgr, scale = None, 1.0
            if mode == "color":
                mode = "shape"
    cam = _camera_info(mosaic)
    alt = altitude_m if altitude_m and altitude_m > 0 else cam.get("altitude_m")
    pitch = gimbal_pitch_deg if gimbal_pitch_deg is not None else cam.get("gimbal_pitch_deg")
    fpx = focal_px if focal_px and focal_px > 0 else focal_px_from_info(cam, getattr(mosaic, "width", None))
    gsd = _resolve_gsd(mosaic, gsd_m, alt)
    gsd_used = gsd_for_size_gate(gsd, size_mode)
    skip_reason = should_skip_low_altitude(
        size_mode=size_mode,
        altitude_m=alt if alt is None else float(alt),
        gsd_m=gsd,
        target_size_m=target_size_m,
        min_altitude_m=min_altitude_m,
    )
    horizon = None
    if mosaic_bgr is not None and is_small_marking(size_mode) and not skip_reason:
        try:
            # detect_horizon on the (possibly downscaled) working image; rows are scaled later.
            horizon = detect_horizon(mosaic_bgr, gimbal_pitch_deg=pitch, focal_px=(fpx * scale if fpx and scale else fpx))
        except Exception:
            horizon = None

    def _pack(hits, timed_out=False, skipped=False, reason=None):
        out = []
        for m in hits:
            item = m.to_dict()
            if mosaic.georef:
                lon, lat = mosaic.georef.pixel_to_lonlat(m.x + m.w / 2, m.y + m.h / 2)
                item["lon"] = lon
                item["lat"] = lat
            out.append(item)
        hz = horizon.horizon_row if horizon is not None else None
        if hz is not None and scale and scale != 1.0:
            hz = hz / scale
        return {
            "matches": out,
            "timed_out": bool(timed_out or budget.timed_out),
            "gsd_m": float(gsd) if gsd else None,
            "gsd_m_used": float(gsd_used) if gsd_used else None,
            "altitude_m": float(alt) if alt else None,
            "horizon_row": int(round(hz)) if hz is not None else None,
            "horizon_source": (horizon.source if horizon is not None else None),
            "skipped": bool(skipped),
            "skip_reason": reason,
            "gimbal_pitch_deg": float(pitch) if pitch is not None else None,
        }

    if skip_reason:
        return _pack([], skipped=True, reason=skip_reason)
    if mosaic_bgr is None or mode == "shape":
        hits = shape_search(
            gray,
            qgray,
            top_n=top_n,
            budget=budget,
            size_mode=size_mode,
            target_size_m=target_size_m,
            target_size_px=target_size_px,
            gsd_m=gsd,
        )
        for m in hits:
            m.method = "shape"
    else:
        shape_hits = []
        if mode in {"shape", "color+shape"}:
            shape_hits = shape_search(
                gray,
                qgray,
                top_n=top_n,
                budget=budget,
                size_mode=size_mode,
                target_size_m=target_size_m,
                target_size_px=target_size_px,
                gsd_m=gsd,
            )
            for m in shape_hits:
                m.method = "shape"
        color_hits = []
        if mode in {"color", "color+shape"} and mosaic_bgr is not None:
            color_gsd = gsd
            if scale < 1 and color_gsd:
                color_gsd = color_gsd / scale
            q_bgr = qcolor if qcolor is not None else None
            use_loose = is_small_marking(size_mode) or (expected_target_px(color_gsd, target_size_m, target_size_px) or 99) < 16
            tight, loose_bands = range_sets_for_request(
                preset=color_preset,
                hex_color=color,
                reference=q_bgr if q_bgr is not None else qgray,
            )
            if not use_loose:
                loose_bands = []
            qh, qw = qgray.shape[0], qgray.shape[1]
            query_wh = (max(4, int(qw * scale)), max(4, int(qh * scale))) if scale < 1 else (qw, qh)
            target_bgr = hex_to_bgr(color)
            if target_bgr is None and q_bgr is not None:
                cols = extract_dominant_colors(q_bgr, k=2)
                target_bgr = cols[0] if cols else None
            color_hits = match_color(
                mosaic_bgr,
                ranges=tight,
                loose_ranges=loose_bands,
                top_n=top_n,
                gsd_m=color_gsd,
                query_wh=query_wh,
                size_mode=size_mode,
                target_size_m=target_size_m,
                target_size_px=target_size_px,
                target_bgr=target_bgr,
                gimbal_pitch_deg=pitch,
                focal_px=(fpx * scale if fpx and scale else fpx),
                horizon=horizon,
            )
            if scale and scale != 1.0:
                for m in color_hits:
                    m.x /= scale
                    m.y /= scale
                    m.w /= scale
                    m.h /= scale
        if mode == "color":
            hits = color_hits[:top_n]
        elif mode == "shape":
            hits = shape_hits[:top_n]
        else:
            hits = fuse_matches(shape_hits, color_hits, top_n=top_n, size_mode=size_mode)
    if scale and scale != 1.0:
        # Color hits are already unscaled in this branch; shape hits are in gray (full) space.
        pass
    return _pack(hits, timed_out=budget.timed_out)


def _scale_gray(img, scale: float):
    import cv2
    import numpy as np
    if img is None or abs(scale - 1) < 1e-6:
        return img
    h, w = img.shape[:2]
    return cv2.resize(np.ascontiguousarray(img), (max(4, int(w * scale)), max(4, int(h * scale))))


def _mosaic_payload(mosaic) -> dict:
    g = mosaic.georef
    bounds = None
    if g is not None:
        w, s = g.pixel_to_lonlat(0, g.height)
        e, n = g.pixel_to_lonlat(g.width, 0)
        if g.crs in {"EPSG:4326", "geographic", "bounds"}:
            west, north = g.pixel_to_xy(0, 0)
            east, south = g.pixel_to_xy(g.width, g.height)
            bounds = {
                "west": min(west, east),
                "south": min(south, north),
                "east": max(west, east),
                "north": max(south, north),
            }
        elif w is not None and s is not None and e is not None and n is not None:
            bounds = {
                "west": min(w, e),
                "south": min(s, n),
                "east": max(w, e),
                "north": max(s, n),
            }
    return {
        "id": mosaic.id,
        "width": mosaic.width,
        "height": mosaic.height,
        "filename": mosaic.filename,
        "expires_at": mosaic.expires,
        "georeferenced": mosaic.georef is not None,
        "crs": mosaic.georef.crs if mosaic.georef else None,
        "bounds": bounds,
    }


@app.exception_handler(HTTPException)
async def http_exc(request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.BIND_HOST, port=config.PORT, workers=1, log_level="info")
