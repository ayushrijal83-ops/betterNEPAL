// js/dashboard.js

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