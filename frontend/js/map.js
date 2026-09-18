// js/map.js

// Global map instance
let nepalMap = null;
const layerGroups = {
  disaster: null,
  citizen: null,
  development: null,
  environment: null,
};

// Mock marker data across Nepal
const MAP_MARKERS = [
  // Disaster layer
  {
    layer: "disaster",
    lat: 26.65,
    lng: 87.15,
    title: "Flood warning — Sunsari",
    meta: "Koshi River • Critical",
    severity: "critical",
    href: "../authority/incident-details.html?id=FL-2026-00231",
  },
  {
    layer: "disaster",
    lat: 27.65,
    lng: 84.43,
    title: "Landslide — Muglin–Narayanghat",
    meta: "Prithvi Highway • Critical",
    severity: "critical",
    href: "../authority/incident-details.html?id=MG-2026-00114",
  },
  {
    layer: "disaster",
    lat: 28.2,
    lng: 83.98,
    title: "Landslide — Manang trail",
    meta: "Manang District • High",
    severity: "warning",
    href: "report-details.html?id=BNP1097",
  },

  // Citizen / guide reports
  {
    layer: "citizen",
    lat: 28.5966,
    lng: 83.8203,
    title: "Bridge damaged — Annapurna Region",
    meta: "Gandaki Province • High",
    severity: "critical",
    href: "report-details.html?id=BNP1098",
  },
  {
    layer: "citizen",
    lat: 27.7,
    lng: 85.31,
    title: "Transformer failure — Lalitpur",
    meta: "Ward 8, Patan • High",
    severity: "warning",
    href: "../authority/incident-details.html?id=EL-2026-00482",
  },
  {
    layer: "citizen",
    lat: 27.8,
    lng: 85.4,
    title: "Waste accumulation — Rest point",
    meta: "Kaski District • Low",
    severity: "success",
    href: "my-reports.html",
  },

  // Development
  {
    layer: "development",
    lat: 28.2096,
    lng: 83.9856,
    title: "Bridge Repair — Trail #104",
    meta: "PRJ-2026-104 • 62% complete",
    severity: "info",
    href: "../authority/project-details.html?id=PRJ-2026-104",
  },
  {
    layer: "development",
    lat: 28.6667,
    lng: 84.0167,
    title: "Trail Clearance — Manang Landslide",
    meta: "PRJ-2026-118 • 35% complete",
    severity: "info",
    href: "../authority/project-details.html?id=PRJ-2026-118",
  },
  {
    layer: "development",
    lat: 28.25,
    lng: 83.9,
    title: "Rest Point Reconstruction — ABC Trail",
    meta: "PRJ-2026-090 • Completed",
    severity: "success",
    href: "../authority/project-details.html?id=PRJ-2026-090",
  },

  // Environment
  {
    layer: "environment",
    lat: 28.3949,
    lng: 84.124,
    title: "Annapurna Conservation Area",
    meta: "Monitoring area",
    severity: "success",
    href: "#",
  },
  {
    layer: "environment",
    lat: 27.5,
    lng: 86.5,
    title: "Sagarmatha National Park",
    meta: "Monitoring area",
    severity: "success",
    href: "#",
  },
];

// --- Marker icon ---
function markerIcon(severity) {
  return L.divIcon({
    className: "",
    html: `<div class="marker-pin ${severity}"><span>•</span></div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 26],
    popupAnchor: [0, -26],
  });
}

// --- Init map ---
function initNepalMap(mapElementId) {
  if (nepalMap) return nepalMap;
  if (!document.getElementById(mapElementId)) return null;

  nepalMap = L.map(mapElementId, {
    center: [28.3949, 84.124],
    zoom: 7,
    scrollWheelZoom: true,
    zoomControl: true,
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 18,
  }).addTo(nepalMap);

  // Create layer groups
  layerGroups.disaster    = L.layerGroup().addTo(nepalMap);
  layerGroups.citizen     = L.layerGroup().addTo(nepalMap);
  layerGroups.development = L.layerGroup().addTo(nepalMap);
  layerGroups.environment = L.layerGroup().addTo(nepalMap);

  // Add markers to their layer groups
  MAP_MARKERS.forEach((m) => {
    const group = layerGroups[m.layer];
    if (!group) return;

    const marker = L.marker([m.lat, m.lng], { icon: markerIcon(m.severity) });
    marker.bindPopup(`
      <strong>${m.title}</strong>
      <div class="popup-meta">${m.meta}</div>
      <a href="${m.href}">View details →</a>
    `);
    marker.addTo(group);
  });

  // Force a redraw so tiles fit the container
  setTimeout(() => nepalMap.invalidateSize(), 100);

  return nepalMap;
}

// --- Layer toggles ---
function bindLayerToggles() {
  const toggles = [
    { elId: "layer-disaster",    key: "disaster" },
    { elId: "layer-citizen",     key: "citizen" },
    { elId: "layer-development", key: "development" },
    { elId: "layer-environment", key: "environment" },
  ];

  toggles.forEach(({ elId, key }) => {
    const el = document.getElementById(elId);
    if (!el || !layerGroups[key]) return;

    el.addEventListener("change", (e) => {
      if (e.target.checked) {
        nepalMap.addLayer(layerGroups[key]);
      } else {
        nepalMap.removeLayer(layerGroups[key]);
      }
    });
  });
}

// --- Nearby activity panel ---
function renderNearby(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;

  el.innerHTML = MAP_MARKERS.slice(0, 4)
    .map(
      (m) => `
    <a href="${m.href}" class="incident-card" style="margin-bottom:8px;">
      <div class="incident-icon ${m.severity === "critical" ? "critical" : m.severity === "warning" ? "warning" : m.severity === "success" ? "success" : "info"}">•</div>
      <div class="incident-body">
        <div class="incident-title">${m.title}</div>
        <div class="incident-meta">${m.meta}</div>
      </div>
    </a>`
    )
    .join("");
}

// --- Search (basic fly-to on Enter) ---
const NEPAL_PLACES = {
  kathmandu: [27.7172, 85.324],
  pokhara:   [28.2096, 83.9856],
  lalitpur:  [27.6588, 85.3247],
  manang:    [28.6667, 84.0167],
  sunsari:   [26.65, 87.15],
  chitwan:   [27.5291, 84.3542],
  bhaktapur: [27.671, 85.4298],
  ilam:      [26.9094, 87.9285],
  nepal:     [28.3949, 84.124],
};

function bindSearch(inputId) {
  const input = document.getElementById(inputId);
  if (!input || !nepalMap) return;

  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = input.value.trim().toLowerCase();
    if (!q) return;
    const found = Object.keys(NEPAL_PLACES).find((p) => p.includes(q) || q.includes(p));
    if (found) {
      nepalMap.flyTo(NEPAL_PLACES[found], 11, { duration: 0.8 });
    }
  });
}

// --- Convenience bootstrap used by each map.html page ---
function bootstrapMapPage() {
  initNepalMap("nepal-map");
  bindLayerToggles();
  renderNearby("map-nearby");
  bindSearch("map-search-input");
}