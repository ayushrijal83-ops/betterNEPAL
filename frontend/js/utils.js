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

// Translation helper that degrades to the supplied English when the i18n
// runtime has not loaded. Every user-facing default string here goes through
// it, so a shared renderer is never the thing that leaves English on a
// Nepali page.
function tr(key, fallback) {
  return typeof t === "function" ? t(key) : fallback;
}

function renderLoading(containerId, message) {
  const el = document.getElementById(containerId);
  if (!el) return;
  // Skeleton rather than a spinner: it reserves the space the content will
  // occupy, so nothing jumps when the data lands.
  el.innerHTML = `
    <div class="loading-state" role="status" aria-live="polite">
      <span class="sr-only">${escapeHtml(message || tr("state.loading", "Loading…"))}</span>
      <div class="skeleton skeleton-line" style="width:45%"></div>
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line"></div>
    </div>
  `;
}

function renderEmpty(containerId, title, text = "") {
  const el = document.getElementById(containerId);
  if (!el) return;
  // A contour ring rather than a magnifying glass or a shrug emoji: it is the
  // same survey-marker motif the map pins and the brand use, so an empty
  // panel still looks like part of this product.
  el.innerHTML = `
    <div class="empty-state">
      <div class="empty-mark" aria-hidden="true">
        <svg width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6"
             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
          <path d="M2 17 Q 7 12 12 15 T 22 12" stroke-opacity=".5"/>
          <path d="M2 21 Q 7 16 12 19 T 22 16" stroke-opacity=".3"/>
          <path d="M6 10 L12 3 L18 10 L12 13 Z"/>
        </svg>
      </div>
      <div class="empty-title">${escapeHtml(title || tr("state.empty.title", "Nothing here yet"))}</div>
      <div class="empty-text">${escapeHtml(text || tr("state.empty.hint", ""))}</div>
    </div>
  `;
}

function renderError(containerId, message, onRetry) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="empty-state" role="alert">
      <div class="empty-mark empty-mark--error" aria-hidden="true">
        <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
          <path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>
        </svg>
      </div>
      <div class="empty-title">${escapeHtml(tr("state.error.title", "Something went wrong"))}</div>
      <div class="empty-text">${escapeHtml(message || tr("state.error.generic", "We could not load this right now."))}</div>
      ${onRetry ? `<button type="button" class="btn btn-outline btn-sm mt-sm" data-retry>${escapeHtml(tr("common.retry", "Try again"))}</button>` : ""}
    </div>
  `;
  if (onRetry) {
    const btn = el.querySelector("[data-retry]");
    if (btn) btn.addEventListener("click", onRetry);
  }
}

// ---------- Toast ----------
function ensureToastContainer() {
  let c = document.getElementById("bn-toast-container");
  if (!c) {
    c = document.createElement("div");
    c.id = "bn-toast-container";
    c.className = "toast-stack";
    // Announced politely: a toast is informational, and assertive would cut
    // across whatever the user is currently reading.
    c.setAttribute("role", "status");
    c.setAttribute("aria-live", "polite");
    document.body.appendChild(c);
  }
  return c;
}

const TOAST_ICONS = {
  success: '<path d="M20 6 9 17l-5-5"/>',
  error: '<path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  warning: '<path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/>',
};

function showToast(message, type = "info", duration = 3600) {
  const container = ensureToastContainer();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <svg class="toast-icon" width="16" height="16" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         viewBox="0 0 24 24" aria-hidden="true">${TOAST_ICONS[type] || TOAST_ICONS.info}</svg>
    <span></span>
  `;
  // textContent, not innerHTML: a toast often carries a server message, and
  // that is not a safe place to interpolate markup.
  toast.querySelector("span").textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 220);
  }, duration);
}

// ---------- Scroll reveal ----------
// Sections fade up as they enter the viewport. The `js-reveal-ready` class is
// added only once the observer actually exists, so a browser without
// IntersectionObserver - or a script that fails to run - leaves every section
// visible rather than permanently blank.
function initScrollReveal() {
  const targets = document.querySelectorAll("[data-reveal]");
  if (!targets.length || !("IntersectionObserver" in window)) return;

  document.documentElement.classList.add("js-reveal-ready");

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        // Stagger siblings slightly so a row of cards arrives as a wave
        // rather than a single snap.
        const delay = Number(entry.target.dataset.revealDelay || 0);
        setTimeout(() => entry.target.classList.add("is-revealed"), delay);
        observer.unobserve(entry.target);
      });
    },
    // Positive bottom margin: the section starts revealing ~220px before it
    // enters the viewport. With a negative margin a fast scroll outruns the
    // transition and the user sees a blank band where content should be.
    { rootMargin: "0px 0px 220px 0px", threshold: 0 }
  );

  targets.forEach((el) => observer.observe(el));
}

document.addEventListener("DOMContentLoaded", initScrollReveal);

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