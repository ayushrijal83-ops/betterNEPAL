// js/reports.js

const REPORT_CATEGORIES = [
  { value: "bridge",      label: "Bridge" },
  { value: "trail",       label: "Trail / Infrastructure" },
  { value: "landslide",   label: "Landslide / Rockfall" },
  { value: "waste",       label: "Waste Management" },
  { value: "water",       label: "Water Supply" },
  { value: "sanitation",  label: "Sanitation" },
  { value: "electricity", label: "Electricity" },
  { value: "environment", label: "Environmental Damage" },
  { value: "road",        label: "Road" },
  { value: "other",       label: "Other" },
];

const MOCK_REPORTS = {
  BNP1098: {
    id: "BNP1098",
    title: "Bridge damaged — Annapurna Region",
    category: "Bridge",
    severity: "High",
    status: "Reported",
    location: "Annapurna Region, Gandaki Province",
    coordinates: "28.5966° N, 83.8203° E",
    date: "2026-09-18T09:30:00",
    reporter: "Sanjal Neupane (Registered Guide)",
    image: "https://images.unsplash.com/photo-1547036967-23d11aacaee0?w=900",
    description:
      "Bridge damaged after recent rainfall. Difficult for trekkers to cross. One railing completely broken and remaining structure looks weakened.",
    ai: {
      classification: "Infrastructure / Bridge",
      hazard: "Possible flood/rain damage",
      confidence: 0.87,
      department: "Department of Roads — Gandaki Province",
      requiresVerification: true,
    },
    timeline: [
      { label: "Reported by guide", time: "18 Sept 2026, 09:30", state: "done" },
      { label: "AI-assisted classification", time: "18 Sept 2026, 09:31", state: "done" },
      { label: "Authority verification", time: "Pending", state: "active" },
      { label: "Repair assigned", time: "—", state: "" },
      { label: "Repair in progress", time: "—", state: "" },
      { label: "Completed", time: "—", state: "" },
    ],
  },
  BNP1097: {
    id: "BNP1097",
    title: "Trail blocked by landslide",
    category: "Landslide",
    severity: "High",
    status: "In Progress",
    location: "Manang District, Gandaki Province",
    coordinates: "28.6667° N, 84.0167° E",
    date: "2026-09-17T14:15:00",
    reporter: "Pemba Sherpa (Registered Guide)",
    image: "https://images.unsplash.com/photo-1580659929733-1c2ef6b8b3f0?w=900",
    description:
      "Large landslide blocking trekking route after heavy rain. Trekker safety at risk. Alternative route required.",
    ai: {
      classification: "Natural Hazard / Landslide",
      hazard: "Slope instability after rainfall",
      confidence: 0.92,
      department: "District Disaster Management Committee — Manang",
      requiresVerification: true,
    },
    timeline: [
      { label: "Reported by guide", time: "17 Sept 2026, 14:15", state: "done" },
      { label: "AI-assisted classification", time: "17 Sept 2026, 14:16", state: "done" },
      { label: "Authority verification", time: "17 Sept 2026, 15:02", state: "done" },
      { label: "Repair assigned", time: "18 Sept 2026, 09:00", state: "done" },
      { label: "Repair in progress", time: "Ongoing", state: "active" },
      { label: "Completed", time: "—", state: "" },
    ],
  },
};

// ---------- Lookups ----------

function getReports() {
  return Object.values(MOCK_REPORTS);
}

function getReportById(id) {
  return MOCK_REPORTS[id] || MOCK_REPORTS.BNP1098;
}

// ---------- Rendering helpers ----------

function renderTimeline(items) {
  return `
    <ul class="timeline">
      ${items
        .map(
          (t) => `
        <li class="timeline-item ${t.state || ""}">
          <span class="timeline-dot"></span>
          <div class="timeline-title">${t.label}</div>
          <div class="timeline-meta">${t.time}</div>
        </li>`
        )
        .join("")}
    </ul>
  `;
}

function statusBadge(status) {
  const map = {
    Reported: "badge-critical",
    Verified: "badge-info",
    "In Progress": "badge-warning",
    Resolved: "badge-success",
    Completed: "badge-success",
    Active: "badge-warning",
    Pending: "badge-neutral",
  };
  const cls = map[status] || "badge-neutral";
  return `<span class="badge ${cls}">${status}</span>`;
}

function severityClass(sev) {
  if (sev === "High") return "sev-high";
  if (sev === "Medium") return "sev-medium";
  return "sev-low";
}

function renderCategoryOptions() {
  return REPORT_CATEGORIES.map(
    (c) => `<option value="${c.value}">${c.label}</option>`
  ).join("");
}

function renderReportsTable(tbodyId, reports) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  el.innerHTML = reports
    .map(
      (r) => `
    <tr>
      <td>#${r.id}</td>
      <td>${r.title}</td>
      <td>${r.category}</td>
      <td>${r.severity}</td>
      <td>${statusBadge(r.status)}</td>
      <td>${formatDate(r.date)}</td>
      <td class="actions"><a href="report-details.html?id=${r.id}" class="btn btn-sm btn-outline">View</a></td>
    </tr>`
    )
    .join("");
}