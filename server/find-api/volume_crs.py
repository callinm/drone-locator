"""Map a drawn pile ring into the DSM raster's coordinate frame.

Users draw in lon/lat (or on the orthophoto). WebODM DSMs may be in a
projected CRS, or in a local meter frame with a (0, 0) tie point and no
usable GPS. Silent zeros are not acceptable: either transform the ring
so it overlaps the raster, or return a clear error.
"""

from __future__ import annotations

from typing import Any

import numpy as np

LOCAL_FRAME_MESSAGE = (
    "This elevation model has no real-world GPS (the reconstruction used a "
    "local frame, often because the photos had no GPS). Draw the pile on the "
    "orthophoto, not on a world map."
)

NO_OVERLAP_MESSAGE = (
    "The pile outline does not overlap the elevation model. Draw the pile on "
    "the orthophoto, or check that the outline is in the right place."
)


def raster_extent(dsm: dict) -> dict[str, float]:
    z = dsm["z"]
    rows, cols = int(z.shape[0]), int(z.shape[1])
    ox, oy, dx, dy = float(dsm["origin_x"]), float(dsm["origin_y"]), float(dsm["dx"]), float(dsm["dy"])
    x1 = ox + cols * dx
    y1 = oy + rows * dy
    return {
        "west": min(ox, x1),
        "east": max(ox, x1),
        "south": min(oy, y1),
        "north": max(oy, y1),
        "width_px": cols,
        "height_px": rows,
        "width_m": abs(cols * dx),
        "height_m": abs(rows * dy),
    }


def is_geographic_crs(crs: str | None) -> bool:
    if not crs:
        return False
    key = str(crs).strip().upper()
    return key in {"EPSG:4326", "GEOGRAPHIC", "BOUNDS", "WGS84"} or key.endswith(":4326")


def is_local_frame(dsm: dict) -> bool:
    """True when the GeoTIFF is tagged projected (or unknown) but has no real world origin.

    Typical case: a (0, 0) tie point and a small local meter extent, even if a
    UTM EPSG code was copied onto the file.
    """
    if is_geographic_crs(dsm.get("crs")):
        return False
    ox, oy = float(dsm.get("origin_x", 0.0)), float(dsm.get("origin_y", 0.0))
    ext = raster_extent(dsm)
    small = ext["width_m"] < 20_000 and ext["height_m"] < 20_000
    origin_near_zero = abs(ox) < 10 and abs(oy) < 10
    return bool(small and origin_near_zero)


def looks_like_lonlat(ring: list[dict]) -> bool:
    if len(ring) < 3:
        return False
    xs = [float(p["x"]) for p in ring]
    ys = [float(p["y"]) for p in ring]
    if any(abs(x) > 180 or abs(y) > 90 for x, y in zip(xs, ys)):
        return False
    # A single pile is a tiny geographic patch, not a continent, and not a
    # whole UTM zone. Local-meter rings on a small site can also fall in this
    # numeric range — overlap is checked first by the caller.
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    return span_x < 5 and span_y < 5


def looks_like_pixels(ring: list[dict], dsm: dict) -> bool:
    rows, cols = dsm["z"].shape[:2]
    if cols < 2 or rows < 2:
        return False
    xs = [float(p["x"]) for p in ring]
    ys = [float(p["y"]) for p in ring]
    if min(xs) < -0.5 or min(ys) < -0.5:
        return False
    if max(xs) > cols + 0.5 or max(ys) > rows + 0.5:
        return False
    # Must actually span some pixels, not a sub-meter geographic/local blip.
    return (max(xs) - min(xs) >= 2) or (max(ys) - min(ys) >= 2)


def ring_overlaps(dsm: dict, ring: list[dict], pad: float | None = None) -> bool:
    if len(ring) < 3:
        return False
    ext = raster_extent(dsm)
    if pad is None:
        pad = max(abs(float(dsm["dx"])), abs(float(dsm["dy"])), 1e-9) * 2
    xs = [float(p["x"]) for p in ring]
    ys = [float(p["y"]) for p in ring]
    return not (
        max(xs) < ext["west"] - pad
        or min(xs) > ext["east"] + pad
        or max(ys) < ext["south"] - pad
        or min(ys) > ext["north"] + pad
    )


def pixel_ring_to_metric(dsm: dict, ring: list[dict]) -> list[dict]:
    ox, oy = float(dsm["origin_x"]), float(dsm["origin_y"])
    dx, dy = float(dsm["dx"]), float(dsm["dy"])
    return [{"x": ox + float(p["x"]) * dx, "y": oy + float(p["y"]) * dy} for p in ring]


def _epsg_code(crs: str | None) -> int | None:
    if not crs:
        return None
    text = str(crs).strip()
    if text.upper().startswith("EPSG:"):
        try:
            return int(text.split(":", 1)[1])
        except ValueError:
            return None
    return None


def transform_lonlat_to_dsm(dsm: dict, ring: list[dict]) -> list[dict] | None:
    """Project lon/lat vertices into the DSM CRS. Returns None if not possible."""
    if is_geographic_crs(dsm.get("crs")):
        return [{"x": float(p["x"]), "y": float(p["y"])} for p in ring]
    if is_local_frame(dsm):
        return None
    epsg = _epsg_code(dsm.get("crs"))
    if epsg is None:
        return None
    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        out = []
        for p in ring:
            x, y = transformer.transform(float(p["x"]), float(p["y"]))
            if not (np.isfinite(x) and np.isfinite(y)):
                return None
            out.append({"x": float(x), "y": float(y)})
        return out
    except Exception:
        return None


def points_from_pile(pile: dict) -> list[dict]:
    ring = pile.get("ring") if isinstance(pile, dict) else None
    if ring and len(ring) >= 3:
        pts = []
        for pt in ring:
            if not isinstance(pt, dict):
                continue
            if "x" in pt and "y" in pt and "lon" not in pt:
                pts.append({"x": float(pt["x"]), "y": float(pt["y"])})
            else:
                pts.append({"x": float(pt.get("lon", pt.get("x"))), "y": float(pt.get("lat", pt.get("y")))})
        if len(pts) >= 3:
            return pts
    lat = float(pile["lat"])
    lon = float(pile["lon"])
    return [{"x": lon, "y": lat}]  # single point; expanded later


def default_tap_square(dsm: dict, x: float, y: float) -> list[dict]:
    """A modest square around a tap, in the DSM's units (meters or degrees)."""
    ext = raster_extent(dsm)
    if is_geographic_crs(dsm.get("crs")) and not is_local_frame(dsm):
        d = 0.00008  # ~9 m at mid latitudes
    else:
        # About 8 m, but never larger than 20% of the raster.
        d = min(8.0, 0.1 * max(ext["width_m"], ext["height_m"], 1.0))
        d = max(d, 4 * max(abs(float(dsm["dx"])), abs(float(dsm["dy"]))))
    return [
        {"x": x - d, "y": y - d},
        {"x": x + d, "y": y - d},
        {"x": x + d, "y": y + d},
        {"x": x - d, "y": y + d},
    ]


class RingError(ValueError):
    """A pile outline cannot be measured; `args[0]` is a user-facing message."""


def prepare_ring(dsm: dict, pile: dict) -> list[dict]:
    """Return a ring in DSM map coordinates, or raise RingError with a clear message."""
    raw = points_from_pile(pile)
    if len(raw) == 1:
        raw = default_tap_square(dsm, raw[0]["x"], raw[0]["y"])
    candidates: list[tuple[str, list[dict]]] = [("as_is", raw)]
    if looks_like_pixels(raw, dsm) and not ring_overlaps(dsm, raw):
        candidates.append(("pixel", pixel_ring_to_metric(dsm, raw)))
    if looks_like_lonlat(raw) and not is_geographic_crs(dsm.get("crs")):
        projected = transform_lonlat_to_dsm(dsm, raw)
        if projected is not None:
            candidates.append(("projected", projected))

    for _kind, ring in candidates:
        if ring_overlaps(dsm, ring):
            return ring

    if looks_like_lonlat(raw) and is_local_frame(dsm):
        raise RingError(LOCAL_FRAME_MESSAGE)
    raise RingError(NO_OVERLAP_MESSAGE)


def frame_info(dsm: dict) -> dict[str, Any]:
    ext = raster_extent(dsm)
    local = is_local_frame(dsm)
    info = {
        "crs": "local-meters" if local else (dsm.get("crs") or "unknown"),
        "tagged_crs": dsm.get("crs") or "unknown",
        "local_frame": local,
        "origin_x": float(dsm["origin_x"]),
        "origin_y": float(dsm["origin_y"]),
        "dx": float(dsm["dx"]),
        "dy": float(dsm["dy"]),
        "west": ext["west"],
        "south": ext["south"],
        "east": ext["east"],
        "north": ext["north"],
        "width_px": ext["width_px"],
        "height_px": ext["height_px"],
        "width_m": ext["width_m"],
        "height_m": ext["height_m"],
        "message": LOCAL_FRAME_MESSAGE if local else None,
    }
    return info
