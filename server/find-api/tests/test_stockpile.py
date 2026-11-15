import io
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from PIL.ExifTags import GPSTAGS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts
import config
import volume_calc


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    import app as appmod
    import stockpile
    import flights
    import syncstore

    with TestClient(appmod.app) as c:
        user = accounts.create_user("Mapper", "long-enough-password", mosaic_enabled=True)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        yield c, user
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _jpeg_with_gps(lat=40.0, lon=-88.0) -> bytes:
    img = Image.new("RGB", (64, 48), (40, 80, 40))
    # Pillow 11+ supports gps in exif
    exif = img.getexif()
    gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}

    def frac(v):
        return (int(round(v * 10000)), 10000)

    def to_dms(deg):
        sign = 1 if deg >= 0 else -1
        d = abs(deg)
        degrees = int(d)
        minutes_f = (d - degrees) * 60
        minutes = int(minutes_f)
        seconds = (minutes_f - minutes) * 60
        return ((degrees, 1), (minutes, 1), (int(seconds * 10000), 10000))

    from PIL.ExifTags import GPSTAGS
    inv = {v: k for k, v in GPSTAGS.items()}
    gps[inv["GPSLatitude"]] = to_dms(lat)
    gps[inv["GPSLatitudeRef"]] = "N" if lat >= 0 else "S"
    gps[inv["GPSLongitude"]] = to_dms(lon)
    gps[inv["GPSLongitudeRef"]] = "E" if lon >= 0 else "W"
    exif[0x8825] = gps
    buf = io.BytesIO()
    try:
        img.save(buf, "JPEG", exif=exif)
    except Exception:
        img.save(buf, "JPEG")
    return buf.getvalue()


def test_limits_and_job_flow(client):
    c, user = client
    limits = c.get("/stockpile/limits")
    assert limits.status_code == 200, limits.text
    body = limits.json()
    assert body["min_photos"] == 30
    assert body["photo_cap"] == 500

    created = c.post("/stockpile/jobs", json={"name": "Yard A"})
    assert created.status_code == 201, created.text
    job = created.json()
    jid = job["id"]

    piles = c.patch(
        f"/stockpile/jobs/{jid}",
        json={"step": "pick", "piles": [{"name": "North pile", "lat": 40.0, "lon": -88.0}]},
    )
    assert piles.status_code == 200, piles.text
    assert piles.json()["piles"][0]["name"] == "North pile"

    # Building a mosaic with no photos is rejected (too few).
    early = c.post(f"/stockpile/jobs/{jid}/mosaic")
    assert early.status_code == 400
    assert "30" in early.json()["detail"]


def test_photo_cap_enforced_and_admin_override(client):
    c, user = client
    admin = accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    admin_sess = accounts.create_session(admin)
    # Lower Mapper's cap to 30 (the minimum).
    r = c.post(
        "/admin/users/Mapper/limits",
        json={"photo_cap": 30},
        headers={"Authorization": f"Bearer {admin_sess.token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["photo_cap"] == 30

    job = c.post("/stockpile/jobs", json={"name": "Cap test"}).json()
    jid = job["id"]
    files = [("files", (f"p{i}.jpg", _jpeg_with_gps(40, -88 + i * 0.0001), "image/jpeg")) for i in range(30)]
    up = c.post(f"/stockpile/jobs/{jid}/photos", files=files)
    assert up.status_code == 200, up.text
    assert up.json()["photo_count"] == 30
    extra = c.post(
        f"/stockpile/jobs/{jid}/photos",
        files=[("files", ("extra.jpg", _jpeg_with_gps(), "image/jpeg"))],
    )
    assert extra.status_code == 400
    assert "30" in extra.json()["detail"] or "cap" in extra.json()["detail"].lower()


def test_locked_user_forbidden(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    import app as appmod
    from fastapi.testclient import TestClient

    with TestClient(appmod.app) as c:
        user = accounts.create_user("rightway", "long-enough-password", mosaic_enabled=False)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        r = c.post("/stockpile/jobs", json={"name": "Nope"})
        assert r.status_code == 403
    accounts.reset_connection()


def test_sync_stockpile_mission_photo_limits(client):
    c, user = client
    too_few = {
        "format": "drone-mapper-mission",
        "kind": "stockpile_counting",
        "name": "Tiny",
        "computed": {"photoCount": 10},
        "settings": {"preset": "stockpile"},
        "waypoints": [{"lat": 1, "lon": 2, "altM": 50}] * 8,
    }
    r = c.post("/sync/missions", json={"mission": too_few, "name": "Tiny"})
    assert r.status_code == 400
    assert "30" in r.json()["detail"]

    ok = dict(too_few)
    ok["computed"] = {"photoCount": 40}
    ok["name"] = "Ok pile"
    r = c.post("/sync/missions", json={"mission": ok, "name": "Ok pile"})
    assert r.status_code == 201, r.text


def test_volume_from_uploaded_dsm(client, tmp_path):
    import tifffile

    c, user = client
    job = c.post("/stockpile/jobs", json={"name": "Vol"}).json()
    jid = job["id"]
    dsm = volume_calc.make_grid_dsm(40, 40, 0.5, 0.5, origin_x=0, origin_y=0)
    dsm["z"].fill(0)
    volume_calc.add_cone(dsm, 10, 10, 4, 3)
    tif_path = tmp_path / "dsm.tif"
    tifffile.imwrite(tif_path, dsm["z"].astype(np.float32))
    # Mark a pile around the cone. Because the test GeoTIFF has no georeference,
    # measure_polygon uses the ring values as x/y in the grid's origin/dx space.
    c.patch(
        f"/stockpile/jobs/{jid}",
        json={
            "piles": [
                {
                    "name": "Cone",
                    "lat": 10,
                    "lon": 10,
                    "ring": [
                        {"lat": 5, "lon": 5},
                        {"lat": 5, "lon": 15},
                        {"lat": 15, "lon": 15},
                        {"lat": 15, "lon": 5},
                    ],
                }
            ]
        },
    )
    up = c.post(f"/stockpile/jobs/{jid}/dsm", files={"file": ("dsm.tif", tif_path.read_bytes(), "image/tiff")})
    # load_dsm may fail without georef tags — still should accept the array
    assert up.status_code == 200, up.text
    vol = c.post(f"/stockpile/jobs/{jid}/volume", json={"density_t_per_m3": 1.6})
    assert vol.status_code == 200, vol.text
    piles = vol.json()["volumes"]["piles"]
    assert piles[0]["ok"] is True
    assert piles[0]["fill_m3"] > 0


def test_cleanup_leaves_stockpile_photos(client):
    c, user = client
    import store
    job = c.post("/stockpile/jobs", json={"name": "Keep me"}).json()
    jid = job["id"]
    files = [("files", (f"p{i}.jpg", _jpeg_with_gps(40, -88 + i * 0.0001), "image/jpeg")) for i in range(5)]
    assert c.post(f"/stockpile/jobs/{jid}/photos", files=files).status_code == 200
    folder = store.ensure_data_dir() / "stockpile" / str(user.id) / jid / "photos"
    assert folder.is_dir()
    before = list(folder.glob("*.jpg"))
    assert len(before) == 5
    store.cleanup()
    after = list(folder.glob("*.jpg"))
    assert len(after) == 5
