// js/projects.js
//
// Projects and the progress ledger.
//
// Two fields the prototype showed have no backend counterpart, and are not
// faked here:
//
//   budget   - Phase 8 deliberately has no budget column. The brief requires
//              financial figures to come from authoritative records, so a
//              number typed beside progress notes is exactly what it forbids.
//   progress - there is no percentage column either. A project has a status
//              and an append-only ledger; a "62%" with nothing behind it is a
//              number somebody will end up reporting upward as fact.
//
// The card below therefore shows status and ledger activity, which are real.

// ---------- reads ----------

// `filters`: status, authority_id, contractor_id, incident_id, unassigned.
async function fetchProjects(filters = {}) {
  const data = await apiGet(`/projects${queryString(filters)}`);
  return data.projects || [];
}

async function fetchProject(projectId) {
  const data = await apiGet(`/projects/${projectId}`);
  return data.project;
}

// Projects awarded to the signed-in contractor. Filtered server-side by id, so
// one contractor never receives another's workload.
async function fetchMyProjects(filters = {}) {
  const user = TokenStore.getUser() || (await apiGet("/auth/me")).user;
  return fetchProjects({ ...filters, contractor_id: user.id, per_page: 100 });
}

async function fetchProgressUpdates(projectId) {
  const data = await apiGet(`/projects/${projectId}/updates`);
  return data.updates || [];
}

// ---------- writes ----------

// Append to the ledger. Passing `newStatus` makes it a status change, recorded
// with both the old and new value in the same transaction.
async function postProgressUpdate(projectId, notes, newStatus) {
  const data = await apiPost(`/projects/${projectId}/updates`, {
    notes,
    new_status: newStatus || undefined,
  });
  // A status change returns the project; a routine note returns the entry.
  return data.project || data.update;
}

async function assignProjectContractor(projectId, contractorId) {
  const data = await apiPatch(`/projects/${projectId}/contractor`, {
    contractor_id: contractorId,
  });
  return data.project;
}

// ---------- rendering ----------

// Status is the honest signal of how far along a project is, since there is no
// stored percentage. The bar reflects lifecycle position, and is labelled as
// such rather than as a completion figure.
const PROJECT_STAGE = {
  planned: { percent: 10, label: "Planned" },
  active: { percent: 50, label: "In progress" },
  on_hold: { percent: 50, label: "On hold" },
  completed: { percent: 100, label: "Completed" },
  cancelled: { percent: 0, label: "Cancelled" },
};

function projectStage(status) {
  return PROJECT_STAGE[status] || { percent: 0, label: humanise(status) };
}

function renderProjectCard(p, detailHref = "project-details.html") {
  const stage = projectStage(p.status);
  const contractor = p.contractor ? p.contractor.full_name : tr("project.noContractor", "No contractor assigned");
  const overdue = p.is_overdue
    ? `<span class="badge badge-critical">${tr("status.overdue","Overdue")}</span>`
    : "";

  return `
    <a href="${detailHref}?id=${encodeURIComponent(p.id)}" class="project-card">
      <div class="project-name">${escapeHtml(p.title)}</div>
      <div class="project-meta">${escapeHtml(contractor)} • ${escapeHtml(
        p.authority || "—"
      )}</div>

      <div class="project-progress">
        <span>Stage</span>
        <span>${escapeHtml(stage.label)}</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" style="width: ${stage.percent}%"></div>
      </div>

      <div class="flex justify-between mt-md">
        <span class="text-sm text-muted">${statusBadge(p.status)} ${overdue}</span>
        <span class="text-sm text-muted">${p.update_count} update${
          p.update_count === 1 ? "" : "s"
        }</span>
      </div>
    </a>
  `;
}

function renderProjectGrid(containerId, projects, detailHref = "project-details.html") {
  const el = document.getElementById(containerId);
  if (!el) return;

  if (!projects || projects.length === 0) {
    renderEmpty(containerId, "No projects", "Work assigned to you will appear here.");
    return;
  }
  el.innerHTML = projects.map((p) => renderProjectCard(p, detailHref)).join("");
}

function renderProjectsTable(tbodyId, projects, detailHref = "project-details.html") {
  const el = document.getElementById(tbodyId);
  if (!el) return;

  if (!projects || projects.length === 0) {
    el.innerHTML = `<tr><td colspan="7">
      <div class="empty-state">
        <div class="empty-title">${tr("state.empty.title","Nothing here yet")}</div>
        <div class="empty-text">${tr("state.empty.projectsHint","Work assigned to you will appear here.")}</div>
      </div>
    </td></tr>`;
    return;
  }

  el.innerHTML = projects
    .map(
      (p) => `
    <tr>
      <td>#${escapeHtml(String(p.id).slice(0, 8))}</td>
      <td>${escapeHtml(p.title)}</td>
      <td>${escapeHtml(p.authority || "—")}</td>
      <td>${escapeHtml(p.contractor ? p.contractor.full_name : "Unassigned")}</td>
      <td>${escapeHtml(p.estimated_end_date || "—")}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="actions">
        <a href="${detailHref}?id=${encodeURIComponent(
          p.id
        )}" class="btn btn-sm btn-outline">View</a>
      </td>
    </tr>`
    )
    .join("");
}

// The ledger, newest first, exactly as the backend returns it.
function renderProgressLedger(containerId, updates) {
  const el = document.getElementById(containerId);
  if (!el) return;

  if (!updates || updates.length === 0) {
    renderEmpty(containerId, "No updates yet", "Progress notes will appear here.");
    return;
  }

  el.innerHTML = `
    <ul class="timeline">
      ${updates
        .map((u) => {
          const transition = u.is_status_change
            ? ` <span class="badge badge-info">${escapeHtml(
                humanise(u.previous_status)
              )} → ${escapeHtml(humanise(u.new_status))}</span>`
            : "";
          return `
        <li class="timeline-item done">
          <span class="timeline-dot"></span>
          <div class="timeline-title">${escapeHtml(u.notes)}${transition}</div>
          <div class="timeline-meta">${escapeHtml(
            u.author.full_name || "Unknown"
          )} · ${escapeHtml(formatDate(u.created_at))} ${escapeHtml(
            formatTime(u.created_at)
          )}</div>
        </li>`;
        })
        .join("")}
    </ul>
  `;
}

// ---------- page bootstrap ----------

async function bootstrapMyProjects(gridId, tbodyId, detailHref = "project-details.html") {
  if (gridId) renderLoading(gridId, "Loading your projects…");

  try {
    const projects = await fetchMyProjects();
    if (gridId) renderProjectGrid(gridId, projects, detailHref);
    if (tbodyId) renderProjectsTable(tbodyId, projects, detailHref);
    return projects;
  } catch (error) {
    const message = reportApiError(error, "Could not load your projects.");
    if (gridId) renderError(gridId, message);
    return [];
  }
}
