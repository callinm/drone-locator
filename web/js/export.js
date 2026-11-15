import { getCamera, MAVIC_AIR_2 } from "./camera.js?v=20261114dlrr2";
import { formatAltitudeFt } from "./units.js?v=20261114dlrr2";

export const MISSION_FORMAT = "drone-mapper-mission";
export const MISSION_VERSION = 1;

function cameraOf(plan, extra = {}) {
  return getCamera(plan.camera || extra.camera || (plan.settings && plan.settings.camera) || MAVIC_AIR_2);
}

function missionNotes(camera) {
  if (camera && camera.id === "phantom-4-pro") {
    return "Phantom 4 Pro plan. Fly it in Litchi with this CSV, or in the Drone Mapper Android app (MSDK v4 supports the Phantom 4 Pro). Keep the phone connected for the entire flight.";
  }
  return "Mavic Air 2 does not support DJI WaypointMission. The companion Android app flies this grid with Virtual Stick and distance-triggered photos. Keep the phone connected for the entire flight.";
}

export function buildMission(plan, extra = {}) {
  const settings = plan.settings || {};
  const coverage = plan.coverage || {};
  const stats = plan.stats || {};
  const camera = cameraOf(plan, extra);
  const rthAltitude = settings.rthAltitudeM ?? settings.altitudeM ?? coverage.altitudeM;
  return {
    format: MISSION_FORMAT,
    version: MISSION_VERSION,
    aircraft: camera.name || MAVIC_AIR_2.name,
    camera: {
      id: camera.id,
      name: camera.name,
      sensorWidthMm: camera.sensorWidthMm,
      sensorHeightMm: camera.sensorHeightMm,
      imageWidthPx: camera.imageWidthPx,
      imageHeightPx: camera.imageHeightPx,
      focalLengthMm: camera.focalLengthMm,
      shutter: camera.shutter,
    },
    createdAt: extra.createdAt || new Date().toISOString(),
    name: settings.name || extra.name || "Untitled survey",
    notes: missionNotes(camera),
    polygon: (plan.polygon || []).map((p) => ({ lat: p.lat, lon: p.lon })),
    kind: settings.kind || extra.kind || settings.preset || undefined,
    settings: {
      altitudeM: settings.altitudeM,
      frontOverlap: settings.frontOverlap,
      sideOverlap: settings.sideOverlap,
      speedMps: settings.speedMps,
      gridAngleDeg: settings.gridAngleDeg,
      crosshatch: !!settings.crosshatch,
      headingMode: settings.headingMode || "along-line",
      fixedHeadingDeg: settings.fixedHeadingDeg || 0,
      gimbalPitchDeg: settings.gimbalPitchDeg ?? -90,
      photoMode: "12MP",
      preset: settings.preset || extra.preset,
      kind: settings.kind || extra.kind || settings.preset,
    },
    computed: {
      gsdCmPerPx: stats.gsdCmPerPx,
      footprintWidthM: stats.footprintWidthM,
      footprintHeightM: stats.footprintHeightM,
      lineSpacingM: stats.lineSpacingM,
      triggerDistanceM: stats.triggerDistanceM,
      photoCount: stats.photoCount,
      lineCount: stats.lineCount,
      pathLengthM: stats.pathLengthM,
      surveyLengthM: stats.surveyLengthM,
      areaM2: stats.areaM2,
      estimatedFlightTimeSec: stats.estimatedFlightTimeSec,
      speedCapped: !!coverage.speedCapped,
      batteryCount: stats.batteryCount ?? (plan.batteries?.count ?? 0),
      usableMissionMin: stats.usableMissionMin ?? plan.batteries?.usableMissionMin,
    },
    waypoints: (plan.waypoints || []).map((w, i) => ({
      index: i,
      lat: w.lat,
      lon: w.lon,
      altM: w.altM,
      headingDeg: w.headingDeg,
      lineIndex: w.lineIndex,
      type: w.type,
    })),
    photos: (plan.photos || []).map((p, i) => ({
      index: i,
      lat: p.lat,
      lon: p.lon,
      altM: p.altM,
      headingDeg: p.headingDeg,
      lineIndex: p.lineIndex,
    })),
    photoTriggers: {
      mode: "distance",
      distanceM: coverage.triggerDistanceM,
      minIntervalSec: camera.minPhotoIntervalSec,
    },
    rth: {
      altitudeM: rthAltitude,
      onAbort: settings.rthOnAbort !== false,
      onSignalLoss: settings.rthOnSignalLoss !== false,
      lowBatteryPercent: settings.lowBatteryPercent ?? 30,
    },
    battery: plan.batteries || null,
    segments: plan.segments || [],
  };
}

export function toMissionJson(plan, extra = {}) {
  return JSON.stringify(buildMission(plan, extra), null, 2);
}

export function parseMissionJson(text) {
  const data = typeof text === "string" ? JSON.parse(text) : text;
  if (!data || data.format !== MISSION_FORMAT) {
    throw new Error("Not a drone-mapper mission file.");
  }
  return data;
}

/** Rebuild a planner plan object from a saved mission JSON (exact waypoints). */
export function planFromMission(mission) {
  const data = parseMissionJson(mission);
  const settings = data.settings || {};
  const computed = data.computed || {};
  const waypoints = data.waypoints || [];
  const photos = data.photos || [];
  const polygon = (data.polygon || []).map((p) => ({ lat: p.lat, lon: p.lon }));
  return {
    ok: true,
    error: null,
    settings: {
      name: data.name || "Untitled survey",
      altitudeM: settings.altitudeM,
      frontOverlap: settings.frontOverlap,
      sideOverlap: settings.sideOverlap,
      speedMps: settings.speedMps,
      gridAngleDeg: settings.gridAngleDeg,
      crosshatch: !!settings.crosshatch,
      headingMode: settings.headingMode || "along-line",
      fixedHeadingDeg: settings.fixedHeadingDeg || 0,
      gimbalPitchDeg: settings.gimbalPitchDeg ?? -90,
      rthAltitudeM: data.rth?.altitudeM ?? settings.altitudeM,
      rthOnAbort: data.rth?.onAbort !== false,
      rthOnSignalLoss: data.rth?.onSignalLoss !== false,
      lowBatteryPercent: data.rth?.lowBatteryPercent ?? 30,
    },
    coverage: {
      altitudeM: settings.altitudeM,
      gsdCmPerPx: computed.gsdCmPerPx,
      footprintWidthM: computed.footprintWidthM,
      footprintHeightM: computed.footprintHeightM,
      lineSpacingM: computed.lineSpacingM,
      triggerDistanceM: computed.triggerDistanceM,
      speedMps: settings.speedMps,
      speedCapped: !!computed.speedCapped,
    },
    origin: null,
    lines: [],
    waypoints,
    photos,
    stats: {
      gsdCmPerPx: computed.gsdCmPerPx,
      footprintWidthM: computed.footprintWidthM,
      footprintHeightM: computed.footprintHeightM,
      lineSpacingM: computed.lineSpacingM,
      triggerDistanceM: computed.triggerDistanceM,
      photoCount: computed.photoCount ?? photos.length,
      lineCount: computed.lineCount ?? 0,
      pathLengthM: computed.pathLengthM,
      surveyLengthM: computed.surveyLengthM,
      areaM2: computed.areaM2,
      estimatedFlightTimeSec: computed.estimatedFlightTimeSec,
      speedCapped: !!computed.speedCapped,
      speedMps: settings.speedMps,
      altitudeM: settings.altitudeM,
      batteryCount: computed.batteryCount ?? data.battery?.count ?? (data.segments || []).length,
      usableMissionMin: computed.usableMissionMin ?? data.battery?.usableMissionMin,
      segmentOverBudget: (data.segments || []).some((s) => s.overBudget),
    },
    polygon,
    batteries: data.battery || null,
    segments: data.segments || [],
  };
}

export function toKml(plan, extra = {}) {
  const mission = buildMission(plan, extra);
  const name = escapeXml(mission.name);
  const alt = mission.settings.altitudeM;
  const polyCoords = (mission.polygon || [])
    .concat(mission.polygon?.[0] ? [mission.polygon[0]] : [])
    .map((p) => `${p.lon},${p.lat},${alt}`)
    .join(" ");
  const lineCoords = (mission.waypoints || [])
    .map((w) => `${w.lon},${w.lat},${w.altM}`)
    .join(" ");
  const photoPlacemarks = (mission.photos || [])
    .map(
      (p, i) => `      <Placemark>
        <name>Photo ${i + 1}</name>
        <styleUrl>#photo</styleUrl>
        <Point><coordinates>${p.lon},${p.lat},${p.altM}</coordinates></Point>
      </Placemark>`
    )
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>${name}</name>
    <description>Piletally survey for ${escapeXml(mission.aircraft)}. Alt ${formatAltitudeFt(alt)} AGL, ${mission.computed.photoCount} photos.</description>
    <Style id="field"><LineStyle><color>ff00ccff</color><width>2</width></LineStyle><PolyStyle><color>3300ccff</color></PolyStyle></Style>
    <Style id="path"><LineStyle><color>ff2d7dff</color><width>3</width></LineStyle></Style>
    <Style id="photo"><IconStyle><color>ff44ddaa</color><scale>0.6</scale></IconStyle></Style>
    <Placemark>
      <name>Survey field</name>
      <styleUrl>#field</styleUrl>
      <Polygon>
        <tessellate>1</tessellate>
        <altitudeMode>relativeToGround</altitudeMode>
        <outerBoundaryIs><LinearRing><coordinates>${polyCoords}</coordinates></LinearRing></outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>Flight path</name>
      <styleUrl>#path</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>relativeToGround</altitudeMode>
        <coordinates>${lineCoords}</coordinates>
      </LineString>
    </Placemark>
${photoPlacemarks}
  </Document>
</kml>
`;
}

/**
 * Litchi Mission Hub CSV (metric). Global settings (heading mode, path mode)
 * are not stored in the CSV and must be set in Litchi after import.
 *
 * Note: Mavic Air 2 has no DJI waypoint mission; Litchi also flies this
 * aircraft with virtual sticks. This export is a convenience if you already
 * use Litchi, not a replacement for the companion app.
 */
export function toLitchiCsv(plan, extra = {}) {
  const mission = buildMission(plan, extra);
  const cols = [
    "latitude",
    "longitude",
    "altitude(m)",
    "heading(deg)",
    "curvesize(m)",
    "rotationdir",
    "gimbalmode",
    "gimbalpitchangle",
  ];
  for (let i = 1; i <= 15; i++) cols.push(`actiontype${i}`, `actionparam${i}`);
  cols.push(
    "altitudemode",
    "speed(m/s)",
    "poi_latitude",
    "poi_longitude",
    "poi_altitude(m)",
    "poi_altitudemode",
    "photo_timeinterval",
    "photo_distinterval"
  );

  const gimbal = mission.settings.gimbalPitchDeg ?? -90;
  const speed = mission.settings.speedMps;
  const trigger = mission.photoTriggers.distanceM;
  const rows = [cols.join(",")];

  for (const w of mission.waypoints || []) {
    const actions = Array(15).fill(["-1", "0"]).flat();
    // Distance-interval photos along the line; no stay-and-shoot at ends.
    const fields = [
      w.lat.toFixed(8),
      w.lon.toFixed(8),
      Number(w.altM).toFixed(1),
      Number(w.headingDeg).toFixed(1),
      "0.2",
      "0",
      "2", // interpolate gimbal
      Number(gimbal).toFixed(1),
      ...actions,
      "0", // altitude above takeoff
      Number(speed).toFixed(2),
      "0",
      "0",
      "0",
      "0",
      "0",
      w.type === "line-start" ? Number(trigger).toFixed(2) : "0",
    ];
    rows.push(fields.join(","));
  }
  return rows.join("\n") + "\n";
}

export function suggestedFilename(plan, ext) {
  const raw = (plan.settings?.name || "survey").toLowerCase();
  const slug = raw.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "survey";
  const stamp = new Date().toISOString().slice(0, 10);
  return `${slug}-${stamp}.${ext}`;
}

function escapeXml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
