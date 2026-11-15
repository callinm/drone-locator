import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402
import locatorstore  # noqa: E402
import syncstore  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    monkeypatch.setattr(config, "LOCATOR_DB", store / "locator.sqlite")
    accounts.reset_connection()
    locatorstore.reset_connection()
    accounts._connect()
    syncstore.init()
    locatorstore.init()
    yield store
    locatorstore.reset_connection()
    accounts.reset_connection()


def test_migration_copies_and_does_not_delete(data_dir):
    user = accounts.create_user("Pat", "user-password-ok")
    # Legacy mosaic folder
    mid = "a" * 32
    folder = data_dir / mid
    folder.mkdir()
    meta = {
        "filename": "search.jpg",
        "filename_stored": "original.jpg",
        "width": 100,
        "height": 80,
        "created": 1,
        "expires": 9_999_999_999,
        "bytes": 12,
        "owner_id": str(user.id),
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "original.jpg").write_bytes(b"fake")

    syncstore.set_find_results(user.id, {"matches": [{"lat": 40.0, "lon": -88.0}]})
    before = syncstore.get_find_results(user.id)
    assert before and before["results"]["matches"]

    report = locatorstore.migrate_from_legacy(data_dir=data_dir)
    assert report["legacy_deleted"] is False
    assert report["mosaics_copied"] == 1
    assert report["results_copied"] == 1

    # Legacy still present
    assert (folder / "meta.json").is_file()
    assert (folder / "original.jpg").read_bytes() == b"fake"
    still = syncstore.get_find_results(user.id)
    assert still and still["results"]["matches"][0]["lat"] == 40.0

    # Copies in the new DB
    conn = sqlite3.connect(str(config.LOCATOR_DB))
    conn.row_factory = sqlite3.Row
    mosaics = list(conn.execute("SELECT * FROM mosaics"))
    results = list(conn.execute("SELECT * FROM find_results"))
    conn.close()
    assert len(mosaics) == 1
    assert mosaics[0]["id"] == mid
    assert mosaics[0]["source"] == "legacy-copy"
    assert len(results) == 1
    copied = json.loads(results[0]["results_json"])
    assert copied["matches"][0]["lon"] == -88.0

    # Re-running does not duplicate or delete
    again = locatorstore.migrate_from_legacy(data_dir=data_dir)
    assert again["mosaics_copied"] == 0
    assert again["mosaics_already_present"] == 1
    assert (folder / "original.jpg").exists()

    # New writes go to the locator DB (legacy row stays at previous version)
    locatorstore.set_find_results(user.id, {"matches": [{"lat": 41.1, "lon": -87.5}]})
    fresh = locatorstore.get_find_results(user.id)
    assert fresh["results"]["matches"][0]["lat"] == 41.1
    legacy = syncstore.get_find_results(user.id)
    assert legacy["results"]["matches"][0]["lat"] == 40.0
