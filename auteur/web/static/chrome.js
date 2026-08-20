/* The bar along the bottom, on every page.
 *
 * Five stacked cards on the home screen were the only way to reach the rest of
 * the app, which meant every room was two taps from every other room and you
 * had to go home to get anywhere. Nobody navigates a phone that way, and more
 * to the point nobody has to *learn* a tab bar: it is the same five-slot bar,
 * in the same place, with the create button in the middle, that Instagram and
 * TikTok have trained everybody on. Familiar is not a compromise here, it is
 * the feature — this app should feel like somewhere you have already been.
 *
 * Injected rather than copied into eight HTML files, because a navigation bar
 * that is right on seven pages and stale on the eighth is worse than none.
 */
(function () {
  var TABS = [
    { id: "feed", href: "/feed", label: "Feed", icon: "home" },
    { id: "templates", href: "/templates", label: "Templates", icon: "grid" },
    { id: "make", href: "/", label: "Create", icon: "plus", big: true },
    { id: "inbox", href: "/inbox", label: "Inbox", icon: "chat" },
    { id: "studio", href: "/studio", label: "Studio", icon: "spark" }
  ];

  /* Line art, one weight, drawn on a 24 grid. Filled glyphs read as "selected"
   * on both iOS apps, so the active tab swaps to the filled variant instead of
   * only changing colour — colour alone is the state nobody notices. */
  var ICONS = {
    home: {
      line: "M3 10.2 12 3.4l9 6.8V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
      fill: true
    },
    grid: { line: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z", fill: true },
    chat: {
      line: "M4 5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H9l-5 4V6a1 1 0 0 1 1-1z",
      fill: true
    },
    spark: {
      line: "M12 3l2.1 5.6L20 10.5l-5.9 1.9L12 18l-2.1-5.6L4 10.5l5.9-1.9z",
      fill: true
    },
    plus: { line: "M12 6v12M6 12h12", fill: false }
  };

  function svg(name, active) {
    var art = ICONS[name];
    var stroke = art.fill && active ? "none" : "currentColor";
    var fill = art.fill && active ? "currentColor" : "none";
    return (
      '<svg viewBox="0 0 24 24" aria-hidden="true" width="24" height="24">' +
      '<path d="' + art.line + '" fill="' + fill + '" stroke="' + stroke +
      '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    );
  }

  function here() {
    var path = location.pathname.replace(/\.html$/, "");
    if (path === "" || path === "/" || path === "/index") return "make";
    if (path.indexOf("/feed") === 0) return "feed";
    if (path.indexOf("/templates") === 0) return "templates";
    if (path.indexOf("/inbox") === 0 || path.indexOf("/messages") === 0) return "inbox";
    if (path.indexOf("/studio") === 0) return "studio";
    /* /ask, /overlays and /connect live under the studio in the bar even though
     * they are their own pages: five slots is the point, and a bar that
     * highlights nothing on a third of the app reads as broken. */
    return "studio";
  }

  function build() {
    if (document.querySelector(".tabbar")) return;
    var live = here();
    var nav = document.createElement("nav");
    nav.className = "tabbar";
    nav.setAttribute("aria-label", "Main");
    nav.innerHTML = TABS.map(function (tab) {
      var on = tab.id === live;
      return (
        '<a class="tab' + (tab.big ? " tab-big" : "") + (on ? " is-on" : "") +
        '" href="' + tab.href + '" data-tab="' + tab.id + '"' +
        (on ? ' aria-current="page"' : "") + ">" +
        /* The badge lives inside the icon, not beside it: it is positioned
           against the nearest positioned ancestor, and as a sibling that was
           the whole fixed bar — so the unread count sat in the far corner of
           the screen, half off the edge, next to the wrong tab. */
        '<span class="tab-icon">' + svg(tab.icon, on) +
        '<span class="tab-badge" hidden></span></span>' +
        '<span class="tab-label">' + tab.label + "</span></a>"
      );
    }).join("");
    document.body.appendChild(nav);
    document.body.classList.add("has-tabbar");
    badge();
  }

  /* The unread count on the inbox tab. One request on load; the inbox page
   * itself refreshes it when a message is read. */
  function badge() {
    fetch("/api/messages", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var dot = document.querySelector('.tab[data-tab="inbox"] .tab-badge');
        if (!dot) return;
        if (data.unread > 0) {
          dot.textContent = data.unread > 9 ? "9+" : String(data.unread);
          dot.hidden = false;
        } else {
          dot.hidden = true;
        }
      })
      .catch(function () { /* signed out, or offline: no badge */ });
  }

  window.auteurChrome = { refreshBadge: badge };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
