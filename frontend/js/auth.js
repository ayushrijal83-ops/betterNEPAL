// js/auth.js
//
// Session handling and route protection.
//
// A word on what the guard below is and is not. `requireRole` keeps an
// unauthorised user from *seeing a page*; it is not a security boundary. Every
// byte of this runs on the user's machine and can be edited in the console.
// The real enforcement is the backend's @require_roles decorator, which is why
// a tampered localStorage role produces an empty dashboard and a 403 rather
// than somebody else's data. This is UX, not access control.

// ---------- login / register ----------

// Sign in and store the session. `uiRole` is the portal the user came through
// ("guide", "citizen", ...); the backend speaks its own role names.
async function login(email, password, uiRole) {
  const data = await apiPost(
    "/auth/login",
    {
      email,
      password,
      // Names the door, never the claim. The server checks the account against
      // this and returns 403 before issuing any token, so a citizen who has the
      // right password still cannot open the authority portal.
      expected_role: uiRole ? toApiRole(uiRole) : undefined,
    },
    { auth: false }
  );

  TokenStore.set(data.access_token, data.refresh_token);
  TokenStore.setUser(data.user);

  const apiRoles = data.user.roles || [];

  // Unreachable while the server enforces expected_role above - kept as the
  // fallback for an older backend, and because it can name the user's actual
  // portal, which a 403 carrying no user deliberately cannot.
  if (uiRole && !apiRoles.includes(toApiRole(uiRole))) {
    const actual = apiRoles.map(toUiRole).filter(Boolean);
    TokenStore.clear();
    throw new ApiError(
      actual.length
        ? `This account is registered as ${actual.join(", ")}. Use the ${actual[0]} portal.`
        : "This account has no portal access assigned.",
      { status: 403, code: "wrong_portal" }
    );
  }

  const resolved = uiRole || toUiRole(apiRoles[0]) || "citizen";
  localStorage.setItem("bn_role", resolved);
  return { user: data.user, role: resolved };
}

// Public registration only ever creates a citizen account: the backend fixes
// the role server-side and ignores any role in the body. The guide portal uses
// the same endpoint - an admin promotes the account afterwards, which is the
// only route to a guide role.
async function register({
  email,
  password,
  fullName,
  phone,
  permanentDistrictId,
  temporaryDistrictId,
}) {
  await apiPost(
    "/auth/register",
    {
      email,
      password,
      full_name: fullName,
      phone: phone || undefined,
      permanent_district_id: permanentDistrictId,
      // Omitted rather than null: the backend treats absent as "same as
      // permanent", whereas an explicit null would just fail validation.
      temporary_district_id: temporaryDistrictId || undefined,
    },
    { auth: false }
  );
  return login(email, password, null);
}

async function logout() {
  try {
    // Revokes the refresh token server-side. The cookie is sent automatically,
    // so no body is needed. The access token stays valid until it expires;
    // that window is exactly why it is kept short.
    await apiPost("/auth/logout", {});
  } catch (_) {
    // An already-expired session cannot be revoked, and that is fine: we are
    // signing out either way, so a failure here must not trap the user.
  }
  TokenStore.clear();
  sessionStorage.setItem("bn_flash", "Signed out");
  window.location.href = basePath() + "index.html";
}

// ---------- route protection ----------

function loginPathFor(uiRole) {
  return `${basePath()}auth/${uiRole || "citizen"}/login.html`;
}

// Call at the top of every protected page. Redirects to the right login when
// there is no session, or to their own portal when it belongs to another role.
// `expected` is normally one role name; pass an array when a page (e.g. a
// report detail view) is legitimately shared by more than one role.
function requireRole(expected) {
  const allowed = Array.isArray(expected) ? expected : [expected];

  if (!TokenStore.isSignedIn()) {
    sessionStorage.setItem("bn_flash", "Please sign in to continue");
    window.location.replace(loginPathFor(allowed[0]));
    return allowed[0];
  }

  const role = localStorage.getItem("bn_role");
  if (allowed[0] && role && !allowed.includes(role)) {
    sessionStorage.setItem("bn_flash", "Redirected to your portal");
    window.location.replace(`${basePath()}pages/${role}/dashboard.html`);
    return role;
  }

  return role || allowed[0];
}

// Confirms the stored session is still real. Cheap, and it catches a refresh
// token revoked from another device - the page would otherwise render a shell
// that fails on its first data call.
async function verifySession(expected) {
  try {
    const data = await apiGet("/auth/me");
    TokenStore.setUser(data.user);
    return data.user;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      TokenStore.clear();
      sessionStorage.setItem("bn_flash", "Your session has expired");
      window.location.replace(loginPathFor(expected));
    }
    return null;
  }
}

// Loads components/sidebar.html + components/navbar.html, then fills role data
async function mountShell(role) {
  const base = basePath();

  const [sidebarRes, navbarRes] = await Promise.all([
    fetch(base + "components/sidebar.html"),
    fetch(base + "components/navbar.html"),
  ]);

  const sidebarHtml = await sidebarRes.text();
  const navbarHtml = await navbarRes.text();

  const sidebarEl = document.getElementById("app-sidebar");
  const navbarEl = document.getElementById("app-navbar");
  if (sidebarEl) sidebarEl.innerHTML = sidebarHtml;
  if (navbarEl) navbarEl.innerHTML = navbarHtml;

  // --- Sidebar data ---
  const items = (typeof NAV_ITEMS !== "undefined" && NAV_ITEMS[role]) || [];
  const current = window.location.pathname.split("/").pop() || "dashboard.html";
  const navList = document.getElementById("sidebar-nav-list");
  if (navList) {
    navList.innerHTML = items
      .map((item) => {
        const active = item.href === current ? "active" : "";
        const svg = `<svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true">${ICONS[item.icon] || ""}</svg>`;
        // data-i18n carries the key; the English label stays as the fallback
        // so the nav is still readable if the dictionaries fail to load.
        const label = item.i18n
          ? `<span data-i18n="${item.i18n}">${item.label}</span>`
          : `<span>${item.label}</span>`;
        return `<li><a href="${item.href}" class="${active}"${
          active ? ' aria-current="page"' : ""
        }>${svg}${label}</a></li>`;
      })
      .join("");
  }

  // The brand link points at the role's own dashboard rather than always at
  // the public landing page - a signed-in user clicking the logo expects to
  // land back in their workspace.
  const brandLink = document.getElementById("sidebar-brand-link");
  if (brandLink) brandLink.href = "dashboard.html";

  // Language switcher. Mounted here rather than written into navbar.html so
  // the markup comes from one place and stays in step with the i18n runtime.
  const langSlot = document.getElementById("navbar-lang");
  if (langSlot && typeof BN_I18N !== "undefined") {
    langSlot.innerHTML = BN_I18N.switcherMarkup();
  }

  if (typeof BN_I18N !== "undefined") BN_I18N.apply(document);

  const user = getUser();
  const roleLabel = document.getElementById("sidebar-role-label");
  const userAvatar = document.getElementById("sidebar-user-avatar");
  const userName = document.getElementById("sidebar-user-name");
  const userRole = document.getElementById("sidebar-user-role");
  if (roleLabel) roleLabel.textContent = role;
  if (userAvatar) userAvatar.textContent = user.initials;
  if (userName) userName.textContent = user.name;
  if (userRole) userRole.textContent = role;

  // --- Navbar data ---
  // A label, not a control: the account's role is set at login, not chosen
  // from the navbar (see navbar.css for why).
  const navbarRoleBadge = document.getElementById("navbar-role-badge");
  if (navbarRoleBadge) {
    navbarRoleBadge.textContent = role.charAt(0).toUpperCase() + role.slice(1);
  }

  // The toggle button just arrived with the navbar markup above - wire it
  // now that it actually exists in the DOM. theme.js's own DOMContentLoaded
  // listener ran too early to find it.
  if (typeof initThemeToggle === "function") initThemeToggle();

  // --- Sign out ---
  document
    .querySelectorAll("[data-logout], #logout-btn, #sidebar-logout")
    .forEach((el) =>
      el.addEventListener("click", (e) => {
        e.preventDefault();
        logout();
      })
    );

  // Confirm the session in the background. The shell is already painted, so a
  // valid session costs nothing visible and an invalid one redirects.
  verifySession(role).then((user) => {
    if (!user) return;
    const nameEl = document.getElementById("sidebar-user-name");
    const avatarEl = document.getElementById("sidebar-user-avatar");
    if (nameEl) nameEl.textContent = user.full_name;
    if (avatarEl) {
      avatarEl.textContent = (user.full_name || "")
        .split(" ")
        .map((part) => part[0])
        .join("")
        .slice(0, 2)
        .toUpperCase();
    }
  });

  // --- Live alert count ---
  // The navbar used to ship a hardcoded "14 Active Alerts" on every page.
  // This asks the public overview for the real figure and simply stays
  // hidden when there is nothing to report or the call fails - an invented
  // number on a civic platform is worse than no number.
  renderAlertCount(base, role);

  // --- Mobile drawer ---
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.querySelector(".sidebar");
  if (toggle && sidebar) {
    let backdrop = null;

    const close = () => {
      sidebar.classList.remove("open");
      if (backdrop) { backdrop.remove(); backdrop = null; }
      toggle.setAttribute("aria-expanded", "false");
    };

    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const opening = !sidebar.classList.contains("open");
      if (!opening) return close();

      sidebar.classList.add("open");
      toggle.setAttribute("aria-expanded", "true");
      backdrop = document.createElement("div");
      backdrop.className = "sidebar-backdrop";
      backdrop.addEventListener("click", close);
      document.body.appendChild(backdrop);
    });

    // Escape closes the drawer, and following a link inside it should not
    // leave the backdrop stranded over the next page's paint.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && sidebar.classList.contains("open")) close();
    });
    sidebar.addEventListener("click", (e) => {
      if (e.target.closest("a")) close();
    });
  }
}

// Fills the navbar's alert chip from live data, or leaves it hidden.
async function renderAlertCount(base, role) {
  const chip = document.getElementById("navbar-alert-count");
  if (!chip) return;

  try {
    const overview = await apiGet("/analytics/overview", { auth: false });
    const count = overview.incidents.active;
    if (!count) return; // Nothing active: no chip rather than a proud zero.

    const label = chip.querySelector("[data-alert-label]");
    if (label) {
      // Kept as data-* so the string re-renders in the new language when the
      // user switches, instead of freezing at whatever it said on load.
      label.setAttribute("data-i18n", "nav.activeAlerts");
      label.setAttribute("data-i18n-count", String(count));
      label.textContent =
        typeof t === "function"
          ? t("nav.activeAlerts", { count })
          : `${count} active alerts`;
    }
    chip.href = role === "citizen" || role === "guide" ? "alerts.html" : "incidents.html";
    chip.classList.remove("hidden");
  } catch (_) {
    // Stays hidden. A visitor does not need a toast about a decorative chip.
  }
}

// ---------- Flash toast after redirect ----------
(function flash() {
  const flag = sessionStorage.getItem("bn_flash");
  if (!flag) return;
  sessionStorage.removeItem("bn_flash");
  setTimeout(() => {
    if (typeof showToast === "function") {
      showToast(flag, "success");
    }
  }, 300);
})();