/**
 * Piletally and Drone Locator share this planner codebase, but they are
 * two products. The active product comes from the path, a query flag, or
 * the <html data-product> attribute.
 */
export const VERSION = "1.0.0";

export function productId() {
  try {
    const html = document.documentElement?.dataset?.product;
    if (html === "locator" || html === "piletally") return html;
  } catch {
    /* ignore */
  }
  try {
    const params = new URLSearchParams(location.search);
    const q = (params.get("app") || params.get("product") || "").toLowerCase();
    if (q === "locator" || q === "drone-locator" || q === "find") return "locator";
    if (q === "piletally" || q === "stockpile") return "piletally";
    if (/\/drone-locator(\/|$)/i.test(location.pathname)) return "locator";
    if (/\/locator(\/|$)/i.test(location.pathname)) return "locator";
  } catch {
    /* ignore */
  }
  return "piletally";
}

export function isLocator() {
  return productId() === "locator";
}

export function productName() {
  return isLocator() ? "Drone Locator" : "Piletally";
}

export function productTagline() {
  return isLocator()
    ? "Search a stitched map for a lost aircraft."
    : "It counts what's in a pile.";
}

export function applyProductChrome() {
  const locator = isLocator();
  document.documentElement.dataset.product = locator ? "locator" : "piletally";
  const title = locator
    ? "Drone Locator — Find a lost aircraft"
    : "Piletally — It counts what's in a pile.";
  document.title = title;
  const meta = document.querySelector('meta[name="description"]');
  if (meta) meta.setAttribute("content", productTagline());

  const brand = document.querySelector(".page-brand strong");
  const tag = document.querySelector(".page-brand span");
  if (brand) {
    const link = brand.querySelector("a");
    if (link) link.textContent = productName();
    else brand.textContent = productName();
  }
  if (tag) tag.textContent = productTagline();

  const hide = (id, on) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = !!on;
    el.style.display = on ? "none" : "";
  };
  hide("tabFind", !locator);
  const findTab = document.getElementById("tabFind");
  if (findTab && locator) findTab.textContent = "Drone Locator";

  hide("tabStockpile", locator);
  // Planner + Saved stay on both; presets are filtered in applyProductChrome.
  const pilePreset = document.getElementById("presetStockpile")?.closest("label");
  const findPreset = document.getElementById("presetFindDrone")?.closest("label");
  if (pilePreset) {
    pilePreset.hidden = locator;
    pilePreset.style.display = locator ? "none" : "";
  }
  if (findPreset) {
    findPreset.hidden = !locator;
    findPreset.style.display = locator ? "" : "none";
  }

  const footer = document.getElementById("siteFooter");
  if (footer) {
    footer.textContent = `${productName()} web ${VERSION}`;
  }
  const ver = document.getElementById("productVersion");
  if (ver) ver.textContent = `${productName()} web ${VERSION}`;
}

export function otherAppsHref() {
  return "/apps/";
}
