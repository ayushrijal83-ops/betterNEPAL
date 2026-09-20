// js/alerts.js
//
// Advisories published by authorities.
//
// This module previously exported a MOCK_ALERTS array of invented warnings
// attached to real places - a monsoon flood warning for Sunsari, a bridge
// closure on the Prithvi Highway at Malekhu. On a civic platform that is not
// placeholder copy: a reader has no way to tell it from a real advisory, and
// the entire value of the product is that its records are trustworthy. It is
// replaced here with the live /announcements endpoint.

// Maps an announcement's severity to the visual class set used by alert cards
// and badges, so a severity reads the same everywhere it appears.
function alertSeverityClass(severity) {
  switch ((severity || "").toLowerCase()) {
    case "critical":
      return "critical";
    case "high":
    case "medium":
      return "warning";
    default:
      return "info";
  }
}

async function fetchAlerts({ districtId = null, limit = 10 } = {}) {
  const params = { per_page: limit };
  if (districtId) params.district_id = districtId;
  const data = await apiGet(`/announcements${queryString(params)}`, { auth: false });
  return data.announcements || [];
}

function renderAlertCard(a) {
  const kind = alertSeverityClass(a.severity);
  const markerClass =
    kind === "critical" ? "np-marker--crimson" : kind === "warning" ? "np-marker--brass" : "";

  return `
    <article class="alert-card ${kind} mb-md">
      <span class="np-marker ${markerClass}" aria-hidden="true">
        <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
          <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4M12 17h.01"/>
        </svg>
      </span>
      <div>
        <div class="alert-title">${escapeHtml(a.title)}</div>
        <div class="alert-meta">
          ${
            // A named district is data and stays as written. The "nationwide"
            // fallback is interface text, so it carries its key and follows a
            // later language switch instead of freezing at render time.
            a.district
              ? escapeHtml(a.district)
              : `<span data-i18n="home.geo.nationwide">${escapeHtml(
                  tr("home.geo.nationwide", "Nationwide")
                )}</span>`
          }
          &middot; ${escapeHtml(formatDate(a.created_at))}
        </div>
      </div>
    </article>
  `;
}

// Loads advisories into a container, using the shared loading, empty and
// error states rather than leaving a blank panel when there is nothing.
async function renderAlertList(containerId, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  renderLoading(containerId);
  try {
    const alerts = await fetchAlerts(options);
    if (!alerts.length) {
      renderEmpty(
        containerId,
        tr("state.empty.title", "Nothing here yet"),
        tr("state.empty.alerts", "No advisories are active for this area.")
      );
      return;
    }
    el.innerHTML = alerts.map(renderAlertCard).join("");
  } catch (error) {
    renderError(
      containerId,
      reportApiError(error, tr("state.error.generic", "We could not load this right now.")),
      () => renderAlertList(containerId, options)
    );
  }
}
