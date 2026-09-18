// js/projects.js

const MOCK_PROJECTS = [
  {
    id: "PRJ-2026-104",
    name: "Bridge Repair — Trail #104",
    location: "Annapurna Region, Gandaki Province",
    contractor: "Himalayan Infra Pvt. Ltd.",
    status: "In Progress",
    budget: 5000000,
    spent: 3100000,
    progress: 62,
    startDate: "2026-09-20",
    expectedCompletion: "2026-11-15",
    incidentId: "BNP1098",
    description:
      "Structural repair of trekking bridge damaged by rainfall. Includes railing replacement and foundation reinforcement.",
    evidence: [
      { url: "https://images.unsplash.com/photo-1504307651254-35680f356dfd?w=600", label: "Site clearing (Jan 10)" },
      { url: "https://images.unsplash.com/photo-1541888946425-d81bb19240f5?w=600", label: "Foundation (Feb 5)" },
      { url: "https://images.unsplash.com/photo-1581094794329-c8112a89af12?w=600", label: "Rebar (Mar 12)" },
    ],
    milestones: [
      { label: "Project approved", time: "20 Sept 2026", state: "done" },
      { label: "Contractor assigned", time: "22 Sept 2026", state: "done" },
      { label: "Site cleared", time: "01 Oct 2026", state: "done" },
      { label: "Foundation repair", time: "Ongoing", state: "active" },
      { label: "Railing replacement", time: "—", state: "" },
      { label: "Inspection & handover", time: "—", state: "" },
    ],
  },
  {
    id: "PRJ-2026-118",
    name: "Trail Clearance — Manang Landslide",
    location: "Manang District, Gandaki Province",
    contractor: "Everest Roadworks Ltd.",
    status: "In Progress",
    budget: 2500000,
    spent: 900000,
    progress: 35,
    startDate: "2026-09-19",
    expectedCompletion: "2026-10-30",
    incidentId: "BNP1097",
    description:
      "Clearing landslide debris from trekking route and installing safety barriers.",
    evidence: [
      { url: "https://images.unsplash.com/photo-1580659929733-1c2ef6b8b3f0?w=600", label: "Initial landslide" },
      { url: "https://images.unsplash.com/photo-1591122947157-26bad3a117d2?w=600", label: "Clearing in progress" },
    ],
    milestones: [
      { label: "Project approved", time: "19 Sept 2026", state: "done" },
      { label: "Contractor assigned", time: "19 Sept 2026", state: "done" },
      { label: "Debris clearing", time: "Ongoing", state: "active" },
      { label: "Safety barriers", time: "—", state: "" },
      { label: "Final inspection", time: "—", state: "" },
    ],
  },
  {
    id: "PRJ-2026-090",
    name: "Rest Point Reconstruction — ABC Trail",
    location: "Kaski District, Gandaki Province",
    contractor: "Annapurna Builders",
    status: "Completed",
    budget: 1800000,
    spent: 1740000,
    progress: 100,
    startDate: "2026-07-01",
    expectedCompletion: "2026-09-10",
    incidentId: null,
    description: "Reconstruction of damaged rest shelter along Annapurna Base Camp trail.",
    evidence: [
      { url: "https://images.unsplash.com/photo-1501555088652-021faa106b9b?w=600", label: "Before" },
      { url: "https://images.unsplash.com/photo-1522199755839-a2bacb67c546?w=600", label: "After" },
    ],
    milestones: [
      { label: "Project approved", time: "01 Jul 2026", state: "done" },
      { label: "Contractor assigned", time: "03 Jul 2026", state: "done" },
      { label: "Construction", time: "Completed 05 Sept 2026", state: "done" },
      { label: "Inspection", time: "10 Sept 2026", state: "done" },
      { label: "Handover", time: "10 Sept 2026", state: "done" },
    ],
  },
];

function getProjects() {
  return MOCK_PROJECTS;
}

function getProjectById(id) {
  return MOCK_PROJECTS.find((p) => p.id === id) || MOCK_PROJECTS[0];
}

function renderProjectCard(p) {
  return `
    <a href="project-details.html?id=${p.id}" class="project-card">
      <div class="project-name">${p.name}</div>
      <div class="project-meta">${p.contractor} • ${p.location}</div>

      <div class="project-progress">
        <span>Progress</span>
        <span>${p.progress}%</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" style="width: ${p.progress}%"></div>
      </div>

      <div class="flex justify-between mt-md">
        <span class="text-sm text-muted">${statusBadge(p.status)}</span>
        <span class="text-sm"><strong>${formatNPR(p.budget)}</strong></span>
      </div>
    </a>
  `;
}

function renderProjectGrid(containerId, projects) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = projects.map(renderProjectCard).join("");
}

function renderProjectsTable(tbodyId, projects) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  el.innerHTML = projects
    .map(
      (p) => `
    <tr>
      <td>#${p.id}</td>
      <td>${p.name}</td>
      <td>${p.contractor}</td>
      <td>${formatNPR(p.budget)}</td>
      <td>${p.progress}%</td>
      <td>${statusBadge(p.status)}</td>
      <td class="actions">
        <a href="project-details.html?id=${p.id}" class="btn btn-sm btn-outline">View</a>
      </td>
    </tr>`
    )
    .join("");
}