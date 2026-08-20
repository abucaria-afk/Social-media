/* The manager: the board, one plan, and making a new one.
 *
 * Two screens in one document for the same reason the inbox has two — coming
 * back to the board should keep its scroll position rather than re-fetch.
 *
 * Nothing in this file posts anything anywhere. The only button that mentions
 * posting says "I posted this", and all it does is move a row.
 */
(function () {
  var boardScreen = document.getElementById("board-screen");
  var planScreen = document.getElementById("plan-screen");
  var plansList = document.getElementById("plans");
  var blank = document.getElementById("board-blank");
  var sheet = document.getElementById("new-sheet");

  var open = null;      // the plan on screen
  var platforms = [];

  function $(id) { return document.getElementById(id); }

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) { return { day: "?", month: "", full: iso }; }
    return {
      day: String(d.getDate()),
      month: d.toLocaleDateString(undefined, { month: "short" }),
      full: d.toLocaleString(undefined, {
        weekday: "long", day: "numeric", month: "long",
        hour: "numeric", minute: "2-digit"
      })
    };
  }

  /* -- the board ----------------------------------------------------------- */

  function loadBoard() {
    fetch("/api/plans", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (said) {
        if (!said) { return; }
        platforms = said.platforms || [];
        fillPlatforms();
        var rows = said.plans || [];
        blank.hidden = rows.length > 0;
        plansList.innerHTML = rows.map(function (plan) {
          var w = when(plan.when);
          return '<li><button type="button" class="plan-row" data-plan="' + escaped(plan.id) + '">' +
            '<span class="plan-when"><span class="plan-day">' + escaped(w.day) + '</span>' +
            '<span class="plan-month">' + escaped(w.month) + "</span></span>" +
            '<span class="plan-lines">' +
              '<span class="plan-title">' + escaped(plan.title) + "</span>" +
              '<span class="plan-where">' + escaped(plan.platform_name || plan.platform) +
              " · " + escaped(plan.seconds) + "s · " +
              escaped((plan.captures || []).length) + " to shoot</span></span>" +
            '<span class="plan-state is-' + escaped(plan.status) + '">' +
            escaped(plan.status) + "</span></button></li>";
        }).join("");
      })
      .catch(function () { /* signed out */ });
  }

  plansList.addEventListener("click", function (event) {
    var row = event.target.closest("[data-plan]");
    if (row) { show(row.dataset.plan); }
  });

  /* -- one plan ------------------------------------------------------------ */

  function show(planId) {
    fetch("/api/plans/" + encodeURIComponent(planId), { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (said) {
        if (!said || !said.plan) { return; }
        open = said.plan;
        draw(open);
        boardScreen.hidden = true;
        planScreen.hidden = false;
        window.scrollTo(0, 0);
      })
      .catch(function () {});
  }

  var MARKS = { pass: "✓", warn: "!", fail: "✗" };

  function draw(plan) {
    $("plan-name").textContent = plan.title;
    $("plan-title").textContent = plan.title;
    var w = when(plan.when);
    $("plan-when").textContent = w.full + " · " + (plan.platform_name || plan.platform) +
      (plan.platform_spec ? " · " + plan.platform_spec : "");

    var check = plan.check || { findings: [] };
    $("findings").innerHTML = (check.findings || []).map(function (f) {
      return '<li class="finding is-' + escaped(f.verdict) + '">' +
        '<span class="finding-mark" aria-hidden="true">' + (MARKS[f.verdict] || "?") + "</span>" +
        '<span><span class="finding-name">' + escaped(f.name) + "</span>" +
        '<span class="finding-detail">' + escaped(f.detail) + "</span>" +
        '<span class="finding-source">against ' + escaped(f.source) + "</span></span></li>";
    }).join("");

    /* The model's number is never shown without the sentence that says what it
     * was fitted on. If there is no number, the reason is shown instead. */
    var note = $("model-note");
    if (check.predicted === null || check.predicted === undefined) {
      note.textContent = "No score: " + (check.provenance || "no model");
    } else {
      note.textContent = "The scoring model says " + Number(check.predicted).toFixed(2) +
        " — " + (check.provenance || "no provenance recorded");
    }

    /* The capture list, not the timeline. A twenty second hypercut is a
     * hundred and ten shots and nobody goes out and shoots a hundred and ten
     * things — they shoot a dozen setups and the edit cuts among them. The
     * timeline is still there and still checked; this is what you take out. */
    var caps = plan.captures || [];
    var shots = plan.shots || [];
    $("shot-summary").textContent = caps.length
      ? caps.length + " things to go and get. The edit cuts among them " +
        shots.length + " times over " + plan.seconds + " seconds — so a setup " +
        "used fourteen times has to be worth looking at fourteen times."
      : "No shot list yet.";
    $("shotlist").innerHTML = caps.map(function (c, i) {
      return '<li class="shot"><span class="shot-n">' + (i + 1) + "</span>" +
        '<span><span class="shot-what">' + escaped(c.what) + "</span>" +
        '<span class="shot-role">' + escaped(c.role) + " · used " + escaped(c.times) +
        (c.times === 1 ? " time" : " times") + "</span></span>" +
        '<span class="shot-secs">' + Number(c.seconds).toFixed(1) + "s</span></li>";
    }).join("");

    $("edit-caption").value = plan.caption || "";
    $("edit-tags").value = (plan.hashtags || []).join(", ");
    $("edit-alt").value = plan.alt_text || "";
    $("plan-make").href = "/?plan=" + encodeURIComponent(plan.id);
    $("plan-posted").hidden = plan.status === "posted";
  }

  function back() {
    open = null;
    planScreen.hidden = true;
    boardScreen.hidden = false;
    loadBoard();
  }

  $("plan-back").addEventListener("click", back);

  function save() {
    if (!open) { return; }
    var tags = $("edit-tags").value.split(/[,\n]/).map(function (t) { return t.trim(); });
    fetch("/api/plans/" + encodeURIComponent(open.id), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        caption: $("edit-caption").value,
        hashtags: tags.filter(Boolean),
        alt_text: $("edit-alt").value
      })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (said) { if (said && said.plan) { open = said.plan; draw(open); } })
      .catch(function () {});
  }

  $("plan-save").addEventListener("click", save);

  $("plan-copy").addEventListener("click", function () {
    if (!open) { return; }
    var tags = (open.hashtags || []).map(function (t) { return "#" + t; }).join(" ");
    var text = (open.caption || "") + (tags ? "\n\n" + tags : "");
    var button = $("plan-copy");
    /* Copying is as far as this goes on purpose: the caption ends up on the
     * clipboard and a person pastes it wherever they are posting. */
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy caption and tags"; }, 1600);
      }).catch(function () {});
    }
  });

  $("plan-posted").addEventListener("click", function () {
    if (!open) { return; }
    fetch("/api/plans/" + encodeURIComponent(open.id) + "/posted", {
      method: "POST", credentials: "same-origin"
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function () { back(); })
      .catch(function () {});
  });

  $("plan-drop").addEventListener("click", function () {
    if (!open) { return; }
    fetch("/api/plans/" + encodeURIComponent(open.id) + "/drop", {
      method: "POST", credentials: "same-origin"
    })
      .then(function () { back(); })
      .catch(function () {});
  });

  /* -- making one ---------------------------------------------------------- */

  function fillPlatforms() {
    var pick = $("new-platform");
    if (!pick || pick.options.length) { return; }
    pick.innerHTML = platforms.map(function (p) {
      return '<option value="' + escaped(p.id) + '">' + escaped(p.name) +
        " — " + escaped(p.spec) + "</option>";
    }).join("");
  }

  function openSheet() {
    fillPlatforms();
    var soon = new Date(Date.now() + 24 * 3600 * 1000);
    soon.setMinutes(0, 0, 0);
    $("new-when").value = new Date(soon.getTime() - soon.getTimezoneOffset() * 60000)
      .toISOString().slice(0, 16);
    $("new-error").hidden = true;
    sheet.hidden = false;
  }

  function closeSheet() { sheet.hidden = true; }

  $("new-plan").addEventListener("click", openSheet);
  $("blank-new").addEventListener("click", openSheet);
  $("new-close").addEventListener("click", closeSheet);
  $("new-scrim").addEventListener("click", closeSheet);

  $("new-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var prompt = $("new-prompt").value.trim();
    if (!prompt) {
      $("new-error").textContent = "Say what kind of film it is.";
      $("new-error").hidden = false;
      return;
    }
    var local = $("new-when").value;
    fetch("/api/plans", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: $("new-title").value.trim(),
        prompt: prompt,
        platform: $("new-platform").value,
        when: local ? new Date(local).toISOString() : "",
        seconds: Number($("new-seconds").value) || 20
      })
    })
      .then(function (r) { return r.json().then(function (said) { return { ok: r.ok, said: said }; }); })
      .then(function (out) {
        if (!out.ok) {
          $("new-error").textContent = out.said.error || "That did not work.";
          $("new-error").hidden = false;
          return;
        }
        closeSheet();
        $("new-prompt").value = "";
        $("new-title").value = "";
        open = out.said.plan;
        draw(open);
        boardScreen.hidden = true;
        planScreen.hidden = false;
        window.scrollTo(0, 0);
      })
      .catch(function () {
        $("new-error").textContent = "That did not work.";
        $("new-error").hidden = false;
      });
  });

  loadBoard();
})();
