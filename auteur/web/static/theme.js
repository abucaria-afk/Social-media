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

  var GROUNDS = { dark: "#0c0b0a", light: "#f6f1e6" };

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

  var group = document.getElementById("appearance");
  if (!group) { return; }

  Array.prototype.forEach.call(group.querySelectorAll(".choice"), function (button) {
    var on = button.dataset.value === saved;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-checked", on ? "true" : "false");
  });

  group.addEventListener("click", function (event) {
    var button = event.target.closest(".choice");
    if (!button) { return; }
    Array.prototype.forEach.call(group.querySelectorAll(".choice"), function (other) {
      var on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-checked", on ? "true" : "false");
    });
    apply(button.dataset.value);
  });
})();
