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
  const data = await apiPost("/auth/login", { email, password }, { auth: false });

  TokenStore.set(data.access_token, data.refresh_token);
  TokenStore.setUser(data.user);

  const apiRoles = data.user.roles || [];

  // The portal is a door, not a claim. If the account does not actually hold
  // the role that door is for, refuse here rather than letting them land on a
  // dashboard where every request 403s and nothing explains why.
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
async function register({ email, password, fullName, phone }) {
  await apiPost(
    "/auth/register",
    { email, password, full_name: fullName, phone: phone || undefined },
    { auth: false }
  );
  return login(email, password, null);
}

async function logout() {
  try {
    // Revokes the refresh token server-side. The access token stays valid
    // until it expires - that window is exactly why it is kept short.
    await apiPost("/auth/logout", { refresh_token: TokenStore.getRefresh() });
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
function requireRole(expected) {
  if (!TokenStore.isSignedIn()) {
    sessionStorage.setItem("bn_flash", "Please sign in to continue");
    window.location.replace(loginPathFor(expected));
    return expected;
  }

  const role = localStorage.getItem("bn_role");
  if (expected && role && role !== expected) {
    sessionStorage.setItem("bn_flash", "Redirected to your portal");
    window.location.replace(`${basePath()}pages/${role}/dashboard.html`);
    return role;
  }

  return role || expected;
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
        const svg = `<svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">${ICONS[item.icon] || ""}</svg>`;
        return `<li><a href="${item.href}" class="${active}">${svg}<span>${item.label}</span></a></li>`;
      })
      .join("");
  }

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
  const switcher = document.getElementById("role-switcher");
  if (switcher) {
    switcher.innerHTML = ROLES
      .map((r) => `<option value="${r}" ${r === role ? "selected" : ""}>${r.charAt(0).toUpperCase() + r.slice(1)}</option>`)
      .join("");
    switcher.addEventListener("change", (e) => {
      const newRole = e.target.value;
      setRole(newRole);
      window.location.href = base + "pages/" + newRole + "/dashboard.html";
    });
  }

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

  // --- Mobile toggle ---
  const toggle = document.getElementById("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      document.querySelector(".sidebar").classList.toggle("open");
    });
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