// js/incidents.js

const MOCK_INCIDENTS = [
  {
    id: "EL-2026-00482",
    title: "Transformer Failure — Lalitpur",
    type: "Infrastructure Failure",
    severity: "High",
    status: "In Progress",
    priority: "High",
    location: "Ward 8, Patan, Lalitpur",
    assignedTo: "NEA Lalitpur Division",
    reportedAt: "2026-10-26T09:30:00",
    image: "https://images.unsplash.com/photo-1473341304170-971dccb5ac1e?w=900",
    description:
      "Insulator failure causing short circuit. Recommended action: replace unit and insulators.",
    ai: { confidence: 0.87, label: "Equipment Failure" },
    team: {
      name: "Team Alpha-04",
      status: "On Site",
      eta: "18:00 PM",
      members: ["Rajesh Sharma (Lead)", "Sita Tamang (Technician)", "Hari Thapa (Electrician)"],
    },
    timeline: [
      { label: "Citizen report", time: "09:30 AM", state: "done" },
      { label: "AI-assisted classification", time: "09:35 AM", state: "done" },
      { label: "Authority verification", time: "10:15 AM", state: "done" },
      { label: "Repair in progress", time: "Dispatched 10:30 AM", state: "active" },
      { label: "Completed", time: "—", state: "" },
    ],
  },
  {
    id: "MG-2026-00114",
    title: "Landslide — Muglin–Narayanghat Highway",
    type: "Natural Hazard",
    severity: "High",
    status: "Reported",
    priority: "Critical",
    location: "Muglin–Narayanghat Road, Km 14.3, Chitwan",
    assignedTo: "Road Authority — Chitwan",
    reportedAt: "2026-10-26T11:05:00",
    image: "https://images.unsplash.com/photo-1580659929733-1c2ef6b8b3f0?w=900",
    description:
      "Landslide blocking both lanes of the Prithvi Highway. Traffic diverted. Heavy machinery en route.",
    ai: { confidence: 0.94, label: "Landslide / Rockfall" },
    team: {
      name: "Road Response Team 2",
      status: "Dispatched",
      eta: "13:00 PM",
      members: ["Bikash Thapa (Lead)", "Ramesh Yadav (Operator)"],
    },
    timeline: [
      { label: "Guide report", time: "11:05 AM", state: "done" },
      { label: "AI classification", time: "11:06 AM", state: "done" },
      { label: "Authority verification", time: "Pending", state: "active" },
      { label: "Repair assigned", time: "—", state: "" },
      { label: "Completed", time: "—", state: "" },
    ],
  },
  {
    id: "FL-2026-00231",
    title: "Flood warning — Koshi River",
    type: "Flood",
    severity: "High",
    status: "Active",
    priority: "Critical",
    location: "Sunsari District",
    assignedTo: "Flood Authority — Koshi",
    reportedAt: "2026-10-26T10:45:00",
    image: "https://images.unsplash.com/photo-1547683905-f686c993aae5?w=900",
    description:
      "Water level 2.5m above danger level. Severe flooding downstream. Evacuations initiated.",
    ai: { confidence: 0.91, label: "Flood / High Water" },
    team: {
      name: "Koshi Flood Response",
      status: "Active",
      eta: "Now",
      members: ["Deployed across district"],
    },
    timeline: [
      { label: "Sensor alert", time: "10:45 AM", state: "done" },
      { label: "Authority verification", time: "10:50 AM", state: "done" },
      { label: "Evacuation in progress", time: "11:00 AM", state: "active" },
      { label: "Resolved", time: "—", state: "" },
    ],
  },
];

function getIncidents() {
  return MOCK_INCIDENTS;
}

function getIncidentById(id) {
  return MOCK_INCIDENTS.find((i) => i.id === id) || MOCK_INCIDENTS[0];
}

function iconClassForSeverity(sev) {
  if (sev === "High" || sev === "Critical") return "critical";
  if (sev === "Medium") return "warning";
  if (sev === "Low") return "success";
  return "info";
}

function iconLetterForType(type) {
  if (!type) return "!";
  if (type.includes("Flood")) return "F";
  if (type.includes("Landslide")) return "L";
  if (type.includes("Transformer") || type.includes("Infrastructure")) return "T";
  if (type.includes("Fire")) return "F";
  return "!";
}

function renderIncidentCard(inc) {
  const iconClass = iconClassForSeverity(inc.severity);
  const iconLetter = iconLetterForType(inc.type);
  const badgeClass =
    inc.severity === "High" || inc.priority === "Critical"
      ? "badge-critical"
      : inc.severity === "Medium"
      ? "badge-warning"
      : "badge-info";

  return `
    <a href="incident-details.html?id=${inc.id}" class="incident-card">
      <div class="incident-icon ${iconClass}">${iconLetter}</div>
      <div class="incident-body">
        <div class="incident-title">${inc.title}</div>
        <div class="incident-meta">${inc.location} • ${formatDate(inc.reportedAt)}</div>
      </div>
      <span class="badge ${badgeClass}">${inc.severity}</span>
    </a>
  `;
}

function renderIncidentList(containerId, incidents) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = incidents.map(renderIncidentCard).join("");
}

function renderIncidentsTable(tbodyId, incidents) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  el.innerHTML = incidents
    .map(
      (i) => `
    <tr>
      <td>#${i.id}</td>
      <td>${i.title}</td>
      <td>${i.type}</td>
      <td>${i.location}</td>
      <td>${statusBadge(i.status)}</td>
      <td>${formatDate(i.reportedAt)}</td>
      <td class="actions">
        <a href="incident-details.html?id=${i.id}" class="btn btn-sm btn-outline">View</a>
      </td>
    </tr>`
    )
    .join("");
}