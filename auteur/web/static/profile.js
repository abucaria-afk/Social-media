/* The profile page: yours, or somebody else's.
 *
 * One document serving two screens, decided by the address. `/profile` is
 * yours; `/u/<name>` is theirs and is a link you can send somebody. The
 * account settings and the accessibility settings are only ever revealed after
 * the server has confirmed the profile is yours — the markup ships them
 * `hidden` and nothing here un-hides them on any other path.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function escaped(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* The same hash the feed and the inbox use, so somebody without a picture is
     one colour across the whole app rather than three. */
  function hueOf(name) {
    var total = 0;
    for (var i = 0; i < name.length; i++) { total = (total * 31 + name.charCodeAt(i)) % 360; }
    return total;
  }

  /* Whose profile this is, from the address. `/u/<name>` carries a name that
     has been through encodeURIComponent, so it comes back through decode. */
  function wanted() {
    var path = location.pathname;
    if (path.indexOf("/u/") === 0) {
      try { return decodeURIComponent(path.slice(3)).replace(/\/+$/, ""); }
      catch (e) { return path.slice(3); }
    }
    return "";
  }

  var who = wanted();
  var live = null;   // the profile as the server last described it
  var me = "";       // my own username, once known

  // ---------------------------------------------------------------- drawing

  function draw(profile, films) {
    live = profile;
    var mine = !!profile.me;

    document.title = "Auteur — " + profile.name;
    $("bar-name").textContent = profile.name;
    $("big-name").textContent = profile.name;

    var pic = $("picture");
    pic.style.setProperty("--hue", hueOf(profile.who));
    pic.dataset.mine = mine ? "1" : "";
    pic.setAttribute(
      "aria-label",
      mine ? "Change your picture" : profile.name + "'s picture"
    );
    if (!mine) { pic.setAttribute("tabindex", "-1"); }
    var img = $("picture-img");
    if (profile.picture) {
      img.src = profile.picture;
      /* Empty alt, not the name: the name is already the heading right beside
         it, and a screen reader reading it twice is noise. */
      img.alt = "";
      img.hidden = false;
      $("picture-initial").hidden = true;
    } else {
      img.hidden = true;
      img.removeAttribute("src");
      $("picture-initial").hidden = false;
      $("picture-initial").textContent = (profile.who[0] || "?");
    }
    $("picture-badge").hidden = !mine;

    $("count-films").textContent = profile.films;
    $("count-followers").textContent = profile.followers;
    $("count-following").textContent = profile.following;

    var bio = $("bio");
    bio.textContent = profile.bio || "";
    bio.hidden = !profile.bio;

    var link = $("link");
    if (profile.link) {
      link.href = profile.link;
      /* Shown without the scheme, which is what every app does and what
         everybody reads anyway. The href keeps it. */
      link.textContent = profile.link.replace(/^https?:\/\//, "").replace(/\/$/, "");
      link.hidden = false;
    } else {
      link.hidden = true;
      link.removeAttribute("href");
    }

    var blocked = !mine && !!profile.you_block;
    $("edit-open").hidden = !mine;
    $("edit-profile").hidden = !mine;
    $("more").hidden = mine;
    $("unblock").hidden = !blocked;
    /* Blocked: the page stays — you may want to undo it — but there is
       nothing on it to follow, write to, or watch. */
    $("follow").hidden = mine || blocked;
    $("message").hidden = mine || blocked;
    $("blocked-note").hidden = !blocked;
    if (blocked) {
      $("blocked-note").textContent =
        "You have blocked " + profile.name + ". Their films are out of your feed, " +
        "neither of you can write to the other, and they cannot see yours.";
    }
    $("mine").hidden = !mine;
    $("settings").hidden = !mine;
    $("back").hidden = mine;
    $("blank-go").hidden = !mine;

    if (blocked) {
      $("films-label").hidden = true;
      $("films-blank").hidden = true;
    } else {
      $("films-label").hidden = false;
    }

    if (!mine) {
      markFollow(profile.you_follow);
      $("blank-line").textContent = profile.name + " has not made anything yet.";
      $("blank-sub").textContent = "Films they finish here will show up on this page.";
    }

    grid(blocked ? [] : (films || []), profile);
  }

  function markFollow(following) {
    var button = $("follow");
    button.textContent = following ? "Following" : "Follow";
    button.classList.toggle("is-following", !!following);
    button.setAttribute("aria-pressed", following ? "true" : "false");
  }

  function grid(films, profile) {
    var box = $("films");
    $("films-label").textContent = profile.me ? "Your films" : "Films";
    if (profile.you_block) { box.innerHTML = ""; $("films-blank").hidden = true; return; }
    if (!films.length) {
      box.innerHTML = "";
      $("films-blank").hidden = false;
      return;
    }
    $("films-blank").hidden = true;
    box.innerHTML = films.map(function (film) {
      return '<button type="button" class="grid-cell" data-film="' + escaped(film.id) +
        '" aria-label="' + escaped(film.prompt || "a film") + '">' +
        '<img src="' + escaped(film.poster) + '" alt="" loading="lazy">' +
        (film.likes ? '<span class="grid-likes">♥ ' + film.likes + "</span>" : "") +
        "</button>";
    }).join("");
  }

  function trouble(message) {
    var box = $("page-error");
    box.textContent = message;
    box.hidden = !message;
  }

  // ---------------------------------------------------------------- loading

  function load() {
    var url = who ? "/api/profiles/" + encodeURIComponent(who) : "/api/profile";
    return fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; return null; }
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (got) {
        if (!got) { return; }
        if (!got.ok) { trouble(got.data.error || "That profile could not be opened."); return; }
        trouble("");
        var profile = got.data.profile;
        if (!who) {
          me = profile.who;
          $("email").textContent = profile.email || "not set";
          twoStepState();
          reportsState();
          restrictionState();
        }
        draw(profile, got.data.films);
        /* `/api/profile` answers about you and does not carry films; the
           by-name route does. */
        if (!who) { films(); }
      })
      .catch(function () { trouble("Could not reach the app."); });
  }

  /* Your own films are not in `/api/profile` — that answer is about you, and
     the feed already knows how to list films by owner. */
  function films() {
    fetch("/api/feed?scope=mine", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data && live) { grid(data.films, live); } })
      .catch(function () {});
  }

  // ------------------------------------------------------------------ edit

  function openSheet(name) {
    $(name + "-sheet").hidden = false;
    document.body.classList.add("sheet-open");
  }

  function closeSheet(name) {
    $(name + "-sheet").hidden = true;
    document.body.classList.remove("sheet-open");
  }

  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-close]");
    if (closer) { closeSheet(closer.dataset.close); }
  });

  function openEdit() {
    if (!live) { return; }
    $("edit-name").value = live.name === live.who ? "" : live.name;
    $("edit-bio").value = live.bio || "";
    $("edit-link").value = live.link || "";
    $("picture-remove").hidden = !live.picture;
    $("edit-error").hidden = true;
    countBio();
    openSheet("edit");
  }

  function countBio() {
    var left = 150 - $("edit-bio").value.length;
    $("bio-left").textContent = left;
    $("bio-left").parentNode.classList.toggle("is-full", left <= 0);
  }

  $("edit-open").addEventListener("click", openEdit);
  $("edit-profile").addEventListener("click", openEdit);
  $("edit-bio").addEventListener("input", countBio);

  $("edit-save").addEventListener("click", function () {
    var problem = $("edit-error");
    problem.hidden = true;
    fetch("/api/profile", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("edit-name").value,
        bio: $("edit-bio").value,
        link: $("edit-link").value
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          problem.textContent = got.d.error || "That did not save.";
          problem.hidden = false;
          return;
        }
        closeSheet("edit");
        draw(got.d.profile, null);
        films();
      })
      .catch(function () {
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  });

  // --------------------------------------------------------------- picture

  $("picture").addEventListener("click", function () {
    if (live && live.me) { $("picture-file").click(); }
  });

  $("picture-file").addEventListener("change", function () {
    var file = this.files && this.files[0];
    this.value = "";  /* so choosing the same file twice fires again */
    if (file) { sendPicture(file); }
  });

  /* Downscaled here before it is sent, and that is not only politeness about
     bandwidth. A photograph off an iPhone is four megabytes of HEIC; the
     browser can already decode it and a canvas cannot hand back anything but
     a real image, so what reaches the network is a square JPEG of about sixty
     kilobytes whatever the camera produced. The server re-encodes regardless —
     this is a convenience, never the check.
     `imageOrientation: "from-image"` is the part that is easy to miss: without
     it a portrait photograph arrives on its side, because the rotation lives
     in a tag rather than in the pixels. */
  function shrink(file) {
    var SIDE = 512;
    if (!window.createImageBitmap || !document.createElement("canvas").toBlob) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: "from-image" })
      .then(function (bitmap) {
        var side = Math.min(bitmap.width, bitmap.height);
        var out = Math.min(side, SIDE);
        var canvas = document.createElement("canvas");
        canvas.width = out;
        canvas.height = out;
        var pen = canvas.getContext("2d");
        pen.drawImage(
          bitmap,
          (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side,
          0, 0, out, out
        );
        bitmap.close();
        return new Promise(function (done) {
          canvas.toBlob(function (blob) { done(blob || file); }, "image/jpeg", 0.86);
        });
      })
      .catch(function () { return file; });
  }

  function sendPicture(file) {
    trouble("");
    shrink(file).then(function (blob) {
      return fetch("/api/profile/picture", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": blob.type || "image/jpeg" },
        body: blob
      });
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) { trouble(got.d.error || "That picture did not work."); return; }
        draw(got.d.profile, null);
        films();
      })
      .catch(function () { trouble("That picture could not be sent."); });
  }

  $("picture-remove").addEventListener("click", function () {
    fetch("/api/profile/picture/remove", { method: "POST", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        closeSheet("edit");
        draw(data.profile, null);
        films();
      })
      .catch(function () {});
  });

  // ---------------------------------------------------------------- follow

  $("follow").addEventListener("click", function () {
    if (!live) { return; }
    var wanted = !live.you_follow;
    /* Marked before the request answers. A follow button that waits for a
       round trip on a home wifi feels broken; if the request fails the next
       line puts it back. */
    markFollow(wanted);
    fetch("/api/profiles/" + encodeURIComponent(live.who) + "/follow", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ follow: wanted })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { markFollow(!wanted); return; }
        live = data.profile;
        markFollow(live.you_follow);
        $("count-followers").textContent = live.followers;
      })
      .catch(function () { markFollow(!wanted); });
  });

  $("more").addEventListener("click", function () {
    if (live && window.auteurSafety) {
      window.auteurSafety.open("person", live.who, live.who, live.name);
    }
  });

  $("unblock").addEventListener("click", function () {
    if (!live || !window.auteurSafety) { return; }
    window.auteurSafety.block(live.who, false).then(function (data) {
      if (!data) { return; }
      draw(data.profile, null);
      load();
      window.auteurSafety.said("Unblocked. You will see " + data.profile.name + " again.");
    });
  });

  $("message").addEventListener("click", function () {
    if (live) { location.href = "/inbox?who=" + encodeURIComponent(live.who); }
  });

  $("back").addEventListener("click", function () {
    /* Back if there is anywhere to go back to, and the feed if this page was
       opened from a link somebody sent. `history.length` is the only thing a
       page can ask, and it counts the whole tab — hence the referrer check
       first, which is what actually distinguishes the two. */
    if (document.referrer && document.referrer.indexOf(location.origin) === 0) {
      history.back();
    } else {
      location.href = "/feed";
    }
  });

  // ----------------------------------------------------------- who follows

  function people(which) {
    var name = live ? live.who : "";
    $("people-title").textContent = which === "following" ? "Following" : "Followers";
    $("people-list").innerHTML = "";
    $("people-empty").hidden = true;
    openSheet("people");
    fetch("/api/profiles/" + encodeURIComponent(name) + "/" + which, {
      credentials: "same-origin",
      cache: "no-store"
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        if (!data.people.length) {
          $("people-empty").textContent = which === "following"
            ? "Not following anybody yet."
            : "No followers yet.";
          $("people-empty").hidden = false;
          return;
        }
        $("people-list").innerHTML = data.people.map(function (row) {
          var face = row.picture
            ? '<span class="avatar"><img src="' + escaped(row.picture) +
              '" alt="" width="44" height="44"></span>'
            : '<span class="avatar" style="--hue:' + hueOf(row.who) + '" aria-hidden="true">' +
              escaped(row.who[0] || "?") + "</span>";
          return '<li><a class="people-row" href="/u/' + encodeURIComponent(row.who) + '">' +
            face +
            '<span class="people-lines"><span class="people-name">' + escaped(row.name) +
            '</span><span class="people-who">@' + escaped(row.who) + "</span></span>" +
            (row.me ? '<span class="people-mark">you</span>'
                    : (row.you_follow ? '<span class="people-mark">following</span>' : "")) +
            "</a></li>";
        }).join("");
      })
      .catch(function () {});
  }

  /* What this copy recorded about what you watched.
   *
   * Shown because the privacy policy says it is shown. A history somebody
   * cannot look at is a history they have no way to judge, and "we measure
   * watch time to rank the feed" is a sentence that should come with the
   * numbers attached rather than as a claim in a document nobody opens. */
  function watchRow(what, how, done) {
    return '<div class="stack-row"><span class="what">' + escaped(what) +
      '</span><span class="how' + (done ? " done" : "") + '">' +
      escaped(how) + "</span></div>";
  }

  $("my-watching").addEventListener("click", function () {
    $("watching-list").innerHTML = "";
    $("watching-mine").innerHTML = "";
    $("watching-mine-title").hidden = true;
    openSheet("watching");
    fetch("/api/watching", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        var seen = data.watched || [];
        $("watching-list").innerHTML = seen.length
          ? seen.map(function (row) {
              var mins = row.seconds >= 60
                ? Math.round(row.seconds / 60) + " min"
                : Math.round(row.seconds) + "s";
              return watchRow(
                row.prompt || "a film since deleted",
                (row.finished ? "finished · " : "") + mins +
                  (row.plays > 1 ? " · " + row.plays + " times" : ""),
                row.finished
              );
            }).join("")
          : '<p class="stack-empty">Nothing yet. Watch something in the feed.</p>';

        var mine = (data.films || []).filter(function (f) { return f.plays; });
        if (mine.length) {
          $("watching-mine-title").hidden = false;
          $("watching-mine").innerHTML = mine.map(function (f) {
            var done = f.plays ? Math.round((f.finishes / f.plays) * 100) : 0;
            return watchRow(
              f.prompt || f.film,
              f.plays + (f.plays === 1 ? " play" : " plays") + " · " +
                done + "% finished",
              done >= 60
            );
          }).join("");
        }
      })
      .catch(function () {});
  });

  $("watching-close").addEventListener("click", function () { closeSheet("watching"); });

  /* What you reported, and what came of it — plus who you have blocked, with
     a way back. A report whose outcome you can never see is a button people
     press once and then stop believing in. */
  $("my-reports").addEventListener("click", function () {
    $("reported-list").innerHTML = "";
    $("blocked-list").innerHTML = "";
    $("reported-empty").hidden = true;
    $("blocked-label").hidden = true;
    openSheet("reports");
    fetch("/api/reports", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        var said = {
          open: "Waiting on whoever runs this copy",
          removed: "Removed",
          kept: "Looked at, and left up"
        };
        if (!data.reports.length) {
          $("reported-empty").hidden = false;
        } else {
          $("reported-list").innerHTML = data.reports.map(function (row) {
            return "<li>" + escaped(data.reasons[row.reason] || row.reason) +
              ', a ' + escaped(row.kind) +
              '<span class="reported-when">' + when(row.at) + " · " +
              '<span class="reported-state is-' + escaped(row.state) + '">' +
              escaped(said[row.state] || row.state) + "</span></span></li>";
          }).join("");
        }
        if (data.blocked.length) {
          $("blocked-label").hidden = false;
          $("blocked-list").innerHTML = data.blocked.map(function (name) {
            return '<li class="people-row"><span class="avatar" style="--hue:' +
              hueOf(name) + '" aria-hidden="true">' + escaped(name[0] || "?") + "</span>" +
              '<span class="people-lines"><span class="people-name">@' + escaped(name) +
              "</span></span>" +
              '<button type="button" class="unblock-row" data-unblock="' + escaped(name) +
              '">Unblock</button></li>';
          }).join("");
        }
      })
      .catch(function () {});
  });

  $("blocked-list").addEventListener("click", function (event) {
    var button = event.target.closest("[data-unblock]");
    if (!button || !window.auteurSafety) { return; }
    button.disabled = true;
    window.auteurSafety.block(button.dataset.unblock, false).then(function () {
      button.closest("li").remove();
      if (!$("blocked-list").children.length) { $("blocked-label").hidden = true; }
    });
  });

  /* "now", "14m", "3h", "12 Mar" — the same ladder the inbox uses. */
  function when(stamp) {
    var ago = Date.now() / 1000 - stamp;
    if (ago < 60) { return "just now"; }
    if (ago < 3600) { return Math.floor(ago / 60) + "m ago"; }
    if (ago < 86400) { return Math.floor(ago / 3600) + "h ago"; }
    return new Date(stamp * 1000).toLocaleDateString(undefined,
      { day: "numeric", month: "short" });
  }

  $("open-followers").addEventListener("click", function () { people("followers"); });
  $("open-following").addEventListener("click", function () { people("following"); });

  // ------------------------------------------------------------- the films

  $("films").addEventListener("click", function (event) {
    var cell = event.target.closest(".grid-cell");
    if (!cell) { return; }
    /* The feed is where a film is watched — it has the player, the heart and
       the send button, and building a second one here would be a second one to
       keep right. It opens on this film rather than at the top. */
    location.href = "/feed?film=" + encodeURIComponent(cell.dataset.film);
  });

  // ---------------------------------------------------------------- account

  /* What this account is shown, and the code that keeps the switch out of
     reach of the person it applies to.
     Turning it *on* never needs the code — anybody may choose to see less.
     Turning it off does, which is the entire point. */
  var restriction = { on: false, locked: false, digits: 4 };

  function restrictionState() {
    return fetch("/api/restriction", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        restriction = data;
        $("restriction-state").textContent = data.on ? "Hidden" : "Shown";
        $("restriction-row").dataset.on = data.on ? "1" : "";
        return data;
      })
      .catch(function () {});
  }

  /* Who else may have an account here.
   *
   * A row rather than a sheet: there are two states and one of them shows a
   * code. A sheet would be a screen to open in order to press one thing.
   */
  function drawJoining(data) {
    var open = !!(data && data.open);
    $("joining-state").textContent = open ? "On" : "Off";
    $("joining-row").dataset.on = open ? "1" : "";
    $("joining-code-row").hidden = !(open && data.code);
    $("joining-code").textContent = (data && data.code) || "";
  }

  function loadJoining() {
    fetch("/api/joining", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) { drawJoining(data); } })
      .catch(function () { /* signed out, or offline */ });
  }
  loadJoining();

  $("joining-row").addEventListener("click", function () {
    /* The current state is on the row, so the click knows which way it is
       going without a second request first. */
    var turningOn = $("joining-row").dataset.on !== "1";
    $("joining-state").textContent = "…";
    fetch("/api/joining", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ open: turningOn })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { drawJoining(data || { open: false }); })
      .catch(function () { loadJoining(); });
  });

  $("restriction-row").addEventListener("click", function () {
    $("restriction-error").hidden = true;
    $("restriction-off-error").hidden = true;
    $("restriction-code").value = "";
    $("restriction-new-lock").value = "";
    $("restriction-want-lock").checked = false;
    $("restriction-lock-box").hidden = true;

    $("restriction-on").hidden = restriction.on;
    $("restriction-off").hidden = !restriction.on;
    $("restriction-code-box").hidden = !restriction.locked;
    $("restriction-off-note").textContent = restriction.locked
      ? "Sensitive films are hidden from this account, and lifting that needs the code."
      : "Sensitive films are hidden from this account.";
    /* Said plainly when it was set for them rather than by them, so the state
       is not a mystery. */
    $("restriction-why").textContent =
      restriction.minor && restriction.on
        ? "This started on because this account is under 18."
        : "";
    openSheet("restriction");
  });

  $("restriction-want-lock").addEventListener("change", function () {
    $("restriction-lock-box").hidden = !this.checked;
    if (this.checked) { $("restriction-new-lock").focus(); }
  });

  $("restriction-go").addEventListener("click", function () {
    var problem = $("restriction-error");
    problem.hidden = true;
    var body = { on: true };
    if ($("restriction-want-lock").checked) {
      body.lock = $("restriction-new-lock").value;
    }
    fetch("/api/restriction", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          problem.textContent = got.d.error || "That did not work.";
          problem.hidden = false;
          return;
        }
        restriction = got.d;
        $("restriction-state").textContent = "Hidden";
        closeSheet("restriction");
        if (window.auteurSafety) {
          window.auteurSafety.said("Sensitive films are hidden from this account.");
        }
      })
      .catch(function () {
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  });

  $("restriction-lift").addEventListener("click", function () {
    var problem = $("restriction-off-error");
    problem.hidden = true;
    fetch("/api/restriction", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: false, code: $("restriction-code").value })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          problem.textContent = got.d.error || "That did not work.";
          problem.hidden = false;
          $("restriction-code").value = "";
          return;
        }
        restriction = got.d;
        $("restriction-state").textContent = "Shown";
        closeSheet("restriction");
        if (window.auteurSafety) {
          window.auteurSafety.said("Everything is shown on this account again.");
        }
      })
      .catch(function () {
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  });

  /* Deleting the account, which is the one thing in here that cannot be
     undone. Two gates, both deliberate: the password, because a live session
     is not proof of who is holding the phone; and the word typed out, because
     the difference between a tap and a mis-tap has to be more than a
     millimetre. */
  $("delete-account").addEventListener("click", function () {
    $("delete-password").value = "";
    $("delete-confirm").value = "";
    $("delete-error").hidden = true;
    if (live) {
      $("delete-what").textContent =
        live.films === 1
          ? "This removes your account, the film on it, and everything else you have made here."
          : "This removes your account, your " + live.films +
            " films, and everything else you have made here.";
    }
    openSheet("delete");
  });

  $("delete-go").addEventListener("click", function () {
    var problem = $("delete-error");
    var button = $("delete-go");
    problem.hidden = true;
    button.disabled = true;
    fetch("/api/profile/delete", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        password: $("delete-password").value,
        confirm: $("delete-confirm").value
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        button.disabled = false;
        if (!got.ok) {
          problem.textContent = got.d.error || "That did not work.";
          problem.hidden = false;
          return;
        }
        /* `replace`, not `href`: the profile is gone, and leaving it in the
           history is a back button that lands on a 401. */
        location.replace("/login");
      })
      .catch(function () {
        button.disabled = false;
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  });

  $("sign-out").addEventListener("click", function () {
    fetch("/api/logout", { method: "POST", credentials: "same-origin" })
      .then(function () { location.href = "/login"; })
      .catch(function () { location.href = "/login"; });
  });

  /* Two-step verification. It was on the edit room's home screen, which is
     where the settings used to be; this page is where they are now. */
  (function () {
    var row = $("two-step-row");
    var sheet = $("two-step-sheet");
    var state = $("two-step-state");

    function show(id, on) { var e = $(id); if (e) { e.hidden = !on; } }

    window.twoStepState = function () {
      fetch("/api/two-step", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (said) {
          if (!said) { state.textContent = ""; return; }
          state.textContent = said.on
            ? "On · " + said.recovery_left + " recovery codes left"
            : "Off";
          row.dataset.on = said.on ? "1" : "";
        })
        .catch(function () { state.textContent = ""; });
    };

    row.addEventListener("click", function () {
      openSheet("two-step");
      show("two-step-recovery", false);
      if (row.dataset.on) {
        show("two-step-setup", false);
        show("two-step-remove", true);
        return;
      }
      show("two-step-remove", false);
      show("two-step-setup", true);
      /* The key takes a round trip. An empty box for that moment reads as a
         dialog that failed to load, so it says what it is waiting for. */
      $("two-step-secret").textContent = "asking for a key…";
      $("two-step-error").hidden = true;
      fetch("/api/two-step/start", { method: "POST", credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (said) {
          if (!said) {
            $("two-step-secret").textContent = "";
            $("two-step-error").textContent = "Could not reach it.";
            $("two-step-error").hidden = false;
            return;
          }
          $("two-step-secret").textContent = said.secret;
          $("two-step-uri").href = said.uri;
        })
        .catch(function () {
          $("two-step-secret").textContent = "";
          $("two-step-error").textContent = "Could not reach it.";
          $("two-step-error").hidden = false;
        });
    });

    $("two-step-confirm").addEventListener("click", function () {
      var problem = $("two-step-error");
      problem.hidden = true;
      fetch("/api/two-step/confirm", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: $("two-step-code").value.trim() })
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (got) {
          if (!got.ok) {
            problem.textContent = got.d.error || "That did not work.";
            problem.hidden = false;
            return;
          }
          show("two-step-setup", false);
          show("two-step-recovery", true);
          $("recovery-list").innerHTML = got.d.recovery
            .map(function (c) { return "<li>" + escaped(c) + "</li>"; })
            .join("");
          window.twoStepState();
        })
        .catch(function () {
          problem.textContent = "Could not reach it.";
          problem.hidden = false;
        });
    });

    $("recovery-copy").addEventListener("click", function () {
      var text = [].map.call($("recovery-list").children, function (li) {
        return li.textContent;
      }).join("\n");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () {
          $("recovery-copy").textContent = "Copied";
        }).catch(function () {});
      }
    });

    $("two-step-off").addEventListener("click", function () {
      var problem = $("two-step-off-error");
      problem.hidden = true;
      fetch("/api/two-step/off", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("two-step-password").value })
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (got) {
          if (!got.ok) {
            problem.textContent = got.d.error || "That did not work.";
            problem.hidden = false;
            return;
          }
          $("two-step-password").value = "";
          closeSheet("two-step");
          window.twoStepState();
        })
        .catch(function () {
          problem.textContent = "Could not reach it.";
          problem.hidden = false;
        });
    });

    $("two-step-close").addEventListener("click", function () { closeSheet("two-step"); });
    $("two-step-scrim").addEventListener("click", function () { closeSheet("two-step"); });

    sheet.hidden = true;
  })();

  function twoStepState() { if (window.twoStepState) { window.twoStepState(); } }

  function reportsState() {
    fetch("/api/reports", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        var waiting = data.reports.filter(function (r) { return r.state === "open"; }).length;
        $("reports-state").textContent = waiting
          ? waiting + " waiting"
          : (data.blocked.length
              ? data.blocked.length + (data.blocked.length === 1 ? " blocked" : " blocked")
              : "");
      })
      .catch(function () {});
  }

  if (window.auteurSafety) {
    window.auteurSafety.onDone = function () { load(); };
  }

  load();
})();
