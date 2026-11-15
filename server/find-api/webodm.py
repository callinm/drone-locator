"""Internal WebODM HTTP client used by find-api only.

Talks to the local WebODM instance over loopback (or the docker network).
A single service account is loaded from a 0600 credentials file. Tokens
and passwords never leave the server and are never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

import httpx

DEFAULT_BASE = os.environ.get("WEBODM_BASE_URL", "http://127.0.0.1:8001")
DEFAULT_CREDENTIALS_FILE = os.environ.get(
    "WEBODM_CREDENTIALS_FILE",
    "/home/ubuntu/private/drone-mapper-webodm.txt",
)


class WebOdmError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _is_internal_host(host: str) -> bool:
    """True for loopback, docker DNS names, and private-network addresses."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h in {"127.0.0.1", "localhost", "webapp", "webodm", "webodm_webapp", "host.docker.internal", "::1"}:
        return True
    if h.endswith(".internal") or h.endswith(".docker") or h.endswith(".local"):
        return True
    # Compose service names are a single DNS label.
    if "." not in h and h.replace("-", "_").isidentifier():
        return True
    # Private IPv4 (docker bridge, etc.)
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or a == 127 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
            return True
        return False
    return False


def assert_internal_base(url: str) -> str:
    """Reject public hostnames. WebODM is only used over localhost or docker."""
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or "").lower()
    if _is_internal_host(host):
        return url
    raise WebOdmError(
        503,
        "The processing service is only reachable on the internal network.",
    )


# Survey-quality stockpile / mapping defaults. fast-orthophoto stays off so
# the DSM and orthophoto come from a full reconstruction.
STOCKPILE_OPTIONS = [
    {"name": "dsm", "value": "true"},
    {"name": "auto-boundary", "value": "true"},
    {"name": "fast-orthophoto", "value": "false"},
]

ALLOWED_ASSETS = frozenset(
    {
        "orthophoto.tif",
        "orthophoto.png",
        "dsm.tif",
        "dtm.tif",
        "report.pdf",
        "shots.geojson",
        "cameras.json",
    }
)

# WebODM Task.STATUS_* codes
STATUS_QUEUED = 10
STATUS_RUNNING = 20
STATUS_FAILED = 30
STATUS_COMPLETED = 40
STATUS_CANCELED = 50

STATUS_LABEL = {
    STATUS_QUEUED: "queued",
    STATUS_RUNNING: "processing",
    STATUS_FAILED: "failed",
    STATUS_COMPLETED: "done",
    STATUS_CANCELED: "failed",
}



class Credentials:
    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE,
        project_id: int | None = None,
        path: Path | None = None,
    ):
        self.username = username
        self.password = password
        self.base_url = assert_internal_base((base_url or DEFAULT_BASE).rstrip("/"))
        self.project_id = project_id
        self.path = path


_KEY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$")


def load_credentials(path: str | Path | None = None) -> Optional[Credentials]:
    """Read the service-account file. Returns None if it is missing or incomplete."""
    raw_path = path or os.environ.get("WEBODM_CREDENTIALS_FILE") or DEFAULT_CREDENTIALS_FILE
    file_path = Path(raw_path)
    if not file_path.is_file():
        return None
    values: dict[str, str] = {}
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_RE.match(stripped)
        if not match:
            continue
        key = match.group(1).strip().upper()
        value = match.group(2).strip().strip("'").strip('"')
        values[key] = value

    username = values.get("WEBODM_USERNAME") or values.get("USERNAME") or ""
    password = values.get("WEBODM_PASSWORD") or values.get("PASSWORD") or ""
    base = assert_internal_base(values.get("WEBODM_BASE_URL") or values.get("BASE_URL") or DEFAULT_BASE)
    project_raw = values.get("WEBODM_PROJECT_ID") or values.get("PROJECT_ID") or ""
    project_id = None
    if project_raw:
        try:
            project_id = int(project_raw)
        except ValueError:
            project_id = None
    if not username or not password:
        return None
    return Credentials(username=username, password=password, base_url=base, project_id=project_id, path=file_path)


def normalize_status(code: Any) -> tuple[int, str]:
    try:
        num = int(code)
    except (TypeError, ValueError):
        num = 0
    return num, STATUS_LABEL.get(num, "unknown")


def bounds_from_task(task: dict[str, Any]) -> Optional[dict[str, float]]:
    for key in ("orthophoto_extent", "extent", "bbox", "bounds"):
        value = task.get(key)
        parsed = _parse_bounds(value)
        if parsed:
            return parsed
    tiles = task.get("tiles")
    if isinstance(tiles, dict):
        parsed = _parse_bounds(tiles.get("bounds"))
        if parsed:
            return parsed
    return None


def _parse_bounds(value: Any) -> Optional[dict[str, float]]:
    if isinstance(value, dict) and all(k in value for k in ("west", "south", "east", "north")):
        try:
            return {
                "west": float(value["west"]),
                "south": float(value["south"]),
                "east": float(value["east"]),
                "north": float(value["north"]),
            }
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            west, south, east, north = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        except (TypeError, ValueError):
            return None
        return {"west": west, "south": south, "east": east, "north": north}
    return None


def public_options(options: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    return [dict(item) for item in (options or STOCKPILE_OPTIONS)]



def _looks_like_disk_full(text: str | None) -> bool:
    blob = (text or "").lower()
    return any(token in blob for token in ("no space left", "errno 28", "enospc"))


def _unavailable_detail(source: object = None) -> str:
    """User-facing 5xx message. Mentions a full disk when the service said so."""
    text = ""
    if isinstance(source, httpx.Response):
        try:
            text = source.text or ""
        except Exception:
            text = ""
    elif source is not None:
        text = str(source)
    if _looks_like_disk_full(text):
        return "The processing service is out of disk space. Processing cannot continue until space is freed."
    return "The processing service is temporarily unavailable."


class WebOdmClient:
    def __init__(
        self,
        credentials: Credentials,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ):
        self.credentials = credentials
        self.base_url = credentials.base_url.rstrip("/") + "/"
        self._token: str | None = None
        self.timeout = timeout
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "WebOdmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"JWT {self._token}"
        return headers

    def _request(self, method: str, path: str, retry_auth: bool = True, **kwargs) -> httpx.Response:
        url = path if path.startswith("http") else urljoin(self.base_url, path.lstrip("/"))
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise WebOdmError(503, _unavailable_detail(exc)) from exc
        if response.status_code in (401, 403) and retry_auth and path.rstrip("/") != "api/token-auth":
            self.authenticate()
            return self._request(method, path, retry_auth=False, **kwargs)
        return response

    def _json(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kwargs) -> Any:
        response = self._request(method, path, **kwargs)
        if response.status_code not in ok:
            raise WebOdmError(self._safe_status(response), self._safe_detail(response))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise WebOdmError(502, "The processing service returned an invalid response.") from exc

    @staticmethod
    def _safe_status(response: httpx.Response) -> int:
        if response.status_code in (401, 403):
            return 503
        if response.status_code >= 500:
            return 503
        if response.status_code == 404:
            return 404
        return 502

    @staticmethod
    def _safe_detail(response: httpx.Response) -> str:
        # Never forward WebODM HTML, stack traces, or URLs to the caller.
        if response.status_code in (401, 403):
            return "The processing service could not be reached."
        if response.status_code == 404:
            return "That processing job was not found."
        if response.status_code == 400:
            return "The processing service rejected that request."
        return _unavailable_detail(response)

    def authenticate(self) -> str:
        response = self.client.post(
            urljoin(self.base_url, "api/token-auth/"),
            data={
                "username": self.credentials.username,
                "password": self.credentials.password,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise WebOdmError(503, "The processing service could not be reached.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebOdmError(503, "The processing service could not be reached.") from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise WebOdmError(503, "The processing service could not be reached.")
        self._token = str(token)
        return self._token

    def ensure_project(self) -> int:
        if self.credentials.project_id:
            return int(self.credentials.project_id)
        if not self._token:
            self.authenticate()
        payload = self._json("GET", "api/projects/")
        items = payload if isinstance(payload, list) else payload.get("results") or []
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                return int(item["id"])
        created = self._json("POST", "api/projects/", json={"name": "Drone Mapper"})
        if not isinstance(created, dict) or created.get("id") is None:
            raise WebOdmError(503, "The processing service could not be reached.")
        return int(created["id"])

    def create_task(
        self,
        name: str,
        options: list[dict[str, str]] | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        if not self._token:
            self.authenticate()
        pid = project_id or self.ensure_project()
        # Create a partial task, then upload images, then commit. A non-partial
        # create requires the images in the same request.
        payload = self._json(
            "POST",
            f"api/projects/{pid}/tasks/",
            files={
                "name": (None, name[:200]),
                "options": (None, _options_json(options or STOCKPILE_OPTIONS)),
                "auto_processing_node": (None, "true"),
                "partial": (None, "true"),
            },
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise WebOdmError(502, "The processing service returned an invalid response.")
        payload["_project_id"] = pid
        return payload

    def upload_images(
        self,
        project_id: int,
        task_id: str,
        files: Iterable[tuple[str, bytes]],
        batch_size: int = 8,
        on_progress=None,
        total: int | None = None,
    ) -> int:
        if not self._token:
            self.authenticate()
        items = [(n, d) for n, d in files if d]
        expected = total if total is not None else len(items)
        uploaded = 0
        batch: list[tuple[str, bytes]] = []
        for filename, data in items:
            batch.append((filename, data))
            if len(batch) >= batch_size:
                uploaded += self._upload_batch(project_id, task_id, batch)
                batch = []
                if on_progress:
                    on_progress(uploaded, expected)
        if batch:
            uploaded += self._upload_batch(project_id, task_id, batch)
            if on_progress:
                on_progress(uploaded, expected)
        return uploaded

    def _upload_batch(self, project_id: int, task_id: str, batch: list[tuple[str, bytes]]) -> int:
        files = [("images", (name, content, "application/octet-stream")) for name, content in batch]
        response = self._request(
            "POST",
            f"api/projects/{project_id}/tasks/{task_id}/upload/",
            files=files,
            timeout=max(self.timeout, 120.0),
        )
        if response.status_code not in (200, 201):
            raise WebOdmError(self._safe_status(response), self._safe_detail(response))
        return len(batch)

    def commit(self, project_id: int, task_id: str) -> dict[str, Any]:
        if not self._token:
            self.authenticate()
        return self._json("POST", f"api/projects/{project_id}/tasks/{task_id}/commit/")

    def get_task(self, project_id: int, task_id: str) -> dict[str, Any]:
        if not self._token:
            self.authenticate()
        return self._json("GET", f"api/projects/{project_id}/tasks/{task_id}/")

    def get_task_output(self, project_id: int, task_id: str, limit: int = 120) -> str:
        """Latest console lines. Used only to map the current ODM stage. Never returned to clients."""
        if not self._token:
            self.authenticate()
        response = self._request(
            "GET",
            f"api/projects/{project_id}/tasks/{task_id}/output/",
            params={"limit": -abs(int(limit or 120)), "f": "text"},
        )
        if response.status_code != 200:
            return ""
        text = response.text or ""
        # Some versions return a JSON string.
        if text.startswith("{") or text.startswith("["):
            try:
                import json
                data = json.loads(text)
                if isinstance(data, list):
                    text = "\n".join(str(x) for x in data)
                elif isinstance(data, dict):
                    text = str(data.get("output") or data.get("console") or "")
            except Exception:
                pass
        return text

    def download_asset(self, project_id: int, task_id: str, name: str) -> tuple[bytes, str]:
        if name not in ALLOWED_ASSETS or "/" in name or ".." in name:
            raise WebOdmError(404, "Unknown asset.")
        if not self._token:
            self.authenticate()
        response = self._request(
            "GET",
            f"api/projects/{project_id}/tasks/{task_id}/download/{name}",
            timeout=max(self.timeout, 180.0),
        )
        if response.status_code != 200:
            raise WebOdmError(self._safe_status(response), self._safe_detail(response))
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return response.content, content_type.split(";")[0].strip()

    def tile_png(self, project_id: int, task_id: str, z: int, x: int, y: int) -> bytes:
        if not self._token:
            self.authenticate()
        # WebODM orthophoto XYZ tiles
        for path in (
            f"api/projects/{project_id}/tasks/{task_id}/orthophoto/tiles/{z}/{x}/{y}.png",
            f"api/projects/{project_id}/tasks/{task_id}/orthophoto/{z}/{x}/{y}.png",
        ):
            response = self._request("GET", path)
            if response.status_code == 200 and response.content:
                return response.content
        raise WebOdmError(404, "That tile is not available.")


def _options_json(options: list[dict[str, str]]) -> str:
    import json

    return json.dumps(public_options(options))
