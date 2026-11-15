"""The mosaic store cap must count finder mosaics only, not processing jobs."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import store  # noqa: E402


def _write(path: Path, size: int, content: bytes = b"x") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (content * size)[:size]
    path.write_bytes(data)
    return path.stat().st_size


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", root)
    return root


def test_store_bytes_counts_only_mosaic_folders(data_dir):
    mosaic = data_dir / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    mosaic_bytes = _write(mosaic / "original.jpg", 1_000)
    mosaic_bytes += _write(mosaic / "gray.tif", 400)
    mosaic_bytes += _write(mosaic / "meta.json", 80, b'{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')

    # These would have pushed a 2 GiB cap over before the fix.
    _write(data_dir / "processing" / "1" / "job" / "photos" / "0001.jpg", 3_000_000)
    _write(data_dir / "processing-uploads" / "1" / "upload" / "manifest.json", 2_000)
    _write(data_dir / "stockpile" / "1" / "job" / "photo.jpg", 2_000_000)
    _write(data_dir / "accounts.sqlite", 50_000)
    _write(data_dir / "locator.sqlite", 4_096)

    # A non-mosaic dir at the root (no meta.json) must not be counted.
    _write(data_dir / "scratch" / "junk.bin", 500_000)

    assert store._store_bytes() == mosaic_bytes


def test_store_bytes_empty_data_dir(data_dir):
    assert store._store_bytes() == 0


def test_store_cap_allows_upload_when_only_processing_is_large(data_dir, monkeypatch, tmp_path):
    """A processing photo cache over the cap must not block a mosaic save."""
    import numpy as np
    import cv2

    _write(data_dir / "processing" / "1" / "job" / "photos" / "0001.jpg", 200_000)
    monkeypatch.setattr(config, "MAX_STORE_BYTES", 50_000)
    monkeypatch.setattr(config, "MAX_MOSAICS", 4)
    monkeypatch.setattr(config, "LOCATOR_DB", data_dir / "locator.sqlite")
    import locatorstore
    locatorstore.reset_connection()

    # Tiny valid JPEG so save_mosaic does not depend on a huge fixture.
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[:] = (20, 80, 160)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    jpeg = buf.tobytes()
    assert len(jpeg) < 50_000

    mosaic = store.save_mosaic(jpeg, "tiny.jpg", "image/jpeg", owner_id="1")
    assert mosaic.id
    assert (data_dir / mosaic.id / "meta.json").is_file()
    # Cap still only sees the new mosaic, not the 200 KiB processing file.
    assert store._store_bytes() < 50_000
    assert store._store_bytes() > 0
