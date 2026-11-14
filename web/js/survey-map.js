/**
 * Shared handle for the latest survey orthophoto so Find My Drone can
 * use a map built on Pile Count.
 */

let latest = null;

export function setLatestSurveyMap(packed) {
  latest = packed || null;
  return latest;
}

export function getLatestSurveyMap() {
  return latest;
}
