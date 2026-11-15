"""HTTP routes for Drone Mapper dashboard/app sync.

Mounted on the existing find-api. Auth uses the same session cookie or
Bearer token as /auth/*. Writes from the browser need the CSRF header.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import flights
import processing
import locatorstore
import syncstore

RequireLogin = Callable[[Request], tuple[Any, Any]]
RequireCsrf = Callable[[Request, Any], None]


def _sync_error(exc: syncstore.SyncError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _parse_since(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="since must be a Unix timestamp.") from exc


async def _read_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return payload



def _full_changes(user_id: int, since_val: float | None) -> dict:
    data = syncstore.changes_since(user_id, since_val)
    data["flights"] = flights.changes_since(user_id, since_val)
    data["flying_now"] = flights.active_flights(user_id)
    data["processing"] = processing.changes_since(user_id, since_val)
    return data


def _latest(user_id: int) -> float:
    return max(syncstore.latest_update(user_id), processing.latest_update(user_id))


def register(app: FastAPI, require_login: RequireLogin, require_csrf: RequireCsrf) -> None:
    def _user(request: Request):
        user, session = require_login(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, session)
        return user

    @app.get("/sync/changes")
    def sync_changes(request: Request, since: str | None = None):
        user = _user(request)
        since_val = _parse_since(since)
        return _full_changes(user.id, since_val)

    @app.get("/sync/events")
    async def sync_events(request: Request, since: str | None = None):
        user = _user(request)
        start = _parse_since(since)

        async def stream():
            last = start if start is not None else 0.0
            # Immediate snapshot so a newly opened screen is current.
            payload = _full_changes(user.id, last)
            last = payload["server_time"]
            yield f"event: changes\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(10)
                if await request.is_disconnected():
                    break
                latest = _latest(user.id)
                if latest > last:
                    payload = _full_changes(user.id, last)
                    last = payload["server_time"]
                    yield f"event: changes\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                else:
                    yield f"event: ping\ndata: {json.dumps({'server_time': syncstore.now()})}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/sync/missions")
    def list_missions(request: Request, include_deleted: bool = False):
        user = _user(request)
        return {
            "server_time": syncstore.now(),
            "missions": syncstore.list_missions(user.id, include_deleted=include_deleted),
        }

    @app.post("/sync/missions")
    async def create_mission(request: Request):
        user = _user(request)
        payload = await _read_json(request)
        try:
            item = syncstore.upsert_mission(
                user.id,
                payload.get("mission"),
                mission_id=payload.get("id"),
                name=payload.get("name"),
            )
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc
        return JSONResponse(item, status_code=201)

    @app.get("/sync/missions/{mission_id}")
    def get_mission(mission_id: str, request: Request):
        user = _user(request)
        try:
            mid = syncstore.validate_id(mission_id)
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc
        item = syncstore.get_mission(user.id, mid, include_deleted=True)
        if item is None:
            raise HTTPException(status_code=404, detail="Unknown mission.")
        if item["deleted"]:
            return item
        return item

    @app.put("/sync/missions/{mission_id}")
    async def put_mission(mission_id: str, request: Request):
        user = _user(request)
        payload = await _read_json(request)
        try:
            item = syncstore.upsert_mission(
                user.id,
                payload.get("mission"),
                mission_id=mission_id,
                name=payload.get("name"),
            )
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc
        return item

    @app.patch("/sync/missions/{mission_id}")
    async def patch_mission(mission_id: str, request: Request):
        user = _user(request)
        payload = await _read_json(request)
        try:
            if payload.get("restore"):
                return syncstore.restore_mission(user.id, mission_id)
            if "mission" in payload:
                return syncstore.upsert_mission(
                    user.id,
                    payload.get("mission"),
                    mission_id=mission_id,
                    name=payload.get("name"),
                )
            if "name" in payload:
                return syncstore.rename_mission(user.id, mission_id, payload.get("name") or "")
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc
        raise HTTPException(status_code=400, detail="Provide a name, a mission, or restore=true.")

    @app.delete("/sync/missions/{mission_id}")
    def delete_mission(mission_id: str, request: Request):
        user = _user(request)
        try:
            return syncstore.soft_delete_mission(user.id, mission_id)
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc

    @app.get("/sync/missions/{mission_id}/progress")
    def get_progress(mission_id: str, request: Request):
        user = _user(request)
        item = syncstore.get_mission(user.id, mission_id, include_deleted=False)
        if item is None:
            raise HTTPException(status_code=404, detail="Unknown mission.")
        return {"progress": item.get("progress")}

    @app.put("/sync/missions/{mission_id}/progress")
    async def put_progress(mission_id: str, request: Request):
        user = _user(request)
        payload = await _read_json(request)
        try:
            return syncstore.set_progress(user.id, mission_id, payload)
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc

    @app.get("/sync/settings")
    def get_settings(request: Request):
        user = _user(request)
        item = syncstore.get_settings(user.id)
        if item is None:
            return {"settings": None, "updated_at": None, "version": 0}
        return item

    @app.put("/sync/settings")
    async def put_settings(request: Request):
        user = _user(request)
        payload = await _read_json(request)
        body = payload.get("settings", payload)
        try:
            return syncstore.set_settings(user.id, body)
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc

    @app.get("/sync/find-results")
    def get_find_results(request: Request):
        user = _user(request)
        item = locatorstore.get_find_results(user.id)
        if item is None:
            return {"results": None, "updated_at": None, "version": 0}
        return item

    @app.put("/sync/find-results")
    async def put_find_results(request: Request):
        user = _user(request)
        payload = await _read_json(request)
        body = payload.get("results", payload)
        try:
            return locatorstore.set_find_results(user.id, body)
        except syncstore.SyncError as exc:
            raise _sync_error(exc) from exc
