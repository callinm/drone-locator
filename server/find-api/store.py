"""Mosaic storage with size caps and auto-expiry."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import config
import locatorstore
from georef import GeoRef, read_geotiff_ref

Image.MAX_IMAGE_PIXELS = config.MAX_DECODE_PIXELS + 1

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TIFF_LE = b"II*\x00"
TIFF_BE = b"MM\x00*"


@dataclass
class Mosaic:
    id: str
    path: Path
    gray_path: Path
    meta_path: Path
    width: int
    height: int
    created: float
    expires: float
    bytes: int
    georef: GeoRef | None
    filename: str
    content_type: str
    owner_id: str | None = None


def _magic_ok(head: bytes) -> str | None:
    if head.startswith(JPEG_MAGIC):
        return "image/jpeg"
    if head.startswith(PNG_MAGIC):
        return "image/png"
    if head.startswith(TIFF_LE) or head.startswith(TIFF_BE):
        return "image/tiff"
    return None


def _now() -> float:
    return time.time()


def ensure_data_dir() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR


def cleanup(now: float | None = None) -> None:
    now = now or _now()
    root = ensure_data_dir()
    # Mosaic previews live in UUID folders with meta.json. Never touch
    # stockpile photos, processing results, or other durable account data.
    skip = {"stockpile", "processing", "locator"}
    for child in root.iterdir():
        if not child.is_dir() or child.name in skip:
            continue
        meta = child / "meta.json"
        stale = False
        if not meta.exists():
            stale = True
        else:
            try:
                data = json.loads(meta.read_text())
                if data.get("expires", 0) < now:
                    stale = True
            except Exception:
                stale = True
        if stale:
            shutil.rmtree(child, ignore_errors=True)


def _store_bytes() -> int:
    total = 0
    root = ensure_data_dir()
    for p in root.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _mosaic_count(owner_id: str | None = None) -> int:
    root = ensure_data_dir()
    total = 0
    for p in root.iterdir():
        meta = p / "meta.json"
        if not p.is_dir() or not meta.exists():
            continue
        if owner_id is None:
            total += 1
            continue
        try:
            data = json.loads(meta.read_text())
        except Exception:
            continue
        if str(data.get("owner_id") or "") == str(owner_id):
            total += 1
    return total


def load_mosaic(mosaic_id: str) -> Mosaic:
    cleanup()
    folder = ensure_data_dir() / mosaic_id
    meta_path = folder / "meta.json"
    if not folder.is_dir() or not meta_path.is_file():
        raise FileNotFoundError("Unknown or expired mosaic id.")
    data = json.loads(meta_path.read_text())
    if data.get("expires", 0) < _now():
        shutil.rmtree(folder, ignore_errors=True)
        raise FileNotFoundError("Unknown or expired mosaic id.")
    georef = GeoRef.from_dict(data["georef"]) if data.get("georef") else None
    return Mosaic(
        id=mosaic_id,
        path=folder / data["filename_stored"],
        gray_path=folder / "gray.tif",
        meta_path=meta_path,
        width=data["width"],
        height=data["height"],
        created=data["created"],
        expires=data["expires"],
        bytes=data["bytes"],
        georef=georef,
        filename=data.get("filename", "mosaic"),
        content_type=data.get("content_type", "application/octet-stream"),
        owner_id=str(data["owner_id"]) if data.get("owner_id") is not None else None,
    )


def open_gray(mosaic: Mosaic) -> np.ndarray:
    """Memory-map the cached grayscale TIFF when possible."""
    import tifffile

    path = mosaic.gray_path
    if path.exists():
        try:
            return tifffile.memmap(path, mode="r")
        except Exception:
            with tifffile.TiffFile(path) as tif:
                return tif.asarray()
    # Fallback: decode the original
    return decode_gray(mosaic.path)


def decode_gray(path: Path) -> np.ndarray:
    head = path.read_bytes()[:16]
    kind = _magic_ok(head)
    if kind == "image/tiff":
        import tifffile

        arr = tifffile.memmap(path, mode="r")
        if arr.ndim == 3:
            # Don't materialize a full RGB 20k image if we can help it — take first band.
            if arr.shape[0] in (3, 4) and arr.shape[0] < min(arr.shape[1:]):
                plane = np.asarray(arr[0])
            else:
                plane = np.asarray(arr[..., 0])
            return _as_u8(plane)
        return _as_u8(np.asarray(arr))
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        # Pillow fallback (e.g. some JPEGs)
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size
            if w * h > config.MAX_DECODE_PIXELS:
                raise ValueError("Image has too many pixels.")
            return np.array(im)
    return img


def _as_u8(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint8:
        return np.ascontiguousarray(arr)
    arr = np.asarray(arr, dtype=np.float64)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    lo, hi = np.percentile(finite, (1, 99))
    if hi <= lo:
        hi = lo + 1
    scaled = np.clip((arr - lo) / (hi - lo) * 255, 0, 255)
    return scaled.astype(np.uint8)


def decode_query(data: bytes) -> np.ndarray:
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Query image is too large (20 MiB cap).")
    kind = _magic_ok(data[:16])
    if not kind:
        raise ValueError("Query must be a JPEG, PNG, or TIFF.")
    arr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if arr is None:
        import io

        with Image.open(io.BytesIO(data)) as im:
            arr = np.array(im.convert("L"))
    if arr.size == 0:
        raise ValueError("Could not decode the query image.")
    if arr.shape[0] * arr.shape[1] > 16_000_000:
        raise ValueError("Query image has too many pixels.")
    return arr


def save_mosaic(
    data: bytes,
    filename: str,
    content_type: str,
    bounds: tuple[float, float, float, float] | None = None,
    owner_id: str | None = None,
) -> Mosaic:
    cleanup()
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"Upload exceeds the {config.MAX_UPLOAD_BYTES // (1024*1024)} MiB cap.")
    kind = _magic_ok(data[:16])
    if not kind:
        raise ValueError("File must be a JPEG, PNG, or TIFF.")
    suffix = Path(filename or "mosaic").suffix.lower()
    if suffix not in config.ALLOWED_SUFFIXES:
        suffix = { "image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif" }.get(kind, ".bin")

    if _store_bytes() + len(data) > config.MAX_STORE_BYTES:
        cleanup()
        if _store_bytes() + len(data) > config.MAX_STORE_BYTES:
            raise ValueError("Mosaic store is full. Wait for an older upload to expire, or delete one.")
    if _mosaic_count(owner_id) >= config.MAX_MOSAICS:
        cleanup()
        if _mosaic_count(owner_id) >= config.MAX_MOSAICS:
            raise ValueError(f"At most {config.MAX_MOSAICS} mosaics can be stored at once.")

    mosaic_id = uuid.uuid4().hex
    folder = ensure_data_dir() / mosaic_id
    folder.mkdir(parents=True, exist_ok=False)
    stored_name = f"original{suffix}"
    orig_path = folder / stored_name
    orig_path.write_bytes(data)

    try:
        gray = decode_gray(orig_path)
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise ValueError(f"Could not read that image: {exc}") from exc

    h, w = gray.shape[:2]
    if w * h > config.MAX_DECODE_PIXELS or w * h == 0:
        shutil.rmtree(folder, ignore_errors=True)
        raise ValueError("Image has too many pixels (or was empty).")

    import tifffile

    gray_path = folder / "gray.tif"
    tifffile.imwrite(gray_path, gray, photometric="minisblack")

    georef = None
    if kind == "image/tiff":
        georef = read_geotiff_ref(orig_path, w, h)
    if georef is None and bounds:
        west, south, east, north = bounds
        georef = GeoRef.from_bounds(west, south, east, north, w, h)
    if georef is not None:
        georef.width = w
        georef.height = h

    now = _now()
    meta = {
        "id": mosaic_id,
        "filename": filename,
        "filename_stored": stored_name,
        "content_type": kind,
        "width": w,
        "height": h,
        "created": now,
        "expires": now + config.TTL_SECONDS,
        "bytes": orig_path.stat().st_size + gray_path.stat().st_size,
        "georef": georef.to_dict() if georef else None,
        "owner_id": owner_id,
    }
    meta_path = folder / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    try:
        locatorstore.register_mosaic(mosaic_id, folder, meta, source="new")
    except Exception:
        pass
    return load_mosaic(mosaic_id)



def decode_color(path: Path) -> np.ndarray:
    """Decode a BGR uint8 image from JPEG/PNG/TIFF. Raises ValueError on failure."""
    head = path.read_bytes()[:16]
    kind = _magic_ok(head)
    if kind == "image/tiff":
        import tifffile
        arr = np.asarray(tifffile.imread(path))
        return _color_u8(arr)
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w * h > config.MAX_DECODE_PIXELS:
                raise ValueError("Image has too many pixels.")
            rgb = np.array(im)
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return img


def decode_query_color(data: bytes) -> np.ndarray:
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Query image is too large (20 MiB cap).")
    kind = _magic_ok(data[:16])
    if not kind:
        raise ValueError("Query must be a JPEG, PNG, or TIFF.")
    arr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        import io
        with Image.open(io.BytesIO(data)) as im:
            rgb = np.array(im.convert("RGB"))
            arr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if arr is None or arr.size == 0:
        raise ValueError("Could not decode the query image.")
    if arr.shape[0] * arr.shape[1] > 16_000_000:
        raise ValueError("Query image has too many pixels.")
    return arr


def _color_u8(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        return cv2.cvtColor(_as_u8(arr), cv2.COLOR_GRAY2BGR)
    # tifffile may be (C,H,W) or (H,W,C)
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[0] < min(arr.shape[1:]):
        arr = np.transpose(arr[:3], (1, 2, 0))
    elif arr.ndim == 3 and arr.shape[2] >= 3:
        arr = arr[:, :, :3]
    else:
        return cv2.cvtColor(_as_u8(np.asarray(arr)[..., 0] if arr.ndim == 3 else arr), cv2.COLOR_GRAY2BGR)
    # Assume RGB for TIFF photometric; OpenCV wants BGR.
    u8 = _as_u8(arr)
    return u8[:, :, ::-1].copy()


def open_color(mosaic: Mosaic, max_dim: int | None = None) -> tuple[np.ndarray, float]:
    """Return (bgr, scale) where boxes in this image map back to mosaic pixels by /scale."""
    img = decode_color(mosaic.path)
    h, w = img.shape[:2]
    cap = int(max_dim or config.MAX_WORKING_DIM)
    scale = min(1.0, cap / max(h, w))
    if scale < 1.0:
        img = cv2.resize(
            img,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    return img, float(scale)


def crop_bbox_color(mosaic: Mosaic, x: float, y: float, w: float, h: float) -> np.ndarray:
    img = decode_color(mosaic.path)
    H, W = img.shape[:2]
    x0 = int(max(0, np.floor(x)))
    y0 = int(max(0, np.floor(y)))
    x1 = int(min(W, np.ceil(x + w)))
    y1 = int(min(H, np.ceil(y + h)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("Query region is too small.")
    if (x1 - x0) * (y1 - y0) > 16_000_000:
        raise ValueError("Query region is too large.")
    return np.ascontiguousarray(img[y0:y1, x0:x1])


def crop_bbox(mosaic: Mosaic, x: float, y: float, w: float, h: float) -> np.ndarray:
    gray = open_gray(mosaic)
    H, W = gray.shape[:2]
    x0 = int(max(0, np.floor(x)))
    y0 = int(max(0, np.floor(y)))
    x1 = int(min(W, np.ceil(x + w)))
    y1 = int(min(H, np.ceil(y + h)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("Query region is too small.")
    if (x1 - x0) * (y1 - y0) > 16_000_000:
        raise ValueError("Query region is too large.")
    return np.ascontiguousarray(gray[y0:y1, x0:x1])


def preview_jpeg(mosaic: Mosaic, max_dim: int = 2048) -> bytes:
    """Downscaled JPEG of the mosaic for the web UI, in color when available."""
    import cv2

    max_dim = int(max(256, min(max_dim, 4096)))
    img = None
    try:
        color, _scale = open_color(mosaic, max_dim=max_dim)
        img = color
    except Exception:
        img = None
    if img is None:
        gray = open_gray(mosaic)
        h, w = gray.shape[:2]
        scale = min(1.0, max_dim / max(h, w))
        if scale < 1.0:
            img = cv2.resize(
                np.ascontiguousarray(gray),
                (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            img = np.ascontiguousarray(gray)
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img = cv2.resize(
            img,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise ValueError("Could not encode a preview.")
    return buf.tobytes()



def delete_user_mosaics(owner_id: str) -> int:
    """Remove every mosaic owned by this user. Returns how many folders were deleted."""
    if not owner_id:
        return 0
    root = ensure_data_dir()
    removed = 0
    for child in list(root.iterdir()):
        meta = child / "meta.json"
        if not child.is_dir() or not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text())
        except Exception:
            continue
        if str(data.get("owner_id") or "") == str(owner_id):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
