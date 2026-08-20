/* Messages: the list, one conversation, and sending.
 *
 * Both screens live in one document and one is hidden. That is not a shortcut
 * — going back to the list has to keep the list's scroll position and its
 * unread state, and a navigation would throw both away and re-fetch. The two
 * apps this is modelled on both push and pop within the tab for the same
 * reason.
 */
(function () {
  var listScreen = document.getElementById("list-screen");
  var threadScreen = document.getElementById("thread-screen");
  var threads = document.getElementById("threads");
  var blank = document.getElementById("list-blank");
  var bubbles = document.getElementById("bubbles");
  var whoLabel = document.getElementById("thread-who");
  var whoAvatar = document.getElementById("thread-avatar");
  var composer = document.getElementById("composer");
  var field = document.getElementById("composer-text");
  var peopleSheet = document.getElementById("people-sheet");
  var peopleList = document.getElementById("people");

  var open = null;   // the name of the conversation on screen, or null
  var timer = null;

  function hueOf(name) {
    var total = 0;
    for (var i = 0; i < name.length; i++) total = (total * 31 + name.charCodeAt(i)) % 360;
    return total;
  }

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  function avatar(name, small) {
    return '<span class="avatar' + (small ? " avatar-sm" : "") + '" style="--hue:' +
      hueOf(name) + '" aria-hidden="true">' + escaped(name[0] || "?") + "</span>";
  }

  /* "now", "14m", "3h", "Tue", "12 Mar" — the same ladder every messages app
     uses, because an ISO timestamp in a list row is unreadable at a glance. */
  function when(stamp) {
    var ago = Date.now() / 1000 - stamp;
    if (ago < 60) return "now";
    if (ago < 3600) return Math.floor(ago / 60) + "m";
    if (ago < 86400) return Math.floor(ago / 3600) + "h";
    var date = new Date(stamp * 1000);
    if (ago < 604800) return date.toLocaleDateString(undefined, { weekday: "short" });
    return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  /* -- the list ------------------------------------------------------------ */

  function loadList() {
    fetch("/api/messages", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { conversations: [] }; })
      .then(function (data) {
        var rows = data.conversations || [];
        blank.hidden = rows.length > 0;
        threads.innerHTML = rows.map(function (row) {
          return '<li><button type="button" class="thread-row' +
            (row.unread ? " is-unread" : "") + '" data-who="' + escaped(row.who) + '">' +
            avatar(row.who) +
            '<span class="thread-lines">' +
              '<span class="thread-name">' + escaped(row.who) + "</span>" +
              '<span class="thread-last">' + (row.mine ? "You: " : "") +
              escaped(row.last) + "</span></span>" +
            '<span class="thread-when">' + when(row.at) + "</span>" +
            (row.unread ? '<span class="thread-dot"></span>' : "") +
            "</button></li>";
        }).join("");
        if (window.auteurChrome) window.auteurChrome.refreshBadge();
      })
      .catch(function () {});
  }

  threads.addEventListener("click", function (event) {
    var row = event.target.closest("[data-who]");
    if (row) show(row.dataset.who);
  });

  /* -- one conversation ---------------------------------------------------- */

  function show(who) {
    open = who;
    whoLabel.textContent = who;
    whoAvatar.textContent = (who[0] || "?");
    whoAvatar.style.setProperty("--hue", hueOf(who));
    listScreen.hidden = true;
    threadScreen.hidden = false;
    bubbles.innerHTML = "";
    loadThread(true);
    /* A conversation somebody is looking at should not need a refresh to show
       what just arrived. Four seconds is slow enough not to be a poll storm
       and fast enough that a reply lands while you are still reading. */
    if (timer) clearInterval(timer);
    timer = setInterval(function () { loadThread(false); }, 4000);
    field.focus();
  }

  function back() {
    open = null;
    if (timer) { clearInterval(timer); timer = null; }
    threadScreen.hidden = true;
    listScreen.hidden = false;
    loadList();
  }

  document.getElementById("thread-back").addEventListener("click", back);

  var lastDrawn = "";
  function loadThread(jump) {
    if (!open) return;
    fetch("/api/messages/" + encodeURIComponent(open), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.who !== open) return;
        var notes = data.messages || [];
        var key = notes.map(function (n) { return n.id; }).join(",");
        /* Redrawing identical bubbles would restart any film playing in one. */
        if (key === lastDrawn) return;
        lastDrawn = key;
        var previous = 0;
        bubbles.innerHTML = notes.map(function (note) {
          var stamp = "";
          /* A time separator when the conversation has a gap in it, which is
             how you tell yesterday's message from this morning's. */
          if (note.at - previous > 3600) {
            stamp = '<span class="bubble-when">' + when(note.at) + "</span>";
          }
          previous = note.at;
          var side = note.sender === open ? "theirs" : "mine";
          if (note.film) {
            var film = (data.films || {})[note.film];
            if (!film) {
              return stamp + '<div class="bubble ' + side +
                '"><em>that film is no longer here</em></div>';
            }
            return stamp + '<figure class="bubble bubble-film ' + side + '">' +
              '<video src="' + film.video + '" poster="' + film.poster +
              '" playsinline muted loop controls preload="none"></video>' +
              "<figcaption>" + escaped(film.heard || film.prompt) + "</figcaption></figure>";
          }
          return stamp + '<div class="bubble ' + side + '">' + escaped(note.text) + "</div>";
        }).join("");
        if (jump || true) bubbles.scrollIntoView({ block: "end" });
        if (window.auteurChrome) window.auteurChrome.refreshBadge();
      })
      .catch(function () {});
  }

  composer.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = field.value.trim();
    if (!text || !open) return;
    field.value = "";
    fetch("/api/messages/send", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to: open, text: text })
    })
      .then(function () { lastDrawn = ""; loadThread(true); })
      .catch(function () { field.value = text; });
  });

  /* -- picking somebody ----------------------------------------------------- */

  function openPeople() {
    peopleSheet.hidden = false;
    peopleList.innerHTML = '<li class="person-note">looking…</li>';
    fetch("/api/people", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { people: [] }; })
      .then(function (data) {
        var people = data.people || [];
        peopleList.innerHTML = people.length
          ? people.map(function (person) {
              return '<li><button type="button" class="person" data-to="' +
                escaped(person.who) + '">' + avatar(person.who) +
                '<span><span class="person-name">' + escaped(person.who) + "</span>" +
                '<span class="person-note">' + person.films +
                (person.films === 1 ? " film" : " films") + "</span></span></button></li>";
            }).join("")
          : '<li class="person-note">Nobody else has an account on this copy yet.</li>';
      })
      .catch(function () {
        peopleList.innerHTML = '<li class="person-note">Could not load that.</li>';
      });
  }

  function closePeople() { peopleSheet.hidden = true; }

  document.getElementById("new-message").addEventListener("click", openPeople);
  document.getElementById("blank-new").addEventListener("click", openPeople);
  document.getElementById("people-close").addEventListener("click", closePeople);
  document.getElementById("people-scrim").addEventListener("click", closePeople);
  peopleList.addEventListener("click", function (event) {
    var button = event.target.closest("[data-to]");
    if (!button) return;
    closePeople();
    lastDrawn = "";
    show(button.dataset.to);
  });

  loadList();
})();
