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
  var scope = document.getElementById("feed-scope");
  var toggle = document.getElementById("feed-toggle");
  var sheet = document.getElementById("share-sheet");
  var sheetPeople = document.getElementById("share-people");
  var sheetNote = document.getElementById("share-note");

  var mine = false;
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

  function avatar(name, small) {
    return '<span class="avatar' + (small ? " avatar-sm" : "") + '" style="--hue:' +
      hueOf(name) + '" aria-hidden="true">' + (name[0] || "?") + "</span>";
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
        '<p class="reel-who">' + avatar(film.owner, true) + escaped(film.owner) + "</p>" +
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
    var url = "/api/feed?" + (mine ? "mine=1&" : "") +
      (!reset && oldest ? "before=" + encodeURIComponent(oldest) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { films: [] }; })
      .then(function (data) {
        var films = data.films || [];
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
        empty.hidden = reels.querySelectorAll(".reel").length > 0;
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
        var people = data.people || [];
        if (!people.length) {
          sheetPeople.innerHTML =
            '<li class="person-note">Nobody else has an account here yet.</li>';
          return;
        }
        sheetPeople.innerHTML = people.map(function (person) {
          return '<li><button type="button" class="person" data-to="' +
            escaped(person.who) + '">' + avatar(person.who) +
            '<span><span class="person-name">' + escaped(person.who) + "</span>" +
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

  /* -- everyone, or just me ---------------------------------------------- */

  toggle.addEventListener("click", function () {
    mine = !mine;
    scope.textContent = mine ? "Yours" : "Everyone";
    toggle.textContent = mine ? "Everyone" : "Just mine";
    reels.scrollTop = 0;
    load(true);
  });

  load(true);
})();
