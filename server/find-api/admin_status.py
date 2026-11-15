"""Admin processing dashboard: host metrics, running job, and queue."""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Any, Optional

import accounts
import config
import processing

# Sample about every 15s; keep a little over an hour.
HISTORY_SECONDS = 3600
SAMPLE_EVERY = 15.0
MAX_SAMPLES = int(HISTORY_SECONDS / SAMPLE_EVERY) + 8

_lock = threading.Lock()
_history: list[dict[str, Any]] = []
_prev_cpu: tuple[int, int] | None = None  # total, idle
_sampler_started = False


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    key = parts[0][:-1]
                    try:
                        out[key] = int(parts[1]) * 1024  # kB -> bytes
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            line = fh.readline()
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = []
    for p in parts[1:]:
        try:
            nums.append(int(p))
        except ValueError:
            break
    if len(nums) < 4:
        return None
    total = sum(nums)
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return total, idle


def _cpu_percent() -> float | None:
    global _prev_cpu
    cur = _read_proc_stat()
    if cur is None:
        return None
    prev = _prev_cpu
    _prev_cpu = cur
    if prev is None:
        return None
    dt = cur[0] - prev[0]
    didle = cur[1] - prev[1]
    if dt <= 0:
        return None
    used = max(0.0, min(100.0, 100.0 * (1.0 - (didle / dt))))
    return round(used, 1)


def _loadavg() -> tuple[float, float, float] | None:
    try:
        a, b, c = os.getloadavg()
        return (round(float(a), 2), round(float(b), 2), round(float(c), 2))
    except (OSError, AttributeError):
        return None


def _ncpu() -> int:
    return os.cpu_count() or 1


def _disk(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except OSError:
        return {"path": path, "total_bytes": 0, "used_bytes": 0, "free_bytes": 0}


def _level(pct: float | None, yellow: float, red: float) -> str:
    if pct is None:
        return "unknown"
    if pct >= red:
        return "red"
    if pct >= yellow:
        return "yellow"
    return "ok"


def host_stats() -> dict[str, Any]:
    mem = _read_meminfo()
    total = mem.get("MemTotal") or 0
    avail = mem.get("MemAvailable")
    if avail is None:
        avail = (mem.get("MemFree") or 0) + (mem.get("Buffers") or 0) + (mem.get("Cached") or 0)
    used = max(0, total - avail) if total else 0
    ram_pct = (100.0 * used / total) if total else None
    swap_total = mem.get("SwapTotal") or 0
    swap_free = mem.get("SwapFree") or 0
    swap_used = max(0, swap_total - swap_free)
    swap_pct = (100.0 * swap_used / swap_total) if swap_total else 0.0

    # Prefer the data volume when it exists, else the root filesystem.
    disk_path = str(config.DATA_DIR)
    try:
        if not os.path.isdir(disk_path):
            disk_path = "/"
    except OSError:
        disk_path = "/"
    disk = _disk(disk_path)
    # If DATA_DIR is on the same fs as /, that's fine. Also report root if different.
    root = _disk("/")
    disk_pct_used = (100.0 * disk["used_bytes"] / disk["total_bytes"]) if disk["total_bytes"] else None
    # Disk alert levels match the VPS policy: warn below 10 GiB, urgent below 3 GiB.
    disk_level = "ok"
    if disk["total_bytes"]:
        free = disk["free_bytes"]
        if free < 3 * 1024**3 or (disk_pct_used is not None and disk_pct_used >= 95):
            disk_level = "red"
        elif free < 10 * 1024**3 or (disk_pct_used is not None and disk_pct_used >= 85):
            disk_level = "yellow"

    cpu_pct = _cpu_percent()
    load = _loadavg()
    ncpu = _ncpu()
    load1 = load[0] if load else None
    load_pct = (100.0 * load1 / ncpu) if load1 is not None and ncpu else None

    return {
        "cpu_pct": cpu_pct,
        "cpu_level": _level(cpu_pct, 70, 90),
        "ncpu": ncpu,
        "load": {"1": load[0], "5": load[1], "15": load[2]} if load else None,
        "load_level": _level(load_pct, 70, 100),
        "ram_total_bytes": total,
        "ram_used_bytes": used,
        "ram_available_bytes": avail,
        "ram_pct": round(ram_pct, 1) if ram_pct is not None else None,
        "ram_level": _level(ram_pct, 80, 90),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_pct": round(swap_pct, 1),
        "swap_level": _level(swap_pct if swap_total else None, 25, 50) if swap_total else "ok",
        "disk": disk,
        "disk_root": root if root["path"] != disk["path"] else None,
        "disk_used_pct": round(disk_pct_used, 1) if disk_pct_used is not None else None,
        "disk_level": disk_level,
        "sampled_at": time.time(),
    }


def _record_sample(stats: dict[str, Any] | None = None) -> None:
    stats = stats or host_stats()
    sample = {
        "t": stats.get("sampled_at") or time.time(),
        "cpu_pct": stats.get("cpu_pct"),
        "load1": (stats.get("load") or {}).get("1") if isinstance(stats.get("load"), dict) else None,
        "ram_pct": stats.get("ram_pct"),
        "swap_pct": stats.get("swap_pct"),
        "disk_free_bytes": (stats.get("disk") or {}).get("free_bytes"),
        "disk_used_pct": stats.get("disk_used_pct"),
    }
    cutoff = time.time() - HISTORY_SECONDS
    with _lock:
        _history.append(sample)
        # Drop anything older than an hour, and cap length.
        while _history and _history[0]["t"] < cutoff:
            _history.pop(0)
        if len(_history) > MAX_SAMPLES:
            del _history[: len(_history) - MAX_SAMPLES]


def history() -> list[dict[str, Any]]:
    cutoff = time.time() - HISTORY_SECONDS
    with _lock:
        return [s for s in _history if s["t"] >= cutoff]


def _username(user_id: int | None) -> str:
    if not user_id:
        return "unknown"
    user = accounts.get_user_by_id(int(user_id))
    return user.username if user else f"user-{user_id}"


def _webodm_snapshot(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-effort NodeODM/WebODM status. Never returns credentials or host URLs."""
    info: dict[str, Any] = {"reachable": False}
    try:
        import webodm

        creds = webodm.load_credentials()
        if not creds:
            info["detail"] = "WebODM credentials are not configured."
            return info
        with webodm.WebOdmClient(creds) as client:
            info["reachable"] = True
            # Prefer a running task's live status if we have one.
            running = next((j for j in jobs if j.get("status") == "processing" and j.get("webodm_task_id")), None)
            if running and running.get("webodm_project_id") is not None:
                try:
                    task = client.get_task(int(running["webodm_project_id"]), str(running["webodm_task_id"]))
                    code, label = webodm.normalize_status(task.get("status"))
                    info["task_status"] = label
                    info["task_status_code"] = code
                    if task.get("processing_node") is not None:
                        info["node"] = str(task.get("processing_node"))
                    if task.get("last_error"):
                        info["task_error"] = str(task.get("last_error"))[:240]
                except Exception:
                    info["task_status"] = "unknown"
            try:
                nodes = client._json("GET", "api/processingnodes/")
                if isinstance(nodes, list):
                    info["nodes"] = len(nodes)
                    online = 0
                    queue = 0
                    for n in nodes:
                        if not isinstance(n, dict):
                            continue
                        if n.get("online") or n.get("available"):
                            online += 1
                        q = n.get("queue_count") or n.get("queue") or n.get("task_queue")
                        if isinstance(q, (int, float)):
                            queue += int(q)
                    info["nodes_online"] = online
                    info["node_queue"] = queue
            except Exception:
                pass
    except Exception as exc:
        info["detail"] = "WebODM status could not be read."
        info["error_type"] = type(exc).__name__
    return info


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    view = processing.public_view(job)
    waiting_since = job.get("created_at") or job.get("started_at") or job.get("updated_at")
    source = "stockpile" if job.get("stockpile_job_id") else "process"
    return {
        "id": view["id"],
        "user": _username(job.get("user_id")),
        "name": view.get("name") or "Untitled job",
        "photo_count": view.get("photo_count") or 0,
        "stage": view.get("stage"),
        "stage_label": view.get("stage_label"),
        "status": view.get("status"),
        "progress_pct": view.get("progress_pct") or 0,
        "elapsed_sec": view.get("elapsed_sec"),
        "progress": view.get("progress"),
        "waiting_since": waiting_since,
        "source": source,
        "stockpile_job_id": job.get("stockpile_job_id"),
    }


def processing_overview() -> dict[str, Any]:
    jobs = processing.list_active_jobs()
    running_raw = [j for j in jobs if j.get("status") in {"processing", "uploading"}]
    # Prefer a processing job over a mere upload as "the" running one.
    running_raw.sort(key=lambda j: (0 if j.get("status") == "processing" else 1, j.get("started_at") or 0))
    running = _job_payload(running_raw[0]) if running_raw else None
    running_ids = {running["id"]} if running else set()
    queue = [_job_payload(j) for j in jobs if j.get("id") not in running_ids]
    # Queue is waiting jobs, oldest first (already created_at ASC from SQL).
    stats = host_stats()
    _record_sample(stats)
    return {
        "host": stats,
        "history": history(),
        "running": running,
        "queue": queue,
        "active_count": len(jobs),
        "webodm": _webodm_snapshot(jobs),
        "updated_at": time.time(),
    }


def _sampler_loop() -> None:
    # Prime the first CPU sample so the next tick has a percent.
    host_stats()
    time.sleep(1.0)
    _record_sample()
    while True:
        time.sleep(SAMPLE_EVERY)
        try:
            _record_sample()
        except Exception:
            continue


def start_sampler() -> None:
    global _sampler_started
    with _lock:
        if _sampler_started:
            return
        _sampler_started = True
    t = threading.Thread(target=_sampler_loop, name="admin-metrics", daemon=True)
    t.start()
