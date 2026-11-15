"""Resource caps sized for a modest VPS (6 CPU, 11 GiB, no swap)."""

import os
from pathlib import Path

HOST = os.environ.get("FIND_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("FIND_API_PORT", "8091"))

# Never listen elsewhere in production; Caddy terminates TLS and proxies here.
BIND_HOST = HOST

DATA_DIR = Path(os.environ.get("FIND_API_DATA", Path(__file__).resolve().parent / "data"))
LOCATOR_DB = Path(os.environ.get("FIND_API_LOCATOR_DB", DATA_DIR / "locator.sqlite"))

CV_THREADS = int(os.environ.get("FIND_API_CV_THREADS", "2"))
MATCH_CONCURRENCY = int(os.environ.get("FIND_API_MATCH_CONCURRENCY", "2"))
# Hard per-request budget; the matcher returns best-so-far when this elapses.
MATCH_TIMEOUT_S = float(os.environ.get("FIND_API_MATCH_TIMEOUT", "20"))

MAX_UPLOAD_BYTES = int(os.environ.get("FIND_API_MAX_UPLOAD", str(250 * 1024 * 1024)))
MAX_STORE_BYTES = int(os.environ.get("FIND_API_MAX_STORE", str(2 * 1024 * 1024 * 1024)))
MAX_MOSAICS = int(os.environ.get("FIND_API_MAX_MOSAICS", "4"))
TTL_SECONDS = int(os.environ.get("FIND_API_TTL", str(12 * 3600)))

# 20k×20k grayscale is 400 MiB; allow that, reject even larger decodes.
MAX_DECODE_PIXELS = int(os.environ.get("FIND_API_MAX_PIXELS", str(500_000_000)))
# Finest full-frame pyramid level held as a dense array.
MAX_WORKING_DIM = int(os.environ.get("FIND_API_WORKING_DIM", "8000"))

TOP_N_DEFAULT = 8
TOP_N_MAX = 100

ALLOWED_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/tif",
    "application/octet-stream",
}
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
PUBLIC_URL = os.environ.get("FIND_API_PUBLIC_URL", "https://piletally.com").rstrip("/")
