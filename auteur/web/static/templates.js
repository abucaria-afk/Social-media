/* The templates tab: pick a reel's timing, or add a reel of your own.
 *
 * The choice is kept in localStorage under `auteur-template` and read by the
 * edit room when it sends a film off to be made. That is the same arrangement
 * the animation tab uses — one setting, written in one place, read in another
 * — rather than two controls that happen to agree.
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  var KEY = "auteur-template";

  function chosen() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  function choose(id) {
    try { localStorage.setItem(KEY, id); } catch (e) { /* private mode */ }
  }

  /* Where the cuts fall, drawn as one tick each.
   *
   * The only part of a template a person can judge at a glance. Two reels with
   * the same median hold can be completely different edits — a steady pulse
   * and a burst-then-rest are the same number and not the same picture — so
   * the number alone is not enough to choose between them.
   */
  function drawShape(canvas, beats) {
    var width = canvas.clientWidth || 300;
    var scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(22 * scale);
    var g = canvas.getContext("2d");
    g.scale(scale, scale);

    var total = 0;
    for (var i = 0; i < beats.length; i++) { total += beats[i][0]; }
    if (total <= 0) { return; }

    var accent = getComputedStyle(canvas).getPropertyValue("--ember") || "#e9a85c";
    var at = 0;
    for (i = 0; i < beats.length; i++) {
      var x = (at / total) * width;
      // Longer holds get a taller tick, so a rest reads as a rest rather than
      // as a gap somebody has to measure with their eye.
      var share = Math.min(1, beats[i][0] / (total / beats.length) / 3);
      var high = 6 + share * 14;
      g.fillStyle = accent.trim();
      g.globalAlpha = 0.45 + share * 0.55;
      g.fillRect(x, 22 - high, Math.max(1, width / Math.max(beats.length, 1) * 0.34), high);
      at += beats[i][0];
    }
  }

  function render(all) {
    var host = $("templates");
    host.innerHTML = "";
    var now = chosen();

    var entries = [{ id: "", label: "Its own", note: "let the film decide its own timing",
                     hold: 0, beats: [], mine: false }].concat(all);

    entries.forEach(function (entry) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "template"
        + (entry.id === now ? " is-on" : "")
        + (entry.mine ? " is-mine" : "");
      button.dataset.value = entry.id;

      var name = document.createElement("span");
      name.className = "template-name";
      name.textContent = entry.label;
      button.appendChild(name);

      var hold = document.createElement("span");
      hold.className = "template-hold";
      hold.textContent = entry.hold ? entry.hold.toFixed(2) + "s" : "—";
      button.appendChild(hold);

      var note = document.createElement("span");
      note.className = "template-note";
      note.textContent = entry.note || "";
      button.appendChild(note);

      if (entry.beats && entry.beats.length) {
        var shape = document.createElement("canvas");
        shape.className = "template-shape";
        button.appendChild(shape);
        // Drawn after it is in the document, so clientWidth is a real number.
        setTimeout(function () { drawShape(shape, entry.beats); }, 0);
      }

      button.addEventListener("click", function () {
        choose(entry.id);
        Array.prototype.forEach.call(host.querySelectorAll(".template"), function (other) {
          other.classList.toggle("is-on", other === button);
        });
        say(entry.id ? "cutting to " + entry.label : "using its own timing");
      });

      host.appendChild(button);
    });

    var mine = all.filter(function (e) { return e.mine; }).length;
    $("template-state").textContent = all.length + " reel" + (all.length === 1 ? "" : "s")
      + (mine ? ", " + mine + " yours" : "");
  }

  function say(words, bad) {
    var box = $("reel-said");
    box.textContent = words;
    box.hidden = !words;
    box.className = "notice" + (bad ? " error" : "");
  }

  function load() {
    return fetch("/api/templates", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (said) { render((said && said.templates) || []); })
      .catch(function () {
        $("template-state").textContent = "could not read the templates";
      });
  }

  /* Adding one. The reel goes up, is measured, and is not kept — what comes
     back is the timing. Said plainly on the page as well as here, because
     "we deleted your video" is exactly the kind of promise nobody believes
     unless it is written down. */
  $("reel").addEventListener("change", function () {
    var file = $("reel").files[0];
    if (!file) { return; }
    $("reel-action").textContent = "Reading it…";
    say("Watching it shot by shot. This takes about as long as the reel does.");

    var form = new FormData();
    form.append("reel", file, file.name);
    fetch("/api/templates", { method: "POST", body: form, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (said) {
        $("reel-action").textContent = "Choose a reel";
        $("reel").value = "";
        if (!said.ok) {
          say(said.body.error || "That reel could not be read.", true);
          return;
        }
        var made = said.body.template;
        choose(made.id);
        return load().then(function () {
          say("Added " + made.label + " — " + made.note + ". It is selected for your next film.");
        });
      })
      .catch(function () {
        $("reel-action").textContent = "Choose a reel";
        say("That upload did not get through.", true);
      });
  });

  load();
})();
