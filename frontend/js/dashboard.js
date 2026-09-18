// js/dashboard.js

// ---------------------------------------------------------------------------
// Analytics loaders
// ---------------------------------------------------------------------------
// Every figure below comes from /analytics, which aggregates in SQL. Nothing
// here fetches a list and counts its length: that would be wrong the moment
// the list is paginated, and it is the difference between one query and
// thousands.

// Headline metrics. Public, so this works signed out too.
async function fetchOverview() {
  return apiGet("/analytics/overview", { auth: false });
}

// Per-authority resolution timing and backlog. Authority/admin only.
async function fetchAuthorityPerformance(authorityId) {
  const data = await apiGet(
    `/analytics/authorities/performance${queryString({ authority_id: authorityId })}`
  );
  return data.authorities || [];
}

async function fetchCategoryDistribution() {
  return apiGet("/analytics/categories", { auth: false });
}

async function fetchDistrictSummary() {
  const data = await apiGet("/analytics/map/districts", { auth: false });
  return data.districts || [];
}

// Load the admin stat row from real aggregates.
async function bootstrapOverviewStats(containerId) {
  renderLoading(containerId, "Loading metrics…");
  try {
    const overview = await fetchOverview();
    renderStats(containerId, [
      { value: overview.reports.total, label: "Total reports" },
      { value: overview.incidents.active, label: "Active incidents" },
      { value: overview.projects.active, label: "Active projects" },
      { value: overview.authorities.total, label: "Authorities" },
    ]);
    return overview;
  } catch (error) {
    renderError(containerId, reportApiError(error, "Could not load metrics."));
    return null;
  }
}

// Authority performance table. `resolution.average_days` is null when nothing
// could be timed, and that is rendered as "—" rather than as 0 - a zero here
// would read as "resolved instantly" instead of "no data".
async function bootstrapAuthorityPerformance(containerId) {
  renderLoading(containerId, "Loading authority performance…");
  try {
    const rows = await fetchAuthorityPerformance();
    if (!rows.length) {
      renderEmpty(containerId, "No authorities yet", "Register an authority to see performance.");
      return [];
    }

    const el = document.getElementById(containerId);
    el.innerHTML = `
      <table class="table">
        <thead>
          <tr>
            <th>Authority</th><th>Assigned</th><th>Backlog</th>
            <th>Resolved</th><th>Avg. days</th><th>Active projects</th>
          </tr>
        </thead>
        <tbody>
          ${rows
            .map((row) => {
              const avg = row.resolution.average_days;
              const unmeasured = row.resolution.unmeasured_count
                ? ` <span class="text-muted">(${row.resolution.unmeasured_count} untimed)</span>`
                : "";
              return `
            <tr>
              <td>${escapeHtml(row.authority_name)}</td>
              <td>${row.assigned_incidents}</td>
              <td>${row.backlog}</td>
              <td>${row.resolved_incidents}</td>
              <td>${avg === null ? "—" : avg}${unmeasured}</td>
              <td>${row.active_projects}</td>
            </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
    return rows;
  } catch (error) {
    renderError(containerId, reportApiError(error, "Could not load performance data."));
    return [];
  }
}

// Renders stat cards into a container
function renderStats(containerId, stats) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!stats || stats.length === 0) {
    renderEmpty(containerId, "No statistics available", "Data will appear once reports come in.");
    return;
  }
  el.innerHTML = stats
    .map(
      (s) => `
    <div class="stat-card">
      <div class="stat-value">${escapeHtml(s.value)}</div>
      <div class="stat-label">${escapeHtml(s.label)}</div>
    </div>`
    )
    .join("");
}

// Renders a reports table into a tbody element
function renderReportsTable(tbodyId, reports) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  if (!reports || reports.length === 0) {
    el.innerHTML = `<tr><td colspan="7">
      <div class="empty-state">
        <div class="empty-title">No reports yet</div>
        <div class="empty-text">Reports you submit will appear here.</div>
      </div>
    </td></tr>`;
    return;
  }
  el.innerHTML = reports
    .map(
      (r) => `
    <tr>
      <td>#${escapeHtml(r.id)}</td>
      <td>${escapeHtml(r.title)}</td>
      <td><span class="badge ${r.badgeClass || "badge-neutral"}">${escapeHtml(r.status)}</span></td>
      <td>${escapeHtml(r.date)}</td>
    </tr>`
    )
    .join("");
}

// ---------- Skeletons ----------
function renderSkeletonList(containerId, rows = 4) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = Array.from({ length: rows })
    .map(
      () => `
    <div style="padding:12px 0;border-bottom:1px solid var(--bn-border)">
      <div class="skeleton title"></div>
      <div class="skeleton line"></div>
      <div class="skeleton line" style="width:70%"></div>
    </div>`
    )
    .join("");
}

function renderSkeletonTable(tbodyId, rows = 4, cols = 5) {
  const el = document.getElementById(tbodyId);
  if (!el) return;
  el.innerHTML = Array.from({ length: rows })
    .map(
      () => `<tr>${Array.from({ length: cols })
        .map(() => `<td><div class="skeleton line"></div></td>`)
        .join("")}</tr>`
    )
    .join("");
}

// ---------- Panel with async loader ----------
async function loadPanel(containerId, loaderFn, { emptyTitle, emptyText, errorMessage } = {}) {
  renderLoading(containerId);
  try {
    const data = await loaderFn();
    if (!data || (Array.isArray(data) && data.length === 0)) {
      renderEmpty(containerId, emptyTitle || "Nothing to show", emptyText || "");
      return null;
    }
    return data;
  } catch (err) {
    console.error(err);
    renderError(containerId, errorMessage || "Could not load data.");
    return null;
  }
}