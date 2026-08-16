"""Assemble the real app front end into one self-contained page.

The published page is the shipped markup and the shipped stylesheet with one
extra script: `browser-render.js`, which cuts the film in the browser because
a published page has no ffmpeg and no Python behind it.

    python3 tools/artifact/build_artifact.py [out.html]
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent / "auteur" / "web" / "static"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "auteur-app.html"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def body_of(html):
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    return m.group(1)


def strip_scripts(html):
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)


home = strip_scripts(body_of(read("index.html")))
studio = strip_scripts(body_of(read("studio.html")))
# The studio's own header links back to "/" — make both screens live on one page.
studio = studio.replace('href="/"', 'href="#" data-goto="home"')
home = home.replace('href="/studio"', 'href="#" data-goto="studio"')
# The save link and the production notes both need files the renderer writes.
# Stripped from the markup rather than removed at runtime, so the published
# page never contains a download link that cannot work.
home = re.sub(r'<a class="go" id="save".*?</a>\s*', "", home, flags=re.S)
home = re.sub(r'<a class="ghost" id="notes".*?</a>\s*', "", home, flags=re.S)


css = "\n".join(read(n) for n in ("theme.css", "style.css", "animations.css", "studio.css"))

DEMO = r"""
/* ------------------------------------------------------------------ *
 * This is the app's real interface, running without its renderer.
 *
 * `auteur` cuts video with ffmpeg from a Python process. A published page
 * has neither, so the parts that need a machine — reading your clips,
 * planning the edit, rendering it — are played back here from a real run
 * rather than performed. Everything you touch is the shipped markup and
 * the shipped stylesheet, on your own phone, in your own theme.
 * ------------------------------------------------------------------ */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  /* -- theme, exactly as the app does it ---------------------------- */
  function applyTheme(choice) {
    if (choice === "system") { document.documentElement.removeAttribute("data-theme"); }
    else { document.documentElement.setAttribute("data-theme", choice); }
    try { localStorage.setItem("auteur-theme", choice); } catch (e) {}
  }
  var saved = "system";
  try { saved = localStorage.getItem("auteur-theme") || "system"; } catch (e) {}
  applyTheme(saved);

  function wireChoices(container, onPick) {
    if (!container) { return; }
    container.addEventListener("click", function (event) {
      var button = event.target.closest(".choice");
      if (!button) { return; }
      Array.prototype.forEach.call(container.querySelectorAll(".choice"), function (other) {
        other.classList.toggle("is-on", other === button);
        other.setAttribute("aria-checked", other === button ? "true" : "false");
      });
      onPick(button.dataset.value);
    });
  }
  wireChoices($("appearance"), applyTheme);
  Array.prototype.forEach.call($("appearance").querySelectorAll(".choice"), function (b) {
    var on = b.dataset.value === saved;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });

  var state = { shape: "reel", seconds: "", clips: 0 };
  wireChoices($("shape"), function (v) { state.shape = v; });
  wireChoices($("seconds"), function (v) { state.seconds = v; });

  /* -- screens ------------------------------------------------------ */
  var screens = ["screen-start", "screen-working", "screen-done", "screen-error"];
  function show(id) {
    screens.forEach(function (s) { $(s).hidden = s !== id; });
    document.getElementById("studio-page").hidden = true;
    document.getElementById("app").hidden = false;
    window.scrollTo(0, 0);
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-goto]");
    if (!link) { return; }
    event.preventDefault();
    var to = link.dataset.goto;
    document.getElementById("studio-page").hidden = to !== "studio";
    document.getElementById("app").hidden = to === "studio";
    window.scrollTo(0, 0);
  });

  /* -- step 1 ------------------------------------------------------- */
  var clips = $("clips");
  clips.addEventListener("change", function () {
    var count = clips.files ? clips.files.length : 0;
    state.clips = count;
    var hint = $("clips-hint"), action = $("clips-action");
    clips.closest(".card").classList.toggle("has-files", count > 0);
    if (!count) {
      hint.textContent = "Straight from your camera roll. Music too, if you have some.";
      action.textContent = "Choose from camera roll";
      return;
    }
    /* Count what you actually picked, by kind. It used to say "9 clips" for
       one video and eight photographs — and then throw the photographs away,
       which is how you find out a count is a lie. */
    var bytes = 0, video = 0, photo = 0, audio = 0;
    for (var i = 0; i < count; i++) {
      var f = clips.files[i];
      bytes += f.size;
      if (/^video\//.test(f.type)) { video++; }
      else if (/^image\//.test(f.type)) { photo++; }
      else if (/^audio\//.test(f.type)) { audio++; }
      else if (/\.(mov|mp4|m4v|webm)$/i.test(f.name)) { video++; }
      else if (/\.(jpe?g|png|heic|heif|gif|webp)$/i.test(f.name)) { photo++; }
      else { photo++; }
    }
    var parts = [];
    if (video) { parts.push(video + (video === 1 ? " clip" : " clips")); }
    if (photo) { parts.push(photo + (photo === 1 ? " photo" : " photos")); }
    if (audio) { parts.push("music"); }
    var mb = bytes > 1073741824
      ? (bytes / 1073741824).toFixed(1) + " GB"
      : Math.max(1, Math.round(bytes / 1048576)) + " MB";
    hint.textContent = parts.join(" and ") + " ready  ·  " + mb;
    action.textContent = "Choose something different";
  });

  $("chips").addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (!chip) { return; }
    $("prompt").value = chip.dataset.prompt || chip.textContent;
  });

  /* -- the run, performed ------------------------------------------ */
  /* This cuts a real film from the clips you picked, in the browser: the
     footage is decoded into a canvas and encoded straight back out. What it
     does not do is read the frames, find the beats or run the crew — those
     need the measurements only the Python side makes. */

  var made = null;

  function run() {
    show("screen-working");
    $("log").innerHTML = "";
    var lastStage = "";

    function step(fraction, label) {
      $("stage").textContent = label;
      $("bar-fill").style.width = Math.round(fraction * 100) + "%";
      $("ring-fill").style.transform = "rotate(" + (fraction * 360) + "deg)";
      var head = label.split(" — ")[0];
      if (head !== lastStage) {
        lastStage = head;
        var li = document.createElement("li");
        li.textContent = head;
        $("log").appendChild(li);
      }
    }

    step(0.02, "Getting ready");
    var files = Array.prototype.slice.call(clips.files || []);

    window.auteurCut
      .cut({
        files: files,
        prompt: $("prompt").value || "",
        shape: state.shape,
        seconds: state.seconds ? parseFloat(state.seconds) : 10,
        onProgress: step,
      })
      .then(function (film) {
        made = film;
        step(1, "Your film is ready");
        finish(film);
      })
      .catch(function (err) {
        show("screen-error");
        $("error-text").textContent =
          "It could not cut that: " + (err && err.message ? err.message : err) +
          ". It cuts video and photographs in the browser — pick either.";
      });

    $("cancel").onclick = function () { show("screen-start"); };
  }

  function finish(film) {
    show("screen-done");

    /* Say back what it heard. Everything below changed because of the words
       you typed, so if none of it matches what you meant, the mismatch is
       visible instead of silent. */
    var heard = "It read that as " + film.reading.cadence
      + ", graded " + film.reading.look
      + ", " + Math.round(film.reading.seconds) + " seconds long.";
    if (film.reading.titles.length) {
      heard += " On screen: " + film.reading.titles.map(function (t) {
        return "“" + t + "”";
      }).join(", ") + ".";
    }
    heard += " It used " + film.clips + (film.clips === 1 ? " clip" : " clips")
      + " and " + film.stills + (film.stills === 1 ? " photo" : " photos") + ".";
    if (film.music) { heard += " Your music is on it."; }
    $("heard").textContent = heard;
    $("heard").hidden = false;

    var facts = [
      film.seconds.toFixed(1) + " seconds",
      film.shots + " shots, a median " + film.shot_seconds.toFixed(2) + "s each",
      { reel: "vertical, for phones", square: "square", wide: "widescreen" }[state.shape],
      Math.max(1, Math.round(film.bytes / 1048576)) + " MB",
    ];
    $("facts").innerHTML = "";
    facts.forEach(function (fact) {
      var li = document.createElement("li");
      li.textContent = fact;
      $("facts").appendChild(li);
    });
    var player = $("player");
    player.src = film.url;
    player.hidden = false;
    player.load();
  }

  $("form").addEventListener("submit", function (event) {
    event.preventDefault();
    var problem = "";
    if (!state.clips) { problem = "Pick some clips first — tap step 1."; }
    else if (!($("prompt").value || "").trim()) { problem = "Say what kind of film you want."; }
    if (problem) {
      $("start-error").textContent = problem;
      $("start-error").hidden = false;
      return;
    }
    $("start-error").hidden = true;
    run();
  });

  $("again").onclick = function () { show("screen-start"); };
  $("retry").onclick = function () { show("screen-start"); };

  /* The two things that need the real program, said plainly rather than faked. */
  $("player").hidden = true;

  /* -- studio ------------------------------------------------------- */
  var PLATFORMS = [
    ["Instagram", "Reels", 1080, 1920, 3, 180],
    ["Instagram", "Feed", 1080, 1350, 3, 60],
    ["Instagram", "Stories", 1080, 1920, 1, 60],
    ["TikTok", "For You", 1080, 1920, 3, 600],
    ["TikTok", "Photo mode", 1080, 1920, 3, 60],
    ["YouTube", "Shorts", 1080, 1920, 1, 180]
  ];
  var host = $("platforms");
  PLATFORMS.forEach(function (p, index) {
    var b = document.createElement("button");
    b.className = "platform" + (index === 3 ? " is-on" : "");
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", index === 3 ? "true" : "false");
    b.innerHTML = '<span class="platform-name"></span><span class="platform-spec"></span>';
    b.querySelector(".platform-name").textContent = p[0] + " " + p[1];
    b.querySelector(".platform-spec").textContent = p[2] + "×" + p[3] + " · " + p[4] + "–" + p[5] + "s";
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(host.children, function (o) {
        o.classList.toggle("is-on", o === b);
        o.setAttribute("aria-checked", o === b ? "true" : "false");
      });
    });
    host.appendChild(b);
  });

  var TARGETS = [["hook", 0.87, "0.80"], ["share", 0.06, "0.05"], ["loop", 1.59, "1.5"]];
  var targets = $("targets");
  TARGETS.forEach(function (t) {
    var d = document.createElement("div");
    d.className = "target";
    d.innerHTML = '<span class="target-value"></span><span class="target-label"></span>';
    d.querySelector(".target-value").textContent = t[1].toFixed(2);
    d.querySelector(".target-label").textContent = t[0].toUpperCase() + " · AIM " + t[2];
    targets.appendChild(d);
  });
  $("provenance").textContent = "fitted on 2000 simulated rows — this predicts the simulator, not any platform";

  wireChoices(null, null);
  var modes = $("modes");
  modes.addEventListener("click", function (event) {
    var b = event.target.closest(".mode");
    if (!b) { return; }
    Array.prototype.forEach.call(modes.children, function (o) { o.classList.toggle("is-on", o === b); });
  });

  /* What the Scholar has actually learned, from its own store. */
  $("scholar-panel").hidden = false;
  var AGREED = [
    "Across 9 films they cut 30.8 times per ten seconds (from 0.7 to 76.1).",
    "Across 9 films they hold a shot for 0.167s (from 0.125 to 7.300).",
    "Across 9 films they cut away from the opening shot after 0.12s.",
    "Across 10 films they measure 0.036 inter-frame motion (from 0.025 to 0.077).",
    "Across 10 films they sit at luma 0.28 (from 0.06 to 0.40)."
  ];
  $("scholar-agree-head").hidden = false;
  AGREED.forEach(function (line) {
    var li = document.createElement("li");
    li.textContent = line;
    $("scholar-consensus").appendChild(li);
  });
  $("scholar-state").textContent = "38 learnings  ·  12 files studied  ·  gaps to fill — web design, accessibility, conversion and 27 more";
  var LEARNED = [
    ["44px touch targets", "A fingertip covers about 44 points of glass, so anything smaller is aimed at rather than tapped.", "tentative"],
    ["the sign-off card is not an edit", "Fourteen of fifteen reference reels end on a held frame carrying a handle, almost exactly four seconds every time.", "tentative"],
    ["cut on subdivisions", "The reels cut at 0.167s against a half-second beat — three cuts per beat, which is triplets rather than an arbitrary rate.", "tentative"]
  ];
  var list = $("scholar-product");
  $("scholar-empty").hidden = true;
  LEARNED.forEach(function (item) {
    var li = document.createElement("li");
    li.className = "learning";
    var title = document.createElement("span"); title.className = "learning-title"; title.textContent = item[0];
    var tag = document.createElement("span"); tag.className = "learning-tag"; tag.textContent = item[2];
    var body = document.createElement("span"); body.className = "learning-body"; body.textContent = item[1];
    li.appendChild(title); li.appendChild(tag); li.appendChild(body);
    list.appendChild(li);
  });

  $("run").addEventListener("click", function () {
    var b = $("run");
    b.disabled = true;
    b.textContent = "Cutting…";
    setTimeout(function () {
      b.disabled = false;
      b.textContent = "Cut it";
      $("proposals-empty").textContent =
        "Running the crew needs the renderer — start auteur on your machine and this fills with proposals to approve.";
    }, 900);
  });

  /* Belt and braces — the CSS above already forces these visible. The class
     is `is-visible`; an earlier version added `is-in`, which matched nothing
     and left the entire page at opacity 0. */
  Array.prototype.forEach.call(document.querySelectorAll(".scroll-reveal"), function (el) {
    el.classList.add("is-visible");
  });
})();
"""

BANNER = """
<div class="demo-note" role="note">
  <strong>This cuts a real film, here, from your camera roll.</strong>
  Video and photographs both, plus a music file if you pick one. Say what you
  want and the page frames every shot, cuts to the cadence your words ask for,
  grades it, and sets anything you put "in quotes" on screen — a hypercut
  really does come back cut at 0.167s a shot. What it cannot do is find the
  beats, read your frames or run the crew: those need measurements only the
  full program makes. Nothing you pick leaves your phone.
</div>
"""

EXTRA = """
.demo-note {
  max-width: 34rem;
  margin: 0 auto 1.25rem;
  padding: 0.85rem 1rem;
  border: 1px solid var(--line);
  border-left: 3px solid var(--ember);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.9rem;
  line-height: 1.5;
}
.demo-note strong { color: var(--text); display: block; margin-bottom: 0.2rem; }
body { background: var(--ground); }

/* The app reveals these with an IntersectionObserver that lives in app.js,
   which this page does not load — so every card sat at opacity 0 and the
   whole page below the banner was invisible. Forced in CSS rather than
   re-added in script: a page that only renders if its JavaScript runs is a
   page that sometimes does not render. */
.scroll-reveal,
.slide-in-left,
.slide-in-right {
  opacity: 1 !important;
  transform: none !important;
}
/* `display: block` on its own beats [hidden]'s display:none, which put the
   studio underneath every other screen. */
#studio-page { display: block; }
#studio-page[hidden], [hidden] { display: none !important; }
"""

RENDERER = (HERE / "browser-render.js").read_text(encoding="utf-8")

page = f"""<title>Auteur Edit Room</title>
<style>
{css}
{EXTRA}
</style>
{BANNER}
{home}
<div id="studio-page" hidden>
{studio}
</div>
<script>
{RENDERER}
</script>
<script>
{DEMO}
</script>
"""
OUT.write_text(page, encoding="utf-8")
print(OUT, len(page), "bytes")
