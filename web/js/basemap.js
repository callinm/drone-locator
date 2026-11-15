/**
 * Shared imagery tiles, map-size refresh, and a GPS default view.
 * Esri World Imagery is primary. If those tiles fail or never arrive,
 * Esri World Street Map is used, then OpenStreetMap. The map must never
 * sit as a blank black or brown rectangle with only zoom buttons.
 */

export const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
export const ESRI_LABELS =
  "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
export const ESRI_STREET =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}";
export const OSM_TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
export const CARTO_TILES = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png";

export const FALLBACK_CENTER = [39.8, -89.6];
export const FALLBACK_ZOOM = 6;
export const GPS_ZOOM = 16;

function addLayerOnce(map, layer) {
  if (!map || !layer) return;
  if (!map.hasLayer(layer)) layer.addTo(map);
}

export function addBaseTiles(map) {
  if (!map) return;
  let imageryOk = 0;
  let streetAdded = false;
  let osmAdded = false;

  const imagery = L.tileLayer(ESRI_IMAGERY, {
    attribution: "Tiles © Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    maxZoom: 19,
    className: "basemap-satellite",
  });
  const labels = L.tileLayer(ESRI_LABELS, {
    maxZoom: 19,
    pane: "overlayPane",
    className: "basemap-labels",
    opacity: 0.9,
  });
  const street = L.tileLayer(ESRI_STREET, {
    attribution: "Tiles © Esri",
    maxZoom: 19,
    className: "basemap-street",
  });
  const carto = L.tileLayer(CARTO_TILES, {
    attribution: "© OpenStreetMap contributors, © CARTO",
    maxZoom: 19,
    subdomains: "abcd",
    className: "basemap-street",
  });
  const osm = L.tileLayer(OSM_TILES, {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
    className: "basemap-street",
  });

  function addStreetFallback() {
    if (streetAdded) return;
    streetAdded = true;
    addLayerOnce(map, street);
    if (imageryOk === 0) {
      try {
        map.removeLayer(imagery);
        map.removeLayer(labels);
      } catch {
        /* already gone */
      }
    }
    let streetOk = 0;
    street.on("tileload", () => {
      streetOk += 1;
    });
    street.on("tileerror", () => {
      if (streetOk > 0 || osmAdded) return;
      osmAdded = true;
      addLayerOnce(map, carto);
      carto.on("tileerror", () => addLayerOnce(map, osm));
    });
    setTimeout(() => {
      if (streetOk === 0 && !osmAdded) {
        osmAdded = true;
        addLayerOnce(map, carto);
        setTimeout(() => {
          // If Carto also failed, try OSM.
          addLayerOnce(map, osm);
        }, 4000);
      }
    }, 4000);
  }

  imagery.on("tileload", () => {
    imageryOk += 1;
  });
  imagery.on("tileerror", () => {
    if (imageryOk === 0) addStreetFallback();
  });
  imagery.addTo(map);
  labels.addTo(map);

  setTimeout(() => {
    if (imageryOk === 0) addStreetFallback();
  }, 3500);
}

export function labelZoomControls(map) {
  if (!map) return;
  const zoom = map.zoomControl;
  if (!zoom) {
    L.control
      .zoom({
        zoomInTitle: "Zoom in",
        zoomOutTitle: "Zoom out",
      })
      .addTo(map);
  }
  const el = map.zoomControl && map.zoomControl.getContainer();
  if (!el) return;
  const links = el.querySelectorAll("a");
  if (links[0]) {
    links[0].setAttribute("title", "Zoom in");
    links[0].setAttribute("aria-label", "Zoom in");
    links[0].setAttribute("role", "button");
  }
  if (links[1]) {
    links[1].setAttribute("title", "Zoom out");
    links[1].setAttribute("aria-label", "Zoom out");
    links[1].setAttribute("role", "button");
  }
  el.setAttribute("aria-label", "Map zoom");
}

export function refreshMapSize(map) {
  if (!map) return;
  const apply = () => {
    try {
      map.invalidateSize({ animate: false });
    } catch {
      /* map not ready */
    }
  };
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 50);
  setTimeout(apply, 250);
  setTimeout(apply, 800);
}

/**
 * Center on the user's location when no project is open.
 * Falls back to the Midwest overview if permission is denied or GPS is unavailable.
 */
export function locateOrFallback(map, { onDenied, onLocated, keepView } = {}) {
  if (!map) return;
  const fallback = () => {
    if (!(keepView && keepView())) map.setView(FALLBACK_CENTER, FALLBACK_ZOOM);
    onDenied?.();
  };
  if (!navigator.geolocation) {
    fallback();
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const lat = pos.coords.latitude;
      const lon = pos.coords.longitude;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        fallback();
        return;
      }
      // A search map loaded while we waited for GPS keeps the view on that map.
      if (!(keepView && keepView())) map.setView([lat, lon], GPS_ZOOM);
      onLocated?.({ lat, lon });
    },
    () => fallback(),
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 5 * 60 * 1000 }
  );
}

export function attachBasemap(map, { locate = false } = {}) {
  addBaseTiles(map);
  labelZoomControls(map);
  refreshMapSize(map);
  if (locate) locateOrFallback(map);
  return map;
}
