"""Photo-count limits for stockpile counting jobs.

Minimum of 30 photos per job. Default cap is 500, configurable per user
by an admin, with a per-plan default ready for future paid plans.
"""

from __future__ import annotations

from typing import Any

MIN_PHOTOS = 30
DEFAULT_PHOTO_CAP = 500
# Hard ceiling so an admin typo cannot set an unbounded cap.
MAX_PHOTO_CAP = 5000
DEFAULT_PLAN = "default"

# Planner guidance (not hard-enforced as flight parameters).
DEFAULT_FRONT_OVERLAP = 0.80
DEFAULT_SIDE_OVERLAP = 0.70
HEIGHT_MULTIPLIER_MIN = 3.0
HEIGHT_MULTIPLIER_MAX = 4.0
HEIGHT_MULTIPLIER_DEFAULT = 3.5


class PhotoLimitError(Exception):
    def __init__(self, status: int, detail: str, code: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


def clamp_cap(value: Any, default: int = DEFAULT_PHOTO_CAP) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return default
    if cap < MIN_PHOTOS:
        return MIN_PHOTOS
    if cap > MAX_PHOTO_CAP:
        return MAX_PHOTO_CAP
    return cap


def evaluate(photo_count: int, cap: int = DEFAULT_PHOTO_CAP) -> dict[str, Any]:
    cap = clamp_cap(cap)
    count = int(photo_count) if photo_count is not None else 0
    too_few = count < MIN_PHOTOS
    too_many = count > cap
    warning = None
    if too_few:
        warning = (
            f"This job has {count} photo{'s' if count != 1 else ''}. "
            f"Pile Count needs at least {MIN_PHOTOS} overlapping photos."
        )
    elif too_many:
        warning = (
            f"This job would take {count} photos, above the cap of {cap} for this account. "
            "Raise altitude, fly a smaller area, lower the overlap, or ask an admin to raise the cap."
        )
    elif count > int(cap * 0.9):
        warning = f"This job is close to the {cap}-photo cap ({count} photos)."
    return {
        "photo_count": count,
        "min_photos": MIN_PHOTOS,
        "photo_cap": cap,
        "ok": not too_few and not too_many,
        "too_few": too_few,
        "too_many": too_many,
        "warning": warning,
    }


def enforce(photo_count: int, cap: int = DEFAULT_PHOTO_CAP) -> dict[str, Any]:
    result = evaluate(photo_count, cap)
    if result["too_few"]:
        raise PhotoLimitError(400, result["warning"], "too_few")
    if result["too_many"]:
        raise PhotoLimitError(400, result["warning"], "too_many")
    return result


def suggested_altitude_m(tallest_pile_m: float | None) -> float | None:
    """Recommend 3–4× the tallest pile, mid-point 3.5×."""
    if tallest_pile_m is None:
        return None
    try:
        h = float(tallest_pile_m)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    return h * HEIGHT_MULTIPLIER_DEFAULT


def is_stockpile_mission(mission: dict | None) -> bool:
    if not isinstance(mission, dict):
        return False
    kind = (
        mission.get("kind")
        or mission.get("purpose")
        or (mission.get("settings") or {}).get("kind")
        or (mission.get("settings") or {}).get("preset")
        or ""
    )
    return str(kind).lower() in {"stockpile", "stockpile_counting", "stockpile-counting"}


def photo_count_of(mission: dict | None) -> int | None:
    if not isinstance(mission, dict):
        return None
    computed = mission.get("computed") or mission.get("stats") or {}
    if isinstance(computed, dict) and computed.get("photoCount") is not None:
        try:
            return int(computed["photoCount"])
        except (TypeError, ValueError):
            return None
    photos = mission.get("photos")
    if isinstance(photos, list):
        return len(photos)
    return None
