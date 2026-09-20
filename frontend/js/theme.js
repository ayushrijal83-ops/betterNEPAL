// js/theme.js
// ---------------------------------------------------------------------------
// Light/dark theme: state, persistence, and the toggle control.
//
// The actual colour swap is pure CSS (variables.css: @media
// prefers-color-scheme + [data-theme] overrides). This file only manages
// which of the three states the page is in:
//
//   no data-theme attribute -> CSS decides from prefers-color-scheme alone
//   data-theme="light"      -> forced light regardless of OS
//   data-theme="dark"       -> forced dark regardless of OS
//
// A tiny inline script in every page's <head> (see build/insert step) applies
// a saved choice before first paint, so this file's job on load is mostly
// just wiring the toggle button - the attribute is usually already correct
// by the time this runs.
// ---------------------------------------------------------------------------

const THEME_STORAGE_KEY = "bn_theme";

function getStoredTheme() {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch (_) {
    return null;
  }
}

function systemPrefersDark() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

// The theme actually being rendered right now, whether that came from an
// explicit choice or the OS default.
function effectiveTheme() {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return systemPrefersDark() ? "dark" : "light";
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

// Explicit user choice: persists, and from this point on wins over whatever
// the OS says until the user clears it (there is no UI for clearing it -
// picking the theme that matches the OS again has the same visible effect).
function setTheme(theme) {
  applyTheme(theme);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (_) {
    /* private browsing: the choice just will not survive a reload */
  }
}

function toggleTheme() {
  setTheme(effectiveTheme() === "dark" ? "light" : "dark");
}

// Reflect current state into any toggle button already on the page (there
// may be more than one instance of the control markup, in principle).
function syncThemeToggleButtons() {
  const isDark = effectiveTheme() === "dark";
  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(isDark));
    btn.setAttribute(
      "aria-label",
      isDark ? "Switch to light theme" : "Switch to dark theme"
    );
  });
}

function initThemeToggle() {
  // Make sure the attribute matches what was actually persisted - the head
  // script already did this before paint, but a page opened via bfcache or
  // without that script (e.g. a component fetched mid-session) should not
  // silently disagree with localStorage.
  const stored = getStoredTheme();
  if (stored) applyTheme(stored);

  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    if (btn.dataset.themeBound) return;
    btn.dataset.themeBound = "true";
    btn.addEventListener("click", () => {
      toggleTheme();
      syncThemeToggleButtons();
    });
  });

  syncThemeToggleButtons();

  // A user with no saved preference is still following the OS live - if
  // they flip their system theme while the tab is open, follow it instantly
  // rather than waiting for a reload. An explicit choice already in
  // localStorage takes precedence and is left alone.
  if (typeof window.matchMedia === "function" && !getStoredTheme()) {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (!getStoredTheme()) syncThemeToggleButtons();
    };
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", onChange);
    } else if (typeof media.addListener === "function") {
      media.addListener(onChange); // Safari < 14
    }
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initThemeToggle);
} else {
  initThemeToggle();
}
