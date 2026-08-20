/* The whole front end. No framework, no build step.
 *
 * Three screens and one loop: post the clips, poll the job, show the film.
 */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var screens = {
    start: $("screen-start"),
    working: $("screen-working"),
    done: $("screen-done"),
    error: $("screen-error")
  };

  // `seconds: ""` means "no length given" — the prompt decides. Anything else
  // is an explicit override the person tapped.
  var state = { jobId: null, timer: null, shape: "reel", seconds: "", era: "",
               template: "", videoUrl: null, lastStage: "", lastPercent: -1 };

  function show(name) {
    Object.keys(screens).forEach(function (key) {
      screens[key].hidden = key !== name;
    });
    window.scrollTo(0, 0);
  }

  // -- the three little pickers ---------------------------------------------

  function wireChoices(container, onPick) {
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

  wireChoices($("shape"), function (value) { state.shape = value; });
  wireChoices($("seconds"), function (value) { state.seconds = value; });
  wireChoices($("era"), function (value) { state.era = value; });

  /* The template lives on its own tab now.
   *
   * Nineteen chips on this screen was a wall you had to scroll past to reach
   * the button that makes the film, and every chip said roughly the same
   * thing. The tab writes the choice to `auteur-template`; this reads it, the
   * same one-setting-two-readers arrangement the animation tab uses. */
  try { state.template = localStorage.getItem("auteur-template") || ""; } catch (e) {}

  /* What is actually in each room.
   *
   * The five tiles used to be five identical cards whose only content was
   * their own name, so the only way to learn that the library held 29 reels
   * or that the Scholar had 400 learnings was to open the room. Each count
   * comes from the same endpoint the room itself reads — never restated
   * here, which is how a tile ends up confidently naming a number the
   * program stopped having. A room whose endpoint is down keeps its blank
   * line rather than showing a zero that is not true. */
  (function () {
    function say(id, text) {
      var slot = $(id);
      if (slot) { slot.textContent = text; }
    }

    function count(n, one, many) {
      return n + " " + (n === 1 ? one : (many || one + "s"));
    }

    function room(id, url, read) {
      if (!$(id)) { return; }
      fetch(url, { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (said) { say(id, read(said || {}) || ""); })
        .catch(function () { /* the tile still links through */ });
    }

    room("room-templates", "/api/templates", function (said) {
      var all = said.templates || [];
      // Two readers of one setting, the way the animation tab does it: the
      // templates tab writes `auteur-template`, this reads it back and says
      // which reel the next film will be cut to.
      var note = $("template-link-note");
      if (note && state.template) {
        for (var i = 0; i < all.length; i++) {
          if (all[i].id === state.template) {
            note.textContent = "cutting to " + all[i].label;
            break;
          }
        }
      }
      return count(all.length, "reel");
    });

    room("room-scholar", "/api/scholar", function (said) {
      if (!said.available) { return "not running"; }
      return count(said.learnings || 0, "learning");
    });

    room("room-overlays", "/api/overlays", function (said) {
      return count((said.kinds || []).length, "shape");
    });

    room("room-studio", "/api/crew", function (said) {
      return count(said.kinds || 0, "proposal");
    });
  })();

  /* Footage shared into the app from Photos, Gallery, or any other app.
   *
   * The share target posts the files and redirects here. If the make screen
   * did not say so, the files would be sitting on the server invisibly and the
   * screen would still be asking somebody to pick some — which reads as the
   * share having failed. */
  (function () {
    var box = $("handed");
    if (!box) { return; }

    function clear() {
      box.hidden = true;
      fetch("/api/shared/clear", { method: "POST", credentials: "same-origin" })
        .catch(function () { /* it is claimed when the film is made anyway */ });
    }

    fetch("/api/shared", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (said) {
        if (!said || !said.waiting) { return; }
        $("handed-title").textContent =
          said.waiting + (said.waiting === 1 ? " clip" : " clips") + " from your camera roll";
        $("handed-note").textContent = said.names
          ? said.names.slice(0, 3).join(", ") + (said.waiting > 3 ? " and more" : "")
          : "";
        box.hidden = false;
        // A caption came with the share often enough to be worth using.
        if (said.said && !$("prompt").value) { $("prompt").value = said.said; }
      })
      .catch(function () { /* nothing waiting, or signed out */ });

    $("handed-drop").addEventListener("click", clear);
  })();

  // -- who is signed in -----------------------------------------------------

  fetch("/api/session", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(function (payload) {
      if (!payload.user) { window.location.href = "/login"; return; }
      $("whoami-name").textContent = "Signed in as " + payload.user;
      $("whoami").hidden = false;
    })
    .catch(function () { /* offline: the cached shell is still worth showing */ });

  $("sign-out").addEventListener("click", function () {
    fetch("/api/logout", { method: "POST", cache: "no-store" })
      .catch(function () { /* going to the login page regardless */ })
      .then(function () { window.location.href = "/login"; });
  });

  // Appearance lives in theme.js, which every page loads — it used to be
  // here, so the studio and the ask page had no way to change it at all.


  $("chips").addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (!chip) { return; }
    $("prompt").value = chip.dataset.prompt || chip.textContent;
  });

  var clips = $("clips");
  clips.addEventListener("change", function () {
    var count = clips.files ? clips.files.length : 0;
    var hint = $("clips-hint");
    var action = $("clips-action");
    $("clips").closest(".card").classList.toggle("has-files", count > 0);
    if (!count) {
      hint.textContent = "Straight from your camera roll. Music too, if you have some.";
      if (action) { action.textContent = "Choose from camera roll"; }
      return;
    }
    var bytes = 0;
    for (var i = 0; i < clips.files.length; i++) { bytes += clips.files[i].size; }
    hint.textContent = count + (count === 1 ? " clip" : " clips") + " ready  ·  " + megabytes(bytes);
    // Once there are clips the card's job changes from "start here" to "swap
    // these", and the button has to say so or it reads as an unfinished step.
    if (action) { action.textContent = "Choose different clips"; }
  });

  function megabytes(bytes) {
    if (bytes > 1024 * 1024 * 1024) { return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB"; }
    return Math.max(1, Math.round(bytes / 1024 / 1024)) + " MB";
  }

  // -- sending it off -------------------------------------------------------

  $("form").addEventListener("submit", function (event) {
    event.preventDefault();
    var problem = $("start-error");
    problem.hidden = true;

    if (!clips.files || !clips.files.length) {
      problem.textContent = "Pick at least one clip first.";
      problem.hidden = false;
      return;
    }
    var prompt = $("prompt").value.trim();
    if (!prompt) {
      prompt = $("prompt").placeholder;  // a sensible default beats a scolding
      $("prompt").value = prompt;
    }

    var form = new FormData();
    form.append("prompt", prompt);
    form.append("shape", state.shape);
    form.append("seconds", state.seconds);
    /* Sent even when empty, so the server always knows the answer rather than
       inferring it from the prompt when the field happens to be missing. A
       control that sets a variable nobody transmits is a control that does
       nothing, which is worse than not offering one. */
    form.append("era", state.era);
    form.append("template", state.template);
    for (var i = 0; i < clips.files.length; i++) {
      form.append("clips", clips.files[i], clips.files[i].name);
    }

    $("go").disabled = true;
    show("working");
    setStage("Sending your clips", "");
    setPercent(0);

    // XHR rather than fetch, only because it reports upload progress — over a
    // phone connection the upload is often longer than the render.
    var request = new XMLHttpRequest();
    request.open("POST", "/api/jobs");
    request.upload.addEventListener("progress", function (progress) {
      if (!progress.lengthComputable) { return; }
      var percent = 100 * progress.loaded / progress.total;
      setPercent(percent);
      setStage("Sending your clips", Math.round(percent) + "% uploaded");
    });
    request.addEventListener("load", function () {
      $("go").disabled = false;
      var payload = {};
      try { payload = JSON.parse(request.responseText); } catch (err) { payload = {}; }
      if (request.status >= 300 || !payload.id) {
        fail(payload.error || "The upload did not go through.");
        return;
      }
      state.jobId = payload.id;
      setStage("Getting ready", "");
      setPercent(0);
      poll();
    });
    request.addEventListener("error", function () {
      $("go").disabled = false;
      fail("Lost the connection while uploading.");
    });
    request.send(form);
  });

  // -- watching it work -----------------------------------------------------

  // A render is minutes long, so polling at a fixed second costs hundreds of
  // requests and keeps a phone's radio awake for no benefit. Start responsive,
  // then ease off — and stop entirely while the page is hidden, resuming the
  // moment it comes back.
  var POLL_MIN = 1000, POLL_MAX = 5000;
  var pollGap = POLL_MIN;

  function schedulePoll(gap) {
    clearTimeout(state.timer);
    if (document.hidden) { return; }
    state.timer = setTimeout(poll, gap);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && state.jobId) { pollGap = POLL_MIN; poll(); }
  });

  function poll() {
    clearTimeout(state.timer);
    fetch("/api/jobs/" + state.jobId, { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (job) {
        if (job.error && job.status === "error") { fail(job.error); return; }
        // Something moved? Poll keenly again. Nothing changed? Ease off.
        var moved = job.stage !== state.lastStage || job.percent !== state.lastPercent;
        state.lastStage = job.stage;
        state.lastPercent = job.percent;
        pollGap = moved ? POLL_MIN : Math.min(POLL_MAX, pollGap * 1.4);

        setStage(job.stage, job.detail);
        setPercent(job.percent);
        setLog(job.lines || []);
        if (job.status === "done") { finish(job); return; }
        schedulePoll(pollGap);
      })
      .catch(function () {
        // A phone that locked its screen drops the request; keep trying rather
        // than declaring failure over one missed poll.
        schedulePoll(2500);
      });
  }

  function setStage(stage, detail) {
    $("stage").textContent = stage || "Working";
    $("detail").textContent = detail || "";
  }

  function setPercent(percent) {
    $("bar-fill").style.width = Math.max(0, Math.min(100, percent || 0)) + "%";
  }

  var lastLogLength = 0;
  function setLog(lines) {
    if (lines.length === lastLogLength) { return; }
    lastLogLength = lines.length;
    var list = $("log");
    list.textContent = "";
    lines.slice(-8).forEach(function (line) {
      var item = document.createElement("li");
      item.className = line.kind === "step" ? "step" : (line.kind === "warn" ? "warn" : "");
      item.textContent = line.kind && line.kind !== "step" && line.kind !== "detail" && line.kind !== "warn"
        ? line.kind + "  " + line.text
        : line.text;
      list.appendChild(item);
    });
  }

  // -- the ending -----------------------------------------------------------

  function finish(job) {
    state.videoUrl = job.video;
    $("player").src = job.video;
    $("save").href = job.video;
    $("notes").href = job.notes || "#";
    $("notes").hidden = !job.notes;

    $("heard").textContent = job.heard || "";
    $("heard").hidden = !job.heard;

    var facts = $("facts");
    facts.textContent = "";
    (job.facts || []).forEach(function (fact) {
      var item = document.createElement("li");
      item.textContent = fact;
      facts.appendChild(item);
    });

    show("done");
  }

  // On an iPhone the share sheet is the only route into Photos, so use it when
  // it exists and fall back to a plain download everywhere else.
  $("save").addEventListener("click", function (event) {
    if (!navigator.canShare || !state.videoUrl) { return; }
    event.preventDefault();
    var button = $("save");
    var was = button.textContent;
    button.textContent = "Preparing…";
    fetch(state.videoUrl)
      .then(function (response) { return response.blob(); })
      .then(function (blob) {
        var file = new File([blob], "auteur-film.mp4", { type: "video/mp4" });
        if (!navigator.canShare({ files: [file] })) { throw new Error("no file sharing"); }
        return navigator.share({ files: [file], title: "My film" });
      })
      .catch(function () {
        window.location.href = state.videoUrl;  // plain download instead
      })
      .then(function () { button.textContent = was; });
  });

  function fail(message) {
    clearTimeout(state.timer);
    $("error-text").textContent = message;
    show("error");
  }

  function reset() {
    clearTimeout(state.timer);
    state.jobId = null;
    state.lastStage = "";
    state.lastPercent = -1;
    pollGap = POLL_MIN;
    lastLogLength = 0;
    $("player").pause();
    $("player").removeAttribute("src");
    $("go").disabled = false;
    show("start");
  }

  $("cancel").addEventListener("click", reset);
  $("again").addEventListener("click", reset);
  $("retry").addEventListener("click", reset);

  // -- installability -------------------------------------------------------

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* fine without it */ });
    });
  }

  // Already installed? Then there is nothing to advertise.
  var standalone = window.matchMedia("(display-mode: standalone)").matches ||
                   window.navigator.standalone === true;

  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    // Chrome only. Holding the event lets us put installation behind our own
    // button instead of whatever the browser decides to show, or not show.
    event.preventDefault();
    deferredPrompt = event;
    if (standalone) { return; }
    $("install").hidden = false;
    $("install-go").hidden = false;
    $("install-hint").textContent = "Keeps it one tap away, and it opens full screen.";
  });

  $("install-go").addEventListener("click", function () {
    if (!deferredPrompt) { return; }
    deferredPrompt.prompt();
    deferredPrompt.userChoice.then(function () {
      deferredPrompt = null;
      $("install").hidden = true;
    });
  });

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    $("install").hidden = true;
  });

  // Safari never fires beforeinstallprompt, and Chrome withholds it unless the
  // page is on a secure origin — which a plain http:// LAN address is not. In
  // both cases say what to do instead of leaving a dead corner of the screen.
  if (!standalone) {
    setTimeout(function () {
      if (deferredPrompt || !$("install").hidden) { return; }
      var ua = navigator.userAgent;
      var iOS = /iPhone|iPad|iPod/.test(ua);
      var hint = "";
      if (iOS) {
        hint = "Add it to your home screen: tap Share, then Add to Home Screen.";
      } else if (!window.isSecureContext) {
        hint = "Open this on the computer itself (localhost) to install it as an app.";
      }
      if (hint) {
        $("install").hidden = false;
        $("install-hint").textContent = hint;
      }
    }, 1200);
  }
  // -- scroll reveal animations ----------------------------------------------

  var reveals = document.querySelectorAll(".scroll-reveal, .slide-in-left, .slide-in-right, .scale-in");
  if (reveals.length && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    reveals.forEach(function (el) { observer.observe(el); });
  } else {
    // No IntersectionObserver: show everything immediately
    reveals.forEach(function (el) { el.classList.add("is-visible"); });
  }

  // Add screen-enter animation class when switching screens
  var originalShow = show;
  show = function (name) {
    originalShow(name);
    var current = screens[name];
    if (current) {
      current.classList.remove("screen-enter");
      void current.offsetWidth; // force reflow
      current.classList.add("screen-enter");
    }
  };
})();
