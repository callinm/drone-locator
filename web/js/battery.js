/**
 * Mavic Air 2 battery budget for mapping flights.
 *
 * Official pack: 3500 mAh, 34 min rated hover (still air, new pack, ideal).
 * Mapping derate: 28 min to empty. Cruise, 180° turns, the camera, and typical
 * wind use more energy than a hover, so we treat about 82% of the rated 34 min
 * as a realistic time-to-empty. That 28 min figure is also what this project
 * already used as usableFlightTimeMin.
 *
 * Reserve: 30% of the pack is left for Return to Home and landing. Auto-RTH
 * in the flight app also fires at 30%, so the planner and the aircraft agree.
 * Usable survey+climb+RTH time per battery = 28 × 0.70 = 19.6 min.
 * Usable minutes from a partially charged pack = (percent − 30) / 100 × 28.
 * Preflight will not start below 40% (only ~2.8 min left before the 30% floor).
 */
import { MAVIC_AIR_2 } from "./camera.js?v=20261114dlpm1";

export const BATTERY = Object.freeze({
  capacityMah: 3500,
  ratedHoverMin: 34,
  realisticEmptyMin: 28,
  reservePercent: 30,
  autoRthPercent: 30,
  minStartPercent: 40,
});

export function usableMissionMin(camera = MAVIC_AIR_2) {
  const empty = camera.usableFlightTimeMin ?? BATTERY.realisticEmptyMin;
  return empty * (1 - BATTERY.reservePercent / 100);
}

/** Minutes of climb + survey + RTH available before the 30% auto-RTH floor. */
export function usableMinutesAtPercent(percent, camera = MAVIC_AIR_2) {
  if (!Number.isFinite(percent) || percent < 0) return 0;
  const empty = camera.usableFlightTimeMin ?? BATTERY.realisticEmptyMin;
  return Math.max(0, ((percent - BATTERY.reservePercent) / 100) * empty);
}

export function canStart(percent, minStart = BATTERY.minStartPercent) {
  return Number.isFinite(percent) && percent >= minStart;
}

export function segmentFits(percent, segmentMin, camera = MAVIC_AIR_2) {
  return usableMinutesAtPercent(percent, camera) + 1e-9 >= segmentMin;
}

export function batteryModel(camera = MAVIC_AIR_2) {
  const emptyMin = camera.usableFlightTimeMin ?? BATTERY.realisticEmptyMin;
  const usableMin = usableMissionMin(camera);
  return {
    capacityMah: BATTERY.capacityMah,
    ratedHoverMin: camera.maxFlightTimeMin ?? BATTERY.ratedHoverMin,
    realisticEmptyMin: emptyMin,
    reservePercent: BATTERY.reservePercent,
    autoRthPercent: BATTERY.autoRthPercent,
    minStartPercent: BATTERY.minStartPercent,
    usableMissionMin: usableMin,
    usableMissionSec: usableMin * 60,
  };
}
