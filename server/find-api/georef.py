"""Read a simple affine from a GeoTIFF or from explicit WGS84 bounds."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class GeoRef:
    # Maps pixel (col, row) of the *original* mosaic to map x/y.
    # x = origin_x + col * pixel_w
    # y = origin_y + row * pixel_h   (pixel_h is usually negative for north-up)
    origin_x: float
    origin_y: float
    pixel_w: float
    pixel_h: float
    crs: str = "unknown"  # "EPSG:4326", "EPSG:32616", "bounds", ...
    width: int = 0
    height: int = 0

    def pixel_to_xy(self, col: float, row: float) -> tuple[float, float]:
        return (self.origin_x + col * self.pixel_w, self.origin_y + row * self.pixel_h)

    def pixel_to_lonlat(self, col: float, row: float) -> tuple[float | None, float | None]:
        x, y = self.pixel_to_xy(col, row)
        if self.crs in {"EPSG:4326", "geographic", "bounds"}:
            return float(x), float(y)  # lon, lat
        if self.crs.startswith("EPSG:326") or self.crs.startswith("EPSG:327"):
            try:
                epsg = int(self.crs.split(":")[1])
            except ValueError:
                return None, None
            lat, lon = utm_to_latlon(x, y, epsg)
            return lon, lat
        return None, None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GeoRef":
        return cls(**d)

    @classmethod
    def from_bounds(cls, west: float, south: float, east: float, north: float, width: int, height: int) -> "GeoRef":
        return cls(
            origin_x=west,
            origin_y=north,
            pixel_w=(east - west) / max(width, 1),
            pixel_h=(south - north) / max(height, 1),
            crs="bounds",
            width=width,
            height=height,
        )


def utm_to_latlon(easting: float, northing: float, epsg: int) -> tuple[float, float]:
    north = epsg < 32700
    zone = epsg - 32600 if north else epsg - 32700
    a = 6378137.0
    f = 1 / 298.257223563
    k0 = 0.9996
    e = np.sqrt(f * (2 - f))
    e1 = (1 - np.sqrt(1 - e * e)) / (1 + np.sqrt(1 - e * e))
    x = easting - 500000.0
    y = northing if north else northing - 10_000_000.0
    lon0 = (zone - 1) * 6 - 180 + 3
    M = y / k0
    mu = M / (a * (1 - e * e / 4 - 3 * e**4 / 64 - 5 * e**6 / 256))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * np.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * np.sin(4 * mu)
        + (151 * e1**3 / 96) * np.sin(6 * mu)
    )
    N1 = a / np.sqrt(1 - e * e * np.sin(phi1) ** 2)
    T1 = np.tan(phi1) ** 2
    C1 = (e * e / (1 - e * e)) * np.cos(phi1) ** 2
    R1 = (a * (1 - e * e)) / (1 - e * e * np.sin(phi1) ** 2) ** 1.5
    D = x / (N1 * k0)
    lat = phi1 - (N1 * np.tan(phi1) / R1) * (
        D * D / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * (e * e / (1 - e * e))) * D**4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * (e * e / (1 - e * e)) - 3 * C1 * C1) * D**6 / 720
    )
    lon = np.radians(lon0) + (
        D
        - (1 + 2 * T1 + C1) * D**3 / 6
        + (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * (e * e / (1 - e * e)) + 24 * T1 * T1) * D**5 / 120
    ) / np.cos(phi1)
    return float(np.degrees(lat)), float(np.degrees(lon))


def read_geotiff_ref(path: Path, width: int, height: int) -> GeoRef | None:
    try:
        import tifffile
    except ImportError:
        return None
    try:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            tags = page.tags
            scale = _tag_values(tags, 33550)  # ModelPixelScale
            tie = _tag_values(tags, 33922)  # ModelTiepoint
            trans = _tag_values(tags, 34264)  # ModelTransformation
            geokeys = _parse_geokeys(tags)
            crs = geokeys or "unknown"
            if trans and len(trans) >= 16:
                # [a b c d e f g h i j k l m n o p] affine 4x4 row-major
                # x = a*col + b*row + d; y = e*col + f*row + h  (common)
                a, b, _c, d, e, f, _g, h = trans[:8]
                # We only support origin+scale (no rotation) in this lightweight reader.
                if abs(b) < 1e-12 and abs(e) < 1e-12:
                    return GeoRef(float(d), float(h), float(a), float(f), crs, width, height)
            if scale and tie and len(scale) >= 2 and len(tie) >= 6:
                # tie: i, j, k, x, y, z  (pixel i,j maps to x,y)
                sx, sy = float(scale[0]), float(scale[1])
                i, j, _k, x, y, _z = (float(v) for v in tie[:6])
                origin_x = x - i * sx
                origin_y = y + j * sy  # north-up: y decreases as row increases
                return GeoRef(origin_x, origin_y, sx, -sy, crs, width, height)
    except Exception:
        return None
    return None


def _tag_values(tags, code):
    try:
        tag = tags.get(code)
        if tag is None:
            return None
        val = tag.value
        if isinstance(val, (bytes, bytearray)):
            return None
        if np.isscalar(val):
            return [float(val)]
        return [float(v) for v in val]
    except Exception:
        return None


def _parse_geokeys(tags) -> str:
    """Best-effort EPSG from GeoKeyDirectoryTag (34735)."""
    try:
        directory = tags.get(34735)
        doubles = tags.get(34736)
        if directory is None:
            return "unknown"
        data = list(directory.value)
        if len(data) < 4:
            return "unknown"
        n = int(data[3])
        keys = {}
        for i in range(n):
            base = 4 + i * 4
            if base + 3 >= len(data):
                break
            key, loc, count, val = (int(data[base + k]) for k in range(4))
            keys[key] = (loc, count, val)
        # 3072 ProjectedCSTypeGeoKey, 2048 GeographicTypeGeoKey
        for code in (3072, 2048):
            if code in keys:
                loc, _c, val = keys[code]
                if loc == 0 and val > 0:
                    return f"EPSG:{val}"
        void = doubles  # kept so the name is used
        _ = void
    except Exception:
        return "unknown"
    return "unknown"


def write_geotiff_4326(path: Path, array: np.ndarray, west: float, north: float, pixel_w: float, pixel_h: float) -> None:
    """Write a north-up WGS84 GeoTIFF (pixel_h should be positive ground size)."""
    import tifffile

    path = Path(path)
    # GeoKeyDirectory: version 1,0,1, key count 1, GeographicTypeGeoKey=4326
    geokey = np.array([1, 1, 0, 1, 2048, 0, 1, 4326], dtype=np.uint16)
    tifffile.imwrite(
        path,
        array,
        photometric="minisblack" if array.ndim == 2 else "rgb",
        extratags=[
            (33550, 12, 3, [float(pixel_w), float(pixel_h), 0.0], True),  # DOUBLE
            (33922, 12, 6, [0.0, 0.0, 0.0, float(west), float(north), 0.0], True),
            (34735, 3, len(geokey), geokey, True),  # SHORT
        ],
    )
