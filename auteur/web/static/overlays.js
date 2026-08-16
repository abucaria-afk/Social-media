/* The animation tab.
 *
 * Every shape on this page is drawn by `overlay-draw.js`, which is the same
 * module the in-browser renderer draws the film with. So what you tap here is
 * the thing itself running, not an illustration of it — and if a movement
 * looks wrong on this page it is wrong in the film too.
 *
 * The choices are kept in localStorage under `auteur-overlays` and read back
 * by the edit room, so picking here changes what the next film gets.
 */
(function () {
  "use strict";

  var overlays = window.auteurOverlays;
  function $(id) { return document.getElementById(id); }

  var STORE = "auteur-overlays";
  var settled = { kinds: ["circle", "bracket", "burst"], move: "pop", density: "some" };
  var chosen = load();

  function load() {
    try {
      var raw = JSON.parse(localStorage.getItem(STORE) || "null");
      if (raw && Array.isArray(raw.kinds) && raw.kinds.length) { return raw; }
    } catch (e) { /* private mode, or somebody edited it by hand */ }
    return { kinds: settled.kinds.slice(), move: settled.move, density: settled.density };
  }

  function save() {
    try { localStorage.setItem(STORE, JSON.stringify(chosen)); } catch (e) {}
    var count = chosen.density === "off" ? 0 : chosen.kinds.length;
    $("overlay-state").textContent = count
      ? count + (count === 1 ? " shape" : " shapes") + " · " + chosen.move + " · " + chosen.density
      : "nothing over the film";
  }

  // -- the chips ------------------------------------------------------------
  // Each is a small canvas running the real cue on a loop, so a movement can
  // be judged by watching it rather than by reading its name.

  var running = [];

  overlays.KINDS.forEach(function (kind) {
    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "overlay-chip";
    chip.setAttribute("aria-pressed", "false");

    var canvas = document.createElement("canvas");
    canvas.width = 240; canvas.height = 180;
    var name = document.createElement("span");
    name.className = "overlay-chip-name";
    name.textContent = kind.label;
    var note = document.createElement("span");
    note.className = "overlay-chip-note";
    note.textContent = kind.note;

    chip.appendChild(canvas);
    chip.appendChild(name);
    chip.appendChild(note);
    chip.addEventListener("click", function () {
      var at = chosen.kinds.indexOf(kind.name);
      if (at === -1) { chosen.kinds.push(kind.name); } else { chosen.kinds.splice(at, 1); }
      paintChips();
      save();
    });
    $("kinds").appendChild(chip);

    running.push({ kind: kind, chip: chip, ctx: canvas.getContext("2d"), canvas: canvas });
  });

  function paintChips() {
    running.forEach(function (item) {
      var on = chosen.kinds.indexOf(item.kind.name) !== -1;
      item.chip.classList.toggle("is-on", on);
      item.chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // One clock for every chip. Twelve separate rAF loops on a phone is twelve
  // times the wake-ups for one animation nobody perceives as separate.
  var CYCLE = 2.2;
  function tickChips(now) {
    var p = ((now / 1000) % CYCLE) / CYCLE;
    running.forEach(function (item) {
      var g = item.ctx;
      var w = item.canvas.width, h = item.canvas.height;
      g.clearRect(0, 0, w, h);
      overlays.draw(g, {
        kind: item.kind.name,
        move: chosen.move,
        anchor: [0.5, 0.5],
        size: item.kind.name === "progress" ? 0.55 : 1.15,
        color: "#e9a85c",
        opacity: 1
      }, w, h, p);
    });
    requestAnimationFrame(tickChips);
  }

  // -- how it arrives -------------------------------------------------------

  overlays.MOVES.forEach(function (move) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "mode" + (move.name === chosen.move ? " is-on" : "");
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", move.name === chosen.move ? "true" : "false");
    button.textContent = move.label;
    button.title = move.note;
    button.addEventListener("click", function () {
      chosen.move = move.name;
      Array.prototype.forEach.call($("moves").children, function (other) {
        var on = other === button;
        other.classList.toggle("is-on", on);
        other.setAttribute("aria-checked", on ? "true" : "false");
      });
      save();
    });
    $("moves").appendChild(button);
  });

  // -- how busy -------------------------------------------------------------

  $("density").addEventListener("click", function (event) {
    var button = event.target.closest(".choice");
    if (!button) { return; }
    chosen.density = button.dataset.value;
    Array.prototype.forEach.call($("density").children, function (other) {
      var on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-checked", on ? "true" : "false");
    });
    save();
  });

  // -- the rehearsal --------------------------------------------------------
  // A cut at the reference cadence with graphics landing on it, so the density
  // choice can be judged against motion rather than against a still.

  var HOLD = 0.167 * 3;          // a shot, at three cuts to a half-second beat
  var RUN = 6.0;
  var stage = $("overlay-stage");
  var sg = stage.getContext("2d", { alpha: false });
  var began = 0;
  var playing = false;

  function frameColour(shot) {
    // Stand-in footage: flat panels that change on the cut, so the only thing
    // moving is the graphics. Nobody's photographs are on this page.
    var tones = ["#2f2a25", "#3a2f28", "#26302f", "#332a33", "#2b3129"];
    return tones[shot % tones.length];
  }

  function everyOther(shot) {
    if (chosen.density === "off") { return 0; }
    if (chosen.density === "busy") { return shot % 2 === 0 ? 3 : 1; }
    return shot % 4 === 0 ? 2 : 0;
  }

  var SPOTS = [[0.32, 0.34], [0.68, 0.55], [0.44, 0.74]];

  function rehearse(now) {
    if (!playing) { return; }
    var elapsed = (now - began) / 1000;
    if (elapsed >= RUN) { playing = false; $("replay").textContent = "Play it again"; return; }

    var shot = Math.floor(elapsed / HOLD);
    var into = (elapsed - shot * HOLD) / HOLD;
    sg.fillStyle = frameColour(shot);
    sg.fillRect(0, 0, stage.width, stage.height);

    var many = Math.min(everyOther(shot), chosen.kinds.length);
    for (var n = 0; n < many; n++) {
      overlays.draw(sg, {
        kind: chosen.kinds[(shot + n) % chosen.kinds.length],
        move: chosen.move,
        anchor: SPOTS[n],
        size: 0.95,
        color: "#f2ede4",
        opacity: 0.95
      }, stage.width, stage.height, into);
    }

    // The progress bar runs on the film's clock, not the shot's — it is the
    // one graphic whose whole job is to say how much is left.
    if (chosen.density !== "off" && chosen.kinds.indexOf("progress") !== -1) {
      overlays.draw(sg, {
        kind: "progress",
        move: "none",
        anchor: [0.5, 0.94],
        size: 0.6,
        color: "#e9a85c",
        opacity: 0.9
      }, stage.width, stage.height, elapsed / RUN);
    }
    requestAnimationFrame(rehearse);
  }

  function play() {
    began = performance.now();
    playing = true;
    $("replay").textContent = "Playing…";
    requestAnimationFrame(rehearse);
  }

  $("replay").addEventListener("click", play);

  // -- start ----------------------------------------------------------------

  Array.prototype.forEach.call($("density").children, function (button) {
    var on = button.dataset.value === chosen.density;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-checked", on ? "true" : "false");
  });
  paintChips();
  save();
  requestAnimationFrame(tickChips);
  sg.fillStyle = frameColour(0);
  sg.fillRect(0, 0, stage.width, stage.height);
  play();
})();
