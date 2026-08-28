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

# `theme.css` and `theme.js` are generated from `auteur/theme.py` and are not
# committed, so a fresh checkout does not have them and the read below fails
# with a bare FileNotFoundError naming a path that does not exist in the
# repository. Generating them here rather than requiring a step beforehand:
# the ordering trap is worth removing, not documenting. This is what `serve`
# does on startup for the same reason.
sys.path.insert(0, str(HERE.parent.parent))
from auteur.web import assets  # noqa: E402

assets.ensure(ROOT)


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def body_of(html):
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    return m.group(1)


def strip_scripts(html):
    """Take the app's own scripts out; this page brings its own.

    Case-insensitive because `<SCRIPT>` is the same tag to a browser and was
    not the same tag to this regex. The closing tag takes anything up to the
    `>` because HTML lets an end tag carry junk a parser then ignores, so
    `</script foo>` closes a script and `</script\\s*>` did not see it.

    A regex is only good enough because the input is markup from this repo,
    which is why the result is checked below rather than trusted.
    """
    return re.sub(r"<script\b[^>]*>.*?</script\b[^>]*>", "", html, flags=re.S | re.I)


home = strip_scripts(body_of(read("index.html")))
studio = strip_scripts(body_of(read("studio.html")))
animation = strip_scripts(body_of(read("overlays.html")))
templates = strip_scripts(body_of(read("templates.html")))
# The invariant the regex is standing in for, stated where it can fail loudly.
# The published page loads `browser-render.js` and its own wiring; a script
# that survived from the app would be a second copy racing the first.
for name, markup in (
    ("index.html", home),
    ("studio.html", studio),
    ("overlays.html", animation),
    ("templates.html", templates),
):
    if re.search(r"<script", markup, re.I):
        raise SystemExit(f"{name}: a <script> survived stripping — check the markup")

# Ids have to be unique across all three, because here they share a document.
# They do not in the app, where each is its own page, so a collision is
# invisible until it is published: `overlays.html` had a canvas called `stage`
# and so does the edit room's progress screen, and the animation rehearsal
# silently drew onto an <h2> — `getContext is not a function`, once, in a
# console nobody was reading.
_seen: dict[str, str] = {}
for name, markup in (
    ("index.html", home),
    ("studio.html", studio),
    ("overlays.html", animation),
    ("templates.html", templates),
):
    for found in re.findall(r'\bid="([^"]+)"', markup):
        if found in _seen:
            raise SystemExit(
                f'id="{found}" is in both {_seen[found]} and {name}; '
                "on the published page they are one document"
            )
        _seen[found] = name
# Their own headers link back to "/" — make every screen live on one page.
studio = studio.replace('href="/"', 'href="#" data-goto="home"')
animation = animation.replace('href="/"', 'href="#" data-goto="home"')
animation = animation.replace('href="/studio"', 'href="#" data-goto="studio"')
animation = animation.replace('href="/ask"', 'href="#" data-goto="home"')
home = home.replace('href="/studio"', 'href="#" data-goto="studio"')
home = home.replace('href="/overlays"', 'href="#" data-goto="animation"')
home = home.replace('href="/templates"', 'href="#" data-goto="templates"')
templates = templates.replace('href="/"', 'href="#" data-goto="home"')
templates = templates.replace('href="/studio"', 'href="#" data-goto="studio"')
# Uploading a reel needs the measurement only the Python side makes, so the
# published page cannot offer it. Stripped rather than left to fail: a page
# that shows a file picker which cannot work is worse than one that does not
# mention the feature.
templates = re.sub(
    r'<section class="panel">\s*<h2 class="panel-title">Add a reel of your own</h2>.*?</section>',
    "",
    templates,
    flags=re.S,
)
# The save link and the production notes both need files the renderer writes.
# Stripped from the markup rather than removed at runtime, so the published
# page never contains a download link that cannot work.
# "Where it goes" needs the server: the link store lives beside the accounts
# file and a published page has neither. Stripped from the markup rather than
# left to fail, so the page never offers a tab that cannot open.
home = re.sub(r'<a class="go" id="save".*?</a>\s*', "", home, flags=re.S)
home = re.sub(r'<a class="ghost" id="notes".*?</a>\s*', "", home, flags=re.S)


# Anything still pointing at a server route, gone — with its row.
#
# This page has four sections and the app has ten, so every link to a room
# that is not here has to be either re-pointed above or removed. Doing it by
# name, one link at a time, is what was here before and it went stale the
# moment a room was added: the published page ended up with ten links to
# `/manager`, `/ask` and `/connect` that a tap did nothing to. On a phone that
# is a dead control; in an App Store review it is a rejection.
#
# So: strip generically, then assert. An `<a>` cannot contain another `<a>`,
# which is what makes the non-greedy match safe here.
def _drop_server_links(markup: str) -> str:
    # The whole row where there is one, so no empty chevron is left behind.
    markup = re.sub(r'<a class="inset-row"[^>]*href="/[^"]*".*?</a>\s*', "", markup, flags=re.S)
    markup = re.sub(r'<a class="onward"[^>]*href="/[^"]*".*?</a>\s*', "", markup, flags=re.S)
    return re.sub(r'<a\b[^>]*href="/[^"]*".*?</a>\s*', "", markup, flags=re.S)


# The way into the other three sections: the tab bar, the same one the app has.
#
# In the app this is injected by a script, which this build strips because the
# script links to routes a published page has none of. The first attempt put a
# list of links at the foot of the home section instead — which is where it
# ended up 1564px down a 1945px page, nearly two screens below the fold, past
# the entire form. Present, and not reachable, which is the same thing as
# missing for anybody who does not already know it is there.
#
# So: the real bar, fixed to the bottom.
#
# **The same five names as the app, in the same order.** This used to be
# Create / Templates / Animation / Studio / You — a hand-written list that had
# never matched what `chrome.js` emits, so anybody following the published
# link met a different product from the one in the screenshots and reasonably
# concluded that was the app. A test now reads both and fails when they
# diverge; there is no second list to forget any more.
#
# Three of the five need somewhere to keep things and a published page is one
# file with no server, so tapping those says that in a line rather than doing
# nothing. Naming them and explaining is honest; renaming the bar around the
# limitation is what made the link look like another app.
TABS = (
    ("feed", "⌂", "Feed", False),
    ("schedule", "▤", "Schedule", False),
    ("home", "＋", "Create", True),
    ("messages", "▣", "Messages", False),
    ("you", "◉", "You", True),
)

#: What the plus offers here. The same chooser the app has, minus the two
#: entries that need an instance — a chooser listing things that cannot happen
#: is worse than a shorter chooser.
MAKE = (
    ("home", "Make a film", "Pick photographs, say what you want, watch it cut."),
    ("templates", "Cut to a template", "Use a reel's timing on your own pictures."),
    ("animation", "Type and stickers", "Words and marks that land on the beat."),
    ("studio", "Meet the crew", "Who decided what, and what they know."),
)
TAB_BAR = (
    '<nav class="tabbar" aria-label="Main">'
    + "".join(
        f'<a class="tab{" tab-big" if key == "home" else ""}" href="#" '
        + (f'data-goto="{key}" ' if here else f'data-needs-instance="{name}" ')
        + f'data-tab="{key}">'
        f'<span class="tab-icon">{mark}</span>'
        f'<span class="tab-label">{name}</span></a>'
        for key, mark, name, here in TABS
    )
    + "</nav>"
    + '<div class="sheet" id="create-sheet" hidden>'
    '<div class="sheet-scrim" data-close-create></div>'
    '<div class="sheet-body" role="dialog" aria-modal="true" aria-label="Make something">'
    '<div class="sheet-grip" aria-hidden="true"></div>'
    '<h2 class="sheet-title">Make something</h2>'
    '<div class="make-list">'
    + "".join(
        f'<a class="make-row" href="#" data-goto="{key}" data-close-create>'
        f'<span class="make-words"><span class="make-title">{title}</span>'
        f'<span class="make-note">{note}</span></span></a>'
        for key, title, note in MAKE
    )
    + "</div>"
    '<button type="button" class="go quiet" data-close-create>Not now</button>'
    "</div></div>"
    '<div class="sheet" id="needs-instance" hidden>'
    '<div class="sheet-scrim" data-close-needs></div>'
    '<div class="sheet-body" role="dialog" aria-modal="true" aria-label="Needs an instance">'
    '<div class="sheet-grip" aria-hidden="true"></div>'
    '<h2 class="sheet-title" id="needs-title">Needs a copy you run</h2>'
    '<p class="sheet-note">This page is one file with nothing behind it, so it '
    "can make you a film and cannot keep one. The feed, the schedule and the "
    "messages need somewhere to put things — run <code>auteur serve</code>, or "
    "the container in the repository, and the app has all five.</p>"
    '<button type="button" class="go quiet" data-close-needs>Back to making one</button>'
    "</div></div>"
)

home = _drop_server_links(home)
studio = _drop_server_links(studio)
animation = _drop_server_links(animation)
templates = _drop_server_links(templates)


# The invariant, stated where it can fail loudly — the same shape as the
# script and duplicate-id guards above. A link to a route this page does not
# have is a control that does nothing, and the only reliable way to keep one
# out is to refuse to build a page containing it.
for name, markup in (
    ("index.html", home),
    ("studio.html", studio),
    ("overlays.html", animation),
    ("templates.html", templates),
):
    left = re.findall(r'href="(/[^"]*)"', markup)
    if left:
        raise SystemExit(
            f"{name}: {len(left)} link(s) to a server route survived — {sorted(set(left))[:4]}. "
            "Either point them at a section on this page with data-goto, or let "
            "_drop_server_links remove them."
        )

# And the other half of it: every in-page destination has to exist. A
# `data-goto` naming a section that is not here is the same dead control by a
# different route.
_targets = set()
for markup in (home, studio, animation, templates):
    _targets.update(re.findall(r'data-goto="([^"]+)"', markup))
_have = {"home", "studio", "animation", "templates", "you"}
if not _targets <= _have:
    raise SystemExit(f"data-goto points at sections that do not exist: {sorted(_targets - _have)}")

css = "\n".join(
    read(n)
    for n in (
        "theme.css",
        "style.css",
        "animations.css",
        "studio.css",
        "overlays.css",
        "templates.css",
        "profile.css",
    )
)

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

  /* Appearance and the accessibility settings are not reimplemented here any
     more: settings.js and theme.js are inlined from the app, so the switches
     on this page are the app's switches running the app's code. What was here
     was a third copy of the same logic, and it had already drifted — it knew
     about the theme and nothing about text size, reduced motion or contrast. */

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
  /* Appearance and the accessibility settings are not reimplemented here any
     more: settings.js and theme.js are inlined from the app, so the switches
     on this page are the app's switches running the app's code. What was here
     was a third copy of the same logic, and it had already drifted — it knew
     about the theme and nothing about text size, reduced motion or contrast. */

  var state = { shape: "reel", seconds: "", era: "", template: "", clips: 0 };
  wireChoices($("shape"), function (v) { state.shape = v; });
  wireChoices($("seconds"), function (v) { state.seconds = v; });
  wireChoices($("era"), function (v) { state.era = v; });

  /* The templates, on their own screen.
     Nineteen chips on the first screen was a wall you had to scroll past to
     reach the button that makes the film, and every chip said roughly the same
     thing. Given a page, each one can show the shape of its cutting — which is
     the only part a person can actually judge at a glance, because two reels
     with the same median hold can be completely different edits. */
  (function () {
    var all = window.auteurTemplates || [];
    var host = $("templates");
    if (!host) { return; }

    function drawShape(canvas, beats) {
      var width = canvas.clientWidth || 300;
      var scale = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(22 * scale);
      var g = canvas.getContext("2d");
      g.scale(scale, scale);
      var total = 0, i;
      for (i = 0; i < beats.length; i++) { total += beats[i][0]; }
      if (total <= 0) { return; }
      var accent = (getComputedStyle(canvas).getPropertyValue("--ember") || "#e9a85c").trim();
      var at = 0;
      for (i = 0; i < beats.length; i++) {
        var x = (at / total) * width;
        var share = Math.min(1, beats[i][0] / (total / beats.length) / 3);
        var high = 6 + share * 14;
        g.fillStyle = accent;
        g.globalAlpha = 0.45 + share * 0.55;
        g.fillRect(x, 22 - high, Math.max(1, width / Math.max(beats.length, 1) * 0.34), high);
        at += beats[i][0];
      }
    }

    function paint() {
      host.innerHTML = "";
      var rows = [{ id: "", label: "Its own", note: "let the film decide its own timing",
                    hold: 0, beats: [] }].concat(all);
      rows.forEach(function (entry) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "template" + (entry.id === state.template ? " is-on" : "");
        button.dataset.value = entry.id;

        var name = document.createElement("span");
        name.className = "template-name";
        name.textContent = entry.label;
        button.appendChild(name);

        var hold = document.createElement("span");
        hold.className = "template-hold";
        hold.textContent = entry.hold ? entry.hold.toFixed(2) + "s" : "\u2014";
        button.appendChild(hold);

        var note = document.createElement("span");
        note.className = "template-note";
        note.textContent = entry.note || "";
        button.appendChild(note);

        if (entry.beats && entry.beats.length) {
          var shape = document.createElement("canvas");
          shape.className = "template-shape";
          button.appendChild(shape);
          setTimeout(function () { drawShape(shape, entry.beats); }, 0);
        }

        button.addEventListener("click", function () {
          state.template = entry.id;
          Array.prototype.forEach.call(host.querySelectorAll(".template"), function (other) {
            other.classList.toggle("is-on", other === button);
          });
          var link = $("template-link-note");
          if (link) {
            link.textContent = entry.id
              ? "Cutting to " + entry.label + " \u00b7 " + entry.note
              : "Cut your pictures to a real reel's timing";
          }
        });
        host.appendChild(button);
      });
      var count = $("template-state");
      if (count) { count.textContent = all.length + " reels"; }
    }
    paint();
  })();

  /* -- screens ------------------------------------------------------ */
  /* Four pages on one, since a published page has no routes: the edit room,
     the studio, the animation tab and the templates. */
  var PAGES = { studio: "studio-page", animation: "animation-page",
                templates: "templates-page", you: "you-page" };
  var screens = ["screen-start", "screen-working", "screen-done", "screen-error"];
  function goto(to) {
    Object.keys(PAGES).forEach(function (name) {
      document.getElementById(PAGES[name]).hidden = name !== to;
    });
    document.getElementById("app").hidden = !!PAGES[to];
    /* Which tab you are on. Without this the bar is four links that never
       say where you are, which is a menu rather than a tab bar. */
    /* Templates, the animation room and the studio are reached through the
       plus, so the plus is what lights up while you are in one — the same
       rule `chrome.js` applies in the app. A bar that highlights nothing
       tells somebody they are lost. */
    var lit = { templates: "home", animation: "home", studio: "home" }[to] || to;
    var tabs = document.querySelectorAll(".tabbar .tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("is-on", tabs[i].dataset.tab === lit);
      if (tabs[i].dataset.tab === lit) {
        tabs[i].setAttribute("aria-current", "page");
      } else {
        tabs[i].removeAttribute("aria-current");
      }
    }
    window.scrollTo(0, 0);
  }
  function show(id) {
    screens.forEach(function (s) { $(s).hidden = s !== id; });
    goto("home");
  }
  /* The plus opens a chooser rather than going somewhere, the way it does in
     the app. Three of the five tabs need an instance and say so instead of
     doing nothing when tapped. */
  function sheet(id, open) {
    var node = document.getElementById(id);
    if (!node) { return; }
    node.hidden = !open;
    document.body.classList.toggle("sheet-open", !!open);
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-close-create]")) { sheet("create-sheet", false); }
    if (event.target.closest("[data-close-needs]")) { sheet("needs-instance", false); }

    var needs = event.target.closest("[data-needs-instance]");
    if (needs) {
      event.preventDefault();
      var title = document.getElementById("needs-title");
      if (title) {
        title.textContent = needs.dataset.needsInstance + " needs a copy you run";
      }
      sheet("needs-instance", true);
      return;
    }

    var plus = event.target.closest('.tab[data-tab="home"]');
    if (plus) {
      event.preventDefault();
      sheet("create-sheet", document.getElementById("create-sheet").hidden);
      return;
    }

    var link = event.target.closest("[data-goto]");
    if (!link) { return; }
    event.preventDefault();
    sheet("create-sheet", false);
    goto(link.dataset.goto);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      sheet("create-sheet", false);
      sheet("needs-instance", false);
    }
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
        era: state.era || null,
        template: state.template || null,
        onProgress: step,
      })
      .then(function (film) {
        made = film;
        // Left where a check can read it. Nothing on the page uses it; it is
        // here so a test can ask the renderer what it thinks it did and
        // compare that against what came out.
        window.__lastFilm = film;
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

  /* How a transition reads in a sentence. The tally counts by internal name
     because that is what the code uses; a person should be told what the film
     did, not which identifier it used. */
  var JOIN_WORDS = {
    cut: "straight cuts",
    portal: "portals opened on the subject",
    carry: "subjects carried across",
    whip: "whip pans",
    push: "pushes",
    luma: "dissolves through the light",
    slice: "sliced joins",
    flash: "flash frames",
    match: "matched framings"
  };

  function finish(film) {
    show("screen-done");

    /* What the edit is made of, left on the window so a check can read it.
       The alternative is parsing it back out of the sentence below, which
       means the check passes or fails on the wording. */
    window.auteurLastEdit = film.edit;

    /* Say back what it heard. Everything below changed because of the words
       you typed, so if none of it matches what you meant, the mismatch is
       visible instead of silent. */
    var heard = "It read that as " + film.reading.cadence
      + ", graded " + film.reading.look
      + ", cut " + film.reading.style
      + (film.reading.era ? ", shot like the " + film.reading.eraName : "")
      + (film.reading.template ? ", to the " + film.reading.template + " reel's timeline" : "")
      + ", " + Math.round(film.reading.seconds) + " seconds long.";
    if (film.reading.titles.length) {
      heard += " On screen: " + film.reading.titles.map(function (t) {
        return "“" + t + "”";
      }).join(", ") + ".";
    }
    heard += " It used " + film.clips + (film.clips === 1 ? " clip" : " clips")
      + " and " + film.stills + (film.stills === 1 ? " photo" : " photos") + ".";
    if (film.music) { heard += " Your music is on it."; }
    heard += " Built in " + film.movements + " movements, opening on the frame with"
      + " the most in it and tightening as it goes";
    heard += film.loops ? ", and it ends back on that frame so it loops." : ".";

    /* The joins it made, in the order it made most of them. This is the part
       that was invisible: the film had one transition — a hard cut — on every
       join of every edit, and nothing anywhere said so, so "it made no edits"
       was both what it looked like and impossible to check. */
    var joins = Object.keys(film.edit.transitions).sort(function (a, b) {
      return film.edit.transitions[b] - film.edit.transitions[a];
    });
    if (joins.length > 1) {
      heard += " The joins: " + joins.slice(0, 4).map(function (kind) {
        return film.edit.transitions[kind] + " " + (JOIN_WORDS[kind] || kind);
      }).join(", ") + ".";
    }
    $("heard").textContent = heard;
    $("heard").hidden = false;

    var facts = [
      film.seconds.toFixed(1) + " seconds",
      film.shots + " shots, a median " + film.shot_seconds.toFixed(2) + "s each",
      film.edit.kinds + " kinds of join, " + film.edit.carrying
        + " carrying the last picture over",
      film.edit.moves + " camera moves, " + film.edit.held + " shots held still",
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

#: Bumped on every publish and shown in the banner, so a screenshot of the
#: page is enough to know which build it is. VERSIONS.md says what each was.
VERSION = "v9 — a You tab, and settings that are really yours"

BANNER = f"""
<div class="demo-note" role="note">
  <span class="demo-version">{VERSION}</span>
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
  /* A grid, not `float: right` on the version. Floating it put the eyebrow
     inside the first line of the headline, and on a narrow screen the two
     printed on top of each other. */
  display: grid;
  gap: 0.2rem;
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
.demo-note strong { color: var(--text); display: block; }
.demo-version {
  color: var(--ember-text);
  font-size: 0.72rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 600;
}
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
#studio-page, #animation-page, #templates-page { display: block; }
/* Room for the bar, on every section — the app does this with .has-tabbar,
   which is set by the script this build strips. */
#app, .studio-wrap, .page {
  padding-bottom: calc(var(--tabbar) + var(--safe-bottom) + 20px) !important;
}
/* The marks are text here rather than the app's inline SVG: the bar is built
   in Python and a copy of nine paths in a string is a copy that goes stale. */
.tabbar .tab-icon { font-size: 20px; line-height: 1; }
.tabbar .tab-big .tab-icon { font-size: 17px; }
#studio-page[hidden], #animation-page[hidden], #templates-page[hidden],
[hidden] { display: none !important; }
"""

RENDERER = (HERE / "browser-render.js").read_text(encoding="utf-8")
# What a cut is made of, and which of those moves this film makes. Two files
# rather than one because they answer different questions: `cutting.js` knows
# what a portal is, `style.js` decides whether this film uses one. Both have to
# be defined before the renderer runs — it calls into them while building the
# shot list, not just while painting.
VOCABULARY = (HERE / "cutting.js").read_text(encoding="utf-8")
# The grading engine. Real tone curves, split toning, halation and grain, run
# once per photograph — the base looks go through it too, because as CSS
# filter strings two of them moved the picture by less than the eye can see.
GRADING = (HERE / "era.js").read_text(encoding="utf-8")
# The reference reels' measured timelines, from make_templates.py. Numbers
# only — no footage travels with them.
TEMPLATES = (HERE / "templates.json").read_text(encoding="utf-8")
TASTE = (HERE / "style.js").read_text(encoding="utf-8")
# The graphics vocabulary. Shipped in the app and read by two callers — the
# animation tab draws its previews with it and the renderer draws the film with
# it — so the published page needs it before either of them runs.
SHAPES = read("overlay-draw.js")
ANIMATION_TAB = read("overlays.js")

# ---------------------------------------------------------------------------
# The You tab
# ---------------------------------------------------------------------------
# The app's profile is a signed-in, server-backed screen: following, films and
# messages are all about more than one person, and a published page has one.
# So this is the half of it that is genuinely yours and genuinely local — your
# picture, your name, your bio, and the four settings that decide how the app
# looks to you. Those four are not a demonstration: settings.js is inlined
# below, so the switches here are the app's switches running the app's code,
# and what you set persists on this device exactly as it does in the app.
#
# What is missing says so, in one line, rather than being drawn as a number
# that would always be zero.
YOU = """
<main class="page" id="you">
  <header class="topbar">
    <h1>You</h1>
    <span class="topbar-right">
      <button type="button" class="chip-button" id="a-edit-open">Edit</button>
    </span>
  </header>

  <h2 class="large-title" id="a-big-name">You</h2>

  <section class="who">
    <button type="button" class="who-pic" id="a-picture" data-mine="1"
            aria-label="Change your picture">
      <img id="a-picture-img" alt="" hidden>
      <span class="who-initial" id="a-picture-initial" aria-hidden="true">Y</span>
      <span class="who-pic-badge" aria-hidden="true">&#65291;</span>
    </button>
    <p class="card-hint" id="a-picture-note">
      Tap the circle to set a picture. It is squared, scaled to 512px and kept
      on this device &mdash; nothing is uploaded from this page.
    </p>
  </section>

  <p class="who-bio" id="a-bio" hidden></p>
  <a class="who-link" id="a-link" href="#" rel="noopener noreferrer nofollow ugc"
     target="_blank" hidden></a>

  <div class="who-actions">
    <button type="button" class="ghost" id="a-edit-profile">Edit profile</button>
  </div>

  <p class="group-caption" style="margin-top: 0">
    Followers, following and the films on your profile need an instance to be
    about more than one person, so they are not drawn here. Run
    <code>auteur serve</code> and they are the same screen with the counts
    filled in.
  </p>

  <h3 class="settings-label">Appearance</h3>
  <div class="settings">
    <div class="choices appearance" role="radiogroup" aria-label="Appearance">
      <button type="button" class="choice" data-value="system" role="radio" aria-checked="false">Automatic<small>match my phone</small></button>
      <button type="button" class="choice" data-value="light" role="radio" aria-checked="false">Light</button>
      <button type="button" class="choice" data-value="dark" role="radio" aria-checked="false">Dark</button>
    </div>
  </div>

  <h3 class="settings-label">Accessibility</h3>
  <div class="settings">
    <span class="settings-label settings-sub" id="a-text-label">Text size</span>
    <div class="choices sizes" data-setting="text" role="radiogroup" aria-labelledby="a-text-label">
      <button type="button" class="choice" data-value="default" role="radio" aria-checked="false"><span class="size-a size-a-1">A</span><small>Default</small></button>
      <button type="button" class="choice" data-value="large" role="radio" aria-checked="false"><span class="size-a size-a-2">A</span><small>Large</small></button>
      <button type="button" class="choice" data-value="larger" role="radio" aria-checked="false"><span class="size-a size-a-3">A</span><small>Larger</small></button>
      <button type="button" class="choice" data-value="largest" role="radio" aria-checked="false"><span class="size-a size-a-4">A</span><small>Largest</small></button>
    </div>
  </div>

  <div class="settings">
    <span class="settings-label settings-sub" id="a-motion-label">Motion</span>
    <div class="choices" data-setting="motion" role="radiogroup" aria-labelledby="a-motion-label">
      <button type="button" class="choice" data-value="system" role="radio" aria-checked="false">Automatic<small>match my phone</small></button>
      <button type="button" class="choice" data-value="still" role="radio" aria-checked="false">Reduce motion<small>no sliding or fading</small></button>
    </div>
  </div>

  <div class="settings">
    <span class="settings-label settings-sub" id="a-contrast-label">Contrast</span>
    <div class="choices" data-setting="contrast" role="radiogroup" aria-labelledby="a-contrast-label">
      <button type="button" class="choice" data-value="system" role="radio" aria-checked="false">Automatic<small>match my phone</small></button>
      <button type="button" class="choice" data-value="more" role="radio" aria-checked="false">Increase contrast<small>stronger edges and text</small></button>
    </div>
  </div>

  <p class="group-caption">
    All four are kept on this device. If your phone already asks for less
    motion or more contrast, that is followed here whatever this says &mdash;
    Automatic never turns either off.
  </p>
</main>

<div class="sheet" id="a-edit-sheet" hidden>
  <div class="sheet-scrim" data-a-close="1"></div>
  <div class="sheet-body" role="dialog" aria-modal="true" aria-label="Edit profile">
    <div class="sheet-grip" aria-hidden="true"></div>
    <h2 class="sheet-title">Edit profile</h2>
    <label class="field-label" for="a-edit-name">Name</label>
    <input type="text" id="a-edit-name" maxlength="40"
           placeholder="What you would like to be called">
    <label class="field-label" for="a-edit-bio">Bio</label>
    <textarea id="a-edit-bio" maxlength="150" rows="3"
              placeholder="One or two lines about what you make"></textarea>
    <p class="counter"><span id="a-bio-left">150</span> left</p>
    <label class="field-label" for="a-edit-link">Link</label>
    <input type="url" id="a-edit-link" maxlength="120" inputmode="url"
           autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="https://">
    <p class="error" id="a-edit-error" role="alert" hidden></p>
    <button type="button" class="go" id="a-edit-save">Save</button>
    <button type="button" class="ghost" id="a-picture-remove" hidden>Remove my picture</button>
    <button type="button" class="ghost" data-a-close="1">Cancel</button>
  </div>
</div>

<input type="file" id="a-picture-file" accept="image/*" hidden>
"""

# The same three rules the app's own profile keeps, because they are about the
# data and not about where it is stored: a bio is one line however it was
# pasted, a link is refused unless it is plainly http or https, and a picture
# is squared and scaled before it is kept.
YOU_SCRIPT = r"""
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var KEY = "auteur-you";
  var me = { name: "", bio: "", link: "", picture: "" };
  try { me = Object.assign(me, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}

  function oneLine(text, limit) {
    return String(text || "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  /* Same rule as auteur/web/profiles.py: repaired input is how a `javascript:`
     eventually gets repaired into something a tap runs. */
  function tidyLink(link) {
    var raw = oneLine(link, 120);
    if (!raw) { return ""; }
    if (/^https?:\/\//i.test(raw)) { return raw; }
    if (raw.split("/")[0].indexOf(":") !== -1) { return null; }
    return "https://" + raw;
  }

  function keep() {
    try { localStorage.setItem(KEY, JSON.stringify(me)); } catch (e) {}
  }

  function draw() {
    $("a-big-name").textContent = me.name || "You";
    $("a-picture-initial").textContent = (me.name || "You")[0];
    var img = $("a-picture-img");
    if (me.picture) {
      img.src = me.picture;
      img.hidden = false;
      $("a-picture-initial").hidden = true;
    } else {
      img.hidden = true;
      img.removeAttribute("src");
      $("a-picture-initial").hidden = false;
    }
    $("a-bio").textContent = me.bio || "";
    $("a-bio").hidden = !me.bio;
    var link = $("a-link");
    if (me.link) {
      link.href = me.link;
      link.textContent = me.link.replace(/^https?:\/\//, "").replace(/\/$/, "");
      link.hidden = false;
    } else {
      link.hidden = true;
      link.removeAttribute("href");
    }
    $("a-picture-note").hidden = !!me.picture;
  }

  function open() {
    $("a-edit-name").value = me.name;
    $("a-edit-bio").value = me.bio;
    $("a-edit-link").value = me.link;
    $("a-picture-remove").hidden = !me.picture;
    $("a-edit-error").hidden = true;
    count();
    $("a-edit-sheet").hidden = false;
    document.body.classList.add("sheet-open");
  }

  function close() {
    $("a-edit-sheet").hidden = true;
    document.body.classList.remove("sheet-open");
  }

  function count() {
    var left = 150 - $("a-edit-bio").value.length;
    $("a-bio-left").textContent = left;
    $("a-bio-left").parentNode.classList.toggle("is-full", left <= 0);
  }

  $("a-edit-open").addEventListener("click", open);
  $("a-edit-profile").addEventListener("click", open);
  $("a-edit-bio").addEventListener("input", count);
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-a-close]")) { close(); }
  });

  $("a-edit-save").addEventListener("click", function () {
    var link = tidyLink($("a-edit-link").value);
    if (link === null) {
      $("a-edit-error").textContent = "That link needs to start with http:// or https://";
      $("a-edit-error").hidden = false;
      return;
    }
    me.name = oneLine($("a-edit-name").value, 40);
    me.bio = oneLine($("a-edit-bio").value, 150);
    me.link = link;
    keep();
    draw();
    close();
  });

  $("a-picture").addEventListener("click", function () { $("a-picture-file").click(); });
  $("a-picture-remove").addEventListener("click", function () {
    me.picture = "";
    keep();
    draw();
    close();
  });

  /* Squared and scaled in a canvas, which is also what the app does before it
     sends anything — and here it is the whole of it, because there is nowhere
     to send it to. A four-megabyte photograph becomes about sixty kilobytes,
     which matters when the store it goes into is localStorage. */
  $("a-picture-file").addEventListener("change", function () {
    var file = this.files && this.files[0];
    this.value = "";
    if (!file || !window.createImageBitmap) { return; }
    createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
      var side = Math.min(bitmap.width, bitmap.height);
      var out = Math.min(side, 512);
      var canvas = document.createElement("canvas");
      canvas.width = out;
      canvas.height = out;
      canvas.getContext("2d").drawImage(
        bitmap,
        (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side,
        0, 0, out, out
      );
      bitmap.close();
      me.picture = canvas.toDataURL("image/jpeg", 0.86);
      keep();
      draw();
    }).catch(function () {});
  });

  draw();
})();
"""

page = f"""<title>Auteur Edit Room</title>
<!-- Before anything paints, and inlined from the app rather than rewritten:
     appearance and the three accessibility settings. This is the same file the
     served pages load in their head, which is what makes the switches on the
     You tab the app's switches rather than a drawing of them. -->
<script>
{read("settings.js")}
</script>
<style>
{css}
{EXTRA}
</style>
{BANNER}
{home}
<div id="studio-page" hidden>
{studio}
</div>
<div id="animation-page" hidden>
{animation}
</div>
<div id="templates-page" hidden>
{templates}
</div>
<div id="you-page" hidden>
{YOU}
</div>
{TAB_BAR}
<script>
{SHAPES}
</script>
<script>
{GRADING}
</script>
<script>
window.auteurTemplates = {TEMPLATES};
</script>
<script>
{VOCABULARY}
</script>
<script>
{TASTE}
</script>
<script>
{RENDERER}
</script>
<script>
{DEMO}
</script>
<script>
{ANIMATION_TAB}
</script>
<script>
{read("theme.js")}
</script>
<script>
{YOU_SCRIPT}
</script>
"""
OUT.write_text(page, encoding="utf-8")
print(OUT, len(page), "bytes")
