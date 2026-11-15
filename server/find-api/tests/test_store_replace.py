"""At the per-account map limit, a new map replaces that account's oldest map (Callin, Nov 14 2026)."""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import locatorstore  # noqa: E402
import store  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "LOCATOR_DB", root / "locator.sqlite")
    monkeypatch.setattr(config, "MAX_MOSAICS", 4)
    locatorstore.reset_connection()
    monkeypatch.setattr(config, "TTL_SECONDS", 12 * 3600)
    store._last_used.clear()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(store, "_now", lambda: clock["t"])
    yield root, clock
    locatorstore.reset_connection()


def _jpeg() -> bytes:
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[:] = (20, 80, 160)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _save(clock, owner, step=1000.0):
    clock["t"] += step  # older maps have earlier "created" and are not "in use"
    return store.save_mosaic(_jpeg(), "m.jpg", "image/jpeg", owner_id=owner)


def _rows(owner):
    conn = locatorstore._connect()
    return {r["id"] for r in conn.execute("SELECT id FROM mosaics WHERE owner_id = ?", (owner,))}


def test_fifth_map_replaces_the_same_users_oldest(env):
    root, clock = env
    other = _save(clock, "2")
    mine = [_save(clock, "1") for _ in range(4)]
    new = _save(clock, "1")
    assert new.replaced == [mine[0].id]
    assert not (root / mine[0].id).exists()
    assert mine[0].id not in _rows("1")
    assert store._mosaic_count("1") == 4
    for m in mine[1:] + [new]:
        assert (root / m.id / "meta.json").is_file()
    # Another account's map is never touched.
    assert (root / other.id / "meta.json").is_file() and other.id in _rows("2")


def test_failed_new_map_never_removes_an_old_one(env):
    root, clock = env
    mine = [_save(clock, "1") for _ in range(4)]
    with pytest.raises(ValueError):
        store.save_mosaic(b"\xff\xd8\xff\xe0 not really a jpeg", "bad.jpg", "image/jpeg", owner_id="1")
    assert store._mosaic_count("1") == 4
    assert all((root / m.id / "meta.json").is_file() for m in mine)


def test_map_in_use_is_skipped_and_next_oldest_goes(env):
    root, clock = env
    mine = [_save(clock, "1") for _ in range(4)]
    clock["t"] += 1000
    store.load_mosaic(mine[0].id)  # e.g. a search running on the oldest map
    new = store.save_mosaic(_jpeg(), "m.jpg", "image/jpeg", owner_id="1")
    assert new.replaced == [mine[1].id]
    assert (root / mine[0].id).exists() and not (root / mine[1].id).exists()


def test_under_the_limit_nothing_is_replaced(env):
    root, clock = env
    mine = [_save(clock, "1") for _ in range(3)]
    new = _save(clock, "1")
    assert new.replaced == []
    assert store._mosaic_count("1") == 4
