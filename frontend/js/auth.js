// js/auth.js

function requireRole(expected) {
  const role = localStorage.getItem("bn_role");
  if (!role) {
    localStorage.setItem("bn_role", expected);
    return expected;
  }
  return role;
}

function loginAs(role, redirectTo) {
  localStorage.setItem("bn_role", role);
  sessionStorage.setItem("bn_flash", "Signed in successfully");
  window.location.href = redirectTo;
}

function logout() {
  localStorage.removeItem("bn_role");
  sessionStorage.setItem("bn_flash", "Signed out");
  window.location.href = basePath() + "index.html";
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