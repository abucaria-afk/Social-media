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
  /* Five slots, the middle one opens rather than goes, and the fifth is you.
   *
   * It was the studio. Both apps this is modelled on end the bar with the
   * person using it — Home, Search, +, Reels, You on one; Home, Friends, +,
   * Inbox, Profile on the other — and neither puts a workroom in the bar at
   * all. Instagram keeps its professional dashboard behind the profile tab,
   * which is exactly the shape the studio is: yours, entered deliberately, not
   * somewhere you flick to. So the studio moved to a row at the top of your
   * own profile, and this slot is the profile itself.
   *
   * Templates had the second slot and no longer does. Making a template, using
   * one, and cutting a film from scratch are the same activity — they are all
   * "make something" — and putting one of the three in the bar while the other
   * two live behind the plus said they were different kinds of thing. On both
   * reference apps the plus is the only way into anything creative, and it
   * does not navigate: it *opens*, and asks which kind. Instagram's asks Post,
   * Story, Reel, Live. This one asks which of the things this app makes.
   *
   * The freed slot went to Schedule. On both reference apps the second slot is
   * Discover — another feed to fall into — and this app is not for falling
   * into. What it is for is the other half of the work: you made something,
   * and now it has to go somewhere, at a time, on a platform, and you need to
   * know whether the last one landed. That is the second most common thing
   * somebody opens this app to do, so it is the second slot.
   *
   * (The first version of this bar put Discover here, pointing at /discover,
   * which the server does not serve. That is the same defect as `brand.SHOTS`
   * naming /looks — a navigation entry to a page that does not exist — and it
   * was caught the same way, by asking the server rather than by reading.)
   *
   * Messages keep their own slot. They had one already, called Inbox; the name
   * is the change. "Inbox" is where a system puts things it has decided to
   * send you, and this is a tab where people you know write to you, which is a
   * different promise. Both reference apps use the person-to-person word.
   */
  var TABS = [
    { id: "feed", href: "/feed", label: "Feed", icon: "home" },
    { id: "schedule", href: "/plan", label: "Schedule", icon: "calendar" },
    /* No href: this one opens the sheet below. It is still an <a> with a real
       target for the no-JavaScript case and for anybody who opens it in a new
       tab — a control that does nothing without script is not a control. */
    { id: "make", href: "/", label: "Create", icon: "plus", big: true, opens: "create" },
    { id: "messages", href: "/inbox", label: "Messages", icon: "chat" },
    { id: "profile", href: "/profile", label: "You", icon: "person" }
  ];

  /* What the plus offers. Every creative surface in the app, in the order
   * somebody meets them, with the one that is the whole product first.
   *
   * The rule for this list: an entry earns its place by *making something that
   * did not exist*. The feed, the album and the settings are not here however
   * often they are used, because a chooser that lists everything is a menu,
   * and a menu is what the five stacked cards on the old home screen were. */
  var CREATE = [
    {
      href: "/",
      title: "Make a film",
      note: "Pick clips, say what you want, get it cut.",
      icon: "spark"
    },
    {
      href: "/templates",
      title: "Cut to a template",
      note: "Use a reel's timing on your own footage.",
      icon: "grid"
    },
    {
      href: "/templates#add",
      title: "Make a template",
      note: "Upload a reel and keep where its cuts fall.",
      icon: "plus"
    },
    {
      href: "/overlays",
      title: "Type and stickers",
      note: "Words and marks that land on the beat.",
      icon: "chat"
    },
    {
      href: "/projects",
      title: "Start a project",
      note: "An album and a map for one piece of work.",
      icon: "home"
    }
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
    plus: { line: "M12 6v12M6 12h12", fill: false },
    /* A glass. Not filled when active — a magnifier with a solid lens reads as
       a lollipop at 24px, so this one stays line art in both states. */
    /* A month, not a clock. A clock says "when", a grid of days says "what is
       already booked", which is the question this tab answers. */
    calendar: {
      line: "M4 6a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM4 10h16M8 3v4M16 3v4",
      fill: false
    },
    /* Head and shoulders, one path, so the filled variant works the same way
       the others do. */
    person: {
      line: "M12 12.4a4.2 4.2 0 1 0 0-8.4 4.2 4.2 0 0 0 0 8.4zM4.2 20.6a7.8 7.8 0 0 1 15.6 0z",
      fill: true
    }
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
    if (path.indexOf("/plan") === 0 || path.indexOf("/manager") === 0 ||
        path.indexOf("/connect") === 0) return "schedule";
    if (path.indexOf("/inbox") === 0 || path.indexOf("/messages") === 0) return "messages";
    /* Templates, overlays and the plan board are all reached through the plus,
       so the plus is what lights up while you are in one of them. A tab bar
       that highlights nothing tells you that you are lost. */
    if (path.indexOf("/templates") === 0 || path.indexOf("/overlays") === 0) return "make";
    if (path.indexOf("/profile") === 0 || path.indexOf("/me") === 0 ||
        path.indexOf("/u/") === 0) return "profile";
    /* The studio, /projects, /ask, /overlays and /connect are all reached from
     * your own profile, so that is the slot that lights up on them. Five slots is the
     * point, and a bar that highlights nothing on a third of the app reads as
     * broken. */
    return "profile";
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
    largeTitles();
  }

  /* The unread count on the inbox tab. One request on load; the inbox page
   * itself refreshes it when a message is read. */
  function badge() {
    fetch("/api/messages", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var dot = document.querySelector('.tab[data-tab="messages"] .tab-badge');
        if (!dot) return;
        if (data.unread > 0) {
          dot.textContent = data.unread > 9 ? "9+" : String(data.unread);
          dot.hidden = false;
        } else {
          dot.hidden = true;
        }
        /* And on the home-screen icon itself. This is the number people
         * actually act on — an unread count inside an app nobody has opened
         * is not a notification. Supported on both phones for an installed
         * app; a plain tab throws, which is why it is guarded rather than
         * feature-detected on `navigator` alone. */
        try {
          if (navigator.setAppBadge) {
            if (data.unread > 0) { navigator.setAppBadge(data.unread); }
            else if (navigator.clearAppBadge) { navigator.clearAppBadge(); }
          }
        } catch (e) { /* not installed, or not permitted */ }
      })
      .catch(function () { /* signed out, or offline: no badge */ });
  }

  /* The large title collapses into the bar as it scrolls under it.
   *
   * A scroll listener rather than `animation-timeline: scroll()`, because
   * Safari does not have scroll-driven animations and this is the platform
   * Safari is on. Passive, and it reads one number per frame — the observer
   * alternative fires on a threshold and the title has to cross-fade, not
   * snap. */
  function largeTitles() {
    var bar = document.querySelector(".topbar");
    var title = document.querySelector(".large-title");
    if (!bar) return;
    var scroller = document.querySelector(".page") ? window : null;

    /* No large title on this page means there is nothing to hand off *from*,
     * so the bar's own title is the only one — and it starts hidden, waiting
     * for a scroll that on a short page never comes. The inbox and the manager
     * were in exactly that state: a heading in the markup, an accessible name
     * on the screen, and nothing a person could read. */
    if (!title) {
      bar.classList.add("is-collapsed");
      return;
    }

    function check() {
      /* Collapsed once the large title's baseline has gone under the bar. */
      var past = title.getBoundingClientRect().bottom < bar.getBoundingClientRect().bottom;
      bar.classList.toggle("is-collapsed", past);
    }

    (scroller || window).addEventListener("scroll", check, { passive: true });
    window.addEventListener("resize", check, { passive: true });
    check();
  }

  /* The share sheet, which on this platform is the *system* one: it can save a
   * film to Photos, AirDrop it, or hand it to any app on the phone. A download
   * link cannot do any of that — on iOS it opens the file in a tab and leaves
   * somebody to work out the rest — so where the browser has Web Share with
   * files, that is the button. Everything else keeps the download.
   *
   * `canShare` with the actual file, not a feature test on `share`: iOS has
   * had `navigator.share` for years and file sharing for fewer, and asking the
   * general question gets a yes and then throws. */
  function canShareFiles(files) {
    return !!(navigator.canShare && navigator.share && navigator.canShare({ files: files }));
  }

  function shareFile(url, name, title) {
    return fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.blob(); })
      .then(function (blob) {
        var file = new File([blob], name, { type: blob.type || "video/mp4" });
        if (!canShareFiles([file])) return false;
        return navigator.share({ files: [file], title: title || name }).then(function () {
          return true;
        });
      });
  }

  window.auteurChrome = {
    refreshBadge: badge,
    shareFile: shareFile,
    canShareFiles: canShareFiles
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
