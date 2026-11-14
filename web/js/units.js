/**
 * UI-boundary unit conversions. Internal math, mission JSON, Litchi CSV,
 * and aircraft commands stay in SI (meters, m/s, t/m³). Convert only when
 * showing or collecting values in the planner or Android UI.
 *
 * Uses the international foot (1 ft = 0.3048 m) and the US short ton
 * (2000 lb). Density converts as short tons per cubic yard.
 */
export const M_PER_FT = 0.3048;
export const FT_PER_M = 1 / M_PER_FT;
export const IN_PER_CM = 0.3937007874015748;
export const FT2_PER_M2 = FT_PER_M * FT_PER_M;
export const M2_PER_ACRE = 4046.8564224;
export const YD3_PER_M3 = 1.3079506193143917; // (1 / 0.3048) ** 3 / 27
export const SHORT_TONS_PER_METRIC_TON = 1.102311310924388; // 1000 kg / 907.18474 kg
export const MPH_PER_MPS = 2.2369362920544025; // 3600 / 1609.344
export const MPS_PER_MPH = 1 / MPH_PER_MPS;

/** FAA Part 107 AGL cap: 400 ft = 121.92 m. */
export const FAA_MAX_ALTITUDE_FT = 400;
export const FAA_MAX_ALTITUDE_M = FAA_MAX_ALTITUDE_FT * M_PER_FT;

export function ftToM(ft) {
  return Number(ft) * M_PER_FT;
}

export function mToFt(m) {
  return Number(m) * FT_PER_M;
}

export function cmToIn(cm) {
  return Number(cm) * IN_PER_CM;
}

export function inToCm(inches) {
  return Number(inches) / IN_PER_CM;
}

export function mpsToMph(mps) {
  return Number(mps) * MPH_PER_MPS;
}

export function mphToMps(mph) {
  return Number(mph) * MPS_PER_MPH;
}

export function m2ToFt2(m2) {
  return Number(m2) * FT2_PER_M2;
}

export function m2ToAcres(m2) {
  return Number(m2) / M2_PER_ACRE;
}

export function m3ToYd3(m3) {
  return Number(m3) * YD3_PER_M3;
}

export function yd3ToM3(yd3) {
  return Number(yd3) / YD3_PER_M3;
}

export function metricTonsToShortTons(t) {
  return Number(t) * SHORT_TONS_PER_METRIC_TON;
}

export function shortTonsToMetricTons(tons) {
  return Number(tons) / SHORT_TONS_PER_METRIC_TON;
}

/** Bulk density: metric tonnes per m³ → US short tons per cubic yard. */
export function tPerM3ToTonsPerYd3(d) {
  return Number(d) * (SHORT_TONS_PER_METRIC_TON / YD3_PER_M3);
}

export function tonsPerYd3ToTPerM3(d) {
  return Number(d) * (YD3_PER_M3 / SHORT_TONS_PER_METRIC_TON);
}

/** Round a meter altitude to the nearest slider step in feet (default 5 ft). */
export function altitudeFtFromM(m, step = 5) {
  const ft = mToFt(m);
  if (!Number.isFinite(ft)) return 0;
  return Math.round(ft / step) * step;
}

export function formatAltitudeFt(m, step = 5) {
  if (!Number.isFinite(m)) return "—";
  return `${altitudeFtFromM(m, step)} ft`;
}

export function formatAltitudeFtExact(m, digits = 0) {
  if (!Number.isFinite(m)) return "—";
  return `${mToFt(m).toFixed(digits)} ft`;
}

export function formatGsd(gsdCmPerPx) {
  if (!Number.isFinite(gsdCmPerPx) || gsdCmPerPx <= 0) return "—";
  return `${cmToIn(gsdCmPerPx).toFixed(2)} in/px`;
}

export function formatSpeedMph(mps, digits = 0) {
  if (!Number.isFinite(mps)) return "—";
  return `${mpsToMph(mps).toFixed(digits)} mph`;
}

export function formatDistance(m) {
  if (!Number.isFinite(m)) return "—";
  const ft = mToFt(m);
  if (Math.abs(ft) >= 5280) return `${(ft / 5280).toFixed(2)} mi`;
  if (Math.abs(ft) >= 100) return `${ft.toFixed(0)} ft`;
  if (Math.abs(ft) >= 10) return `${ft.toFixed(1)} ft`;
  return `${ft.toFixed(2)} ft`;
}

export function formatCellSize(m) {
  if (!Number.isFinite(m)) return "—";
  const inches = m * 39.37007874015748;
  if (inches >= 24) return `${(inches / 12).toFixed(2)} ft`;
  return `${inches.toFixed(2)} in`;
}

export function formatArea(m2) {
  if (!Number.isFinite(m2) || m2 <= 0) return "—";
  const acres = m2ToAcres(m2);
  const ft2 = m2ToFt2(m2);
  if (acres >= 0.1) return `${acres.toFixed(2)} ac`;
  return `${Math.round(ft2)} sq ft`;
}

export function formatVolumeYd3(m3) {
  if (!Number.isFinite(m3)) return "—";
  const yd = m3ToYd3(m3);
  if (Math.abs(yd) >= 100) return `${yd.toFixed(0)} yd³`;
  return `${yd.toFixed(2)} yd³`;
}

export function formatShortTons(metricTons) {
  if (!Number.isFinite(metricTons)) return "—";
  const t = metricTonsToShortTons(metricTons);
  if (Math.abs(t) >= 100) return `${t.toFixed(0)} tons`;
  return `${t.toFixed(1)} tons`;
}

export function formatDensity(tPerM3) {
  if (!Number.isFinite(tPerM3)) return "—";
  return `${tPerM3ToTonsPerYd3(tPerM3).toFixed(2)} tons/yd³`;
}
