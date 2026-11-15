"""Photo-to-search-map flow against a fake NodeODX (no network, no WebODM)."""
import io
import json
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ["SEARCHMAP_DISABLE_POLLER"] = "1"
os.environ["FIND_API_DISABLE_WEBODM_POLLER"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import locatorstore  # noqa: E402
import searchmap  # noqa: E402
import store  # noqa: E402
from app import app  # noqa: E402

LOSTDRONE = Path("/workspace/lostdrone/photos/DJI_20261107180521_0037_D.JPG")


def _rational(x):
    return (int(x), 1)


def gps_jpeg(lat, lon, size=(800, 600), seed=0, with_gps=True) -> bytes:
    rng = np.random.default_rng(seed)
    arr = (rng.random((size[1], size[0], 3)) * 255).astype(np.uint8)
    im = Image.fromarray(arr)
    exif = Image.Exif()
    if with_gps:
        def dms(v):
            v = abs(v)
            d = int(v); m = int((v - d) * 60); s = (v - d - m / 60) * 3600
            return (float(d), float(m), round(s, 4))
        exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif.tobytes(), quality=80)
    return out.getvalue()


def make_ortho(path: Path):
    import rasterio
    from rasterio.transform import from_origin

    # 300 x 200 px at 0.05 m in UTM 15N near 44.5N, -95.83W; transparent border.
    e0, n0 = 334000.0, 4929000.0
    data = np.zeros((4, 200, 300), dtype=np.uint8)
    data[0, 20:180, 30:270] = 200
    data[1, 20:180, 30:270] = 120
    data[2, 20:180, 30:270] = 40
    data[3, 20:180, 30:270] = 255
    with rasterio.open(
        path, "w", driver="GTiff", width=300, height=200, count=4, dtype="uint8",
        crs="EPSG:32615", transform=from_origin(e0, n0, 0.05, 0.05),
    ) as dst:
        dst.write(data)


class FakeNode:
    def __init__(self, tmp: Path):
        self.tasks = {}
        self.tmp = tmp
        self.ortho = tmp / "fake-ortho.tif"
        make_ortho(self.ortho)
        self.fail_next = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/info":
            return httpx.Response(200, json={"taskQueueCount": 0, "maxParallelTasks": 1})
        if path == "/task/new/init":
            # Like NodeODX (multer): only multipart bodies are parsed.
            if "multipart/form-data" not in request.headers.get("content-type", ""):
                opts = []
            else:
                body = request.content.decode("latin1")
                m = re.search(r'name="options"\r\n\r\n(.*?)\r\n--', body, re.S)
                opts = json.loads(m.group(1)) if m else []
            uid = f"u{len(self.tasks) + 1}"
            self.tasks[uid] = {"code": 0, "images": 0, "options": opts, "removed": False}
            return httpx.Response(200, json={"uuid": uid})
        if path.startswith("/task/new/upload/"):
            uid = path.rsplit("/", 1)[1]
            self.tasks[uid]["images"] += request.content.count(b'name="images"')
            return httpx.Response(200, json={"success": True})
        if path.startswith("/task/new/commit/"):
            uid = path.rsplit("/", 1)[1]
            self.tasks[uid]["code"] = 30 if self.fail_next else 10
            self.fail_next = False
            return httpx.Response(200, json={"uuid": uid})
        if path.endswith("/info"):
            uid = path.split("/")[2]
            t = self.tasks.get(uid)
            if not t:
                return httpx.Response(200, json={"error": "not found"})
            return httpx.Response(200, json={"status": {"code": t["code"]}, "progress": t.get("progress", 0), "options": t["options"]})
        if path.endswith("/output"):
            uid = path.split("/")[2]
            t = self.tasks[uid]
            if t["code"] == 30:
                return httpx.Response(200, json=["Running opensfm stage", "Not enough images to continue"])
            return httpx.Response(200, json=["Running dataset stage", "Running opensfm stage"])
        if path.endswith("/download/all.zip"):
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("odm_orthophoto/odm_orthophoto.tif", self.ortho.read_bytes())
                zf.writestr("images.json", "[]")
            return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "application/zip"})
        if "/download/" in path:
            return httpx.Response(200, json={"error": "Invalid asset"})
        if path in ("/task/cancel", "/task/remove"):
            uid = request.content.decode().split("=", 1)[1]
            if uid in self.tasks:
                self.tasks[uid]["removed"] = True
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": "nope"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "store"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "LOCATOR_DB", data / "locator.sqlite")
    monkeypatch.setattr(searchmap, "MIN_FREE_DISK", 0)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    locatorstore.reset_connection()
    node = FakeNode(tmp_path)
    searchmap.set_client_factory(lambda: httpx.Client(base_url="http://node", transport=httpx.MockTransport(node.handler)))
    with TestClient(app) as c:
        user = accounts.create_user("pilot", "test-password-ok", is_admin=False, mosaic_enabled=True)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        yield c, node, user
    searchmap.set_client_factory(None)
    accounts.reset_connection()
    locatorstore.reset_connection()


def _wait_status(c, jid, want, tries=50):
    for _ in range(tries):
        j = c.get(f"/searchmaps/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(0.05)
    return j


def _upload(c, jid, n=6, start=0, **kw):
    files = [("photos", (f"DJI_{i:04d}.JPG", gps_jpeg(44.4975 + i * 1e-4, -95.8330 + i * 1e-4, seed=i, **kw), "image/jpeg")) for i in range(start, start + n)]
    return c.post(f"/searchmaps/{jid}/photos", files=files)


def test_prepare_photo_resizes_and_keeps_gps_and_xmp():
    if not LOSTDRONE.exists():
        pytest.skip("sample DJI photo not on this machine")
    out, info = searchmap.prepare_photo(LOSTDRONE.read_bytes(), max_edge=2048)
    assert max(info["width"], info["height"]) == 2048
    assert info["lat"] == pytest.approx(44.4979, abs=1e-3)
    assert info["lon"] == pytest.approx(-95.8335, abs=1e-3)
    assert info["rel_alt_m"] == pytest.approx(30.0)
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) == 2048
        assert searchmap._gps_of(im) is not None
        assert im.info.get("xmp")
    assert len(out) < LOSTDRONE.stat().st_size / 2


def test_success_makes_a_georeferenced_map_and_deletes_photos(env):
    c, node, user = env
    j = c.post("/searchmaps").json()
    jid = j["id"]
    r = _upload(c, jid, 6)
    assert r.status_code == 200, r.text
    assert r.json()["gps_count"] == 6
    photos = searchmap._photos_dir(user.id, jid)
    assert len(list(photos.glob("*.jpg"))) == 6
    # staging never counts against the 2 GiB mosaic cap
    assert store._store_bytes() == 0

    r = c.post(f"/searchmaps/{jid}/start")
    assert r.status_code == 200, r.text
    j = _wait_status(c, jid, {"queued", "failed"})
    assert j["status"] == "queued", j
    uid = next(iter(node.tasks))
    names = {o["name"]: o["value"] for o in node.tasks[uid]["options"]}
    assert names["fast-orthophoto"] is True and names["skip-3dmodel"] is True
    assert names["orthophoto-resolution"] >= 2.0
    assert node.tasks[uid]["images"] == 6

    node.tasks[uid]["code"] = 20
    node.tasks[uid]["progress"] = 30
    searchmap.tick()
    j = c.get(f"/searchmaps/{jid}").json()
    assert j["status"] == "processing" and j["stage"] == "matching_photos"
    assert 8 <= j["progress_pct"] < 60

    node.tasks[uid]["code"] = 40
    searchmap.tick()
    j = c.get(f"/searchmaps/{jid}").json()
    assert j["status"] == "done", j
    assert j["stage_text"] == "Success" and j["progress_pct"] == 100
    assert j["photos_deleted"] is True
    assert not photos.exists()
    assert not searchmap._job_dir(user.id, jid).exists()  # manifest and scratch gone too
    assert node.tasks[uid]["removed"] is True
    m = c.get(f"/mosaics/{j['mosaic_id']}").json()
    assert m["georeferenced"] is True
    b = m["bounds"]
    assert -95.2 < b["west"] < b["east"] < -95.0 and 44.3 < b["south"] < b["north"] < 44.7
    # cropped to the photographed area (alpha), not the transparent border
    assert m["width"] < 300 and m["height"] < 200


def test_failure_keeps_photos_and_retry_works(env):
    c, node, user = env
    jid = c.post("/searchmaps").json()["id"]
    _upload(c, jid, 5)
    node.fail_next = True
    c.post(f"/searchmaps/{jid}/start")
    _wait_status(c, jid, {"queued", "failed"})
    searchmap.tick()
    j = c.get(f"/searchmaps/{jid}").json()
    assert j["status"] == "failed"
    assert "overlap" in j["error"]
    assert j["can_retry"] is True
    assert len(list(searchmap._photos_dir(user.id, jid).glob("*.jpg"))) == 5
    r = c.post(f"/searchmaps/{jid}/start")
    assert r.status_code == 200
    j = _wait_status(c, jid, {"queued"})
    assert j["status"] == "queued"


def test_photos_without_gps_are_skipped_and_too_few_is_explained(env):
    c, node, user = env
    jid = c.post("/searchmaps").json()["id"]
    r = _upload(c, jid, 3, with_gps=False)
    body = r.json()
    assert body["gps_count"] == 0 and len(body["skipped"]) == 3
    assert "GPS" in body["skipped"][0]["reason"]
    r = c.post(f"/searchmaps/{jid}/start")
    assert r.status_code == 400
    assert "at least" in r.json()["detail"]


def test_other_users_and_locked_users_are_refused(env):
    c, node, user = env
    jid = c.post("/searchmaps").json()["id"]
    other = accounts.create_user("other", "test-password-ok", is_admin=False, mosaic_enabled=True)
    s2 = accounts.create_session(other)
    assert c.get(f"/searchmaps/{jid}", headers={"Authorization": f"Bearer {s2.token}"}).status_code == 404
    locked = accounts.create_user("locked", "test-password-ok", is_admin=False, mosaic_enabled=False)
    s3 = accounts.create_session(locked)
    assert c.post("/searchmaps", headers={"Authorization": f"Bearer {s3.token}"}).status_code == 403
    assert c.post("/searchmaps", headers={"Authorization": ""}).status_code == 401


def test_cancel_deletes_photos_and_ttl_expires_failed(env, monkeypatch):
    c, node, user = env
    jid = c.post("/searchmaps").json()["id"]
    _upload(c, jid, 5)
    j = c.post(f"/searchmaps/{jid}/cancel").json()
    assert j["status"] == "cancelled" and not searchmap._job_dir(user.id, jid).exists()

    jid2 = c.post("/searchmaps").json()["id"]
    _upload(c, jid2, 5)
    searchmap._update(jid2, status="failed", stage="failed")
    assert searchmap.cleanup(now=time.time() + 60) == 0
    assert searchmap.cleanup(now=time.time() + searchmap.TTL_SECONDS + 60) == 1
    assert not searchmap._job_dir(user.id, jid2).exists()


def test_store_cleanup_never_touches_searchmap_staging(env):
    c, node, user = env
    jid = c.post("/searchmaps").json()["id"]
    _upload(c, jid, 5)
    store.cleanup(now=time.time() + 10 * 24 * 3600)
    assert len(list(searchmap._photos_dir(user.id, jid).glob("*.jpg"))) == 5


def test_second_active_job_is_refused_but_abandoned_upload_is_replaced(env):
    c, node, user = env
    j1 = c.post("/searchmaps").json()["id"]
    _upload(c, j1, 5)
    j2 = c.post("/searchmaps").json()["id"]  # replaces the abandoned collecting job
    assert c.get(f"/searchmaps/{j1}").json()["status"] == "cancelled"
    _upload(c, j2, 5)
    c.post(f"/searchmaps/{j2}/start")
    _wait_status(c, j2, {"queued"})
    r = c.post("/searchmaps")
    assert r.status_code == 409


def test_full_account_is_not_refused_new_map_will_replace_oldest(env, monkeypatch):
    c, node, user = env
    import config, store
    monkeypatch.setattr(store, "_mosaic_count", lambda owner: config.MAX_MOSAICS)
    r = c.post("/searchmaps")
    assert r.status_code == 201
