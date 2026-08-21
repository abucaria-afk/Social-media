/* The switches that drive settings.js, wherever a page shows them.
 *
 * settings.js decides how the page looks and does it before anything paints.
 * This is only the wiring for the controls: it finds every switch on the
 * document, marks the live choice, and hands a tap back to `auteurSettings`.
 *
 * "Every switch on the document", not the one with the id — the published
 * single-page build puts three appearance switches in one document, and
 * `getElementById` returns the first, so the studio's switch and the animation
 * tab's did nothing at all, silently. That is also why the marking runs off
 * the `auteur:settings` event rather than only off the click: two switches for
 * one setting must never be able to disagree about what it is set to.
 */
(function () {
  "use strict";

  if (!window.auteurSettings) { return; }

  /* A group says which setting it drives. `.appearance` is the older markup —
     it is in index.html and studio.html and in the published build — and it
     means the theme, so it is read as `data-setting="theme"` without having to
     be edited everywhere. */
  function settingOf(group) {
    return group.getAttribute("data-setting") || (group.classList.contains("appearance") ? "theme" : "");
  }

  var groups = Array.prototype.filter.call(
    document.querySelectorAll(".appearance, [data-setting]"),
    function (group) { return !!settingOf(group); }
  );
  if (!groups.length) { return; }

  function mark(settings) {
    groups.forEach(function (group) {
      var key = settingOf(group);
      var live = settings[key];
      Array.prototype.forEach.call(group.querySelectorAll(".choice"), function (button) {
        var on = button.dataset.value === live;
        button.classList.toggle("is-on", on);
        /* radio, so aria-checked; a control that only changes colour is a
           control a screen reader cannot report the state of. */
        button.setAttribute("aria-checked", on ? "true" : "false");
      });
    });
  }

  mark(window.auteurSettings.get());
  document.addEventListener("auteur:settings", function (event) { mark(event.detail); });

  groups.forEach(function (group) {
    group.addEventListener("click", function (event) {
      var button = event.target.closest(".choice");
      if (!button || !group.contains(button)) { return; }
      window.auteurSettings.set(settingOf(group), button.dataset.value);
    });
  });
})();
