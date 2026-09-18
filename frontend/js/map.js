// js/map.js
//
// The public map, backed by GET /api/v1/analytics/map/points.
//
// That endpoint is deliberately narrow: nine fields per point, no reporter, no
// description. So this file renders exactly what it is given and never assumes
// a richer object - a popup that reached for `point.reporter` would silently
// render "undefined" and, worse, imply the data is available.

let nepalMap = null;
let mapPointLayers = null;

// The backend answers in two kinds; the existing layer toggles are named for
// four. `citizen` carries unverified reports, `disaster` carries verified
// incidents. The development and environment layers have no backing data yet -
// projects have no coordinates of their own, and conservation areas were never
// imported - so they are created empty rather than filled with invented pins.
const LAYER_KEYS = ["disaster", "citizen", "development", "environment"];

// Where to look when a map has no data at all: the centre of Nepal.
const NEPAL_CENTER = [28.3949, 84.124];

function markerIcon(severity) {
  return L.divIcon({
    className: "",
    html: `<div class="marker-pin ${severity}"><span>•</span></div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 26],
    popupAnchor: [0, -26],
  });
}

// Pin colour. Incidents carry a real severity; a report has none (the backend
// Report model has no severity column), so its status is used instead.
function pinClassFor(point) {
  if (point.type === "incident") {
    return (typeof SEVERITY_PIN !== "undefined" && SEVERITY_PIN[point.severity]) || "warning";
  }
  return point.status === "under_review" ? "warning" : "info";
}

function detailHrefFor(point) {
  const base = basePath();
  return point.type === "incident"
    ? `${base}pages/authority/incident-details.html?id=${encodeURIComponent(point.id)}`
    : `${base}pages/citizen/report-details.html?id=${encodeURIComponent(point.id)}`;
}

function popupHtml(point) {
  const badgeClass =
    (typeof STATUS_BADGE !== "undefined" && STATUS_BADGE[point.status]) || "badge-neutral";
  const severity = point.severity
    ? `<span class="badge badge-neutral">${escapeHtml(humanise(point.severity))}</span>`
    : "";

  return `
    <strong>${escapeHtml(point.title)}</strong>
    <div class="popup-meta">
      ${escapeHtml(humanise(point.category))} ·
      ${escapeHtml(point.type === "incident" ? "Incident" : "Report")}
    </div>
    <div class="popup-meta" style="margin:6px 0">
      <span class="badge ${badgeClass}">${escapeHtml(humanise(point.status))}</span>
      ${severity}
    </div>
    <div class="popup-meta">#${escapeHtml(String(point.id).slice(0, 8))} · ${escapeHtml(
      formatDate(point.created_at)
    )}</div>
    <a href="${detailHrefFor(point)}">View details →</a>
  `;
}

// --- init ------------------------------------------------------------------

function initNepalMap(mapElementId) {
  if (nepalMap) return nepalMap;
  if (!document.getElementById(mapElementId)) return null;

  nepalMap = L.map(mapElementId, {
    center: NEPAL_CENTER,
    zoom: 7,
    scrollWheelZoom: true,
    zoomControl: true,
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 18,
  }).addTo(nepalMap);

  mapPointLayers = {};
  LAYER_KEYS.forEach((key) => {
    mapPointLayers[key] = L.layerGroup().addTo(nepalMap);
  });

  setTimeout(() => nepalMap.invalidateSize(), 100);
  return nepalMap;
}

// Kept under its old name; several pages reference it.
const layerGroups = new Proxy(
  {},
  {
    get: (_, key) => (mapPointLayers ? mapPointLayers[key] : null),
  }
);

// --- data ------------------------------------------------------------------

let lastLoadedPoints = [];

// Fetch and draw. Public endpoint, so this works signed out - which is the
// whole point of the landing-page map.
async function loadMapPoints(filters = {}) {
  if (!nepalMap) return [];

  const data = await apiGet(`/analytics/map/points${queryString(filters)}`, { auth: false });
  const points = data.points || [];
  lastLoadedPoints = points;

  LAYER_KEYS.forEach((key) => mapPointLayers[key] && mapPointLayers[key].clearLayers());

  points.forEach((point) => {
    if (typeof point.latitude !== "number" || typeof point.longitude !== "number") return;

    const group = mapPointLayers[point.type === "incident" ? "disaster" : "citizen"];
    if (!group) return;

    L.marker([point.latitude, point.longitude], { icon: markerIcon(pinClassFor(point)) })
      .bindPopup(popupHtml(point))
      .addTo(group);
  });

  // A capped feed must say so rather than quietly drawing a partial map.
  if (data.truncated && typeof showToast === "function") {
    showToast(`Showing the most recent ${data.count} of a larger set.`, "info", 5000);
  }

  return points;
}

// --- layer toggles ---------------------------------------------------------

function bindLayerToggles() {
  LAYER_KEYS.forEach((key) => {
    const el = document.getElementById(`layer-${key}`);
    if (!el || !mapPointLayers || !mapPointLayers[key]) return;

    el.addEventListener("change", (e) => {
      if (e.target.checked) nepalMap.addLayer(mapPointLayers[key]);
      else nepalMap.removeLayer(mapPointLayers[key]);
    });
  });
}

// --- nearby panel ----------------------------------------------------------

function renderNearby(containerId, points = lastLoadedPoints) {
  const el = document.getElementById(containerId);
  if (!el) return;

  if (!points.length) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">Nothing reported yet</div>
        <div class="empty-text">Reports and incidents will appear here as they come in.</div>
      </div>`;
    return;
  }

  el.innerHTML = points
    .slice(0, 5)
    .map(
      (p) => `
    <a href="${detailHrefFor(p)}" class="incident-card" style="margin-bottom:8px;">
      <div class="incident-icon ${pinClassFor(p)}">•</div>
      <div class="incident-body">
        <div class="incident-title">${escapeHtml(p.title)}</div>
        <div class="incident-meta">${escapeHtml(humanise(p.category))} · ${escapeHtml(
          humanise(p.status)
        )}</div>
      </div>
    </a>`
    )
    .join("");
}

// --- search ----------------------------------------------------------------

// A small local gazetteer. Deliberately not a geocoding API call: this is a
// convenience for jumping the viewport, and it should keep working offline and
// without a third-party dependency or key.
const NEPAL_PLACES = {
  kathmandu: [27.7172, 85.324],
  pokhara: [28.2096, 83.9856],
  lalitpur: [27.6588, 85.3247],
  bhaktapur: [27.671, 85.4298],
  biratnagar: [26.4525, 87.2718],
  birgunj: [27.0104, 84.8821],
  butwal: [27.7006, 83.4484],
  nepalgunj: [28.05, 81.6167],
  dhangadhi: [28.6833, 80.6],
  manang: [28.6667, 84.0167],
  mustang: [28.9985, 83.8473],
  chitwan: [27.5291, 84.3542],
  sunsari: [26.65, 87.15],
  ilam: [26.9094, 87.9285],
  nepal: NEPAL_CENTER,
};

function bindSearch(inputId) {
  const input = document.getElementById(inputId);
  if (!input || !nepalMap) return;

  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = input.value.trim().toLowerCase();
    if (!q) return;

    const place = Object.keys(NEPAL_PLACES).find((p) => p.includes(q) || q.includes(p));
    if (place) {
      nepalMap.flyTo(NEPAL_PLACES[place], 11, { duration: 0.8 });
      return;
    }

    // No place matched: fall back to searching the loaded points by title, so
    // typing a report's name finds it.
    const point = lastLoadedPoints.find((p) =>
      String(p.title).toLowerCase().includes(q)
    );
    if (point) nepalMap.flyTo([point.latitude, point.longitude], 13, { duration: 0.8 });
    else if (typeof showToast === "function") showToast(`No match for "${q}".`, "info");
  });
}

// --- bootstrap -------------------------------------------------------------

async function bootstrapMapPage(filters = {}) {
  if (!initNepalMap("nepal-map")) return;
  bindLayerToggles();
  bindSearch("map-search-input");

  const nearbyEl = document.getElementById("map-nearby");
  if (nearbyEl) renderLoading("map-nearby", "Loading nearby activity…");

  try {
    const points = await loadMapPoints(filters);
    renderNearby("map-nearby", points);
  } catch (error) {
    reportApiError(error, "Could not load map data.");
    if (nearbyEl) renderError("map-nearby", "Could not load nearby activity.");
  }
}

// --- location picker -------------------------------------------------------

// Lets a report form collect coordinates by clicking the map. Returns a
// handle whose `getCoordinates()` the submit handler reads.
function enableLocationPicker(mapElementId, { onPick } = {}) {
  const map = initNepalMap(mapElementId);
  if (!map) return null;

  let marker = null;
  let picked = null;

  const place = (lat, lng) => {
    picked = { lat, lng };
    if (marker) marker.setLatLng([lat, lng]);
    else marker = L.marker([lat, lng], { icon: markerIcon("critical") }).addTo(map);
    if (typeof onPick === "function") onPick(picked);
  };

  map.on("click", (e) => place(e.latlng.lat, e.latlng.lng));

  return {
    getCoordinates: () => picked,
    setCoordinates: (lat, lng, zoom = 15) => {
      place(lat, lng);
      map.flyTo([lat, lng], zoom, { duration: 0.6 });
    },
  };
}
