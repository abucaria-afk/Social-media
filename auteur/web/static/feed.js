/* The feed: build the reels, play only what is on screen, and let one be sent.
 *
 * The two things that make this feel like the apps it is modelled on, rather
 * than a list of <video> tags:
 *
 *   1. Exactly one film plays. An IntersectionObserver at a 0.6 threshold
 *      starts whatever is mostly on screen and pauses everything else. Without
 *      it, a phone decodes every video in the list at once, the fans spin up,
 *      and scrolling stutters — which reads as the app being slow rather than
 *      as eight decoders fighting.
 *   2. Films are built once and reused. Rebuilding the list on every fetch
 *      restarts the video that is playing, and a feed that flickers when you
 *      reach the bottom is worse than one that ends.
 */
(function () {
  var reels = document.getElementById("reels");
  var empty = document.getElementById("feed-empty");
  var scopes = document.querySelector(".feed-scopes");
  var sheet = document.getElementById("share-sheet");
  var sheetPeople = document.getElementById("share-people");
  var sheetNote = document.getElementById("share-note");

  var scope = "all";   // all | following | mine
  var people = {};     // username -> { name, picture }, from the feed itself
  /* A film id in the address, from a tap on somebody's profile grid. Cleared
     once it has been scrolled to, so a refresh does not jump again. */
  var wanted = (function () {
    var match = /[?&]film=([^&]+)/.exec(location.search);
    try { return match ? decodeURIComponent(match[1]) : ""; } catch (e) { return ""; }
  })();
  var shown = {};      // film id -> the element, so a refetch does not rebuild
  var oldest = null;   // the created stamp of the last film, for paging
  var sharing = null;  // the film id the sheet is open for
  var loading = false;
  var ended = false;

  /* Same hash as the inbox uses, so a person is one colour across the app. */
  function hueOf(name) {
    var total = 0;
    for (var i = 0; i < name.length; i++) total = (total * 31 + name.charCodeAt(i)) % 360;
    return total;
  }

  /* Their picture if they have set one, and the disc with their initial on it
     if they have not. The pictures arrive with the feed rather than one
     request per film — eight films by four people is four requests for a
     30px disc, which is how a scroll ends up slower than the video in it. */
  function avatar(name, small) {
    var card = people[name] || {};
    var size = small ? 30 : 44;
    if (card.picture) {
      return '<span class="avatar' + (small ? " avatar-sm" : "") + '"><img src="' +
        escaped(card.picture) + '" alt="" width="' + size + '" height="' + size + '"></span>';
    }
    return '<span class="avatar' + (small ? " avatar-sm" : "") + '" style="--hue:' +
      hueOf(name) + '" aria-hidden="true">' + escaped(name[0] || "?") + "</span>";
  }

  function nameOf(who) {
    var card = people[who] || {};
    return card.name || who;
  }

  var HEART = '<svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor">' +
    '<path d="M12 21s-7.5-4.7-9.4-9A5.2 5.2 0 0 1 12 6.6 5.2 5.2 0 0 1 21.4 12c-1.9 4.3-9.4 9-9.4 9z"/></svg>';
  var HEART_OUTLINE = '<svg viewBox="0 0 24 24" width="30" height="30" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linejoin="round">' +
    '<path d="M12 20.3s-7-4.4-8.8-8.4A4.7 4.7 0 0 1 12 7.4a4.7 4.7 0 0 1 8.8 4.5c-1.8 4-8.8 8.4-8.8 8.4z"/></svg>';
  var SEND = '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linejoin="round">' +
    '<path d="M21.5 2.5 11 13M21.5 2.5 15 21.5l-4-8.5-8.5-4z"/></svg>';

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  function build(film) {
    var node = document.createElement("article");
    node.className = "reel";
    node.dataset.film = film.id;

    var facts = (film.facts || []).slice(0, 3).map(function (fact) {
      return "<li>" + escaped(fact) + "</li>";
    }).join("");

    node.innerHTML =
      '<img class="reel-poster" src="' + film.poster + '" alt="" decoding="async">' +
      '<video playsinline muted loop preload="none" ' +
      'poster="' + film.poster + '" src="' + film.video + '"></video>' +
      '<div class="reel-caption">' +
        '<a class="reel-who" href="/u/' + encodeURIComponent(film.owner) + '">' +
          avatar(film.owner, true) + escaped(nameOf(film.owner)) + "</a>" +
        '<p class="reel-prompt">' + escaped(film.heard || film.prompt) + "</p>" +
        (facts ? '<ul class="reel-facts">' + facts + "</ul>" : "") +
      "</div>" +
      '<div class="reel-rail">' +
        '<button type="button" class="rail-button heart' + (film.liked ? " is-on" : "") +
          '" data-like="' + film.id + '" aria-pressed="' + (film.liked ? "true" : "false") + '">' +
          (film.liked ? HEART : HEART_OUTLINE) +
          '<span class="rail-count">' + film.likes + "</span></button>" +
        '<button type="button" class="rail-button" data-send="' + film.id + '">' +
          SEND + "<span>Send</span></button>" +
      "</div>";

    /* A square or landscape film letterboxes rather than being cropped to a
       slice of itself. Only the video knows its own shape, and only once it
       has read enough of the file to say. */
    var video = node.querySelector("video");
    video.addEventListener("loadedmetadata", function () {
      if (video.videoWidth >= video.videoHeight) node.classList.add("is-wide");
    });

    /* Tap the film to pause, like both apps. Not the caption or the rail. */
    node.addEventListener("click", function (event) {
      if (event.target.closest(".rail-button") || event.target.closest(".reel-caption")) return;
      if (event.target.closest("a")) return;
      if (video.paused) { video.play().catch(function () {}); } else { video.pause(); }
    });
    return node;
  }

  var watcher = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var video = entry.target.querySelector("video");
      if (!video) return;
      if (entry.isIntersecting) {
        entry.target.classList.add("is-playing");
        video.play().catch(function () { /* a phone that wants a tap first */ });
        /* Near the bottom: fetch the next page before it is needed. */
        if (entry.target === reels.lastElementChild) load();
      } else {
        video.pause();
        video.currentTime = 0;
        entry.target.classList.remove("is-playing");
      }
    });
  }, { threshold: 0.6 });

  function load(reset) {
    if (loading || (ended && !reset)) return;
    loading = true;
    var url = "/api/feed?scope=" + scope +
      (!reset && oldest ? "&before=" + encodeURIComponent(oldest) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { films: [] }; })
      .then(function (data) {
        var films = data.films || [];
        /* Merged rather than replaced: a later page carries only the people in
           it, and replacing would blank the discs on everything above. */
        var cards = data.people || {};
        Object.keys(cards).forEach(function (name) { people[name] = cards[name]; });
        following = data.following || 0;
        if (reset) {
          Object.keys(shown).forEach(function (id) { watcher.unobserve(shown[id]); });
          shown = {};
          oldest = null;
          ended = false;
          reels.querySelectorAll(".reel").forEach(function (n) { n.remove(); });
        }
        if (!films.length) ended = true;
        films.forEach(function (film) {
          if (shown[film.id]) return;
          var node = build(film);
          shown[film.id] = node;
          reels.appendChild(node);
          watcher.observe(node);
          oldest = film.created;
        });
        var any = reels.querySelectorAll(".reel").length > 0;
        empty.hidden = any;
        if (!any) { explainEmpty(); }
        if (wanted) { jump(); }
      })
      .catch(function () { ended = true; })
      .then(function () { loading = false; });
  }

  /* -- liking ------------------------------------------------------------ */

  reels.addEventListener("click", function (event) {
    var like = event.target.closest("[data-like]");
    if (like) {
      fetch("/api/films/" + like.dataset.like + "/like", {
        method: "POST", credentials: "same-origin"
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          like.classList.toggle("is-on", data.liked);
          like.setAttribute("aria-pressed", data.liked ? "true" : "false");
          like.innerHTML = (data.liked ? HEART : HEART_OUTLINE) +
            '<span class="rail-count">' + data.likes + "</span>";
        })
        .catch(function () {});
      return;
    }
    var send = event.target.closest("[data-send]");
    if (send) openSheet(send.dataset.send);
  });

  /* -- sending ----------------------------------------------------------- */

  function openSheet(filmId) {
    sharing = filmId;
    sheetNote.hidden = true;
    sheetPeople.innerHTML = '<li class="person-note">looking…</li>';
    sheet.hidden = false;
    fetch("/api/people", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { people: [] }; })
      .then(function (data) {
        var rows = data.people || [];
        if (!rows.length) {
          sheetPeople.innerHTML =
            '<li class="person-note">Nobody else has an account here yet.</li>';
          return;
        }
        sheetPeople.innerHTML = rows.map(function (person) {
          people[person.who] = person;
          return '<li><button type="button" class="person" data-to="' +
            escaped(person.who) + '">' + avatar(person.who) +
            '<span><span class="person-name">' + escaped(person.name || person.who) + "</span>" +
            '<span class="person-note">' + person.films +
            (person.films === 1 ? " film" : " films") + "</span></span></button></li>";
        }).join("");
      })
      .catch(function () {
        sheetPeople.innerHTML = '<li class="person-note">Could not load that.</li>';
      });
  }

  function closeSheet() { sheet.hidden = true; sharing = null; }
  document.getElementById("sheet-close").addEventListener("click", closeSheet);
  document.getElementById("sheet-scrim").addEventListener("click", closeSheet);

  sheetPeople.addEventListener("click", function (event) {
    var button = event.target.closest("[data-to]");
    if (!button || !sharing) return;
    button.disabled = true;
    fetch("/api/messages/send", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to: button.dataset.to, film: sharing })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { button.disabled = false; return; }
        button.insertAdjacentHTML("beforeend", '<span class="person-sent">Sent</span>');
        sheetNote.textContent = "It is in your messages with " + button.dataset.to + ".";
        sheetNote.hidden = false;
        if (window.auteurChrome) window.auteurChrome.refreshBadge();
      })
      .catch(function () { button.disabled = false; });
  });

  /* -- everyone, the people you follow, or your own ----------------------- */

  var following = 0;

  scopes.addEventListener("click", function (event) {
    var button = event.target.closest(".feed-scope");
    if (!button || button.dataset.scope === scope) { return; }
    scope = button.dataset.scope;
    Array.prototype.forEach.call(scopes.children, function (tab) {
      var on = tab.dataset.scope === scope;
      tab.classList.toggle("is-on", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    reels.scrollTop = 0;
    load(true);
  });

  /* An empty feed is three different situations, and one sentence for all of
     them is the sentence that is wrong twice. */
  function explainEmpty() {
    var line = document.getElementById("feed-empty-line");
    var sub = document.getElementById("feed-empty-sub");
    var go = document.getElementById("feed-empty-go");
    if (scope === "following") {
      line.textContent = following ? "Nothing from them yet." : "You are not following anybody.";
      sub.textContent = following
        ? "Films from the people you follow show up here."
        : "Open somebody's name on a film to follow them.";
      go.textContent = "Go to Everyone";
      go.href = "#";
      go.dataset.scope = "all";
      return;
    }
    delete go.dataset.scope;
    if (scope === "mine") {
      line.textContent = "You have not finished a film yet.";
      sub.textContent = "Anything you make here shows up on this tab.";
    } else {
      line.textContent = "Nothing here yet.";
      sub.textContent = "Films you finish show up here, newest first.";
    }
    go.textContent = "Make one";
    go.href = "/";
  }

  document.getElementById("feed-empty-go").addEventListener("click", function (event) {
    if (!this.dataset.scope) { return; }
    event.preventDefault();
    var tab = scopes.querySelector('[data-scope="' + this.dataset.scope + '"]');
    if (tab) { tab.click(); }
  });

  /* Opened from a tap on a profile grid: scroll that film into view rather
     than starting at the top of everybody's. */
  function jump() {
    var node = shown[wanted];
    if (!node) { return; }
    wanted = "";
    node.scrollIntoView();
    history.replaceState(null, "", location.pathname);
  }

  load(true);
})();
