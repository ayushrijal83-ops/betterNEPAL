// js/reports.js
//
// Report creation, listing and evidence upload.
//
// REPORT_CATEGORIES lives in config.js now, mirroring the backend enum. It is
// not redefined here: two lists of categories that drift apart is exactly how a
// citizen ends up filing a report under a value the server rejects.

// ---------- reads ----------

// `filters` maps straight onto the backend query parameters: category, status,
// district_id, reporter_id, page, per_page.
async function fetchReports(filters = {}) {
  const data = await apiGet(`/reports${queryString(filters)}`);
  return data.reports || [];
}

// The signed-in user's own reports. The backend has no "mine" shortcut, so we
// filter by the id /auth/me gave us rather than pulling everything and sifting
// client-side - that would ship every citizen's reports to every browser.
async function fetchMyReports(filters = {}) {
  const user = TokenStore.getUser() || (await apiGet("/auth/me")).user;
  return fetchReports({ ...filters, reporter_id: user.id, per_page: 100 });
}

async function fetchReport(reportId) {
  const data = await apiGet(`/reports/${reportId}`);
  return data.report;
}

async function fetchReportMedia(reportId) {
  const data = await apiGet(`/media/entity/report/${reportId}`);
  return data.media || [];
}

// ---------- writes ----------

// Create a report. Coordinates are required by the backend; everything about
// where it happened is derived from them server-side, so no district is sent.
async function createReport({ title, description, category, lat, lng }) {
  const data = await apiPost("/reports", {
    title,
    description,
    category,
    lat,
    lng,
  });
  return data.report;
}

// Attach evidence to a record. Returns null rather than throwing when there is
// no file: a report with no photo is still a perfectly good report.
async function uploadEvidence(file, entityType, entityId) {
  if (!file) return null;

  const form = new FormData();
  form.append("file", file);
  form.append("entity_type", entityType);
  form.append("entity_id", entityId);

  const data = await apiPost("/media/upload", form);
  return data.media;
}

// ---------- geolocation ----------

// Ask the browser where we are. Resolves to null instead of rejecting when the
// user declines: refusing to share location is a choice, not an error, and the
// caller should fall back to picking a point on the map.
function getCurrentPosition({ timeout = 10000 } = {}) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (position) =>
        resolve({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy: position.coords.accuracy,
        }),
      () => resolve(null),
      { enableHighAccuracy: true, timeout, maximumAge: 60000 }
    );
  });
}

// ---------- rendering ----------

function renderTimeline(items) {
  return `
    <ul class="timeline">
      ${items
        .map(
          (t) => `
        <li class="timeline-item ${t.state || ""}">
          <span class="timeline-dot"></span>
          <div class="timeline-title">${escapeHtml(t.label)}</div>
          <div class="timeline-meta">${escapeHtml(t.time)}</div>
        </li>`
        )
        .join("")}
    </ul>
  `;
}

// Build a lifecycle view from a report's real status. The backend stores one
// status, not a history, so this shows where the report has reached rather
// than inventing timestamps for steps it has no record of.
function reportTimeline(report) {
  const order = ["submitted", "under_review", "verified_as_incident"];
  const labels = {
    submitted: "Submitted by reporter",
    under_review: "Under authority review",
    verified_as_incident: "Verified as an incident",
  };

  if (report.status === "rejected") {
    return renderTimeline([
      { label: labels.submitted, time: formatDate(report.created_at), state: "done" },
      {
        label: report.is_duplicate ? "Merged as a duplicate" : "Rejected after review",
        time: formatDate(report.updated_at),
        state: "done",
      },
    ]);
  }

  const reached = order.indexOf(report.status);
  return renderTimeline(
    order.map((key, index) => ({
      label: labels[key],
      time:
        index === 0
          ? formatDate(report.created_at)
          : index <= reached
            ? formatDate(report.updated_at)
            : "Pending",
      state: index < reached ? "done" : index === reached ? "active" : "",
    }))
  );
}

function statusBadge(status) {
  const cls = (typeof STATUS_BADGE !== "undefined" && STATUS_BADGE[status]) || "badge-neutral";
  return `<span class="badge ${cls}">${escapeHtml(humanise(status))}</span>`;
}

function severityClass(sev) {
  const map = { critical: "sev-high", high: "sev-high", medium: "sev-medium", low: "sev-low" };
  return map[String(sev).toLowerCase()] || "sev-low";
}

function renderCategoryOptions(selected) {
  return REPORT_CATEGORIES.map(
    (c) =>
      `<option value="${c.value}"${c.value === selected ? " selected" : ""}>${escapeHtml(
        c.label
      )}</option>`
  ).join("");
}

// Shared table renderer. `detailHref` differs per portal (citizen vs guide),
// so it is passed in rather than guessed from the URL.
function renderReportsTable(tbodyId, reports, detailHref = "report-details.html") {
  const el = document.getElementById(tbodyId);
  if (!el) return;

  if (!reports || reports.length === 0) {
    el.innerHTML = `<tr><td colspan="7">
      <div class="empty-state">
        <div class="empty-title">${tr("state.empty.title","Nothing here yet")}</div>
        <div class="empty-text">${tr("state.empty.reportsHint","Reports you submit will appear here.")}</div>
      </div>
    </td></tr>`;
    return;
  }

  el.innerHTML = reports
    .map((r) => {
      const location = r.district || "Location pending";
      return `
    <tr>
      <td>#${escapeHtml(String(r.id).slice(0, 8))}</td>
      <td>${escapeHtml(r.title)}</td>
      <td>${escapeHtml(humanise(r.category))}</td>
      <td>${escapeHtml(location)}</td>
      <td>${statusBadge(r.status)}</td>
      <td>${escapeHtml(formatDate(r.created_at))}</td>
      <td class="actions">
        <a href="${detailHref}?id=${encodeURIComponent(r.id)}" class="btn btn-sm btn-outline">View</a>
      </td>
    </tr>`;
    })
    .join("");
}

// ---------- page bootstrap ----------

// Load and render the signed-in user's reports.
async function bootstrapMyReports(tbodyId, detailHref = "report-details.html") {
  const el = document.getElementById(tbodyId);
  if (el) {
    el.innerHTML = `<tr><td colspan="7"><div class="loading-state"><span class="spinner lg"></span><span>Loading your reports…</span></div></td></tr>`;
  }

  try {
    renderReportsTable(tbodyId, await fetchMyReports(), detailHref);
  } catch (error) {
    const message = reportApiError(error, "Could not load your reports.");
    if (el) el.innerHTML = `<tr><td colspan="7"><div class="error-state">${escapeHtml(message)}</div></td></tr>`;
  }
}
