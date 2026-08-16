/* Cutting a film in the browser.
 *
 * The real renderer is ffmpeg driven from Python, and a published page has
 * neither. But a browser can already decode video, draw it, and encode it:
 * <video> into a <canvas>, canvas.captureStream() into a MediaRecorder, out
 * comes a file that plays. So the link cuts a real film from your own clips
 * rather than describing one.
 *
 * What it is not: this cuts to a cadence, frames each shot, applies a grade
 * and sets your quoted words on screen. It does not detect beats, read the
 * frames or run the crew — those need the measurements only the Python side
 * makes. It is the shape of the thing, performed rather than played back.
 */
(function (global) {
  "use strict";

  var SHAPES = {
    reel: [1080, 1920],
    square: [1080, 1080],
    wide: [1920, 1080]
  };

  /* The looks. Each is a canvas filter plus how hard to vignette, and each
   * carries the word it will report back, because a person who typed
   * something and got a film needs to see which part of it was heard.
   *
   * There is no "none". Every film gets a look: the previous version fell
   * through to `filter: none` whenever a prompt missed its five keywords,
   * which is most prompts, and the result was ungraded footage that looked
   * exactly like the camera roll it came from. "It made no edits" is what
   * that looks like from the outside. */
  var LOOKS = {
    noir:      { label: "black and white", filter: "grayscale(1) contrast(1.35) brightness(0.97)", vignette: 0.55 },
    neon:      { label: "neon",            filter: "saturate(1.5) contrast(1.2) hue-rotate(-10deg) brightness(0.96)", vignette: 0.5 },
    warm:      { label: "warm",            filter: "saturate(1.15) contrast(1.08) sepia(0.2) brightness(1.04)", vignette: 0.3 },
    cool:      { label: "cool",            filter: "saturate(1.05) contrast(1.12) hue-rotate(8deg) brightness(0.99)", vignette: 0.35 },
    cinematic: { label: "cinematic",       filter: "saturate(0.94) contrast(1.24) brightness(0.94)", vignette: 0.45 },
    punchy:    { label: "punchy",          filter: "saturate(1.32) contrast(1.28) brightness(1.02)", vignette: 0.28 },
    faded:     { label: "faded film",      filter: "saturate(0.82) contrast(0.92) sepia(0.28) brightness(1.08)", vignette: 0.22 },
    house:     { label: "house grade",     filter: "saturate(1.12) contrast(1.1) brightness(1.01)", vignette: 0.3 }
  };

  /* Words, not five regexes. A prompt is somebody describing a feeling —
   * "my trip to the coast", "gym stuff", "the wedding" — and none of those
   * used to match anything at all. Each look now answers to the words people
   * actually reach for, and anything unmatched still gets the house grade. */
  var LOOK_WORDS = [
    ["noir", /black\s*(and|&)\s*white|b\s*&\s*w|monochrome|greyscale|grayscale|noir|gritty|stark|documentary|street/],
    ["neon", /neon|cyber|synth|vaporwave|night\s*(life|out|drive|s)?|club|rave|party|city|electric|glow|miami|arcade|techno|edm/],
    ["warm", /warm|summer|golden|sunset|sunrise|nostalg|memor|cozy|cosy|home|family|holiday|vacation|beach|honey|autumn|fall\b|love|wedding|birthday|friends/],
    ["cool", /cool|cold|blue|winter|snow|ice|steel|clean|minimal|ocean|sea|rain|storm|moody\s*blue|calm/],
    ["cinematic", /cinematic|filmic|epic|movie|dramatic|teal|anamorphic|trailer|landscape|travel|mountain|widescreen|serious/],
    ["punchy", /punchy|hypercut|hard\s*to\s*the\s*beat|rapid|fast|energy|energet|hype|gym|workout|sport|training|run|skate|bold|aggressive|loud/],
    ["faded", /faded|vintage|retro|super\s*8|8mm|film\s*grain|old|analog|analogue|polaroid|70s|80s|90s|dreamy|soft/]
  ];

  function lookFor(prompt) {
    var p = (prompt || "").toLowerCase();
    for (var i = 0; i < LOOK_WORDS.length; i++) {
      if (LOOK_WORDS[i][1].test(p)) { return LOOKS[LOOK_WORDS[i][0]]; }
    }
    return LOOKS.house;
  }

  /* The cadence, from the same words the director reads. A hypercut is the
   * measured reference median: 0.167s a shot. */
  var CADENCES = [
    [0.167, "a hypercut — three cuts a beat", /hypercut|rapid\s*fire|flurry|machine\s*gun|frantic|chaotic|blitz/],
    [0.35, "very fast", /very\s*fast|super\s*fast|breakneck|relentless|hype|edm|techno|rave/],
    [0.5, "fast, hard cuts", /punchy|hard\s*to\s*the\s*beat|on\s*the\s*beat|fast|kinetic|snappy|quick|energet|gym|workout|sport|party|montage/],
    [1.8, "slow and held", /slow|cinematic|calm|gentle|meditative|dreamy|relaxed|ambient|peaceful|soft|tender/],
    [1.2, "unhurried", /steady|documentary|story|narrative|travel|landscape/]
  ];

  function cadenceFor(prompt) {
    var p = (prompt || "").toLowerCase();
    // An explicit rate wins over any adjective: "cut every 0.4 seconds".
    var each = /(?:every|each|shots?\s*of)\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)/i.exec(p);
    if (each) {
      var want = Math.max(0.1, Math.min(6, parseFloat(each[1])));
      return { hold: want, label: want.toFixed(2) + "s a shot, as asked" };
    }
    // A tempo, if they gave one: 120bpm and a cut a beat.
    var bpm = /(\d{2,3})\s*bpm/i.exec(p);
    if (bpm) {
      var beat = 60 / Math.max(40, Math.min(220, parseInt(bpm[1], 10)));
      return { hold: beat, label: bpm[1] + "bpm — a cut a beat" };
    }
    for (var i = 0; i < CADENCES.length; i++) {
      if (CADENCES[i][2].test(p)) { return { hold: CADENCES[i][0], label: CADENCES[i][1] }; }
    }
    return { hold: 0.9, label: "an even cut" };
  }

  // Kept as the old name so anything calling it still works.
  function shotSecondsFor(prompt) { return cadenceFor(prompt).hold; }

  function secondsFrom(prompt, fallback) {
    var p = prompt || "";
    var m = /(\d+(?:\.\d+)?)\s*(?:s\b|sec|secs|second|seconds)/i.exec(p);
    if (m) { return Math.max(3, Math.min(60, parseFloat(m[1]))); }
    if (/half\s*a?\s*minute/i.test(p)) { return 30; }
    if (/a?\s*minute/i.test(p)) { return 60; }
    return fallback;
  }

  /* Anything in quotes goes on screen — the app has promised this under the
   * prompt box the whole time and the browser cut never did it. Straight and
   * curly quotes both, because a phone keyboard types curly ones. */
  function titlesFrom(prompt) {
    var found = [];
    var re = /["“‘']([^"“”‘’']{1,48})["”’']/g;
    var m;
    while ((m = re.exec(prompt || "")) !== null) {
      var text = m[1].trim();
      if (text) { found.push(text); }
      if (found.length >= 8) { break; }
    }
    return found;
  }

  /* What it understood, in the person's own terms. Returned with the film so
   * the page can say it back — a prompt that changes nothing visible and is
   * never acknowledged is indistinguishable from a prompt that was ignored. */
  function read(prompt, fallbackSeconds) {
    var look = lookFor(prompt);
    var cadence = cadenceFor(prompt);
    return {
      look: look,
      lookName: look.label,
      hold: cadence.hold,
      cadence: cadence.label,
      seconds: secondsFrom(prompt, fallbackSeconds),
      titles: titlesFrom(prompt)
    };
  }

  /* Deterministic per-shot randomness. Two runs of the same prompt over the
   * same clips should give the same film; Math.random would make every
   * re-render a different edit and nothing could be compared. */
  function rng(seed) {
    var a = (seed * 2654435761) >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* Resolves with a source, or with null if this file will not open.
   *
   * Never rejects. A camera roll has odd things in it — a codec this browser
   * will not take, a file that is still syncing — and one of them should cost
   * one shot rather than the whole film. */
  function loadVideo(file) {
    return new Promise(function (resolve) {
      var v = document.createElement("video");
      var settled = false;
      function done(value) { if (!settled) { settled = true; resolve(value); } }

      v.preload = "auto";
      v.muted = true;            // autoplay on iOS needs this
      v.playsInline = true;
      v.setAttribute("playsinline", "");
      /* `loadeddata`, not `loadedmetadata`: metadata gives the dimensions but
       * no picture, and a <video> with nothing decoded draws as a no-op. That
       * made the frame-size benchmark time an empty draw, come back fast, and
       * choose a size this device could not hold. */
      v.addEventListener("loadeddata", function () {
        // Zero dimensions means it opened and there is no picture in it.
        done(v.videoWidth && v.videoHeight
          ? { el: v, kind: "video", duration: v.duration || 1 }
          : null);
      }, { once: true });
      v.addEventListener("error", function () { done(null); }, { once: true });
      // Some files never fire either event. Do not wait on them forever.
      setTimeout(function () { done(null); }, 8000);
      v.src = URL.createObjectURL(file);
    });
  }

  /* Photographs. The previous version filtered the picked files down to
   * `video/*` and said nothing about the rest, so a camera roll — which is
   * mostly photographs — arrived as one or two clips and the pictures were
   * silently thrown away. They are shots. */
  function loadImage(file) {
    return new Promise(function (resolve) {
      var img = new Image();
      var settled = false;
      function done(value) { if (!settled) { settled = true; resolve(value); } }
      img.onload = function () {
        done(img.naturalWidth && img.naturalHeight
          ? { el: img, kind: "image", duration: 0 }
          : null);
      };
      img.onerror = function () { done(null); };
      setTimeout(function () { done(null); }, 8000);
      img.src = URL.createObjectURL(file);
    });
  }

  function loadSource(file) {
    if (/^video\//.test(file.type)) { return loadVideo(file); }
    if (/^image\//.test(file.type)) { return loadImage(file); }
    // Some phones hand over a HEIC or a .mov with an empty type. Guess from
    // the name rather than dropping it.
    if (/\.(mov|mp4|m4v|webm|avi|mkv)$/i.test(file.name || "")) { return loadVideo(file); }
    if (/\.(jpe?g|png|heic|heif|gif|webp|avif)$/i.test(file.name || "")) { return loadImage(file); }
    return Promise.resolve(null);
  }

  function sizeOf(source) {
    var el = source.el;
    return [
      el.videoWidth || el.naturalWidth || el.width || 1,
      el.videoHeight || el.naturalHeight || el.height || 1
    ];
  }

  /* Grade a photograph once, into a canvas, and draw from that afterwards.
   *
   * `ctx.filter` is the single most expensive thing in the loop — measured at
   * 60ms a frame for one 1080x1920 filtered draw where the same draw ungraded
   * costs 9ms. Paid per frame it starves the paint loop, and a film that
   * should hold each shot for 0.167s comes out with five distinct pictures a
   * second. A photograph does not change, so its grade need not be recomputed
   * thirty times a second. */
  function gradeStill(source, filter) {
    var s = sizeOf(source);
    var scale = Math.min(1, 2200 / Math.max(s[0], s[1]));
    var c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(s[0] * scale));
    c.height = Math.max(1, Math.round(s[1] * scale));
    var g = c.getContext("2d", { alpha: false });
    g.filter = filter;
    try {
      g.drawImage(source.el, 0, 0, c.width, c.height);
    } catch (e) {
      return source;
    }
    return { el: c, kind: "image", duration: 0, graded: true };
  }

  /* Video frames still have to be graded as they arrive, so the frame size has
   * to be one this device can actually sustain. Measured here rather than
   * assumed: a phone with a GPU keeps 1080, a slow software renderer drops to
   * something it can hold, and either way the cut lands on time. */
  function affordableSize(W, H, sample, filter) {
    var steps = [1, 0.75, 0.5];
    for (var i = 0; i < steps.length; i++) {
      var w = Math.round(W * steps[i] / 2) * 2;
      var h = Math.round(H * steps[i] / 2) * 2;
      var c = document.createElement("canvas");
      c.width = w; c.height = h;
      var g = c.getContext("2d", { alpha: false });
      g.filter = filter;
      try {
        g.drawImage(sample, 0, 0, w, h);          // warm up, then time it
        var began = performance.now();
        for (var n = 0; n < 3; n++) { g.drawImage(sample, 0, 0, w, h); }
        var each = (performance.now() - began) / 3;
      } catch (e) {
        return [W, H];
      }
      // 30fps is 33ms a frame and the decode has to fit in there too.
      if (each < 14 || i === steps.length - 1) { return [w, h]; }
    }
    return [W, H];
  }

  /* Draw one framed shot.
   *
   * `zoom` is on top of cover, and `dx`/`dy` slide within whatever the crop
   * throws away, so two shots off the same photograph are two different
   * pictures. Without this a film cut from one clip looked like that clip
   * played straight through: the cuts were there and nothing on screen
   * changed at them, which is the second half of "it made no edits". */
  function drawFramed(ctx, source, W, H, zoom, dx, dy) {
    var s = sizeOf(source);
    var scale = Math.max(W / s[0], H / s[1]) * zoom;
    var w = s[0] * scale, h = s[1] * scale;
    var x = (W - w) / 2 + dx * Math.max(0, (w - W) / 2);
    var y = (H - h) / 2 + dy * Math.max(0, (h - H) / 2);
    ctx.drawImage(source.el, x, y, w, h);
  }

  function vignetteCanvas(W, H, strength) {
    var c = document.createElement("canvas");
    c.width = W; c.height = H;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(
      W / 2, H / 2, Math.min(W, H) * 0.32,
      W / 2, H / 2, Math.max(W, H) * 0.72
    );
    grad.addColorStop(0, "rgba(0,0,0,0)");
    grad.addColorStop(1, "rgba(0,0,0," + strength.toFixed(2) + ")");
    g.fillStyle = grad;
    g.fillRect(0, 0, W, H);
    return c;
  }

  function drawTitle(ctx, text, W, H, alpha) {
    var size = Math.round(Math.min(W, H) * 0.072);
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.font = "700 " + size + "px 'Helvetica Neue', Helvetica, Arial, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    try { ctx.letterSpacing = Math.round(size * 0.06) + "px"; } catch (e) {}
    var shown = text.toUpperCase();
    // Shrink until it fits with a margin, rather than running off the frame.
    while (ctx.measureText(shown).width > W * 0.86 && size > 18) {
      size -= 2;
      ctx.font = "700 " + size + "px 'Helvetica Neue', Helvetica, Arial, sans-serif";
    }
    var y = H * 0.78;
    ctx.shadowColor = "rgba(0,0,0,0.6)";
    ctx.shadowBlur = Math.round(size * 0.5);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(shown, W / 2, y);
    ctx.restore();
  }

  function pickRecorder(stream) {
    var tries = [
      "video/mp4;codecs=avc1",   // what Safari gives, and what a phone saves
      "video/mp4",
      "video/webm;codecs=vp9",
      "video/webm;codecs=vp8",
      "video/webm"
    ];
    for (var i = 0; i < tries.length; i++) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(tries[i])) {
        return new MediaRecorder(stream, { mimeType: tries[i], videoBitsPerSecond: 6000000 });
      }
    }
    return new MediaRecorder(stream);
  }

  /* Music, if any was picked. The picker has always said "Music too, if you
   * have some" and the cut has always been silent. Best effort: not every
   * browser will hand over a media element's audio, and a film without the
   * track is better than no film. */
  function isMusic(file) {
    return /^audio\//.test(file.type)
      || /\.(mp3|m4a|aac|wav|ogg|flac)$/i.test(file.name || "");
  }

  function attachMusic(files, stream) {
    var track = null;
    for (var i = 0; i < files.length; i++) {
      if (isMusic(files[i])) { track = files[i]; break; }
    }
    if (!track) { return null; }
    try {
      var el = document.createElement("audio");
      el.src = URL.createObjectURL(track);
      el.loop = true;
      var capture = el.captureStream || el.mozCaptureStream;
      if (!capture) { return null; }
      var captured = capture.call(el);
      var tracks = captured.getAudioTracks();
      if (!tracks.length) {
        // Chrome populates the tracks only once playback starts; add them on
        // the way through instead of giving up here.
        captured.onaddtrack = function (e) {
          try { stream.addTrack(e.track); } catch (err) {}
        };
      }
      tracks.forEach(function (t) { try { stream.addTrack(t); } catch (err) {} });
      return el;
    } catch (e) {
      return null;
    }
  }

  /* Cut a film. Returns {url, type, shots, seconds, reading, ...}.
     `onProgress(fraction, label)` is called as it goes, because a phone doing
     this needs to show that something is happening. */
  function cut(options) {
    var files = (options.files || []).slice();
    var prompt = options.prompt || "";
    var shape = SHAPES[options.shape] || SHAPES.reel;
    var onProgress = options.onProgress || function () {};

    var W = shape[0], H = shape[1];
    var plan = read(prompt, options.seconds || 10);
    var hold = plan.hold;
    var total = plan.seconds;
    var filter = plan.look.filter;

    var pickable = files.filter(function (f) { return !isMusic(f); });
    if (!pickable.length) {
      return Promise.reject(new Error("nothing to cut — pick some clips or photos"));
    }

    return Promise.all(pickable.map(loadSource)).then(function (opened) {
      var loaded = opened.filter(Boolean);
      if (!loaded.length) {
        throw new Error("none of those would open in this browser");
      }
      var videos = loaded.filter(function (s) { return s.kind === "video"; });
      var stills = loaded.length - videos.length;
      var skipped = opened.length - loaded.length;

      onProgress(0.06, "Looking at everything you gave it — "
        + videos.length + (videos.length === 1 ? " clip" : " clips")
        + " and " + stills + (stills === 1 ? " photo" : " photos")
        + (skipped ? ", " + skipped + " would not open" : ""));

      // Photographs get their grade baked in now, once each. Video has to be
      // graded frame by frame, so the frame size is set by what this device
      // can hold at that cost.
      var sources = loaded.map(function (s) {
        return s.kind === "image" ? gradeStill(s, filter) : s;
      });
      if (videos.length) {
        /* Time the filter against something that actually has pixels in it.
         * Handing the benchmark a <video> that has not decoded a frame yet
         * measures a drawImage that does nothing, comes back fast, and picks
         * a frame size this device cannot hold — which is how a 0.167s cut
         * came out with five distinct pictures a second. A photograph is
         * loaded and the same size class as a frame of video, so it is the
         * honest stand-in; failing that, the video, imprecisely. */
        var sample = null;
        for (var s = 0; s < loaded.length; s++) {
          if (loaded[s].kind === "image") { sample = loaded[s].el; break; }
        }
        var size = affordableSize(W, H, sample || videos[0].el, filter);
        W = size[0]; H = size[1];
      }

      var canvas = document.createElement("canvas");
      canvas.width = W; canvas.height = H;
      var ctx = canvas.getContext("2d", { alpha: false });
      var vignette = vignetteCanvas(W, H, plan.look.vignette);

      /* The shot list. Round-robin through everything you picked, so a
       * camera roll of eight photographs and one clip is a film of eight
       * photographs and one clip, not a film of the clip.
       *
       * Each shot carries its own framing, and lengths follow a phrase —
       * three on the beat and one held — so the cut has a shape rather than
       * a metronome. The median stays at `hold`, which is the number the
       * cadence words promise. */
      var PHRASE = [1, 1, 1, 1.5];
      var shots = [];
      var at = 0;
      var i = 0;
      while (at < total && shots.length < 400) {
        var which = i % sources.length;
        var source = sources[which];
        var pick = rng(i + 1);
        var dur = Math.min(hold * PHRASE[i % PHRASE.length], total - at);
        if (dur < 0.05) { break; }
        // A still needs somewhere to go, so it gets more push than a clip
        // that is already moving.
        var zoom = 1 + pick() * (source.kind === "image" ? 0.22 : 0.14);
        var drift = source.kind === "image" ? 0.06 + pick() * 0.08 : 0.02 + pick() * 0.04;
        shots.push({
          source: source,
          start: at,
          dur: dur,
          zoom: zoom,
          drift: (pick() < 0.5 ? -1 : 1) * drift,
          dx: pick() * 1.6 - 0.8,
          dy: pick() * 1.2 - 0.6
        });
        at += dur;
        i += 1;
      }
      var runFor = shots.length ? shots[shots.length - 1].start + shots[shots.length - 1].dur : total;

      /* Titles, spread across the film rather than stacked at the front. */
      plan.titles.forEach(function (text, n) {
        var slot = Math.floor(
          (shots.length / (plan.titles.length + 1)) * (n + 1)
        );
        var shot = shots[Math.min(shots.length - 1, slot)];
        if (shot) { shot.title = text; }
      });

      onProgress(0.14, "Planning the edit — " + shots.length + " shots, "
        + plan.cadence + ", " + plan.lookName);

      /* Hand the recorder finished frames instead of letting it help itself.
       *
       * `captureStream(30)` samples the canvas on its own clock, and a draw
       * that takes 10ms gets sampled halfway through: the film came out with
       * frames that were the top of one shot above the bottom of the next,
       * torn along a horizontal line. `captureStream(0)` emits nothing until
       * `requestFrame()` is called, which the paint loop does once the frame
       * is complete. Where that is not supported, fall back to the timed
       * capture — a torn frame now and then beats no film. */
      var stream = canvas.captureStream(0);
      var track = stream.getVideoTracks()[0];
      var byHand = !!(track && typeof track.requestFrame === "function");
      if (!byHand) { stream = canvas.captureStream(30); }

      var music = attachMusic(files, stream);
      var recorder = pickRecorder(stream);
      var chunks = [];
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) { chunks.push(e.data); } };

      return new Promise(function (resolve, reject) {
        recorder.onerror = function (e) { reject(e.error || new Error("recording failed")); };

        var began = 0;
        recorder.onstop = function () {
          var type = recorder.mimeType || "video/webm";
          var blob = new Blob(chunks, { type: type });
          var ran = (performance.now() - began) / 1000;
          if (music) { try { music.pause(); } catch (e) {} }
          resolve({
            url: URL.createObjectURL(blob),
            type: type,
            bytes: blob.size,
            shots: shots.length,
            clips: videos.length,
            stills: stills,
            skipped: skipped,
            music: !!music,
            width: W,
            height: H,
            // What it actually is, measured, not what was asked for.
            seconds: ran,
            shot_seconds: ran / Math.max(shots.length, 1),
            reading: {
              look: plan.lookName,
              cadence: plan.cadence,
              seconds: total,
              titles: plan.titles.slice()
            }
          });
        };

        /* Every video plays through once, and the loop chooses which one is
         * drawn. Seeking per shot was the first version and it does not work
         * at this speed: a seek plus a play() costs more wall time than a
         * 0.167s shot lasts, so a six second hypercut came out eighteen
         * seconds long with every shot three times its length. The recorder
         * captures wall time, so the cut has to happen in wall time. */
        videos.forEach(function (source, n) {
          var span = Math.max(0.1, (source.duration || 1) - 0.2);
          try { source.el.currentTime = (n * span / Math.max(videos.length, 1)) % span; } catch (e) {}
          source.el.loop = true;
        });

        Promise.all(videos.map(function (source) {
          return source.el.play().catch(function () { return null; });
        })).then(function () {
          if (music) { music.play().catch(function () {}); }
          began = performance.now();
          recorder.start();

          var cursor = 0;
          var sent = -1;
          function paint() {
            var elapsed = (performance.now() - began) / 1000;
            if (elapsed >= runFor) {
              recorder.stop();
              videos.forEach(function (s) { try { s.el.pause(); } catch (e) {} });
              return;
            }
            while (cursor < shots.length - 1
                   && elapsed >= shots[cursor].start + shots[cursor].dur) {
              cursor += 1;
            }
            var shot = shots[cursor];
            var into = Math.min(1, Math.max(0, (elapsed - shot.start) / shot.dur));

            // A push through the shot, and a small pop out of the cut so the
            // edit is felt as well as seen.
            var pop = 1 + 0.045 * Math.max(0, 1 - into * 6);
            var zoom = shot.zoom * (1 + shot.drift * into) * pop;

            // A photograph arrived already graded; only live video pays here.
            ctx.filter = shot.source.graded ? "none" : filter;
            try {
              drawFramed(ctx, shot.source, W, H, zoom, shot.dx, shot.dy);
            } catch (e) { /* not ready yet */ }
            ctx.filter = "none";
            ctx.drawImage(vignette, 0, 0);

            if (shot.title) {
              // In over four frames, out over the last four, so it lands.
              var fade = Math.min(1, into * 6, (1 - into) * 6);
              drawTitle(ctx, shot.title, W, H, Math.max(0, fade));
            }

            /* The frame is finished. Only now does the recorder get to see
             * it, and no faster than 30 a second: handing the encoder frames
             * at the full animation rate leaves it behind, and a frame it
             * only half finished arrives in the file torn across the middle,
             * the top of one shot above the bottom of the next. */
            if (byHand && elapsed - sent >= 1 / 31) {
              sent = elapsed;
              track.requestFrame();
            }

            if (cursor !== paint.said) {
              paint.said = cursor;
              onProgress(0.14 + 0.76 * (elapsed / runFor),
                         "Rendering — shot " + (cursor + 1) + " of " + shots.length);
            }
            requestAnimationFrame(paint);
          }
          requestAnimationFrame(paint);
        });
      });
    });
  }

  global.auteurCut = {
    cut: cut,
    read: read,
    shotSecondsFor: shotSecondsFor,
    lookFor: lookFor,
    titlesFrom: titlesFrom
  };
})(window);
