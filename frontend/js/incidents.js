// js/incidents.js
//
// Incident listing, verification and authority routing.
//
// Note what is absent: nothing here decides anything. Promoting a report and
// choosing which authority owns it are both human actions, and the backend
// records who performed each. The UI's job is to make those choices quick, not
// to make them automatically.

// ---------- reads ----------

// `filters`: status, severity, category, district_id, authority_id, page.
async function fetchIncidents(filters = {}) {
  const data = await apiGet(`/incidents${queryString(filters)}`);
  return data.incidents || [];
}

async function fetchIncident(incidentId) {
  const data = await apiGet(`/incidents/${incidentId}`);
  return data.incident;
}

// Unlinked reports near an incident, already filtered by proximity and then by
// the AI. The response says which proximity method produced it - pass it on so
// a caller can tell an exact PostGIS answer from the approximate fallback.
async function fetchNearbyReports(incidentId, radius) {
  return apiGet(`/incidents/${incidentId}/nearby-reports${queryString({ radius })}`);
}

// ---------- writes ----------

// Turn a verified report into an incident. Coordinates, category and district
// are inherited from the report server-side; only the human's judgement
// (severity, and optionally a better title) is sent.
async function promoteReport(reportId, { severity, title, description } = {}) {
  const data = await apiPost("/incidents/from-report", {
    report_id: reportId,
    severity,
    title,
    description,
  });
  return data.incident;
}

// Gather an additional report under an existing incident.
async function linkReport(incidentId, reportId) {
  const data = await apiPost(`/incidents/${incidentId}/link-report`, {
    report_id: reportId,
  });
  return data.incident;
}

// Route an incident to the body responsible for fixing it. An OPEN incident
// becomes IN_PROGRESS as a side effect - having an owner is the work starting.
async function assignIncident(incidentId, authorityId) {
  const data = await apiPost(`/incidents/${incidentId}/assign`, {
    authority_id: authorityId,
  });
  return data.incident;
}

async function updateIncident(incidentId, { status, severity } = {}) {
  const data = await apiPatch(`/incidents/${incidentId}`, { status, severity });
  return data.incident;
}

// ---------- rendering ----------

function iconClassForSeverity(severity) {
  const map = { critical: "critical", high: "critical", medium: "warning", low: "success" };
  return map[String(severity).toLowerCase()] || "info";
}

function iconLetterForCategory(category) {
  const map = {
    road_damage: "R",
    water_leak: "W",
    waste_management: "G",
    electricity: "E",
    public_property: "P",
    natural_disaster: "H",
  };
  return map[category] || "!";
}

function severityBadge(severity) {
  const map = {
    critical: "badge-critical",
    high: "badge-critical",
    medium: "badge-warning",
    low: "badge-info",
  };
  const cls = map[String(severity).toLowerCase()] || "badge-neutral";
  return `<span class="badge ${cls}">${escapeHtml(humanise(severity))}</span>`;
}

function renderIncidentCard(inc, detailHref = "incident-details.html") {
  const location = inc.district || "Location pending";
  return `
    <a href="${detailHref}?id=${encodeURIComponent(inc.id)}" class="incident-card">
      <div class="incident-icon ${iconClassForSeverity(inc.severity)}">${iconLetterForCategory(
        inc.category
      )}</div>
      <div class="incident-body">
        <div class="incident-title">${escapeHtml(inc.title)}</div>
        <div class="incident-meta">${escapeHtml(location)} • ${escapeHtml(
          formatDate(inc.created_at)
        )}</div>
      </div>
      ${severityBadge(inc.severity)}
    </a>
  `;
}

function renderIncidentList(containerId, incidents, detailHref = "incident-details.html") {
  const el = document.getElementById(containerId);
  if (!el) return;

  if (!incidents || incidents.length === 0) {
    renderEmpty(containerId, "No incidents", "Verified problems will appear here.");
    return;
  }
  el.innerHTML = incidents.map((inc) => renderIncidentCard(inc, detailHref)).join("");
}

function renderIncidentsTable(tbodyId, incidents, detailHref = "incident-details.html") {
  const el = document.getElementById(tbodyId);
  if (!el) return;

  if (!incidents || incidents.length === 0) {
    el.innerHTML = `<tr><td colspan="7">
      <div class="empty-state">
        <div class="empty-title">No incidents match these filters</div>
        <div class="empty-text">Try clearing the status or severity filter.</div>
      </div>
    </td></tr>`;
    return;
  }

  el.innerHTML = incidents
    .map(
      (i) => `
    <tr>
      <td>#${escapeHtml(String(i.id).slice(0, 8))}</td>
      <td>${escapeHtml(i.title)}</td>
      <td>${escapeHtml(humanise(i.category))}</td>
      <td>${escapeHtml(i.district || "—")}</td>
      <td>${severityBadge(i.severity)}</td>
      <td>${statusBadge(i.status)}</td>
      <td>${escapeHtml(i.authority ? i.authority.name : "Unassigned")}</td>
      <td class="actions">
        <a href="${detailHref}?id=${encodeURIComponent(
          i.id
        )}" class="btn btn-sm btn-outline">View</a>
        ${
          // A closed incident is terminal, so offering Assign there would only
          // produce a 409 the user cannot act on.
          i.status === "closed"
            ? ""
            : `<button class="btn btn-sm btn-primary" data-assign="${i.id}" data-district="${
                i.district_id || ""
              }">${i.authority_id ? "Reassign" : "Assign"}</button>`
        }
      </td>
    </tr>`
    )
    .join("");
}

// ---------- page bootstrap ----------

// Wire the authority incidents page: load, filter, and re-render.
async function bootstrapIncidentsPage(tbodyId, { statusFilterId, severityFilterId } = {}) {
  const el = document.getElementById(tbodyId);

  async function load() {
    if (el) {
      el.innerHTML = `<tr><td colspan="8"><div class="loading-state"><span class="spinner lg"></span><span>Loading incidents…</span></div></td></tr>`;
    }
    try {
      // Filtering happens server-side: sending the filter is one indexed query,
      // whereas fetching everything and filtering here ships the whole table to
      // the browser and breaks as soon as there is more than one page of it.
      const incidents = await fetchIncidents({
        status: statusFilterId ? document.getElementById(statusFilterId)?.value : "",
        severity: severityFilterId ? document.getElementById(severityFilterId)?.value : "",
        per_page: 100,
      });
      renderIncidentsTable(tbodyId, incidents);
    } catch (error) {
      const message = reportApiError(error, "Could not load incidents.");
      if (el) {
        el.innerHTML = `<tr><td colspan="8"><div class="error-state">${escapeHtml(
          message
        )}</div></td></tr>`;
      }
    }
  }

  [statusFilterId, severityFilterId].forEach((id) => {
    const filter = id && document.getElementById(id);
    if (filter) filter.addEventListener("change", load);
  });

  bindAssignButtons(tbodyId, load);
  await load();
}


// ---------- assignment UI ----------

// Open the shared modal with a list of candidate authorities. Suggestions come
// first, each showing why it matched, so the person routing sees the reasoning
// and can disagree - the backend never assigns anything on its own.
async function openAssignDialog(incidentId, districtId, onDone) {
  let incident;
  try {
    incident = await fetchIncident(incidentId);
  } catch (error) {
    reportApiError(error, "Could not load that incident.");
    return;
  }

  let suggestions = [];
  let authorities = [];
  try {
    [suggestions, authorities] = await Promise.all([
      fetchAuthoritySuggestions(incident.category, districtId || undefined),
      fetchAuthorities({ district_id: districtId || "", include_national: districtId ? "true" : "", per_page: 100 }),
    ]);
  } catch (error) {
    reportApiError(error, "Could not load authorities.");
    return;
  }

  if (!authorities.length) {
    showToast("No authorities are registered yet. An admin must add one first.", "error", 6000);
    return;
  }

  const suggestedIds = new Set(suggestions.map((s) => s.id));
  const rest = authorities.filter((a) => !suggestedIds.has(a.id));

  const options = [
    ...suggestions.map(
      (s) =>
        `<option value="${s.id}">${escapeHtml(s.name)} — ${escapeHtml(
          s.match_reasons.join(", ")
        )}</option>`
    ),
    ...rest.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}</option>`),
  ].join("");

  openModal({
    title: "Assign to an authority",
    bodyHtml: `
      <div class="form-group">
        <label class="form-label" for="assign-authority">${escapeHtml(incident.title)}</label>
        <select class="form-select" id="assign-authority">${options}</select>
        <div class="form-hint">
          Suggestions are ranked by category and district. They are a shortlist,
          not a decision — your choice is what gets recorded.
        </div>
      </div>`,
    confirmText: "Assign",
    onConfirm: async () => {
      const select = document.getElementById("assign-authority");
      if (!select || !select.value) return;
      try {
        await assignIncident(incidentId, select.value);
        showToast("Incident assigned", "success");
        if (typeof onDone === "function") onDone();
      } catch (error) {
        reportApiError(error, "Could not assign the incident.");
      }
    },
  });
}

// Delegated so it survives the table being re-rendered after a filter change.
function bindAssignButtons(tbodyId, onDone) {
  const el = document.getElementById(tbodyId);
  if (!el || el.dataset.assignBound) return;
  el.dataset.assignBound = "true";

  el.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-assign]");
    if (!btn) return;
    e.preventDefault();
    openAssignDialog(btn.dataset.assign, btn.dataset.district, onDone);
  });
}
