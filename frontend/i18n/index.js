// i18n/index.js — the translation runtime
//
// Load order matters: en.js, ne.js, mai.js, then this file.
//
// Usage in markup:
//   <h1 data-i18n="home.hero.title1">See the problem.</h1>
//   <input data-i18n-placeholder="map.searchPlaceholder">
//   <button data-i18n-aria="common.close">
//   <span data-i18n="report.reportCount" data-i18n-count="14">
//
// The English text stays in the HTML as the fallback. If this script fails to
// load, or a key is missing, the page still reads correctly in English rather
// than showing raw key names - which is the failure mode that makes a
// half-finished i18n layer worse than none.

(function () {
  "use strict";

  var STORAGE_KEY = "bn_lang";
  var DEFAULT_LANG = "en";
  var SUPPORTED = ["en", "ne", "mai"];

  var dicts = window.BN_I18N_DICT || {};
  var current = DEFAULT_LANG;
  var listeners = [];

  // --- storage -------------------------------------------------------------

  function readStored() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      return SUPPORTED.indexOf(saved) !== -1 ? saved : null;
    } catch (e) {
      // Private browsing, or storage disabled. Not an error worth surfacing -
      // the language simply will not persist.
      return null;
    }
  }

  function writeStored(lang) {
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (e) {
      /* see readStored */
    }
  }

  // --- lookup --------------------------------------------------------------

  // Returns the translated string, falling back through: current language ->
  // English -> the key's own fallback text -> the key itself.
  function t(key, vars) {
    var dict = dicts[current] || {};
    var value = dict[key];

    if (value === undefined) value = (dicts[DEFAULT_LANG] || {})[key];
    if (value === undefined) return key;

    if (vars) {
      value = value.replace(/\{(\w+)\}/g, function (whole, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? vars[name] : whole;
      });
    }
    return value;
  }

  // --- applying to the DOM -------------------------------------------------

  function varsFor(el) {
    // data-i18n-count="14" -> { count: "14" }. Covers every interpolated
    // string the product currently has; add more prefixes here if needed.
    var vars = null;
    for (var i = 0; i < el.attributes.length; i++) {
      var attr = el.attributes[i];
      if (attr.name.indexOf("data-i18n-var-") === 0) {
        vars = vars || {};
        vars[attr.name.slice("data-i18n-var-".length)] = attr.value;
      }
    }
    if (el.hasAttribute("data-i18n-count")) {
      vars = vars || {};
      vars.count = el.getAttribute("data-i18n-count");
    }
    if (el.hasAttribute("data-i18n-distance")) {
      vars = vars || {};
      vars.distance = el.getAttribute("data-i18n-distance");
    }
    return vars;
  }

  function applyTo(el) {
    var key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key, varsFor(el));

    var placeholder = el.getAttribute("data-i18n-placeholder");
    if (placeholder) el.setAttribute("placeholder", t(placeholder));

    var aria = el.getAttribute("data-i18n-aria");
    if (aria) el.setAttribute("aria-label", t(aria));

    var title = el.getAttribute("data-i18n-title");
    if (title) el.setAttribute("title", t(title));

    // Alt text is interface copy, not decoration: a screen-reader user
    // switching to Nepali should hear the photograph described in Nepali.
    var alt = el.getAttribute("data-i18n-alt");
    if (alt) el.setAttribute("alt", t(alt));
  }

  var SELECTOR =
    "[data-i18n],[data-i18n-placeholder],[data-i18n-aria],[data-i18n-title],[data-i18n-alt]";

  function applyTranslations(root) {
    var scope = root || document;
    if (scope.nodeType === 1 && scope.matches && scope.matches(SELECTOR)) applyTo(scope);
    var nodes = scope.querySelectorAll ? scope.querySelectorAll(SELECTOR) : [];
    for (var i = 0; i < nodes.length; i++) applyTo(nodes[i]);
  }

  // --- language switching --------------------------------------------------

  function setLanguage(lang, opts) {
    if (SUPPORTED.indexOf(lang) === -1) return;
    current = lang;

    // `lang` on <html> is what tells the browser which font and hyphenation
    // rules to use, and what a screen reader switches pronunciation on. The
    // Devanagari line-height rules in main.css key off it too.
    document.documentElement.setAttribute("lang", lang);

    if (!opts || opts.persist !== false) writeStored(lang);

    applyTranslations(document);
    syncSwitchers();

    for (var i = 0; i < listeners.length; i++) {
      try {
        listeners[i](lang);
      } catch (e) {
        console.error("i18n listener failed", e);
      }
    }
    document.dispatchEvent(new CustomEvent("bn:languagechange", { detail: { lang: lang } }));
  }

  function onChange(fn) {
    listeners.push(fn);
  }

  // --- the selector control ------------------------------------------------

  // Marks the active button in every language switcher on the page. Switchers
  // are plain markup (see renderSwitcher) so they inherit page styling.
  function syncSwitchers() {
    var buttons = document.querySelectorAll("[data-lang-option]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var isActive = btn.getAttribute("data-lang-option") === current;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    }
    var selects = document.querySelectorAll("select[data-lang-select]");
    for (var j = 0; j < selects.length; j++) selects[j].value = current;
  }

  // Inline segmented control: EN | नेपाली | मैथिली
  function switcherMarkup() {
    var html = '<div class="lang-switch" role="group" aria-label="' + t("lang.label") + '">';
    for (var i = 0; i < SUPPORTED.length; i++) {
      var code = SUPPORTED[i];
      html +=
        '<button type="button" class="lang-switch-btn" data-lang-option="' +
        code +
        '" lang="' +
        code +
        '">' +
        // Each language is named in its own script, which is the one piece of
        // a language picker that must never itself be translated.
        (dicts[code] ? dicts[code]["lang." + code] : code) +
        "</button>";
    }
    return html + "</div>";
  }

  // One delegated listener for the whole document, so switchers rendered
  // later (in a shell, a drawer, a modal) work without re-binding.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-lang-option]");
    if (!btn) return;
    e.preventDefault();
    setLanguage(btn.getAttribute("data-lang-option"));
  });

  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("select[data-lang-select]")) {
      setLanguage(e.target.value);
    }
  });

  // --- dynamic content -----------------------------------------------------

  // Most pages render lists by assigning innerHTML after a fetch. Rather than
  // find and patch every one of those call sites across 14 scripts, watch the
  // document and translate whatever appears. The observer only walks nodes
  // that were actually added, so the cost tracks how much the page renders.
  function watchForNewContent() {
    if (!window.MutationObserver) return;
    new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) applyTranslations(added[j]);
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }

  // --- developer tooling ---------------------------------------------------

  // Compares a locale against English and reports drift. Cheap to run, and it
  // is the difference between "the translation is done" and knowing it is.
  function audit(lang) {
    var base = dicts[DEFAULT_LANG] || {};
    var target = dicts[lang] || {};
    var missing = Object.keys(base).filter(function (k) {
      return !(k in target);
    });
    var extra = Object.keys(target).filter(function (k) {
      return !(k in base);
    });
    var total = Object.keys(base).length;
    return {
      language: lang,
      translated: total - missing.length,
      total: total,
      percent: total ? Math.round(((total - missing.length) / total) * 100) : 0,
      missing: missing,
      extra: extra,
    };
  }

  // --- boot ----------------------------------------------------------------

  function init() {
    // Stored choice wins. Otherwise fall back to the browser's preference if
    // it happens to be one we speak, and English if not.
    var stored = readStored();
    if (stored) {
      current = stored;
    } else {
      var nav = (navigator.language || "").toLowerCase();
      if (nav.indexOf("ne") === 0) current = "ne";
      else if (nav.indexOf("mai") === 0) current = "mai";
      else current = DEFAULT_LANG;
    }

    document.documentElement.setAttribute("lang", current);
    applyTranslations(document);
    syncSwitchers();
    watchForNewContent();
  }

  window.BN_I18N = {
    t: t,
    apply: applyTranslations,
    setLanguage: setLanguage,
    onChange: onChange,
    switcherMarkup: switcherMarkup,
    audit: audit,
    supported: SUPPORTED,
    get language() {
      return current;
    },
  };

  // Shorthand. Scripts call t("nav.map") without reaching through BN_I18N.
  window.t = t;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
