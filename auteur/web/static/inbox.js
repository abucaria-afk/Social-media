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
  var whoProfile = document.getElementById("thread-profile");

  var open = null;   // the name of the conversation on screen, or null
  var timer = null;
  /* username -> { name, picture }. Filled from whatever answer mentioned them,
     so a row can be drawn without a request per person. */
  var people = {};

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

  /* Everybody here, once, so the list rows have pictures and chosen names on
     them. The list itself is a store of conversations and knows only
     usernames; this is the one request that turns those into people. */
  function loadPeople() {
    return fetch("/api/people", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { people: [] }; })
      .then(function (data) {
        (data.people || []).forEach(function (person) { people[person.who] = person; });
        return data;
      })
      .catch(function () { return { people: [] }; });
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
              '<span class="thread-name">' + escaped(nameOf(row.who)) + "</span>" +
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
    whoLabel.textContent = nameOf(who);
    var card = people[who] || {};
    if (card.picture) {
      whoAvatar.innerHTML = '<img src="' + escaped(card.picture) +
        '" alt="" width="30" height="30">';
    } else {
      whoAvatar.textContent = (who[0] || "?");
      whoAvatar.style.setProperty("--hue", hueOf(who));
    }
    /* The name in the bar opens their profile, which is where it goes in every
       messages app anybody has used. */
    whoProfile.href = "/u/" + encodeURIComponent(who);
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

  /* Reporting a conversation reports its last message and, by default, blocks
     the person — which is what somebody opening this menu almost always
     wants, and making it two journeys through two screens is how people end
     up doing neither. */
  document.getElementById("thread-more").addEventListener("click", function () {
    if (!open || !window.auteurSafety) { return; }
    var last = bubbles.lastElementChild;
    window.auteurSafety.open(
      "message",
      (last && last.dataset.note) || open,
      open,
      nameOf(open)
    );
  });

  if (window.auteurSafety) {
    /* Blocked from in here means this conversation is gone; the list behind
       it is where there is still something to look at. */
    window.auteurSafety.onDone = function (said) {
      if (said && said.blocked) { back(); }
    };
  }

  var lastDrawn = "";
  function loadThread(jump) {
    if (!open) return;
    fetch("/api/messages/" + encodeURIComponent(open), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.who !== open) return;
        if (data.closed) {
          /* Blocked, either way round. The same answer both ways on purpose:
             telling somebody they have been blocked turns a wall into a
             notification. */
          bubbles.innerHTML = '<p class="bubble-note">This conversation is closed.</p>';
          lastDrawn = "closed";
          if (timer) { clearInterval(timer); timer = null; }
          return;
        }
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
            return stamp + '<figure class="bubble bubble-film ' + side +
              '" data-note="' + escaped(note.id) + '">' +
              '<video src="' + film.video + '" poster="' + film.poster +
              '" playsinline muted loop controls preload="none"></video>' +
              "<figcaption>" + escaped(film.heard || film.prompt) + "</figcaption></figure>";
          }
          return stamp + '<div class="bubble ' + side + '" data-note="' + escaped(note.id) +
            '">' + escaped(note.text) + "</div>";
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
    loadPeople()
      .then(function (data) {
        var rows = data.people || [];
        peopleList.innerHTML = rows.length
          ? rows.map(function (person) {
              return '<li><button type="button" class="person" data-to="' +
                escaped(person.who) + '">' + avatar(person.who) +
                '<span><span class="person-name">' + escaped(person.name || person.who) + "</span>" +
                '<span class="person-note">' + person.films +
                (person.films === 1 ? " film" : " films") + "</span></span></button></li>";
            }).join("")
          : '<li class="person-note">Nobody else has an account on this copy yet.</li>';
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

  /* `?who=<name>` opens straight into that conversation. It is how the Message
     button on somebody's profile gets here, and it means a conversation is a
     link rather than a place you have to go and find in a list. */
  function askedFor() {
    var match = /[?&]who=([^&]+)/.exec(location.search);
    if (!match) { return ""; }
    try { return decodeURIComponent(match[1]); } catch (e) { return ""; }
  }

  /* People first: the list and the conversation header both draw names and
     pictures out of it, and drawing them before it arrives means every row
     shows a username and then silently changes. */
  loadPeople().then(function () {
    loadList();
    var straight = askedFor();
    if (straight) {
      /* Taken out of the address once used, so going back to the list and
         then reloading does not re-open the conversation. */
      history.replaceState(null, "", location.pathname);
      show(straight);
    }
  });
})();
