// js/districts.js
// ---------------------------------------------------------------------------
// Nepal's 77 districts, and a searchable picker for them.
//
// A plain <select> with 77 options is a poor control on a phone, which is where
// most of these registrations will happen - so this is a filterable combobox
// built on a real <input>, keyboard-navigable, with the chosen id kept in a
// hidden field so normal form handling still works.
// ---------------------------------------------------------------------------

let districtCache = null;

// One fetch per page, shared by every picker on it. The list changes about
// once a decade, so re-fetching it per dropdown would be pure waste.
async function loadDistricts() {
  if (districtCache) return districtCache;

  const data = await apiGet("/map/districts", { auth: false });
  districtCache = (data.districts || []).map((d) => ({
    id: d.id,
    name: d.name,
    province: d.province || "",
  }));
  return districtCache;
}

function districtById(id) {
  return (districtCache || []).find((d) => d.id === id) || null;
}

/**
 * Turn a container into a searchable district picker.
 *
 * The container must hold an <input data-district-search> and an
 * <input type="hidden" data-district-id>. The hidden field is the value the
 * form submits; the visible one is only ever a search box, so a half-typed
 * district name can never be mistaken for a selection.
 */
async function mountDistrictPicker(containerId, { onSelect, initialId } = {}) {
  const container = document.getElementById(containerId);
  if (!container) return null;

  const search = container.querySelector("[data-district-search]");
  const hidden = container.querySelector("[data-district-id]");
  const list = container.querySelector("[data-district-list]");
  if (!search || !hidden || !list) return null;

  let districts = [];
  try {
    districts = await loadDistricts();
  } catch (error) {
    search.placeholder = "Could not load districts";
    search.disabled = true;
    reportApiError(error, "Could not load the district list.");
    return null;
  }

  let highlighted = -1;
  let matches = [];

  function close() {
    list.classList.add("hidden");
    highlighted = -1;
  }

  function choose(district) {
    hidden.value = district.id;
    search.value = district.name;
    search.dataset.chosen = district.name;
    close();
    if (typeof onSelect === "function") onSelect(district);
  }

  function render(query) {
    const needle = query.trim().toLowerCase();
    matches = needle
      ? districts.filter(
          (d) =>
            d.name.toLowerCase().includes(needle) ||
            d.province.toLowerCase().includes(needle)
        )
      : districts;

    if (!matches.length) {
      list.innerHTML =
        `<div class="district-results-empty">${escapeHtml(
          typeof t === "function" ? t("map.noResults") : "No district matches that."
        )}</div>`;
      list.classList.remove("hidden");
      return;
    }

    list.innerHTML = matches
      .slice(0, 60)
      .map(
        (d, index) => `
        <button type="button" data-index="${index}"
          class="district-result${index === highlighted ? " is-highlighted" : ""}">
          <span class="district-name">${escapeHtml(d.name)}</span>
          <span class="district-province">${escapeHtml(d.province)}</span>
        </button>`
      )
      .join("");

    list.querySelectorAll("button").forEach((button) =>
      button.addEventListener("mousedown", (e) => {
        // mousedown, not click: blur fires first and would close the list
        // before a click could land.
        e.preventDefault();
        choose(matches[Number(button.dataset.index)]);
      })
    );
    list.classList.remove("hidden");
  }

  search.addEventListener("focus", () => render(search.value));
  search.addEventListener("input", () => {
    // Typing invalidates any previous choice: the hidden id must never keep
    // pointing at a district the visible text no longer shows.
    hidden.value = "";
    delete search.dataset.chosen;
    render(search.value);
  });

  search.addEventListener("keydown", (e) => {
    if (list.classList.contains("hidden")) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      highlighted = Math.max(
        0,
        Math.min(matches.length - 1, highlighted + (e.key === "ArrowDown" ? 1 : -1))
      );
      render(search.value);
      list.children[highlighted]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && highlighted >= 0) {
      e.preventDefault();
      choose(matches[highlighted]);
    } else if (e.key === "Escape") {
      close();
    }
  });

  search.addEventListener("blur", () => {
    setTimeout(() => {
      close();
      // Restore the last real selection, so leaving a half-typed name behind
      // does not look like a choice that was made.
      if (!hidden.value && search.dataset.chosen) search.value = search.dataset.chosen;
      else if (!hidden.value) search.value = "";
    }, 120);
  });

  if (initialId) {
    const initial = districts.find((d) => d.id === initialId);
    if (initial) choose(initial);
  }

  return {
    value: () => hidden.value || null,
    clear: () => {
      hidden.value = "";
      search.value = "";
      delete search.dataset.chosen;
    },
  };
}

// The markup a picker needs. Kept here so the three pages that use one cannot
// drift apart in structure.
function districtPickerMarkup(id, { label, hint = "", required = false } = {}) {
  return `
    <div id="${id}" class="form-group district-picker">
      <label for="${id}-search" class="form-label">
        ${escapeHtml(label)}${required ? ' <span class="req">*</span>' : ""}
      </label>
      <input id="${id}-search" data-district-search type="text" autocomplete="off"
        class="form-input"
        placeholder="${escapeHtml(typeof t === "function" ? t("common.search") : "Search")}" />
      <input type="hidden" data-district-id />
      <div data-district-list class="district-results hidden"></div>
      ${hint ? `<p class="form-hint">${escapeHtml(hint)}</p>` : ""}
    </div>`;
}

