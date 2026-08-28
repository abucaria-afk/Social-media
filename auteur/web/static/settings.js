/* How this app looks, decided by the person looking at it.
 *
 * Loaded from <head>, synchronously, on every page — and that is deliberate.
 * These settings paint: a page that renders at the default size and then jumps
 * to 140% once a deferred script runs has shown somebody who needs large text
 * a screen of small text first. A blocking same-origin script is one cache hit
 * and no flash.
 *
 * It replaces the eight-line theme snippet that was copied into the head of
 * every page. Copied chrome is chrome that is right on nine pages and stale on
 * the tenth, which is the argument chrome.js already makes about the tab bar.
 *
 * Four settings. Appearance is a preference; the other three are accessibility
 * settings, and they follow a rule worth stating: **this app can turn an
 * accessibility setting on, and never off.** Both phones already publish
 * "reduce motion" and "increase contrast" to the browser, and somebody who has
 * set those in the operating system has said something about themselves that
 * an app has no business overriding. So the choices are Automatic and On, not
 * Automatic, On and Off — the media queries stay live underneath in every
 * case, and the attribute only ever adds.
 *
 * Text size is the exception, because it is the one the operating system does
 * *not* publish. iOS keeps Settings > Display & Brightness > Text Size to
 * itself, reachable from CSS only through the `-apple-system-*` shorthands
 * (see the Dynamic Type block in style.css), so a page that wants a size
 * control has to have its own. It scales the root font size, and every rung of
 * the type scale is in rem, so all of it moves together.
 */
(function () {
  "use strict";

  var KEY = "auteur-settings";
  /* The theme was stored under its own key before there was anything else to
     store, and phones have it. Read it, honour it, and keep writing it, so a
     browser that still has the old page cached and this one both agree. */
  var THEME_KEY = "auteur-theme";

  var GROUNDS = { dark: "#19181b", light: "#f7f5f2" };

  /* Percentages rather than a free slider: four rungs somebody can hit with a
     thumb, and each one is a real step rather than a number to fiddle with.
     140% is where 17px body text reaches 24px, which is roughly the largest
     non-accessibility Dynamic Type size on iOS. */
  var TEXT = { default: 100, large: 112, larger: 125, largest: 140 };

  var DEFAULTS = { theme: "system", text: "default", motion: "system", contrast: "system" };

  function read() {
    var saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
    } catch (e) { saved = {}; }
    if (typeof saved !== "object") { saved = {}; }
    var out = {};
    for (var key in DEFAULTS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) {
        out[key] = typeof saved[key] === "string" ? saved[key] : DEFAULTS[key];
      }
    }
    /* The older key wins only when the newer store has nothing to say, so
       moving the switch on a new page does not get undone by a stale value. */
    if (!saved.theme) {
      try {
        var legacy = localStorage.getItem(THEME_KEY);
        if (legacy === "light" || legacy === "dark") { out.theme = legacy; }
      } catch (e) { /* private mode */ }
    }
    return out;
  }

  function write(settings) {
    try {
      localStorage.setItem(KEY, JSON.stringify(settings));
      /* Kept in step for anything still reading the old key. */
      localStorage.setItem(THEME_KEY, settings.theme);
    } catch (e) { /* private mode: the choice lasts as long as the tab */ }
  }

  function apply(settings) {
    var root = document.documentElement;

    /* A page may lock its own theme, and one does: the feed is dark whatever
       the rest of the app is set to, because a film is a picture on a black
       surround everywhere it is ever watched. Without this check, applying
       "Automatic" would strip that attribute and paint a bone-white page
       around a 1080x1920 video. The other three settings still apply — text
       size and contrast are not decoration and do not stop being needed on
       the one screen that has an opinion about its own colour. */
    if (!root.hasAttribute("data-theme-locked")) {
      if (settings.theme === "light" || settings.theme === "dark") {
        root.setAttribute("data-theme", settings.theme);
      } else {
        /* Automatic stamps nothing, which is what lets prefers-color-scheme
           decide — theme.css is built around exactly that. */
        root.removeAttribute("data-theme");
      }
    }

    var percent = TEXT[settings.text] || 100;
    if (percent === 100) {
      root.style.removeProperty("font-size");
    } else {
      root.style.fontSize = percent + "%";
    }
    root.setAttribute("data-text", settings.text);

    /* Add-only, as above: absent means "whatever the phone says". */
    if (settings.motion === "still") {
      root.setAttribute("data-motion", "still");
    } else {
      root.removeAttribute("data-motion");
    }
    if (settings.contrast === "more") {
      root.setAttribute("data-contrast", "more");
    } else {
      root.removeAttribute("data-contrast");
    }

    /* The iOS status bar. The media-scoped <meta theme-color> tags handle
       Automatic; an explicit choice needs the value written directly, and
       removing the tag hands the question back to them. */
    var bar = document.querySelector('meta[name="theme-color"]:not([media])');
    if (root.hasAttribute("data-theme-locked")) {
      /* Its own <meta> already says what colour it is. */
    } else if (settings.theme === "system") {
      if (bar) { bar.parentNode.removeChild(bar); }
    } else {
      if (!bar) {
        bar = document.createElement("meta");
        bar.setAttribute("name", "theme-color");
        (document.head || root).appendChild(bar);
      }
      bar.setAttribute("content", GROUNDS[settings.theme]);
    }
  }

  var current = read();
  apply(current);

  window.auteurSettings = {
    get: function () {
      var copy = {};
      for (var key in current) {
        if (Object.prototype.hasOwnProperty.call(current, key)) { copy[key] = current[key]; }
      }
      return copy;
    },
    set: function (key, value) {
      if (!Object.prototype.hasOwnProperty.call(DEFAULTS, key)) { return; }
      current[key] = value;
      apply(current);
      write(current);
      /* So a switch shown in more than one place on the same document moves
         with it. The published single-page build has three of them, and a page
         that disagrees with itself about what theme it is in is the bug this
         event exists to prevent. */
      try {
        document.dispatchEvent(new CustomEvent("auteur:settings", { detail: this.get() }));
      } catch (e) { /* very old browser: the switches just do not sync */ }
    }
  };
})();
