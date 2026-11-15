import {
  MAVIC_AIR_2,
  FAA_MAX_ALTITUDE_M,
  DEFAULT_ALTITUDE_M,
  DEFAULT_FRONT_OVERLAP,
  DEFAULT_SIDE_OVERLAP,
  DEFAULT_SPEED_MPS,
  computeGsdCm,
  computeLineSpacingM,
  computeTriggerDistanceM,
  capSpeedMps,
  clampAltitudeM,
  footprintWidthM,
  footprintHeightM,
} from "./camera.js?v=20261114dlrr2";
import { batteryModel, usableMissionMin } from "./battery.js?v=20261114dlrr2";
import {
  originOf,
  toLocal,
  toLatLon,
  rotateToGrid,
  rotateFromGrid,
  uniqueVertices,
  scanlineSegments,
  haversineM,
  bearingDeg,
  polygonAreaM2,
  wrapDeg,
} from "./geo.js?v=20261114dlrr2";

export function defaultSettings(overrides = {}) {
  return {
    altitudeM: DEFAULT_ALTITUDE_M,
    frontOverlap: DEFAULT_FRONT_OVERLAP,
    sideOverlap: DEFAULT_SIDE_OVERLAP,
    speedMps: DEFAULT_SPEED_MPS,
    gridAngleDeg: 0,
    crosshatch: false,
    headingMode: "along-line", // or "fixed"
    fixedHeadingDeg: 0,
    gimbalPitchDeg: -90,
    rthAltitudeM: null, // defaults to mission altitude
    rthOnAbort: true,
    rthOnSignalLoss: true,
    lowBatteryPercent: 30,
    name: "Untitled survey",
    ...overrides,
  };
}

export function computeCoverage(settings, camera = MAVIC_AIR_2) {
  const altitudeM = clampAltitudeM(settings.altitudeM);
  const frontOverlap = settings.frontOverlap ?? DEFAULT_FRONT_OVERLAP;
  const sideOverlap = settings.sideOverlap ?? DEFAULT_SIDE_OVERLAP;
  const lineSpacingM = computeLineSpacingM(altitudeM, sideOverlap, camera);
  const triggerDistanceM = computeTriggerDistanceM(altitudeM, frontOverlap, camera);
  const requestedSpeed = settings.speedMps ?? DEFAULT_SPEED_MPS;
  const speedMps = capSpeedMps(
    requestedSpeed,
    triggerDistanceM,
    camera.minPhotoIntervalSec,
    camera.maxSpeedNormalMps
  );
  return {
    altitudeM,
    altitudeClamped: altitudeM !== settings.altitudeM,
    aboveFaaLimit: (settings.altitudeM ?? 0) > FAA_MAX_ALTITUDE_M,
    gsdCmPerPx: computeGsdCm(altitudeM, camera),
    footprintWidthM: footprintWidthM(altitudeM, camera),
    footprintHeightM: footprintHeightM(altitudeM, camera),
    lineSpacingM,
    triggerDistanceM,
    requestedSpeedMps: requestedSpeed,
    speedMps,
    speedCapped: speedMps < requestedSpeed - 1e-9,
  };
}

/**
 * Build a lawnmower (and optional crosshatch) survey over a polygon.
 * polygon: [{lat, lon}, ...] (need not be closed)
 */
export function generateGrid(polygon, settingsInput = {}, camera = MAVIC_AIR_2) {
  const settings = defaultSettings(settingsInput);
  const coverage = computeCoverage(settings, camera);
  const vertices = uniqueVertices(polygon || []);
  if (vertices.length < 3) {
    return emptyResult(settings, coverage, "Draw a field with at least 3 corners.");
  }

  const origin = originOf(vertices);
  const passes = [];
  passes.push(
    buildPass(vertices, origin, settings.gridAngleDeg, coverage, 0)
  );
  if (settings.crosshatch) {
    passes.push(
      buildPass(vertices, origin, settings.gridAngleDeg + 90, coverage, passes[0].lines.length)
    );
  }

  const lines = passes.flatMap((p) => p.lines);
  if (!lines.length) {
    return emptyResult(settings, coverage, "The field is too small for a flight line at this altitude and overlap.");
  }

  const waypoints = [];
  const photos = [];
  let pathLengthM = 0;
  let surveyLengthM = 0;
  let transitLengthM = 0;
  let lastPoint = null;
  let turnCount = 0;

  const headingForLine = (line) =>
    settings.headingMode === "fixed"
      ? wrapDeg(settings.fixedHeadingDeg || 0)
      : line.headingDeg;

  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    const heading = headingForLine(line);
    const start = line.start;
    const end = line.end;

    if (lastPoint) {
      const d = hyp(lastPoint, start);
      if (d > 0.05) {
        pathLengthM += d;
        transitLengthM += d;
        turnCount += 1;
        waypoints.push(
          makeWp(start, coverage.altitudeM, heading, li, "line-start", false)
        );
      }
    } else {
      waypoints.push(
        makeWp(start, coverage.altitudeM, heading, li, "line-start", false)
      );
    }

    const linePhotos = placePhotos(start, end, coverage.triggerDistanceM);
    for (const ph of linePhotos) {
      photos.push({
        lat: ph.lat,
        lon: ph.lon,
        altM: coverage.altitudeM,
        headingDeg: heading,
        lineIndex: li,
      });
    }

    const length = hyp(start, end);
    pathLengthM += length;
    surveyLengthM += length;
    waypoints.push(makeWp(end, coverage.altitudeM, heading, li, "line-end", true));
    lastPoint = end;
  }

  const estimated = estimateFlightTime({
    pathLengthM,
    surveyLengthM,
    transitLengthM,
    turnCount,
    speedMps: coverage.speedMps,
    altitudeM: coverage.altitudeM,
    camera,
    home: waypoints[0],
    last: waypoints[waypoints.length - 1],
  });

  const result = {
    ok: true,
    error: null,
    settings: { ...settings, altitudeM: coverage.altitudeM, speedMps: coverage.speedMps },
    coverage,
    origin,
    lines,
    waypoints,
    photos,
    stats: {
      ...coverage,
      lineCount: lines.length,
      photoCount: photos.length,
      pathLengthM,
      surveyLengthM,
      transitLengthM,
      areaM2: polygonAreaM2(vertices, origin),
      estimatedFlightTimeSec: estimated.totalSec,
      estimatedBreakdown: estimated,
      batteryHintMin: camera.usableFlightTimeMin,
    },
    polygon: vertices,
    batteries: null,
    segments: [],
  };
  const batteries = splitIntoSegments(result, camera);
  result.batteries = batteries.model;
  result.segments = batteries.segments;
  result.stats.batteryCount = batteries.model.count;
  result.stats.usableMissionMin = batteries.model.usableMissionMin;
  result.stats.segmentOverBudget = batteries.segments.some((s) => s.overBudget);
  return result;
}

function buildPass(vertices, origin, gridAngleDeg, coverage, lineIndexOffset) {
  const local = vertices.map((p) => {
    const xy = toLocal(p.lat, p.lon, origin);
    return rotateToGrid(xy.x, xy.y, gridAngleDeg);
  });

  let minY = Infinity, maxY = -Infinity;
  for (const p of local) {
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }

  const spacing = coverage.lineSpacingM;
  const span = maxY - minY;
  const ys = [];
  if (span <= spacing) {
    ys.push((minY + maxY) / 2);
  } else {
    const first = minY + spacing / 2;
    const last = maxY - spacing / 2;
    const count = Math.max(1, Math.floor((last - first) / spacing + 0.5) + 1);
    for (let i = 0; i < count; i++) {
      const y = first + i * spacing;
      if (y <= last + spacing * 0.05) ys.push(Math.min(y, last));
    }
    if (!ys.length) ys.push((minY + maxY) / 2);
    // Dedup in case of rounding
    const dedup = [];
    for (const y of ys) {
      if (!dedup.length || Math.abs(y - dedup[dedup.length - 1]) > spacing * 0.1) {
        dedup.push(y);
      }
    }
    ys.length = 0;
    ys.push(...dedup);
  }

  const lines = [];
  let direction = 1;
  for (const y of ys) {
    const segs = scanlineSegments(local, y);
    const ordered = direction === 1 ? segs : segs.slice().reverse();
    for (const seg of ordered) {
      const xStart = direction === 1 ? seg.x0 : seg.x1;
      const xEnd = direction === 1 ? seg.x1 : seg.x0;
      const start = fromGrid(xStart, y, gridAngleDeg, origin);
      const end = fromGrid(xEnd, y, gridAngleDeg, origin);
      if (hyp(start, end) < 0.5) continue;
      lines.push({
        index: lineIndexOffset + lines.length,
        start,
        end,
        headingDeg: bearingDeg(start, end),
        lengthM: hyp(start, end),
        gridAngleDeg,
      });
    }
    direction *= -1;
  }
  return { lines };
}

function fromGrid(x, y, gridAngleDeg, origin) {
  const xy = rotateFromGrid(x, y, gridAngleDeg);
  return toLatLon(xy.x, xy.y, origin);
}

function hyp(a, b) {
  // Local straight-line is fine; use haversine for consistency at export scale.
  return haversineM(a, b);
}

/**
 * Photos at the start of a line, every trigger distance, and at the end
 * unless the last planned trigger is already on the end point.
 */
export function placePhotos(start, end, triggerDistanceM) {
  const length = haversineM(start, end);
  if (length < 0.01) return [{ ...start }];
  const heading = bearingDeg(start, end);
  const photos = [{ lat: start.lat, lon: start.lon }];
  if (triggerDistanceM <= 0) {
    photos.push({ lat: end.lat, lon: end.lon });
    return photos;
  }
  const steps = Math.floor(length / triggerDistanceM);
  for (let i = 1; i <= steps; i++) {
    const d = i * triggerDistanceM;
    if (length - d < 0.05) break;
    const p = pointAlong(start, end, heading, d, length);
    photos.push(p);
  }
  const last = photos[photos.length - 1];
  if (haversineM(last, end) > Math.min(0.3 * triggerDistanceM, 1.0)) {
    photos.push({ lat: end.lat, lon: end.lon });
  }
  return photos;
}

function pointAlong(start, end, heading, distanceM, totalLength) {
  const t = distanceM / totalLength;
  // Linear interpolation in lat/lon is acceptable for short survey legs;
  // blend with a spherical destination for better spacing.
  const dest = {
    lat: start.lat + (end.lat - start.lat) * t,
    lon: start.lon + (end.lon - start.lon) * t,
  };
  return dest;
}

export function estimateFlightTime({
  pathLengthM,
  surveyLengthM,
  transitLengthM,
  turnCount,
  speedMps,
  altitudeM,
  camera = MAVIC_AIR_2,
  home,
  last,
}) {
  const cruise = Math.max(0.5, speedMps);
  const transitSpeed = Math.min(cruise * 1.15, camera.maxSpeedNormalMps * 0.7);
  const surveySec = (surveyLengthM || 0) / cruise;
  const transitSec = (transitLengthM || 0) / Math.max(0.5, transitSpeed);
  const turnSec = (turnCount || 0) * (1.5 + 180 / camera.yawRateDegPerSec);
  const climbSec = (altitudeM || 0) / Math.max(1, camera.maxAscentMps * 0.75) + 4;
  const rthDist = home && last ? haversineM(home, last) : 0;
  const rthSec = rthDist / 8 + (altitudeM || 0) / Math.max(1, camera.maxDescentMps) + 8;
  const totalSec = climbSec + surveySec + transitSec + turnSec + rthSec;
  return {
    climbSec,
    surveySec,
    transitSec,
    turnSec,
    rthSec,
    totalSec,
  };
}

function makeWp(pt, altM, headingDeg, lineIndex, type, isLineEnd) {
  return {
    lat: pt.lat,
    lon: pt.lon,
    altM,
    headingDeg,
    lineIndex,
    type,
    isLineEnd,
  };
}


/**
 * Pack flight lines into per-battery segments. Each segment's time includes
 * climb, survey, turns, transit, and RTH back to the home/start point, and
 * must fit in the 19.6 min usable budget when possible. A single line that
 * cannot fit is still placed alone and flagged overBudget.
 */
export function splitIntoSegments(plan, camera = MAVIC_AIR_2) {
  const model = batteryModel(camera);
  const budgetSec = model.usableMissionSec;
  const lines = plan.lines || [];
  const waypoints = plan.waypoints || [];
  const photos = plan.photos || [];
  const home = waypoints[0] || lines[0]?.start || null;
  const speedMps = plan.settings?.speedMps ?? plan.coverage?.speedMps;
  const altitudeM = plan.settings?.altitudeM ?? plan.coverage?.altitudeM;
  const segments = [];
  if (!lines.length) {
    return { model: { ...model, count: 0 }, segments };
  }

  const timeFor = (from, to) => {
    const slice = lines.slice(from, to + 1);
    let survey = 0, transit = 0, turns = 0;
    let last = home;
    for (const line of slice) {
      if (last) {
        const d = haversineM(last, line.start);
        if (d > 0.05) {
          transit += d;
          turns += 1;
        }
      }
      survey += line.lengthM || haversineM(line.start, line.end);
      last = line.end;
    }
    return estimateFlightTime({
      pathLengthM: survey + transit,
      surveyLengthM: survey,
      transitLengthM: transit,
      turnCount: turns,
      speedMps,
      altitudeM,
      camera,
      home,
      last,
    });
  };

  const makeSeg = (from, to, estimated) => {
    const lineIdx = [];
    for (let i = from; i <= to; i++) lineIdx.push(lines[i].index ?? i);
    const wps = waypoints
      .map((w, i) => ({ w, i }))
      .filter(({ w }) => lineIdx.includes(w.lineIndex));
    const ph = photos
      .map((p, i) => ({ p, i }))
      .filter(({ p }) => lineIdx.includes(p.lineIndex));
    return {
      batteryIndex: segments.length,
      lineStart: lineIdx[0],
      lineEnd: lineIdx[lineIdx.length - 1],
      lineCount: to - from + 1,
      waypointStart: wps.length ? wps[0].i : 0,
      waypointEnd: wps.length ? wps[wps.length - 1].i : 0,
      photoStart: ph.length ? ph[0].i : 0,
      photoEnd: ph.length ? ph[ph.length - 1].i : 0,
      pathLengthM: (estimated.surveySec + estimated.transitSec) > 0
        ? (estimated.surveySec * speedMps + estimated.transitSec * Math.min(speedMps * 1.15, camera.maxSpeedNormalMps * 0.7))
        : 0,
      estimatedFlightTimeSec: estimated.totalSec,
      overBudget: estimated.totalSec > budgetSec + 1e-6,
    };
  };

  let start = 0;
  for (let end = 0; end < lines.length; end++) {
    const next = timeFor(start, end);
    if (end > start && next.totalSec > budgetSec) {
      const prev = timeFor(start, end - 1);
      segments.push(makeSeg(start, end - 1, prev));
      start = end;
    }
  }
  segments.push(makeSeg(start, lines.length - 1, timeFor(start, lines.length - 1)));
  return { model: { ...model, count: segments.length }, segments };
}

function emptyResult(settings, coverage, error) {
  return {
    ok: false,
    error,
    settings,
    coverage,
    origin: null,
    lines: [],
    waypoints: [],
    photos: [],
    stats: {
      ...coverage,
      lineCount: 0,
      photoCount: 0,
      pathLengthM: 0,
      surveyLengthM: 0,
      transitLengthM: 0,
      areaM2: 0,
      estimatedFlightTimeSec: 0,
    },
    polygon: [],
  };
}
