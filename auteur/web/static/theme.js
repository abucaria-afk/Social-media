/* The appearance switch, on every page that has one.
 *
 * Three states, not two: Automatic follows the phone, and Light and Dark
 * override it in both directions. Automatic stamps nothing on the root, which
 * is what lets `prefers-color-scheme` decide — theme.css is built around that,
 * so this only has to add or remove the attribute.
 *
 * It lived inside app.js, so the studio and the ask page had no way to change
 * appearance at all: you could set it in the edit room and nowhere else.
 */
(function () {
  "use strict";

  var GROUNDS = { dark: "#19181b", light: "#f7f5f2" };

  function apply(choice) {
    if (choice === "light" || choice === "dark") {
      document.documentElement.setAttribute("data-theme", choice);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    // Keep the iOS status bar in step with the page. The media-scoped
    // <meta theme-color> tags cover Automatic; an explicit choice needs the
    // value written directly, and removing the tag hands it back to them.
    var bar = document.querySelector('meta[name="theme-color"]:not([media])');
    if (choice === "system") {
      if (bar) { bar.remove(); }
    } else {
      if (!bar) {
        bar = document.createElement("meta");
        bar.setAttribute("name", "theme-color");
        document.head.appendChild(bar);
      }
      bar.setAttribute("content", GROUNDS[choice]);
    }
    try { localStorage.setItem("auteur-theme", choice); } catch (e) { /* private mode */ }
  }

  var saved = "system";
  try { saved = localStorage.getItem("auteur-theme") || "system"; } catch (e) {}

  apply(saved);

  /* Every switch on the document, not the one with the id.
   *
   * Each page has its own, which is fine while each page is its own document.
   * The published single-page build puts three of them in one, and
   * `getElementById` returns the first — so the studio's switch and the
   * animation tab's did nothing at all, silently. A class says "there may be
   * several of these" where an id says "there is exactly one". */
  var groups = document.querySelectorAll(".appearance");
  if (!groups.length) { return; }

  function mark(choice) {
    Array.prototype.forEach.call(groups, function (group) {
      Array.prototype.forEach.call(group.querySelectorAll(".choice"), function (button) {
        var on = button.dataset.value === choice;
        button.classList.toggle("is-on", on);
        button.setAttribute("aria-checked", on ? "true" : "false");
      });
    });
  }

  mark(saved);

  Array.prototype.forEach.call(groups, function (group) {
    group.addEventListener("click", function (event) {
      var button = event.target.closest(".choice");
      if (!button) { return; }
      // Every switch moves together: they are one setting shown in three
      // places, and leaving the others behind is how a page ends up
      // disagreeing with itself about what theme it is in.
      mark(button.dataset.value);
      apply(button.dataset.value);
    });
  });
})();
