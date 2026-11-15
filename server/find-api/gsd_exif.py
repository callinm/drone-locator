"""Estimate ground sample distance from EXIF / DJI XMP, or from explicit camera hints.

GSD (m/px) = altitude_m * sensor_width_m / (focal_length_m * image_width_px)

When only a 35 mm-equivalent focal length is known, the 35 mm frame width
(36 mm) stands in for the sensor width.
"""

from __future__ import annotations

import re
import struct
from typing import Any

# Typical active widths (mm) for common drone sensors, keyed by model substrings.
SENSOR_WIDTH_MM = {
    "avata 2": 9.6,  # 1/1.3"
    "avata2": 9.6,
    "avata": 7.6,  # original Avata is 1/1.7"-class
    "mini 4": 9.6,
    "mini 3": 9.7,
    "mini 2": 6.17,
    "mini se": 6.17,
    "air 3": 9.6,
    "air 2s": 13.05,  # 1"
    "air 2": 6.17,
    "mavic 3": 17.3,  # 4/3"
    "mavic 2 pro": 13.2,
    "mavic 2": 6.17,
    "mavic air": 6.17,
    "phantom 4 pro": 13.2,
    "phantom 4": 6.17,
    "fc4370": 9.6,  # some Avata 2 model codes
}

FRAME_35MM_WIDTH_MM = 36.0


def estimate_gsd_m(
    *,
    altitude_m: float | None,
    image_width_px: int | None,
    focal_length_mm: float | None = None,
    sensor_width_mm: float | None = None,
    focal_length_35mm: float | None = None,
) -> float | None:
    """Return metres per pixel, or None if the inputs are not enough."""
    if not altitude_m or altitude_m <= 0:
        return None
    if not image_width_px or image_width_px <= 1:
        return None
    sw = sensor_width_mm
    fl = focal_length_mm
    if (not sw or sw <= 0) and focal_length_35mm and focal_length_35mm > 0:
        # 35 mm equivalent: GSD = alt * 36 mm / (f_35 * width)
        sw = FRAME_35MM_WIDTH_MM
        fl = focal_length_35mm
    if not sw or not fl or sw <= 0 or fl <= 0:
        return None
    gsd = (float(altitude_m) * (float(sw) / 1000.0)) / ((float(fl) / 1000.0) * float(image_width_px))
    if gsd <= 0 or gsd > 5:  # more than 5 m/px is not a close drone still
        return None
    return float(gsd)


def sensor_width_for_model(make: str | None, model: str | None) -> float | None:
    text = f"{make or ''} {model or ''}".strip().lower()
    if not text:
        return None
    # Prefer the longest / most specific key that appears.
    hits = [(len(k), mm) for k, mm in SENSOR_WIDTH_MM.items() if k in text]
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, tuple) and len(value) == 2 and value[1]:
        return float(value[0]) / float(value[1])
    text = str(value).strip().replace("+", "")
    # DJI often writes "+12.34" or "12.3 mm"
    text = re.sub(r"[^0-9eE.+-].*$", "", text)
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse_xmp(data: bytes) -> dict:
    """Pull altitude, focal length, and camera model out of a DJI / standard XMP packet."""
    out: dict = {}
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        text = data.decode("latin1", errors="ignore")
    start = text.find("<x:xmpmeta")
    if start < 0:
        start = text.find("<?xpacket")
    if start < 0 and "RelativeAltitude" not in text and "FocalLength" not in text:
        return out
    packet = text[start : start + 200_000] if start >= 0 else text

    def grab(*names: str) -> float | None:
        for name in names:
            patterns = [
                rf"{name}\s*>\s*([-+0-9.eE]+)\s*<",
                rf"{name}\s*=\s*['\"]([-+0-9.eE]+)['\"]",
                rf"{name}\s*=\s*['\"]([-+])?([0-9.eE]+)['\"]",
            ]
            for pat in patterns:
                m = re.search(pat, packet)
                if m:
                    if m.lastindex and m.lastindex >= 2 and m.group(1) in {"+", "-"}:
                        return _to_float((m.group(1) or "") + m.group(2))
                    return _to_float(m.group(1))
        return None

    alt = grab(
        "drone-dji:RelativeAltitude",
        "drone-dji:AbsoluteAltitude",
        "RelativeAltitude",
        "AbsoluteAltitude",
        "exif:GPSAltitude",
        "GPSAltitude",
    )
    if alt is not None:
        out["altitude_m"] = abs(alt)
    fl = grab("exif:FocalLength", "tiff:FocalLength", "FocalLength")
    if fl:
        out["focal_length_mm"] = fl
    fl35 = grab("exif:FocalLengthIn35mmFilm", "FocalLengthIn35mmFilm", "FocalLength35mm")
    if fl35:
        out["focal_length_35mm"] = fl35
    cal = grab("drone-dji:CalibratedFocalLength")
    if cal:
        if cal < 200 and not out.get("focal_length_mm"):
            out["focal_length_mm"] = cal
        elif cal >= 200:
            out["focal_length_px"] = cal
    make = None
    model = None
    for name, key in (("tiff:Make", "make"), ("exif:Make", "make"), ("tiff:Model", "model"), ("exif:Model", "model"), ("drone-dji:Model", "model")):
        m = re.search(rf"{name}\s*=\s*['\"]([^'\"]+)['\"]", packet) or re.search(
            rf"{name}\s*>\s*([^<]+)<", packet
        )
        if m:
            out[key] = m.group(1).strip()
    w = grab("tiff:ImageWidth", "exif:PixelXDimension", "PixelXDimension")
    if w:
        out["image_width_px"] = int(w)
    pitch = grab("drone-dji:GimbalPitchDegree", "GimbalPitchDegree")
    if pitch is not None:
        out["gimbal_pitch_deg"] = pitch  # 0=horizon, negative=down
    return out


def _parse_exif_ifd(data: bytes) -> dict:
    """Minimal APP1 Exif parser for focal length, 35mm equiv, model, and altitude."""
    out: dict = {}
    idx = data.find(b"Exif\x00\x00")
    if idx < 0:
        return out
    tiff = data[idx + 6 :]
    if len(tiff) < 8:
        return out
    endian = "<" if tiff[:2] == b"II" else ">"
    if tiff[:2] not in (b"II", b"MM"):
        return out

    def u16(off: int) -> int:
        return struct.unpack(endian + "H", tiff[off : off + 2])[0]

    def u32(off: int) -> int:
        return struct.unpack(endian + "I", tiff[off : off + 4])[0]

    def rational(off: int) -> float | None:
        if off + 8 > len(tiff):
            return None
        n, d = struct.unpack(endian + "II", tiff[off : off + 8])
        return None if d == 0 else n / d

    def srational(off: int) -> float | None:
        if off + 8 > len(tiff):
            return None
        n, d = struct.unpack(endian + "ii", tiff[off : off + 8])
        return None if d == 0 else n / d

    def read_ifd(off: int) -> dict[int, Any]:
        tags: dict[int, Any] = {}
        if off <= 0 or off + 2 > len(tiff):
            return tags
        n = u16(off)
        p = off + 2
        for _ in range(n):
            if p + 12 > len(tiff):
                break
            tag, typ, count = struct.unpack(endian + "HHI", tiff[p : p + 8])
            val_off = p + 8
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1) * count
            if size <= 4:
                data_pos = val_off
            else:
                data_pos = u32(val_off)
            try:
                if typ == 2:
                    raw = tiff[data_pos : data_pos + count]
                    tags[tag] = raw.split(b"\x00", 1)[0].decode("latin1", errors="ignore")
                elif typ == 3 and count == 1:
                    tags[tag] = struct.unpack(endian + "H", tiff[data_pos : data_pos + 2])[0]
                elif typ == 4 and count == 1:
                    tags[tag] = u32(data_pos)
                elif typ == 5 and count >= 1:
                    tags[tag] = rational(data_pos)
                elif typ == 10 and count >= 1:
                    tags[tag] = srational(data_pos)
            except Exception:
                pass
            p += 12
        next_off = u32(p) if p + 4 <= len(tiff) else 0
        return tags, next_off

    try:
        first = u32(4)
        tags, _ = read_ifd(first)
    except Exception:
        return out
    # 0x8769 Exif IFD pointer, 0x8825 GPS
    exif_off = tags.get(0x8769)
    gps_off = tags.get(0x8825)
    if isinstance(exif_off, int):
        try:
            etags, _ = read_ifd(exif_off)
            tags.update(etags)
        except Exception:
            pass
    if isinstance(gps_off, int):
        try:
            gtags, _ = read_ifd(gps_off)
            if 6 in gtags:  # GPSAltitude
                alt = gtags[6]
                ref = gtags.get(5, 0)  # 1 = below sea level
                if isinstance(alt, (int, float)):
                    out["altitude_m"] = abs(float(alt)) * (-1 if ref == 1 else 1)
                    out["altitude_m"] = abs(out["altitude_m"])
        except Exception:
            pass
    if 0x010F in tags:
        out["make"] = str(tags[0x010F])
    if 0x0110 in tags:
        out["model"] = str(tags[0x0110])
    if 0xA002 in tags:
        try:
            out["image_width_px"] = int(tags[0xA002])
        except Exception:
            pass
    if 0x920A in tags:
        fl = _to_float(tags[0x920A])
        if fl:
            out["focal_length_mm"] = fl
    if 0xA405 in tags:
        fl35 = _to_float(tags[0xA405])
        if fl35:
            out["focal_length_35mm"] = fl35
    return out


def extract_camera_info(image_bytes: bytes, image_width_px: int | None = None) -> dict:
    info = parse_xmp(image_bytes)
    exif = _parse_exif_ifd(image_bytes)
    for k, v in exif.items():
        info.setdefault(k, v)
    if image_width_px and not info.get("image_width_px"):
        info["image_width_px"] = int(image_width_px)
    sw = sensor_width_for_model(info.get("make"), info.get("model"))
    if sw:
        info["sensor_width_mm"] = sw
    return info


def gsd_from_image_bytes(
    image_bytes: bytes,
    *,
    image_width_px: int | None = None,
    altitude_m: float | None = None,
) -> float | None:
    """Best-effort GSD (m/px) from EXIF/XMP, optionally overriding altitude."""
    info = extract_camera_info(image_bytes, image_width_px=image_width_px)
    alt = altitude_m if altitude_m and altitude_m > 0 else info.get("altitude_m")
    width = image_width_px or info.get("image_width_px")
    return estimate_gsd_m(
        altitude_m=_to_float(alt),
        image_width_px=int(width) if width else None,
        focal_length_mm=info.get("focal_length_mm"),
        sensor_width_mm=info.get("sensor_width_mm"),
        focal_length_35mm=info.get("focal_length_35mm"),
    )


def focal_px_from_info(info: dict, image_width_px: int | None = None) -> float | None:
    """Focal length in pixels from calibrated px, or from mm + sensor / 35mm equiv."""
    if not info:
        return None
    if info.get("focal_length_px"):
        try:
            v = float(info["focal_length_px"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    width = image_width_px or info.get("image_width_px")
    try:
        width = int(width) if width else None
    except (TypeError, ValueError):
        width = None
    if not width or width <= 0:
        return None
    fl = info.get("focal_length_mm")
    sw = info.get("sensor_width_mm")
    if fl and sw and float(sw) > 0:
        return float(fl) / float(sw) * width
    fl35 = info.get("focal_length_35mm")
    if fl35 and float(fl35) > 0:
        return float(fl35) / 36.0 * width
    return None
