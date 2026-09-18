// js/authorities.js
//
// The registry of government bodies and utilities that own civic problems.

// `filters`: level, type, district_id, search, include_national.
async function fetchAuthorities(filters = {}) {
  const data = await apiGet(`/authorities${queryString(filters)}`, { auth: false });
  return data.authorities || [];
}

async function fetchAuthority(authorityId) {
  const data = await apiGet(`/authorities/${authorityId}`, { auth: false });
  return data.authority;
}

// Plausible authorities for a category and district, ranked, each with the
// reasons it matched. A ranking aid for whoever is routing an incident - it
// assigns nothing, and the reasons are shown so a human can disagree.
async function fetchAuthoritySuggestions(category, districtId) {
  const data = await apiGet(
    `/authorities/suggestions${queryString({ category, district_id: districtId })}`,
    { auth: false }
  );
  return data.suggestions || [];
}

async function createAuthority({ name, level, type, contactEmail, contactPhone, districtId }) {
  const data = await apiPost("/authorities", {
    name,
    level,
    type,
    contact_email: contactEmail || null,
    contact_phone: contactPhone || null,
    district_id: districtId || null,
  });
  return data.authority;
}

function renderAuthoritiesTable(tbodyId, authorities) {
  const el = document.getElementById(tbodyId);
  if (!el) return;

  if (!authorities || authorities.length === 0) {
    el.innerHTML = `<tr><td colspan="6">
      <div class="empty-state">
        <div class="empty-title">No authorities registered</div>
        <div class="empty-text">An admin can add government bodies here.</div>
      </div>
    </td></tr>`;
    return;
  }

  el.innerHTML = authorities
    .map(
      (a) => `
    <tr>
      <td>${escapeHtml(a.name)}</td>
      <td>${escapeHtml(a.district || (a.is_national ? "National" : "—"))}</td>
      <td>${escapeHtml(humanise(a.level))}</td>
      <td>${escapeHtml(humanise(a.type))}</td>
      <td>${escapeHtml(a.contact_phone || "Not listed")}</td>
      <td>${escapeHtml(a.contact_email || "Not listed")}</td>
    </tr>`
    )
    .join("");
}

// Options for an "assign to authority" select. Includes national bodies
// alongside the district's own, since a national authority can own a problem
// inside a district without being scoped to it.
async function renderAuthorityOptions(selectId, districtId) {
  const el = document.getElementById(selectId);
  if (!el) return [];

  try {
    const authorities = await fetchAuthorities({
      district_id: districtId,
      include_national: districtId ? "true" : "",
      per_page: 100,
    });

    el.innerHTML =
      `<option value="">Select an authority…</option>` +
      authorities
        .map(
          (a) =>
            `<option value="${a.id}">${escapeHtml(a.name)} (${escapeHtml(
              humanise(a.level)
            )})</option>`
        )
        .join("");
    return authorities;
  } catch (error) {
    reportApiError(error, "Could not load authorities.");
    return [];
  }
}

async function bootstrapAuthoritiesPage(tbodyId) {
  const el = document.getElementById(tbodyId);
  if (el) {
    el.innerHTML = `<tr><td colspan="6"><div class="loading-state"><span class="spinner lg"></span><span>Loading authorities…</span></div></td></tr>`;
  }
  try {
    renderAuthoritiesTable(tbodyId, await fetchAuthorities({ per_page: 100 }));
  } catch (error) {
    const message = reportApiError(error, "Could not load authorities.");
    if (el) el.innerHTML = `<tr><td colspan="6"><div class="error-state">${escapeHtml(message)}</div></td></tr>`;
  }
}
