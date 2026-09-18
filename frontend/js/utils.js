// js/utils.js

function qs(sel, root = document) {
  return root.querySelector(sel);
}

function qsa(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

function getRole() {
  return localStorage.getItem("bn_role") || "citizen";
}

function setRole(role) {
  localStorage.setItem("bn_role", role);
}

function getUser() {
  // Read the real session. The placeholder shows only in the moment before
  // /auth/me returns, so a signed-in user never sees another person's name
  // baked into the markup.
  const stored =
    typeof TokenStore !== "undefined" && TokenStore.getUser ? TokenStore.getUser() : null;
  const name = (stored && stored.full_name) || "Signing in…";
  return {
    name,
    email: (stored && stored.email) || "",
    roles: (stored && stored.roles) || [],
    initials: name
      .split(" ")
      .map((n) => n[0])
      .join("")
      .slice(0, 2)
      .toUpperCase(),
  };
}

function formatDate(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  return d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function formatTime(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function formatNPR(amount) {
  return "NPR " + Number(amount).toLocaleString("en-IN");
}

function generateId(prefix = "BNP") {
  return prefix + Math.floor(1000 + Math.random() * 9000);
}

function basePath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  const idx = parts.indexOf("frontend");
  const depthFromRoot = idx >= 0 ? parts.length - idx - 2 : parts.length - 1;
  return "../".repeat(Math.max(0, depthFromRoot));
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------- UI helper components ----------

function renderLoading(containerId, message = "Loading…") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="loading-state">
      <span class="spinner lg"></span>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function renderEmpty(containerId, title = "Nothing here yet", text = "") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">
        <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
      </div>
      <div class="empty-title">${escapeHtml(title)}</div>
      ${text ? `<div class="empty-text">${escapeHtml(text)}</div>` : ""}
    </div>
  `;
}

function renderError(containerId, message = "Something went wrong.") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<div class="error-state">${escapeHtml(message)}</div>`;
}

// ---------- Toast ----------
function ensureToastContainer() {
  let c = document.getElementById("bn-toast-container");
  if (!c) {
    c = document.createElement("div");
    c.id = "bn-toast-container";
    c.className = "toast-container";
    document.body.appendChild(c);
  }
  return c;
}

function showToast(message, type = "info", duration = 3000) {
  const container = ensureToastContainer();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transition = "opacity 0.3s";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ---------- Modal ----------
async function openModal({ title, bodyHtml, confirmText = "Confirm", cancelText = "Cancel", onConfirm }) {
  const base = basePath();
  // Try to use the shared template first; fall back to inline markup
  let tpl = "";
  try {
    const res = await fetch(base + "components/modal.html");
    if (res.ok) tpl = await res.text();
  } catch (_) { /* ignore */ }

  const wrapper = document.createElement("div");
  wrapper.className = "modal-backdrop";
  // Strip the template's leading comment before substituting. It lists the
  // placeholder names verbatim, and String.replace with a string pattern
  // replaces only the FIRST match - which was the one inside the comment, so
  // every modal rendered a literal "{{title}}". Global regexes, and no comment
  // to trip over.
  const body = (tpl || "").replace(/<!--[\s\S]*?-->/g, "");

  wrapper.innerHTML = body.includes("modal-title")
    ? body
        .replace(/\{\{\s*title\s*\}\}/g, escapeHtml(title))
        .replace(/\{\{\s*body\s*\}\}/g, bodyHtml)
        .replace(/\{\{\s*confirmText\s*\}\}/g, escapeHtml(confirmText))
        .replace(/\{\{\s*cancelText\s*\}\}/g, escapeHtml(cancelText))
    : `
      <div class="modal">
        <div class="modal-header">
          <div class="modal-title">${escapeHtml(title)}</div>
          <button class="modal-close" data-close>&times;</button>
        </div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-footer">
          <button class="btn btn-outline" data-close>${escapeHtml(cancelText)}</button>
          <button class="btn btn-primary" data-confirm>${escapeHtml(confirmText)}</button>
        </div>
      </div>
    `;

  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", close)
  );
  wrapper.addEventListener("click", (e) => {
    if (e.target === wrapper) close();
  });
  const confirmBtn = wrapper.querySelector("[data-confirm]");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      if (typeof onConfirm === "function") onConfirm();
      close();
    });
  }
}