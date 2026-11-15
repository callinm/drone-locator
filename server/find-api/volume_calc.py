"""DSM-minus-base-plane stockpile volume, ported from planner/js/volume.js."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

DEFAULT_DENSITY_T_PER_M3 = 1.6
DEFAULT_MIN_HEIGHT_M = 0.15


def plane_z(plane: dict, x: float, y: float) -> float:
    return plane["a"] * x + plane["b"] * y + plane["c"]


def fit_plane(points: list[dict]) -> dict[str, float]:
    pts = [p for p in points if all(np.isfinite(p.get(k, np.nan)) for k in ("x", "y", "z"))]
    n = len(pts)
    if n == 0:
        return {"a": 0.0, "b": 0.0, "c": 0.0}
    if n < 3:
        return {"a": 0.0, "b": 0.0, "c": float(sum(p["z"] for p in pts) / n)}
    sw = sx = sy = sz = 0.0
    sxx = sxy = syy = sxz = syz = 0.0
    for p in pts:
        x, y, z = p["x"], p["y"], p["z"]
        sw += 1
        sx += x
        sy += y
        sz += z
        sxx += x * x
        sxy += x * y
        syy += y * y
        sxz += x * z
        syz += y * z
    a = np.array([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, sw]], dtype=np.float64)
    b = np.array([sxz, syz, sz], dtype=np.float64)
    try:
        sol = np.linalg.solve(a, b)
        if not np.all(np.isfinite(sol)):
            raise np.linalg.LinAlgError
        return {"a": float(sol[0]), "b": float(sol[1]), "c": float(sol[2])}
    except np.linalg.LinAlgError:
        return {"a": 0.0, "b": 0.0, "c": float(sz / sw)}


def point_in_polygon(x: float, y: float, ring: list[dict]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]["x"], ring[i]["y"]
        xj, yj = ring[j]["x"], ring[j]["y"]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def pixel_area_m2(dsm: dict) -> float:
    return abs(float(dsm["dx"]) * float(dsm["dy"]))


def volume_from_mask(dsm: dict, mask: np.ndarray, plane: dict) -> dict[str, float]:
    z = dsm["z"]
    rows, cols = z.shape
    ox, oy = dsm["origin_x"], dsm["origin_y"]
    dx, dy = dsm["dx"], dsm["dy"]
    area = pixel_area_m2(dsm)
    fill = cut = used = 0.0
    for r in range(rows):
        for c in range(cols):
            if not mask[r, c]:
                continue
            zz = z[r, c]
            if not np.isfinite(zz):
                continue
            x = ox + (c + 0.5) * dx
            y = oy + (r + 0.5) * dy
            dh = zz - plane_z(plane, x, y)
            used += 1
            if dh >= 0:
                fill += dh * area
            else:
                cut += (-dh) * area
    return {
        "fill_m3": fill,
        "cut_m3": cut,
        "net_m3": fill - cut,
        "area_m2": used * area,
        "cells": used,
    }


def edge_mask_pixels(dsm: dict, mask: np.ndarray) -> list[dict]:
    z = dsm["z"]
    rows, cols = z.shape
    ox, oy = dsm["origin_x"], dsm["origin_y"]
    dx, dy = dsm["dx"], dsm["dy"]
    pts = []
    for r in range(rows):
        for c in range(cols):
            if not mask[r, c]:
                continue
            edge = (
                r == 0
                or c == 0
                or r == rows - 1
                or c == cols - 1
                or not mask[r - 1, c]
                or not mask[r + 1, c]
                or not mask[r, c - 1]
                or not mask[r, c + 1]
            )
            if not edge:
                continue
            zz = z[r, c]
            if not np.isfinite(zz):
                continue
            pts.append({"x": ox + (c + 0.5) * dx, "y": oy + (r + 0.5) * dy, "z": float(zz)})
    return pts


def fit_base_plane_from_mask(dsm: dict, mask: np.ndarray) -> dict[str, float]:
    pts = edge_mask_pixels(dsm, mask)
    return fit_plane(pts)


def mask_from_polygon(dsm: dict, ring: list[dict]) -> np.ndarray:
    z = dsm["z"]
    rows, cols = z.shape
    ox, oy = dsm["origin_x"], dsm["origin_y"]
    dx, dy = dsm["dx"], dsm["dy"]
    mask = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        y = oy + (r + 0.5) * dy
        for c in range(cols):
            x = ox + (c + 0.5) * dx
            if point_in_polygon(x, y, ring):
                mask[r, c] = True
    return mask


def measure_polygon(dsm: dict, ring: list[dict], density_t_per_m3: float = DEFAULT_DENSITY_T_PER_M3) -> dict[str, Any]:
    mask = mask_from_polygon(dsm, ring)
    plane = fit_base_plane_from_mask(dsm, mask)
    vol = volume_from_mask(dsm, mask, plane)
    density = float(density_t_per_m3) if density_t_per_m3 and density_t_per_m3 > 0 else DEFAULT_DENSITY_T_PER_M3
    vol["density_t_per_m3"] = density
    vol["tons"] = vol["fill_m3"] * density
    vol["plane"] = plane
    return vol


def make_grid_dsm(width: int, height: int, dx: float, dy: float, origin_x: float = 0.0, origin_y: float = 0.0) -> dict:
    return {
        "z": np.zeros((height, width), dtype=np.float64),
        "dx": dx,
        "dy": dy,
        "origin_x": origin_x,
        "origin_y": origin_y,
    }


def add_cone(dsm: dict, cx: float, cy: float, radius: float, height: float, plane: dict | None = None) -> None:
    plane = plane or {"a": 0.0, "b": 0.0, "c": 0.0}
    z = dsm["z"]
    rows, cols = z.shape
    ox, oy = dsm["origin_x"], dsm["origin_y"]
    dx, dy = dsm["dx"], dsm["dy"]
    for r in range(rows):
        y = oy + (r + 0.5) * dy
        for c in range(cols):
            x = ox + (c + 0.5) * dx
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist <= radius:
                h = height * (1.0 - dist / radius)
                z[r, c] = plane_z(plane, x, y) + h
            elif not np.isfinite(z[r, c]) or z[r, c] == 0:
                # leave existing values; initialize ground if still zero and outside
                if z[r, c] == 0:
                    z[r, c] = plane_z(plane, x, y)


def load_dsm_geotiff(data: bytes) -> dict:
    """Load a single-band GeoTIFF DSM into the grid used by measure_polygon."""
    import io
    import tifffile

    with tifffile.TiffFile(io.BytesIO(data)) as tif:
        page = tif.pages[0]
        arr = page.asarray()
        if arr.ndim == 3:
            arr = arr[:, :, 0]
        arr = np.array(arr, dtype=np.float64)
        # Common no-data sentinels
        arr[arr < -1e20] = np.nan
        arr[arr > 1e20] = np.nan
        dx = dy = 1.0
        origin_x = 0.0
        origin_y = 0.0
        if page.geotiff_tags or getattr(page, "get_transform", None):
            try:
                model = page.tags.get("ModelPixelScaleTag")
                tie = page.tags.get("ModelTiepointTag")
                if model is not None and tie is not None:
                    scale = model.value
                    tie_v = tie.value
                    dx = float(scale[0])
                    dy = -abs(float(scale[1]))
                    # tie: i, j, k, x, y, z
                    origin_x = float(tie_v[3])
                    origin_y = float(tie_v[4])
            except Exception:
                pass
        crs = "unknown"
        try:
            from georef import _parse_geokeys
            crs = _parse_geokeys(page.tags) or "unknown"
        except Exception:
            crs = "unknown"
        return {
            "z": arr,
            "dx": dx,
            "dy": dy,
            "origin_x": origin_x,
            "origin_y": origin_y,
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "crs": crs,
        }
