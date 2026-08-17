/* The studio page.
 *
 * Plain ES5-ish, no build step, no framework — the same rule the rest of this
 * app follows, because a phone on a home network should not have to fetch a
 * megabyte of JavaScript to approve a cut.
 *
 * Every number shown here comes from /api/insight and /api/agents. Nothing is
 * computed twice: if the page and the renderer ever disagreed about a
 * prediction, the page would be the one lying.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function get(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json(); });
  }

  var state = { platform: "tiktok", mode: "supervised", prediction: null };

  // -- destinations -----------------------------------------------------

  function drawPlatforms(list) {
    var host = $("platforms");
    host.innerHTML = "";
    list.forEach(function (p) {
      var b = document.createElement("button");
      b.className = "platform" + (p.name === state.platform ? " is-on" : "");
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", p.name === state.platform ? "true" : "false");
      b.dataset.name = p.name;
      b.innerHTML =
        '<span class="platform-name"></span><span class="platform-spec"></span>';
      b.querySelector(".platform-name").textContent = p.service + " " + p.surface;
      b.querySelector(".platform-spec").textContent =
        p.width + "×" + p.height + " · " + p.min_seconds + "–" + p.max_seconds + "s";
      b.addEventListener("click", function () {
        state.platform = p.name;
        drawPlatforms(list);
      });
      host.appendChild(b);
    });
  }

  // -- what the data says -----------------------------------------------

  function drawInsight(model) {
    $("provenance").textContent = model.provenance;

    var targets = [
      ["hook", model.elite_three_second, "0.80"],
      ["share", model.elite_share, "0.05"],
      ["loop", model.elite_loop, "1.5"],
    ];
    var host = $("targets");
    host.innerHTML = "";
    targets.forEach(function (t) {
      var d = document.createElement("div");
      d.className = "target";
      d.innerHTML = '<span class="target-value"></span><span class="target-name"></span>';
      d.querySelector(".target-value").textContent =
        t[1] ? Number(t[1]).toFixed(2) : "—";
      d.querySelector(".target-name").textContent = t[0] + " · aim " + t[2];
      host.appendChild(d);
    });

    var drivers = $("drivers");
    drivers.innerHTML = "";
    (model.drivers || []).forEach(function (d) {
      var li = document.createElement("li");
      var up = d[2] > 0;
      li.innerHTML =
        '<span class="driver-r"></span><span class="driver-label"></span>';
      var r = li.querySelector(".driver-r");
      r.className = "driver-r " + (up ? "up" : "down");
      r.textContent = (up ? "↑" : "↓") + " " + Math.abs(d[2]).toFixed(2);
      li.querySelector(".driver-label").textContent = d[1];
      drivers.appendChild(li);
    });

    var notes = [];
    if (model.caveat) notes.push(model.caveat);
    (model.generated_forms || []).forEach(function (f) {
      notes.push(f + " looks generated rather than observed — down-weighted.");
    });
    (model.conflicts || []).forEach(function (c) {
      notes.push("Your exports disagree: " + c);
    });
    var caveat = $("data-caveat");
    if (notes.length) {
      caveat.textContent = notes.join("  ");
      caveat.hidden = false;
    } else {
      caveat.hidden = true;
    }
  }

  // -- prediction --------------------------------------------------------

  var DIAL = 2 * Math.PI * 52;

  function drawPrediction(p) {
    state.prediction = p;
    $("prediction-card").hidden = false;

    $("dial-value").textContent = Math.round(p.overall * 100) + "%";
    $("dial-fill").style.strokeDashoffset = String(DIAL * (1 - Math.max(0, Math.min(1, p.overall))));

    var host = $("objectives");
    host.innerHTML = "";
    p.objectives.forEach(function (o) {
      var li = document.createElement("li");
      li.className = "objective " + (o.meets_target ? "met" : "missed");
      li.innerHTML =
        '<span class="objective-mark"></span>' +
        '<span><span class="objective-name"></span>' +
        '<span class="objective-note"></span></span>' +
        '<span class="objective-value"></span>';
      li.querySelector(".objective-mark").textContent = o.meets_target ? "✓" : "·";
      li.querySelector(".objective-name").textContent = o.name;
      li.querySelector(".objective-note").textContent = o.note;
      li.querySelector(".objective-value").textContent =
        o.predicted.toFixed(o.name === "loop" ? 2 : 3);
      host.appendChild(li);
    });

    drawCurve(p.retention_curve || [], p.drop_off_second, p.runtime);
  }

  function drawCurve(curve, dropAt, runtime) {
    if (!curve.length) return;
    var W = 300, H = 90;
    var step = W / (curve.length - 1);
    var points = curve.map(function (v, i) {
      return [i * step, H - Math.max(0, Math.min(1, v)) * (H - 6) - 3];
    });
    var line = points
      .map(function (pt, i) { return (i ? "L" : "M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1); })
      .join(" ");
    $("curve-line").setAttribute("d", line);
    $("curve-area").setAttribute("d", line + " L" + W + " " + H + " L0 " + H + " Z");

    if (runtime > 0 && dropAt > 0) {
      var x = Math.max(0, Math.min(W, (dropAt / runtime) * W));
      var drop = $("curve-drop");
      drop.setAttribute("x1", x); drop.setAttribute("x2", x);
      drop.setAttribute("y1", 0); drop.setAttribute("y2", H);
      $("curve-label").textContent = "steepest drop " + dropAt.toFixed(1) + "s";
    }
  }

  // -- agent proposals ---------------------------------------------------

  function drawProposals(list) {
    var host = $("proposals");
    host.innerHTML = "";
    $("proposals-empty").hidden = list.length > 0;

    list.forEach(function (p, index) {
      var li = document.createElement("li");
      li.className = "proposal risk-" + p.risk + (p.decided_by ? " is-decided" : "");
      li.innerHTML =
        '<div class="proposal-head">' +
        '<span class="proposal-title"></span><span class="proposal-gain"></span></div>' +
        '<div class="proposal-meta"></div>' +
        '<p class="proposal-reason"></p>' +
        '<div class="proposal-actions"></div>';

      li.querySelector(".proposal-title").textContent = p.title;
      li.querySelector(".proposal-gain").textContent =
        (p.predicted_gain >= 0 ? "+" : "") + (p.predicted_gain * 100).toFixed(1) + "%";
      li.querySelector(".proposal-meta").textContent =
        p.agent + " agent · " + p.risk + " risk · " + p.objective.replace(/_/g, " ");
      li.querySelector(".proposal-reason").textContent = p.reason;

      var actions = li.querySelector(".proposal-actions");
      if (p.decided_by) {
        var note = document.createElement("p");
        note.className = "decided";
        note.textContent =
          (p.applied ? "applied" : "skipped") +
          " — " + (p.decision_note || p.decided_by);
        actions.appendChild(note);
      } else {
        var yes = document.createElement("button");
        yes.className = "yes";
        yes.textContent = "Apply";
        yes.addEventListener("click", function () { decide(index, "approve"); });
        var no = document.createElement("button");
        no.textContent = "Leave it";
        no.addEventListener("click", function () { decide(index, "reject"); });
        actions.appendChild(yes);
        actions.appendChild(no);
      }
      host.appendChild(li);
    });
  }

  function decide(index, answer) {
    post("/api/agents/decide", { index: index, answer: answer }).then(function (data) {
      if (data.proposals) drawProposals(data.proposals);
      if (data.prediction) drawPrediction(data.prediction);
    });
  }

  // -- modes -------------------------------------------------------------

  Array.prototype.forEach.call(document.querySelectorAll(".mode"), function (b) {
    b.addEventListener("click", function () {
      state.mode = b.dataset.mode;
      Array.prototype.forEach.call(document.querySelectorAll(".mode"), function (other) {
        other.classList.toggle("is-on", other === b);
        other.setAttribute("aria-checked", other === b ? "true" : "false");
      });
    });
  });

  $("run").addEventListener("click", function () {
    var button = $("run");
    button.disabled = true;
    button.textContent = "Planning…";
    post("/api/agents/plan", { platform: state.platform, mode: state.mode })
      .then(function (data) {
        if (data.prediction) drawPrediction(data.prediction);
        drawProposals(data.proposals || []);
        button.textContent = "Cut it";
        button.disabled = false;
      })
      .catch(function () {
        button.textContent = "Cut it";
        button.disabled = false;
      });
  });

  // -- what the Scholar is doing -----------------------------------------

  function drawScholar(s) {
    if (!s || !s.available) { return; }
    $("scholar-panel").hidden = false;

    var parts = [s.learnings + (s.learnings === 1 ? " learning" : " learnings")];
    parts.push(s.sessions + (s.sessions === 1 ? " session" : " sessions"));
    if (!s.can_study) { parts.push("cannot reach YouTube right now"); }
    else if (s.wants_to_study) { parts.push("about to study — " + (s.why || "")); }
    $("scholar-state").textContent = parts.join("  ·  ");

    // What the films agree on, first: it is the only thing here an editing
    // agent can be held to.
    var agreed = s.consensus || [];
    var agreedHost = $("scholar-consensus");
    agreedHost.innerHTML = "";
    // One heading for the group, not one per line — the same three words
    // five times is a label repeated, not information.
    $("scholar-agree-head").hidden = agreed.length === 0;
    agreed.forEach(function (line) {
      var li = document.createElement("li");
      li.textContent = line;
      agreedHost.appendChild(li);
    });

    var product = (s.product && s.product.learnings) || [];
    var host = $("scholar-product");
    host.innerHTML = "";
    $("scholar-empty").hidden = product.length > 0 || agreed.length > 0;
    product.forEach(function (item) {
      var li = document.createElement("li");
      li.className = "learning";
      // textContent throughout: these strings come from video descriptions,
      // which is to say from strangers.
      var title = document.createElement("span");
      title.className = "learning-title";
      title.textContent = item.technique;
      var tag = document.createElement("span");
      tag.className = "learning-tag";
      tag.textContent = item.confidence;
      var body = document.createElement("span");
      body.className = "learning-body";
      body.textContent = item.insight;
      li.appendChild(title); li.appendChild(tag); li.appendChild(body);
      host.appendChild(li);
    });
  }

  // -- start -------------------------------------------------------------

  get("/api/platforms").then(function (d) { drawPlatforms(d.platforms || []); });
  get("/api/insight").then(drawInsight).catch(function () {
    $("provenance").textContent = "no performance data loaded";
  });
  // Never fatal: the studio works with no Scholar at all.
  get("/api/scholar").then(drawScholar).catch(function () {});

  /* What can go over the cut, straight from the agent that places it. */
  function drawGraphics(state) {
    if (!state || !state.kinds) { return; }
    var chosen = null;
    try { chosen = JSON.parse(localStorage.getItem("auteur-overlays") || "null"); } catch (e) {}
    var picked = chosen && chosen.kinds ? chosen.kinds.length : 0;
    var busy = chosen && chosen.density ? chosen.density : "some";
    $("graphics-state").textContent =
      state.kinds.length + " shapes and " + state.moves.length + " ways to arrive"
      + (chosen ? "  ·  you have " + picked + " on, " + busy : "  ·  nothing chosen yet");
    var list = $("graphics-rules");
    state.rules.forEach(function (rule) {
      var item = document.createElement("li");
      item.textContent = rule;
      list.appendChild(item);
    });
    $("graphics-panel").hidden = false;
  }
  get("/api/overlays").then(drawGraphics).catch(function () {});
})();
