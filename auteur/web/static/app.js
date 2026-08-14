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

  var state = { jobId: null, timer: null, shape: "reel", seconds: "20", videoUrl: null };

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

  $("chips").addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (!chip) { return; }
    $("prompt").value = chip.dataset.prompt || chip.textContent;
  });

  var clips = $("clips");
  clips.addEventListener("change", function () {
    var count = clips.files ? clips.files.length : 0;
    var hint = $("clips-hint");
    $("clips").closest(".card").classList.toggle("has-files", count > 0);
    if (!count) {
      hint.textContent = "Straight from your camera roll. Music too, if you have some.";
      return;
    }
    var bytes = 0;
    for (var i = 0; i < clips.files.length; i++) { bytes += clips.files[i].size; }
    hint.textContent = count + (count === 1 ? " clip" : " clips") + " ready  ·  " + megabytes(bytes);
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

  function poll() {
    clearTimeout(state.timer);
    fetch("/api/jobs/" + state.jobId, { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (job) {
        if (job.error && job.status === "error") { fail(job.error); return; }
        setStage(job.stage, job.detail);
        setPercent(job.percent);
        setLog(job.lines || []);
        if (job.status === "done") { finish(job); return; }
        state.timer = setTimeout(poll, 1200);
      })
      .catch(function () {
        // A phone that locked its screen drops the request; keep trying rather
        // than declaring failure over one missed poll.
        state.timer = setTimeout(poll, 2500);
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
})();
