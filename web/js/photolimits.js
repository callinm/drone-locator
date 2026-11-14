export const MIN_PHOTOS = 30;
export const DEFAULT_PHOTO_CAP = 500;
export const MAX_PHOTO_CAP = 5000;
export const DEFAULT_FRONT_OVERLAP = 0.8;
export const DEFAULT_SIDE_OVERLAP = 0.7;
export const HEIGHT_MULTIPLIER_MIN = 3;
export const HEIGHT_MULTIPLIER_MAX = 4;
export const HEIGHT_MULTIPLIER_DEFAULT = 3.5;

export function clampCap(value, fallback = DEFAULT_PHOTO_CAP) {
  const cap = Number(value);
  if (!Number.isFinite(cap)) return fallback;
  return Math.min(MAX_PHOTO_CAP, Math.max(MIN_PHOTOS, Math.round(cap)));
}

export function suggestedAltitudeM(tallestPileM) {
  const h = Number(tallestPileM);
  if (!Number.isFinite(h) || h <= 0) return null;
  return h * HEIGHT_MULTIPLIER_DEFAULT;
}

export function evaluatePhotoCount(photoCount, cap = DEFAULT_PHOTO_CAP) {
  const count = Number(photoCount) || 0;
  const limit = clampCap(cap);
  const tooFew = count < MIN_PHOTOS;
  const tooMany = count > limit;
  let warning = null;
  if (tooFew) {
    warning = `This job has ${count} photo${count === 1 ? "" : "s"}. Pile Count needs at least ${MIN_PHOTOS} overlapping photos.`;
  } else if (tooMany) {
    warning = `This job would take ${count} photos, above the cap of ${limit} for this account. Raise altitude, fly a smaller area, lower the overlap, or ask an admin to raise the cap.`;
  } else if (count > Math.floor(limit * 0.9)) {
    warning = `This job is close to the ${limit}-photo cap (${count} photos).`;
  }
  return {
    photoCount: count,
    minPhotos: MIN_PHOTOS,
    photoCap: limit,
    ok: !tooFew && !tooMany,
    tooFew,
    tooMany,
    warning,
  };
}

export function isStockpileMission(mission) {
  if (!mission || typeof mission !== "object") return false;
  const kind = mission.kind || mission.purpose || mission.settings?.kind || mission.settings?.preset || "";
  return ["stockpile", "stockpile_counting", "stockpile-counting"].includes(String(kind).toLowerCase());
}
