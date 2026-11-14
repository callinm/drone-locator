/**
 * Minimal JPEG EXIF GPS reader for the bonus photo-preview mosaic.
 * Only GPSLatitude / GPSLongitude (and optional altitude, direction) are used.
 */
export async function readPhotoGps(file) {
  const buf = await file.arrayBuffer();
  const gps = parseJpegGps(new DataView(buf));
  if (!gps) return null;
  return {
    name: file.name,
    lat: gps.lat,
    lon: gps.lon,
    altM: gps.altM,
    file,
  };
}

export function parseJpegGps(view) {
  if (view.byteLength < 4 || view.getUint16(0, false) !== 0xffd8) return null;
  let offset = 2;
  const len = view.byteLength;
  while (offset + 4 <= len) {
    if (view.getUint8(offset) !== 0xff) break;
    const marker = view.getUint8(offset + 1);
    const size = view.getUint16(offset + 2, false);
    if (marker === 0xe1 && size >= 8) {
      const start = offset + 4;
      if (ascii(view, start, 4) === "Exif") {
        const gps = parseExifGps(view, start + 6);
        if (gps) return gps;
      }
    }
    if (marker === 0xda) break; // SOS
    offset += 2 + size;
  }
  return null;
}

function ascii(view, start, n) {
  let s = "";
  for (let i = 0; i < n; i++) s += String.fromCharCode(view.getUint8(start + i));
  return s;
}

function parseExifGps(view, tiffStart) {
  if (tiffStart + 8 > view.byteLength) return null;
  const le = view.getUint16(tiffStart, false) === 0x4949;
  if (!le && view.getUint16(tiffStart, false) !== 0x4d4d) return null;
  if (view.getUint16(tiffStart + 2, le) !== 42) return null;
  const ifd0 = tiffStart + view.getUint32(tiffStart + 4, le);
  const gpsOffset = findTagOffset(view, ifd0, tiffStart, le, 0x8825);
  if (gpsOffset == null) return null;
  const gpsIfd = tiffStart + gpsOffset;
  const lat = readGpsCoord(view, gpsIfd, tiffStart, le, 0x0002, 0x0001);
  const lon = readGpsCoord(view, gpsIfd, tiffStart, le, 0x0004, 0x0003);
  if (lat == null || lon == null) return null;
  const alt = readGpsAltitude(view, gpsIfd, tiffStart, le);
  return { lat, lon, altM: alt };
}

function findTagOffset(view, ifd, tiffStart, le, tag) {
  if (ifd + 2 > view.byteLength) return null;
  const count = view.getUint16(ifd, le);
  for (let i = 0; i < count; i++) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > view.byteLength) return null;
    if (view.getUint16(entry, le) === tag) {
      const type = view.getUint16(entry + 2, le);
      const value = view.getUint32(entry + 8, le);
      // GPS IFD pointer is typically type LONG (4).
      return type === 4 ? value : value;
    }
  }
  return null;
}

function findTag(view, ifd, tiffStart, le, tag) {
  if (ifd + 2 > view.byteLength) return null;
  const count = view.getUint16(ifd, le);
  for (let i = 0; i < count; i++) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > view.byteLength) return null;
    if (view.getUint16(entry, le) === tag) {
      return {
        type: view.getUint16(entry + 2, le),
        count: view.getUint32(entry + 4, le),
        valueOffset: entry + 8,
        value: view.getUint32(entry + 8, le),
      };
    }
  }
  return null;
}

function readRational(view, offset, le) {
  const num = view.getUint32(offset, le);
  const den = view.getUint32(offset + 4, le) || 1;
  return num / den;
}

function readGpsCoord(view, gpsIfd, tiffStart, le, tag, refTag) {
  const coord = findTag(view, gpsIfd, tiffStart, le, tag);
  const ref = findTag(view, gpsIfd, tiffStart, le, refTag);
  if (!coord || coord.count < 3) return null;
  const ptr = coord.count * 8 > 4 ? tiffStart + coord.value : coord.valueOffset;
  if (ptr + 24 > view.byteLength) return null;
  const deg = readRational(view, ptr, le);
  const min = readRational(view, ptr + 8, le);
  const sec = readRational(view, ptr + 16, le);
  let dec = deg + min / 60 + sec / 3600;
  if (ref) {
    const ch = String.fromCharCode(view.getUint8(ref.valueOffset));
    if (ch === "S" || ch === "W") dec = -dec;
  }
  return dec;
}

function readGpsAltitude(view, gpsIfd, tiffStart, le) {
  const tag = findTag(view, gpsIfd, tiffStart, le, 0x0006);
  if (!tag) return null;
  const ptr = tag.count * 8 > 4 ? tiffStart + tag.value : tag.valueOffset;
  if (ptr + 8 > view.byteLength) return null;
  let alt = readRational(view, ptr, le);
  const ref = findTag(view, gpsIfd, tiffStart, le, 0x0005);
  if (ref && view.getUint8(ref.valueOffset) === 1) alt = -alt;
  return alt;
}

/**
 * Full still-photo metadata: EXIF GPS plus DJI XMP (RelativeAltitude,
 * gimbal pitch/yaw/roll, FlightYawDegree) scanned from APP1/APP13 segments.
 */
export async function readPhotoMeta(file) {
  const buf = await file.arrayBuffer();
  const meta = parseJpegMeta(new DataView(buf));
  return {
    name: file.name,
    file,
    ...meta,
  };
}

export function parseJpegMeta(view) {
  const gps = parseJpegGps(view);
  const xmp = parseDjiXmp(view);
  const size = jpegSize(view);
  return {
    lat: gps?.lat ?? null,
    lon: gps?.lon ?? null,
    altM: gps?.altM ?? null,
    relativeAltitudeM: xmp.relativeAltitudeM,
    gimbalPitchDeg: xmp.gimbalPitchDeg,
    gimbalYawDeg: xmp.gimbalYawDeg,
    gimbalRollDeg: xmp.gimbalRollDeg,
    flightYawDeg: xmp.flightYawDeg,
    imageWidth: size?.width ?? null,
    imageHeight: size?.height ?? null,
  };
}

export function parseDjiXmp(view) {
  const xml = extractXmpString(view);
  const out = {
    relativeAltitudeM: null,
    gimbalPitchDeg: null,
    gimbalYawDeg: null,
    gimbalRollDeg: null,
    flightYawDeg: null,
  };
  if (!xml) return out;
  const num = (re) => {
    const m = xml.match(re);
    if (!m) return null;
    const v = Number(m[1]);
    return Number.isFinite(v) ? v : null;
  };
  out.relativeAltitudeM = num(/RelativeAltitude\s*=\s*["']([+-]?\d+(?:\.\d+)?)["']/i);
  out.gimbalPitchDeg = num(/GimbalPitchDegree\s*=\s*["']([+-]?\d+(?:\.\d+)?)["']/i);
  out.gimbalYawDeg = num(/GimbalYawDegree\s*=\s*["']([+-]?\d+(?:\.\d+)?)["']/i);
  out.gimbalRollDeg = num(/GimbalRollDegree\s*=\s*["']([+-]?\d+(?:\.\d+)?)["']/i);
  out.flightYawDeg = num(/FlightYawDegree\s*=\s*["']([+-]?\d+(?:\.\d+)?)["']/i);
  return out;
}

function extractXmpString(view) {
  const chunks = [];
  // Walk JPEG markers and also do a raw scan for the drone-dji packet.
  if (view.byteLength >= 4 && view.getUint16(0, false) === 0xffd8) {
    let offset = 2;
    const len = view.byteLength;
    while (offset + 4 <= len) {
      if (view.getUint8(offset) !== 0xff) break;
      const marker = view.getUint8(offset + 1);
      if (marker === 0xda) break;
      const size = view.getUint16(offset + 2, false);
      if ((marker === 0xe1 || marker === 0xed) && size >= 8) {
        const start = offset + 4;
        const end = Math.min(offset + 2 + size, len);
        chunks.push(asciiLoose(view, start, end - start));
      }
      if (size < 2) break;
      offset += 2 + size;
    }
  }
  let blob = chunks.join("\n");
  if (!/RelativeAltitude|GimbalPitchDegree/.test(blob)) {
    blob += "\n" + asciiLoose(view, 0, Math.min(view.byteLength, 256000));
  }
  return blob;
}

function asciiLoose(view, start, n) {
  let s = "";
  const end = Math.min(view.byteLength, start + n);
  for (let i = start; i < end; i++) {
    const c = view.getUint8(i);
    if (c === 0) s += " ";
    else if (c >= 32 && c < 127) s += String.fromCharCode(c);
  }
  return s;
}

function jpegSize(view) {
  if (view.byteLength < 4 || view.getUint16(0, false) !== 0xffd8) return null;
  let offset = 2;
  const len = view.byteLength;
  while (offset + 8 <= len) {
    if (view.getUint8(offset) !== 0xff) break;
    const marker = view.getUint8(offset + 1);
    const size = view.getUint16(offset + 2, false);
    // SOF0 / SOF2
    if (marker === 0xc0 || marker === 0xc2) {
      const height = view.getUint16(offset + 5, false);
      const width = view.getUint16(offset + 7, false);
      return { width, height };
    }
    if (marker === 0xda) break;
    if (size < 2) break;
    offset += 2 + size;
  }
  return null;
}
