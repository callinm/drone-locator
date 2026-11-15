import io
import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts
import config
import processing
import volume_calc
import webodm


def _jpeg(lat=40.0, lon=-88.0, color=(40, 80, 40)) -> bytes:
    img = Image.new("RGB", (64, 48), color)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def _geotiff_bytes() -> bytes:
    import tifffile

    dsm = volume_calc.make_grid_dsm(40, 40, 0.00001, -0.00001, origin_x=-88.001, origin_y=40.001)
    dsm["z"].fill(10.0)
    volume_calc.add_cone(dsm, -88.0, 40.0, 0.0002, 8.0)
    buf = io.BytesIO()
    tifffile.imwrite(buf, dsm["z"].astype(np.float32))
    return buf.getvalue()


class FakeWebOdm:
    """In-memory WebODM stand-in used through httpx.MockTransport."""

    def __init__(self):
        self.token = "test-token-not-a-secret"
        self.project_id = 9
        self.tasks: dict[str, dict] = {}
        self.uploads: dict[str, list[str]] = {}
        self.status_code = 40
        self.assets = {
            "orthophoto.png": b"\x89PNG\r\n\x1a\n" + b"ortho",
            "orthophoto.tif": b"ortho-tif",
            "dsm.tif": _geotiff_bytes(),
        }
        self.committed: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if request.method == "POST" and path.endswith("/api/token-auth"):
            return httpx.Response(200, json={"token": self.token})
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("JWT "):
            return httpx.Response(401, json={"detail": "no"})
        if request.method == "GET" and path.endswith("/api/projects"):
            return httpx.Response(200, json=[{"id": self.project_id, "name": "Drone Mapper"}])
        if request.method == "POST" and path.endswith(f"/api/projects/{self.project_id}/tasks"):
            task_id = f"task-{len(self.tasks)+1}"
            self.tasks[task_id] = {
                "id": task_id,
                "status": 10,
                "running_progress": 0,
                "orthophoto_extent": [-88.01, 39.99, -87.99, 40.01],
                "output": "",
            }
            self.uploads[task_id] = []
            return httpx.Response(201, json=self.tasks[task_id])
        parts = [p for p in path.split("/") if p]
        # api / projects / pid / tasks / tid / ...
        if "tasks" in parts:
            tid = parts[parts.index("tasks") + 1] if parts.index("tasks") + 1 < len(parts) else ""
            if request.method == "POST" and path.rstrip("/").endswith("/upload"):
                self.uploads.setdefault(tid, [])
                # count image parts
                self.uploads[tid].append("batch")
                return httpx.Response(200, json={"uploaded": True})
            if request.method == "POST" and path.rstrip("/").endswith("/commit"):
                self.committed.add(tid)
                self.tasks[tid]["status"] = self.status_code
                self.tasks[tid]["running_progress"] = 1 if self.status_code == 40 else 0
                return httpx.Response(200, json=self.tasks[tid])
            if request.method == "GET" and "/download/" in path:
                name = path.rsplit("/", 1)[-1]
                data = self.assets.get(name)
                if data is None:
                    return httpx.Response(404, json={})
                return httpx.Response(200, content=data, headers={"content-type": "image/tiff"})
            if request.method == "GET" and "/tiles/" in path:
                return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"tile")
            if request.method == "GET" and path.rstrip("/").endswith("/output"):
                text = (self.tasks.get(tid) or {}).get("output") or ""
                return httpx.Response(200, text=text)
            if request.method == "GET" and tid in self.tasks:
                return httpx.Response(200, json=self.tasks[tid])
        return httpx.Response(404, json={"detail": path})


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    creds = tmp_path / "webodm.txt"
    creds.write_text(
        "WEBODM_BASE_URL=http://127.0.0.1:8001\n"
        "WEBODM_USERNAME=dronemapper-service\n"
        "WEBODM_PASSWORD=test-password-not-used\n"
        "WEBODM_PROJECT_ID=9\n"
    )
    monkeypatch.setattr(config, "DATA_DIR", store)
    monkeypatch.setenv("WEBODM_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("WEBODM_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("FIND_API_DISABLE_WEBODM_POLLER", "1")
    monkeypatch.setenv("FIND_API_WEBODM_SYNC_SUBMIT", "1")
    accounts.reset_connection()
    accounts.reset_rate_limits()
    fake = FakeWebOdm()
    transport = httpx.MockTransport(fake.handler)

    def factory():
        return webodm.WebOdmClient(webodm.load_credentials(), transport=transport)

    processing.set_client_factory(factory)
    import app as appmod

    with TestClient(appmod.app) as c:
        user = accounts.create_user("Mapper", "long-enough-password", mosaic_enabled=True)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        yield c, user, fake
    processing.set_client_factory(None)
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _make_job_with_photos(c, n=30):
    created = c.post("/stockpile/jobs", json={"name": "Yard A", "piles": [{"name": "North", "lat": 40.0, "lon": -88.0}]})
    assert created.status_code == 201, created.text
    job = created.json()
    # piles via patch if not accepted on create
    c.patch(f"/stockpile/jobs/{job['id']}", json={"piles": [{"name": "North", "lat": 40.0, "lon": -88.0}]})
    files = [("files", (f"p{i}.jpg", _jpeg(40, -88 + i * 0.0001, (i, 80, 40)), "image/jpeg")) for i in range(n)]
    up = c.post(f"/stockpile/jobs/{job['id']}/photos", files=files)
    assert up.status_code == 200, up.text
    return job["id"]


def test_too_few_photos_rejected(client):
    c, user, fake = client
    jid = _make_job_with_photos(c, n=10)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 400, res.text
    assert "30" in res.json()["detail"]


def test_cap_enforced(client):
    c, user, fake = client
    admin = accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    admin_sess = accounts.create_session(admin)
    r = c.post(
        "/admin/users/Mapper/limits",
        json={"photo_cap": 30},
        headers={"Authorization": f"Bearer {admin_sess.token}"},
    )
    assert r.status_code == 200, r.text
    jid = _make_job_with_photos(c, n=30)
    # 30 is allowed
    ok = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert ok.status_code == 201, ok.text


def test_submit_poll_done_and_proxy(client):
    c, user, fake = client
    jid = _make_job_with_photos(c, n=30)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 201, res.text
    body = res.json()
    proc = body["processing"]
    assert proc["status"] in {"queued", "processing", "done", "uploading"}
    assert "webodm" not in json.dumps(proc).lower()
    assert "127.0.0.1" not in json.dumps(proc)
    assert "token" not in json.dumps(proc).lower()
    pid = proc["id"]

    # Force completed and harvest
    for tid, task in fake.tasks.items():
        task["status"] = 40
        task["running_progress"] = 1
    refreshed = c.post(f"/process/{pid}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    done = refreshed.json()
    assert done["status"] == "done"
    assert done["result"]["quality"] == "survey"
    assert done["result"]["has_dsm"] is True
    assert done["orthophoto_url"] == f"/process/{pid}/orthophoto.png"
    assert "task-" not in json.dumps(done)

    png = c.get(f"/process/{pid}/orthophoto.png")
    assert png.status_code == 200
    assert png.content.startswith(b"\x89PNG") or len(png.content) > 0

    dsm = c.get(f"/process/{pid}/dsm.tif")
    assert dsm.status_code == 200

    # Sync includes processing and status labels
    changes = c.get("/sync/changes", params={"since": "0"})
    assert changes.status_code == 200, changes.text
    payload = changes.json()
    assert "processing" in payload
    statuses = {j["status"] for j in payload["processing"]}
    assert "done" in statuses
    for item in payload["processing"]:
        assert "webodm" not in json.dumps(item).lower()

    # Stockpile job was upgraded
    sp = c.get(f"/stockpile/jobs/{jid}").json()
    assert sp["processing"]["status"] == "done"
    assert (sp.get("mosaic") or {}).get("quality") == "survey" or (sp.get("mosaic") or {}).get("kind") == "survey"


def test_account_isolation(client):
    c, user, fake = client
    jid = _make_job_with_photos(c, n=30)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    pid = res.json()["processing"]["id"]

    other = accounts.create_user("Other", "long-enough-password", mosaic_enabled=True)
    other_sess = accounts.create_session(other)
    headers = {"Authorization": f"Bearer {other_sess.token}"}
    assert c.get(f"/process/{pid}", headers=headers).status_code == 404
    assert c.get(f"/process/{pid}/orthophoto.png", headers=headers).status_code == 404
    assert c.get(f"/process/{pid}/dsm.tif", headers=headers).status_code == 404
    listed = c.get("/process", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["jobs"] == []


def test_options_are_survey_quality(client):
    c, user, fake = client
    jid = _make_job_with_photos(c, n=30)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    pid = res.json()["processing"]["id"]
    # Internal record keeps options; public view does not need to list them, but
    # the stored job must have dsm / auto-boundary / fast-orthophoto off.
    job = processing.get_job(user.id, pid)
    names = {o["name"]: o["value"] for o in job["options"]}
    assert names["dsm"] == "true"
    assert names["auto-boundary"] == "true"
    assert names["fast-orthophoto"] == "false"


def test_missing_credentials_is_503(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    monkeypatch.setenv("WEBODM_CREDENTIALS_FILE", str(tmp_path / "missing.txt"))
    monkeypatch.setenv("FIND_API_DISABLE_WEBODM_POLLER", "1")
    accounts.reset_connection()
    accounts.reset_rate_limits()
    processing.set_client_factory(None)
    import importlib
    import app as appmod

    importlib.reload(appmod)
    with TestClient(appmod.app) as c:
        user = accounts.create_user("Mapper", "long-enough-password", mosaic_enabled=True)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        job = c.post("/stockpile/jobs", json={"name": "X"}).json()
        files = [("files", (f"p{i}.jpg", _jpeg(), "image/jpeg")) for i in range(30)]
        assert c.post(f"/stockpile/jobs/{job['id']}/photos", files=files).status_code == 200
        res = c.post(f"/stockpile/jobs/{job['id']}/process", json={})
        assert res.status_code == 503
    accounts.reset_connection()
    accounts.reset_rate_limits()


def test_rejects_public_webodm_url():
    with pytest.raises(webodm.WebOdmError):
        webodm.assert_internal_base("https://wheresthepelican.com:8000")
    with pytest.raises(webodm.WebOdmError):
        webodm.assert_internal_base("http://8.8.8.8:8001")
    assert webodm.assert_internal_base("http://127.0.0.1:8001") == "http://127.0.0.1:8001"
    assert webodm.assert_internal_base("http://webapp:8000").startswith("http://webapp")


def test_stage_and_progress_pct(client):
    c, user, fake = client
    jid = _make_job_with_photos(c, n=30)
    fake.status_code = 20
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 201, res.text
    proc = res.json()["processing"]
    assert "stage" in proc
    assert proc["stage_label"] in {"Uploading", "Queued", "Matching photos", "Building 3D surface", "Making map", "Computing volume", "Processing", "Done", "Failed"}
    assert isinstance(proc["progress_pct"], int)
    assert 0 <= proc["progress_pct"] <= 100

    for tid, task in fake.tasks.items():
        task["status"] = 20
        task["running_progress"] = 0.9
    pid = proc["id"]
    mid = c.post(f"/process/{pid}/refresh").json()
    assert mid["status"] == "processing"
    assert mid["stage"] == "making_map"
    assert mid["stage_label"] == "Making map"
    assert mid["progress_pct"] == 90
    assert isinstance(mid.get("elapsed_sec"), int)

    for tid, task in fake.tasks.items():
        task["status"] = 40
        task["running_progress"] = 1
    done = c.post(f"/process/{pid}/refresh").json()
    assert done["status"] == "done"
    assert done["stage"] == "done"
    assert done["stage_label"] == "Done"
    assert done["progress_pct"] == 100


def test_stage_creep_stays_under_odm_end(client):
    """Stuck WebODM progress (21% during openmvs) creeps but never reaches the stage end."""
    c, user, fake = client
    jid = _make_job_with_photos(c, n=30)
    fake.status_code = 20
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 201, res.text
    pid = res.json()["processing"]["id"]
    for _tid, task in fake.tasks.items():
        task["status"] = 20
        task["running_progress"] = 0.21
        task["output"] = "[INFO] Finished opensfm stage\n[INFO] Running openmvs stage\n"

    mid = c.post(f"/process/{pid}/refresh").json()
    assert mid["stage"] == "building_surface"
    assert mid["stage_label"] == "Building 3D surface"
    assert mid["progress_pct"] == 21
    assert mid["elapsed_sec"] >= 0

    # Pretend the stage has been running for 20 minutes.
    processing._update(pid, user.id, stage_started_at=processing._now() - 1200)
    later = c.post(f"/process/{pid}/refresh").json()
    assert later["stage_label"] == "Building 3D surface"
    assert later["progress_pct"] > 21
    assert later["progress_pct"] < 50
    assert "min" in (later.get("progress") or "") or "h" in (later.get("progress") or "") or "s" in (later.get("progress") or "")


def test_display_progress_never_exceeds_stage_end():
    assert processing.display_progress(21, 50, 0, 0, 21) == 21
    assert processing.display_progress(21, 50, 0, 480, 21) == 35
    assert processing.display_progress(21, 50, 0, 10_000_000, 21) == 49
    assert processing.display_progress(50, 50, 0, 100, 49) == 50
    assert processing.parse_odm_stage("[INFO] Running opensfm stage\n[INFO] Running openmvs stage") == "openmvs"



def test_staged_upload_session_and_submit(client):
    c, user, fake = client
    created = c.post("/process/uploads")
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    assert created.json()["count"] == 0

    first = [("files", (f"p{i}.jpg", _jpeg(40, -88 + i * 0.0001, (i, 80, 40)), "image/jpeg")) for i in range(20)]
    up = c.post(f"/process/uploads/{upload_id}/files", files=first)
    assert up.status_code == 200, up.text
    assert up.json()["count"] == 20
    assert len(up.json()["added"]) == 20

    # Re-sending the same name+size is skipped, not duplicated.
    again = c.post(
        f"/process/uploads/{upload_id}/files",
        files=[("files", ("p0.jpg", first[0][1][1], "image/jpeg"))],
    )
    assert again.status_code == 200, again.text
    assert again.json()["count"] == 20
    assert again.json()["skipped"][0]["name"] == "p0.jpg"

    listed = c.get(f"/process/uploads/{upload_id}")
    assert listed.status_code == 200
    assert listed.json()["count"] == 20
    assert len(listed.json()["received"]) == 20

    # Too few still rejected, and the session stays reusable.
    too_few = c.post("/process", json={"upload_id": upload_id, "name": "Staged"})
    assert too_few.status_code == 400, too_few.text
    assert c.get(f"/process/uploads/{upload_id}").json()["count"] == 20

    rest = [("files", (f"p{i}.jpg", _jpeg(40, -88 + i * 0.0001, (i, 80, 40)), "image/jpeg")) for i in range(20, 30)]
    up2 = c.post(f"/process/uploads/{upload_id}/files", files=rest)
    assert up2.status_code == 200, up2.text
    assert up2.json()["count"] == 30

    started = c.post("/process", json={"upload_id": upload_id, "name": "Staged mosaic"})
    assert started.status_code == 201, started.text
    job = started.json()
    assert job["photo_count"] == 30
    assert job["status"] in {"queued", "uploading", "processing", "done"}
    # Session is consumed; a second submit must not reuse the moved files.
    again_submit = c.post("/process", json={"upload_id": upload_id, "name": "Again"})
    assert again_submit.status_code in {409, 400}


def test_staged_upload_is_isolated(client):
    c, user, fake = client
    created = c.post("/process/uploads")
    upload_id = created.json()["id"]
    other = accounts.create_user("Other", "long-enough-password", mosaic_enabled=True)
    session = accounts.create_session(other)
    other_headers = {"Authorization": f"Bearer {session.token}"}
    res = c.get(f"/process/uploads/{upload_id}", headers=other_headers)
    assert res.status_code == 404



def test_tick_null_status_stays_queued(client):
    """A just-committed WebODM task with status=null must not be marked failed."""
    c, user, fake = client
    fake.status_code = None
    jid = _make_job_with_photos(c, n=30)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 201, res.text
    pid = res.json()["processing"]["id"]
    # Submit already ticked once (FIND_API_WEBODM_SYNC_SUBMIT). Refresh again.
    refreshed = c.post(f"/process/{pid}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["status"] == "queued", body
    assert body.get("error") in (None, "")
    assert "failed" not in (body.get("progress") or "").lower()


def test_tick_real_failure_still_fails(client):
    c, user, fake = client
    fake.status_code = 30
    jid = _make_job_with_photos(c, n=30)
    res = c.post(f"/stockpile/jobs/{jid}/process", json={})
    assert res.status_code == 201, res.text
    pid = res.json()["processing"]["id"]
    body = c.post(f"/process/{pid}/refresh").json()
    assert body["status"] == "failed"
    assert body.get("error")


def test_disk_full_error_is_explicit():
    assert "out of disk space" in webodm._unavailable_detail("OSError: [Errno 28] No space left on device").lower()
    resp = httpx.Response(500, text="ENOSPC: no space left on device")
    assert "out of disk space" in webodm._unavailable_detail(resp).lower()
    assert "temporarily unavailable" in webodm._unavailable_detail("connection reset").lower()


def _finish_all(fake):
    for tid, task in fake.tasks.items():
        task["status"] = 40
        task["running_progress"] = 1


def test_success_deletes_input_photos_keeps_outputs(client):
    c, user, fake = client
    created = c.post("/process/uploads")
    upload_id = created.json()["id"]
    files = [("files", (f"p{i}.jpg", _jpeg(40, -88 + i * 0.0001, (i, 80, 40)), "image/jpeg")) for i in range(30)]
    assert c.post(f"/process/uploads/{upload_id}/files", files=files).status_code == 200
    started = c.post("/process", json={"upload_id": upload_id, "name": "Purge me"})
    assert started.status_code == 201, started.text
    pid = started.json()["id"]
    job_dir = config.DATA_DIR / "processing" / str(user.id) / pid
    staging = config.DATA_DIR / "processing-uploads" / str(user.id) / upload_id
    assert len(list((job_dir / "photos").iterdir())) == 30
    assert staging.is_dir()
    _finish_all(fake)
    done = c.post(f"/process/{pid}/refresh").json()
    assert done["status"] == "done"
    assert done["result"]["photos_removed"] is True
    assert not (job_dir / "photos").exists()
    assert not staging.exists()
    assert c.get(f"/process/uploads/{upload_id}").status_code == 404
    assert not staging.exists()
    assert (job_dir / "dsm.tif").is_file()
    assert c.get(f"/process/{pid}/dsm.tif").status_code == 200
    assert c.get(f"/process/{pid}/orthophoto.png").status_code == 200


def test_failure_keeps_input_photos(client):
    c, user, fake = client
    created = c.post("/process/uploads")
    upload_id = created.json()["id"]
    files = [("files", (f"p{i}.jpg", _jpeg(40, -88 + i * 0.0001, (i, 80, 40)), "image/jpeg")) for i in range(30)]
    assert c.post(f"/process/uploads/{upload_id}/files", files=files).status_code == 200
    pid = c.post("/process", json={"upload_id": upload_id, "name": "Fails"}).json()["id"]
    for tid, task in fake.tasks.items():
        task["status"] = 30  # FAILED
        task["last_error"] = "boom"
    failed = c.post(f"/process/{pid}/refresh").json()
    assert failed["status"] == "failed"
    job_dir = config.DATA_DIR / "processing" / str(user.id) / pid
    assert len(list((job_dir / "photos").iterdir())) == 30


def test_purge_leaves_other_jobs_alone(client):
    c, user, fake = client
    other = config.DATA_DIR / "processing" / str(user.id) / "other-job" / "photos"
    other.mkdir(parents=True)
    (other / "a.jpg").write_bytes(b"x")
    info = processing.purge_input_photos(user.id, "missing-job")
    assert info["photos_removed_files"] == 0
    assert (other / "a.jpg").is_file()
