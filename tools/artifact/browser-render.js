/* Cutting a film in the browser.
 *
 * The real renderer is ffmpeg driven from Python, and a published page has
 * neither. But a browser can already decode video, draw it, and encode it:
 * <video> into a <canvas>, canvas.captureStream() into a MediaRecorder, out
 * comes a file that plays. So the link cuts a real film from your own clips
 * rather than describing one.
 *
 * What it is not: this cuts to a cadence, frames each shot, applies a grade
 * and sets your quoted words on screen. It does not detect beats or run the
 * crew — those need the measurements only the Python side makes. It is the
 * shape of the thing, performed rather than played back.
 *
 * The vocabulary and the choosing live next door, in `cutting.js` and
 * `style.js`. This file is the machinery: it loads the files, works out what
 * each shot is, and paints. What a portal *is* and whether this film should
 * use one are questions asked elsewhere, because the version of this file
 * that answered both itself answered them the same way every time.
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
  /* `ink` is what the graphics are drawn in. A grade and the marks over it
   * have to belong to each other: an orange ring on a black and white film is
   * somebody else's sticker, and a grey one on neon disappears. */
  var LOOKS = {
    noir:      { label: "black and white", ink: "#ffffff", filter: "grayscale(1) contrast(1.35) brightness(0.97)", vignette: 0.55 },
    neon:      { label: "neon",            ink: "#7ef0ff", filter: "saturate(1.5) contrast(1.2) hue-rotate(-10deg) brightness(0.96)", vignette: 0.5 },
    warm:      { label: "warm",            ink: "#ffe6bd", filter: "saturate(1.15) contrast(1.08) sepia(0.2) brightness(1.04)", vignette: 0.3 },
    cool:      { label: "cool",            ink: "#dff1ff", filter: "saturate(1.05) contrast(1.12) hue-rotate(8deg) brightness(0.99)", vignette: 0.35 },
    cinematic: { label: "cinematic",       ink: "#f5efe4", filter: "saturate(0.94) contrast(1.24) brightness(0.94)", vignette: 0.45 },
    punchy:    { label: "punchy",          ink: "#ffd45c", filter: "saturate(1.32) contrast(1.28) brightness(1.02)", vignette: 0.28 },
    faded:     { label: "faded film",      ink: "#fff4e0", filter: "saturate(0.82) contrast(0.92) sepia(0.28) brightness(1.08)", vignette: 0.22 },
    house:     { label: "house grade",     ink: "#f2ede4", filter: "saturate(1.12) contrast(1.1) brightness(1.01)", vignette: 0.3 }
  };

  /* And what each look actually does to a photograph.
   *
   * The `filter` strings above are what a *clip* gets, because a clip pays
   * per frame and `filter` is the only thing cheap enough. A photograph is
   * graded once, so it gets one of these instead: real tone curves, real
   * split toning, real grain. The two are not equivalent and pretending they
   * were is how this shipped a look that moved the picture by 6.6 parts in
   * 255 — measured, against the ungraded photograph, on the fallback that
   * most prompts land on. A person looked at the result and said the photo
   * never changed, and they were reading the numbers correctly by eye.
   *
   * The vignette stays on the look above rather than in the recipe: the
   * renderer draws it as a separate layer over the whole frame, so a still
   * that baked its own would get it twice.
   */
  var LOOK_GRADES = {
    noir: {
      gamma: [0.96, 0.96, 0.96], lift: [0.020, 0.020, 0.024], gain: [1.0, 1.0, 0.99],
      contrast: 1.42, saturation: 0.0,
      splitShadow: [-0.006, 0.0, 0.014], splitHigh: [0.014, 0.010, 0.0],
      halation: 0.20, halationTint: [255, 255, 255], grain: 0.16, grainSize: 2
    },
    neon: {
      gamma: [1.0, 1.02, 0.94], lift: [0.030, 0.026, 0.062], gain: [1.02, 0.98, 1.10],
      contrast: 1.26, saturation: 1.55,
      splitShadow: [0.010, -0.014, 0.060], splitHigh: [0.030, -0.006, 0.044],
      halation: 0.58, halationTint: [126, 240, 255], grain: 0.05, grainSize: 1
    },
    warm: {
      gamma: [0.95, 1.0, 1.07], lift: [0.052, 0.040, 0.026], gain: [1.05, 1.0, 0.92],
      contrast: 1.10, saturation: 1.16,
      splitShadow: [0.020, 0.008, -0.004], splitHigh: [0.060, 0.030, -0.026],
      halation: 0.36, halationTint: [255, 186, 120], grain: 0.08, grainSize: 2
    },
    cool: {
      gamma: [1.05, 1.0, 0.94], lift: [0.018, 0.030, 0.056], gain: [0.95, 0.99, 1.07],
      contrast: 1.20, saturation: 1.04,
      splitShadow: [-0.024, 0.004, 0.048], splitHigh: [-0.006, 0.014, 0.034],
      halation: 0.18, halationTint: [200, 226, 255], grain: 0.05, grainSize: 1
    },
    cinematic: {
      // Teal shadows, warm skin, and a real roll-off at both ends. The look
      // everybody means by "cinematic", done the way it is actually done.
      gamma: [1.0, 1.0, 1.0], lift: [0.026, 0.036, 0.052], gain: [1.02, 0.99, 0.96],
      contrast: 1.34, saturation: 0.98,
      splitShadow: [-0.034, 0.008, 0.046], splitHigh: [0.052, 0.026, -0.024],
      halation: 0.34, halationTint: [255, 176, 128], grain: 0.09, grainSize: 2
    },
    punchy: {
      gamma: [0.97, 0.97, 0.97], lift: [0.008, 0.008, 0.012], gain: [1.09, 1.07, 1.05],
      contrast: 1.44, saturation: 1.40,
      splitShadow: [-0.010, 0.0, 0.020], splitHigh: [0.034, 0.020, -0.006],
      halation: 0.30, halationTint: [255, 220, 160], grain: 0.04, grainSize: 1
    },
    faded: {
      gamma: [0.98, 1.0, 1.04], lift: [0.105, 0.098, 0.086], gain: [0.98, 0.96, 0.92],
      contrast: 0.86, saturation: 0.80,
      splitShadow: [0.020, 0.014, 0.004], splitHigh: [0.048, 0.030, -0.010],
      halation: 0.44, halationTint: [255, 200, 150], grain: 0.26, grainSize: 2
    },
    house: {
      /* The fallback, and therefore the most important one in the file: it is
       * what a prompt gets when it matches nothing, which is most prompts. It
       * has to be a grade somebody would notice while still being the one
       * that suits any footage — so, a gentle S-curve, slightly lifted blacks
       * and a warm-highlight/cool-shadow split, which is what a colourist
       * does to a shot before they know what it is. */
      gamma: [0.99, 1.0, 1.01], lift: [0.034, 0.038, 0.048], gain: [1.04, 1.02, 0.99],
      contrast: 1.24, saturation: 1.14,
      splitShadow: [-0.020, 0.004, 0.032], splitHigh: [0.044, 0.024, -0.014],
      halation: 0.28, halationTint: [255, 198, 148], grain: 0.07, grainSize: 2
    }
  };


  /* A word list has to match words.
   *
   * Every vocabulary below is an alternation of stems, and without a leading
   * boundary each of them also matches inside longer words — silently, and
   * with no way to tell from the outside. Measured examples, all real: `rave`
   * matched *travel*, so every prompt mentioning travel was cut as a rave;
   * `run` matched *brunch*; `fall` matched *waterfall*; `ice` matched *nice*;
   * `rain` matched *training*; `home` matched *chrome*. A person typing "our
   * travel film" got a hypercut and no explanation, which is the "it ignored
   * my prompt" complaint arriving from a direction nobody would look in.
   *
   * A *leading* boundary only. Every entry here is a word-initial stem —
   * `nostalg`, `memor`, `energet` — and are meant to match longer words that
   * begin with them, so a trailing boundary would break them. */
  function wordy(pattern) {
    return new RegExp("\\b(?:" + pattern.source + ")", pattern.flags || "");
  }

  function vocabulary(entries) {
    return entries.map(function (entry) {
      return [entry[0], wordy(entry[1])];
    });
  }

  /* A prompt is somebody describing a feeling — "my trip to the coast", "gym
   * stuff", "the wedding" — and none of those used to match anything at all.
   * Each look answers to the words people actually reach for, and anything
   * unmatched still gets the house grade. */
  var LOOK_WORDS = vocabulary([
    ["noir", /black\s*(and|&)\s*white|b\s*&\s*w|monochrome|greyscale|grayscale|noir|gritty|stark|documentary|street/],
    ["neon", /neon|cyber|synth|vaporwave|night\s*(life|out|drive|s)?|club|rave|party|city|electric|glow|miami|arcade|techno|edm/],
    ["warm", /warm|summer|golden|sunset|sunrise|nostalg|memor|cozy|cosy|home|family|holiday|vacation|beach|honey|autumn|fall\b|love|wedding|birthday|friends/],
    ["cool", /cool|cold|blue|winter|snow|ice|steel|clean|minimal|ocean|sea|rain|storm|moody\s*blue|calm/],
    ["cinematic", /cinematic|filmic|epic|movie|dramatic|teal|anamorphic|trailer|landscape|travel|mountain|widescreen|serious/],
    ["punchy", /punchy|hypercut|hard\s*to\s*the\s*beat|rapid|fast|energy|energet|hype|gym|workout|sport|training|run|skate|bold|aggressive|loud/],
    ["faded", /faded|vintage|retro|super\s*8|8mm|film\s*grain|old|analog|analogue|polaroid|70s|80s|90s|dreamy|soft/]
  ]);

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
    [0.167, "a hypercut — three cuts a beat", wordy(/hypercut|rapid\s*fire|flurry|machine\s*gun|frantic|chaotic|blitz/)],
    [0.35, "very fast", wordy(/very\s*fast|super\s*fast|breakneck|relentless|hype|edm|techno|rave/)],
    [0.5, "fast, hard cuts", wordy(/punchy|hard\s*to\s*the\s*beat|on\s*the\s*beat|fast|kinetic|snappy|quick|energet|gym|workout|sport|party|montage/)],
    [1.8, "slow and held", wordy(/slow|cinematic|calm|gentle|meditative|dreamy|relaxed|ambient|peaceful|soft|tender/)],
    [1.2, "unhurried", wordy(/steady|documentary|story|narrative|travel|landscape/)]
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
  function read(prompt, fallbackSeconds, wanted) {
    var look = lookFor(prompt);
    var cadence = cadenceFor(prompt);
    var style = global.auteurStyle.styleFor(prompt);
    /* A decade chosen on the form beats one inferred from the words. Somebody
       who picked "80s" and then wrote "sunny afternoon" has said which one
       they meant; reading the sentence back over the top of an explicit
       control is the same fault as the length control that used to silently
       beat the number typed in the prompt. */
    var era = wanted || global.auteurEra.eraFor(prompt);
    if (!global.auteurEra.about(era)) { era = null; }
    var lookKey = "house";
    for (var k in LOOKS) {
      if (LOOKS[k] === look) { lookKey = k; break; }
    }
    return {
      look: look,
      lookKey: lookKey,
      lookName: look.label,
      /* Which decade the pictures should look like they came from, or null
         for none. Separate from the look on purpose: "warm" is a colour
         decision and "1990s" is a decision about what recorded it, and a
         person can want either, both, or neither. */
      era: era,
      eraName: era ? global.auteurEra.about(era).label : "",
      eraNote: era ? global.auteurEra.about(era).note : "",
      hold: cadence.hold,
      cadence: cadence.label,
      seconds: secondsFrom(prompt, fallbackSeconds),
      titles: titlesFrom(prompt),
      // Which moves this film is allowed to make, and how often. Reported
      // back with everything else it heard, because a decision the person
      // cannot see it made is a decision they will assume it did not make.
      style: style,
      styleName: style.label,
      styleNote: style.note
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

  /* What the person chose on the animation tab, or a sensible default.
   *
   * Read from the same `auteur-overlays` key that page writes, so the two are
   * one setting rather than two that happen to agree. */
  function overlayChoice() {
    var chosen = { kinds: ["circle", "bracket", "burst"], move: "pop", density: "some" };
    try {
      var raw = JSON.parse(localStorage.getItem("auteur-overlays") || "null");
      if (raw && Array.isArray(raw.kinds)) {
        chosen.kinds = raw.kinds;
        chosen.move = raw.move || chosen.move;
        chosen.density = raw.density || chosen.density;
      }
    } catch (e) { /* private mode, or nobody has been to that page */ }
    return chosen;
  }

  /* Where the graphics land, shot by shot.
   *
   * Lanes rather than free positions: two shapes placed at random overlap
   * often enough to look like a mistake, and three lanes with a shot's own
   * jitter inside them never do. Layered up to three at once against the
   * accents, which is what the OverlayAgent does on the downbeats.
   */
  var LANES = [[0.30, 0.30], [0.70, 0.52], [0.46, 0.74]];

  function graphicsFor(chosen, shot, index, colour) {
    if (chosen.density === "off" || !chosen.kinds.length) { return []; }
    var many;
    if (chosen.density === "busy") { many = index % 2 === 0 ? 3 : 1; }
    else { many = index % 4 === 0 ? 2 : 0; }
    many = Math.min(many, chosen.kinds.length, LANES.length);

    var pick = rng(index + 977);
    var cues = [];
    for (var n = 0; n < many; n++) {
      var lane = LANES[n];
      cues.push({
        kind: chosen.kinds[(index + n) % chosen.kinds.length],
        move: chosen.move,
        anchor: [
          Math.min(0.88, Math.max(0.12, lane[0] + (pick() - 0.5) * 0.12)),
          Math.min(0.88, Math.max(0.12, lane[1] + (pick() - 0.5) * 0.12))
        ],
        size: 0.8 + pick() * 0.45,
        color: colour,
        opacity: 0.92
      });
    }
    return cues;
  }

  /* How much there is to look at in a source, and where.
   *
   * Both come off one pass over a 48px thumbnail in `cutting.js`. `strength`
   * decides which picture opens the film and which one it returns to — the
   * first frame is the whole hook, and picking it by upload order is picking
   * it at random. `focus` is what a portal or a carry is built around, so the
   * hole opens over the subject of the picture rather than over the middle of
   * the rectangle. */
  function readSource(source) { return global.auteurCutting.readSource(source); }

  /* One visiting order per movement, each a different tour of every source.
   *
   * Stepping by a number coprime with the count visits all of them before
   * repeating any, and a different step per movement gives a different order
   * each time — so the second minute of a film is not the first minute again.
   * Falls back to a rotation when the count has no other coprime, which is
   * only ever the case for one or two sources. */
  function visitOrders(count, movements, opener) {
    function coprime(a, b) {
      while (b) { var t = b; b = a % b; a = t; }
      return a === 1;
    }
    var steps = [];
    for (var s = 1; s < Math.max(count, 2); s++) {
      if (coprime(s, count)) { steps.push(s); }
    }
    if (!steps.length) { steps = [1]; }

    var orders = [];
    for (var m = 0; m < movements; m++) {
      var step = steps[m % steps.length];
      // Start each movement somewhere new, and never on the opening image —
      // the hook is spent, and coming straight back to it reads as a stutter.
      var start = (opener + 1 + m) % Math.max(count, 1);
      var tour = [];
      for (var n = 0; n < count; n++) { tour.push((start + n * step) % count); }
      orders.push(tour);
    }
    return orders;
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
  function gradeStill(source, plan) {
    var s = sizeOf(source);
    var scale = Math.min(1, 2200 / Math.max(s[0], s[1]));
    var c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(s[0] * scale));
    c.height = Math.max(1, Math.round(s[1] * scale));
    var g = c.getContext("2d", { alpha: false });
    try {
      g.drawImage(source.el, 0, 0, c.width, c.height);
    } catch (e) {
      return source;
    }
    /* Once, properly, rather than thirty times a second, approximately.
     *
     * This used to set `ctx.filter` and draw, which is all a clip can afford
     * and is nowhere near a grade: the house look, applied that way, moved
     * the picture by 6.6 parts in 255. A photograph does not change, so it
     * can have the real thing — tone curves, split toning, halation, grain —
     * for one pass at load time. The era, if one was asked for, goes on top:
     * the look decides the colour, the era decides what the colour was
     * recorded on. */
    try {
      global.auteurEra.apply(c, LOOK_GRADES[plan.lookKey] || LOOK_GRADES.house);
      if (plan.era) { global.auteurEra.grade(c, plan.era); }
    } catch (e) { /* a browser without getImageData here: keep the picture */ }
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
  function drawFramed(ctx, source, W, H, zoom, dx, dy, offX, offY) {
    var s = sizeOf(source);
    var scale = Math.max(W / s[0], H / s[1]) * zoom;
    var w = s[0] * scale, h = s[1] * scale;
    var x = (W - w) / 2 + dx * Math.max(0, (w - W) / 2);
    var y = (H - h) / 2 + dy * Math.max(0, (h - H) / 2);
    // `offX`/`offY` slide the whole frame in pixels rather than within the
    // crop, which is what a push needs: the picture has to leave the frame,
    // and `dx` can only ever move it inside what the crop already threw away.
    ctx.drawImage(source.el, x + (offX || 0), y + (offY || 0), w, h);
  }

  /* Where a source-space point ends up on screen at a given framing. Used to
   * aim a portal at the subject of the shot it is opening out of. */
  function framedFocus(source, W, H, zoom, dx, dy, focus) {
    return global.auteurCutting.focusInFrame(sizeOf(source), W, H, zoom, dx, dy, focus);
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

  /* What the edit is made of, counted off the shot list.
   *
   * Exists so "is this film varied or is it the same move forty times" can be
   * answered by a check rather than by watching it. `longestRun` is the one
   * that matters most: a film can use six transitions and still read as
   * mechanical if it uses them in a fixed rotation, and a run of the same
   * loud move is worse than never making it — it stops reading as a choice
   * and starts reading as a tic. */
  function tally(shots) {
    var transitions = {}, gestures = {}, accents = 0, carrying = 0;
    var run = 1, longestRun = 1, loudRun = 0, longestLoud = 0, held = 0;
    for (var i = 0; i < shots.length; i++) {
      var s = shots[i];
      transitions[s.transition] = (transitions[s.transition] || 0) + 1;
      gestures[s.gesture] = (gestures[s.gesture] || 0) + 1;
      if (s.accent) { accents += 1; }
      if (global.auteurCutting.carries(s.transition)) { carrying += 1; }
      if (s.gesture === "hold") { held += 1; }
      if (i && shots[i - 1].transition === s.transition) {
        run += 1;
        if (run > longestRun) { longestRun = run; }
        /* Runs of hard cuts are counted separately and are not a fault — the
         * measured references hard-cut most of their joins, and a stretch of
         * them is what leaves room for a portal to mean something. A run of
         * the same *loud* move is the actual fault: it stops reading as a
         * choice and starts reading as a tic. Counting both together, which
         * the first version of this did, called a healthy hypercut broken. */
        if (s.transition !== "cut") {
          loudRun = loudRun ? loudRun + 1 : 2;
          if (loudRun > longestLoud) { longestLoud = loudRun; }
        }
      } else {
        run = 1;
        loudRun = 0;
      }
    }
    return {
      transitions: transitions,
      gestures: gestures,
      accents: accents,
      // How many joins leave part of the outgoing picture on the incoming
      // one — the portals, carries, whips, slices and luma dissolves.
      carrying: carrying,
      held: held,
      kinds: Object.keys(transitions).length,
      moves: Object.keys(gestures).length,
      longestRun: longestRun,
      longestLoudRun: longestLoud
    };
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
    var plan = read(prompt, options.seconds || 10, options.era);
    var hold = plan.hold;
    var total = plan.seconds;
    /* What a *clip* gets, per frame, and it is less than a photograph gets.
       A photograph is graded once with real curves; a clip pays thirty times
       a second, so it gets the filter-string approximation of the same look
       and of the era on top of it. Worth naming rather than hiding: a film
       cut from clips reads as the era in colour but not in texture. */
    var filter = plan.look.filter;
    if (plan.era) {
      filter = filter + " " + global.auteurEra.filterFor(plan.era);
    }

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
        return s.kind === "image" ? gradeStill(s, plan) : s;
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

      /* The shot list.
       *
       * Built in three parts, the shape the reference reels use:
       *
       *   the hook   the strongest frame you gave it, held widest, first
       *   movements  each one visits every source in a different order and at
       *              a tighter framing than the last, so the film closes in
       *              as it runs
       *   the return the last shot goes back to the hook's source and its
       *              framing, so the final frame matches the first and the
       *              whole thing loops without a seam
       *
       * What each shot then *does* is a matter of taste, and taste lives in
       * `style.js`: the rhythm comes from that style's bars rather than from
       * one fixed four-beat phrase, each shot gets a role in the film, and
       * the role decides how the shot moves and how it arrives. The previous
       * version gave every shot the same phrase position, the same eased
       * nothing and the same hard cut, which is a film with no decisions in
       * it however varied the pictures are. */
      var style = plan.style;
      var overlayPlan = overlayChoice();
      overlayPlan.progress = total >= 8
        && overlayPlan.density !== "off"
        && overlayPlan.kinds.indexOf("progress") !== -1;

      // One pass per source for strength and subject position. Both are used
      // below; reading the thumbnail twice for two numbers off the same pass
      // is the sort of thing that shows up as a stutter on a slow phone.
      var reads = sources.map(readSource);
      var opener = 0;
      for (var n = 1; n < reads.length; n++) {
        if (reads[n].strength > reads[opener].strength) { opener = n; }
      }

      // How many movements the film can carry. Six seconds is two; thirty is
      // five. Fewer than two and there is no development to speak of.
      var movements = Math.max(2, Math.min(5, Math.round(total / 6)));
      var visits = visitOrders(sources.length, movements, opener);

      /* The rhythm, in beats, before anything is a shot. Asked for with
       * enough margin that the film runs out of time before it runs out of
       * bars — cycling the bar list would put the same phrase back at the end
       * of a long film, which is the repetition this replaced. */
      var roll = rng(Math.round(total * 97) + sources.length * 31 + plan.titles.length);
      var wanted = Math.max(4, Math.ceil((total / hold) * 1.6));
      var bars = global.auteurStyle.arrange(style, movements, wanted, roll);

      var shots = [];
      var at = 0;
      var i = 0;
      var hookFrame = null;
      var before = {};
      while (at < total && i < bars.length && shots.length < 400) {
        var bar = bars[i];
        var dur = Math.min(hold * bar.beats, total - at);
        if (dur < 0.05) { break; }

        // Where in the film this shot falls, 0 at the first frame and 1 at
        // the last. Everything about the framing is a function of this.
        var through = total > 0 ? at / total : 0;
        var movement = Math.min(movements - 1, Math.floor(through * movements));
        var order = visits[movement];
        var which = i === 0 ? opener : order[i % order.length];
        var source = sources[which];
        var pick = rng(i + 1);

        var chose = global.auteurStyle.choices(style, bar.role, roll, before);
        before = chose;

        // Wide at the top, tight at the end. A still needs somewhere to go,
        // so it gets more push than a clip that is already moving.
        var room = source.kind === "image" ? 0.26 : 0.16;
        var zoom = 1 + room * (0.12 + 0.78 * through) * (0.7 + 0.6 * pick());
        var frame = {
          source: source,
          reading: reads[which],
          start: at,
          dur: dur,
          role: bar.role,
          zoom: zoom,
          dx: pick() * 1.6 - 0.8,
          dy: pick() * 1.2 - 0.6,
          gesture: chose.gesture,
          transition: i === 0 ? "cut" : chose.transition,
          accent: chose.accent,
          // How far this shot's gesture is allowed to travel, and how hard it
          // pushes. Per style, so a gallery film's "press" is not a hype
          // film's "press" — the same named move at two different volumes.
          arc: {
            push: style.push * (0.6 + 0.8 * pick()),
            travel: style.travel * (pick() < 0.5 ? -1 : 1) * (0.6 + 0.8 * pick()),
            way: pick() < 0.5 ? -1 : 1,
            invert: chose.invert,
            ink: plan.look.ink
          }
        };
        frame.transitionFor = global.auteurCutting.transitionSeconds(frame.transition, dur);
        frame.graphics = graphicsFor(overlayPlan, frame, i, plan.look.ink);
        if (i === 0) { hookFrame = frame; }
        shots.push(frame);
        at += dur;
        i += 1;
      }

      /* Come back to where it started. The last shot is the opening image at
       * the opening framing, so the final frame and the first frame are the
       * same picture and the reel loops into itself rather than jumping. It
       * arrives on a match, which is the transition that makes a join
       * disappear — a portal onto the frame the film opened with would
       * announce the loop rather than hide it. */
      if (hookFrame && shots.length > 3) {
        var last = shots[shots.length - 1];
        last.source = hookFrame.source;
        last.reading = hookFrame.reading;
        last.zoom = hookFrame.zoom;
        last.dx = hookFrame.dx;
        last.dy = hookFrame.dy;
        last.role = "close";
        last.gesture = "hold";
        last.transition = "match";
        last.accent = null;
        last.transitionFor = global.auteurCutting.transitionSeconds("match", last.dur);
        last.closes = true;
      }

      /* Second pass: what each transition needs to know about the shot it is
       * coming *out* of.
       *
       * A portal opens where the outgoing picture's subject was, which is not
       * where that subject sits in the photograph — the shot is cropped to
       * 9:16 and pushed in, and the subject moves with the crop. A match cut
       * needs the outgoing framing expressed relative to the incoming one, so
       * the incoming shot can start there and ease away. Neither can be known
       * until the shot before is finished, which is why this is its own pass
       * rather than a line in the loop above. */
      for (var k = 1; k < shots.length; k++) {
        var shot = shots[k], prev = shots[k - 1];
        var rest = (global.auteurCutting.GESTURES[prev.gesture]
          || global.auteurCutting.GESTURES.hold).at(1, prev.arc);
        var endZoom = prev.zoom * rest.zoom;
        var endDx = prev.dx + rest.dx, endDy = prev.dy + rest.dy;

        shot.arc.focus = framedFocus(
          prev.source, W, H, endZoom, endDx, endDy, prev.reading.focus
        );
        shot.arc.radius = prev.reading.radius;
        shot.arc.from = {
          zoom: endZoom / Math.max(shot.zoom, 0.0001),
          dx: endDx - shot.dx,
          dy: endDy - shot.dy
        };
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
            movements: movements,
            loops: !!(shots.length && shots[shots.length - 1].closes),
            graphics: shots.reduce(function (sum, s) { return sum + s.graphics.length; }, 0),
            // What it actually did, tallied off the shot list rather than
            // described. A count of portals is the only way to answer "did
            // it use any" without watching thirty seconds of film, and it is
            // what the checks assert against.
            edit: tally(shots),
            reading: {
              look: plan.lookName,
              cadence: plan.cadence,
              seconds: total,
              titles: plan.titles.slice(),
              style: plan.styleName,
              styleNote: plan.styleNote,
              // Carried through to the page. It was applied and not reported,
              // which is the same class of fault as a grade too faint to see:
              // the film changed and nothing told the person why.
              era: plan.era,
              eraName: plan.eraName
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

          /* The outgoing frame, frozen at the moment of the cut.
           *
           * Every transition that leaves part of one picture on top of the
           * next needs the picture that was just there. Re-rendering the
           * outgoing shot for the two to five frames a transition lasts would
           * double the cost of the most expensive frames in the film, on the
           * device least able to afford it. Copying the canvas costs one
           * draw *per cut* and is what was actually on screen — including its
           * vignette, its graphics and its title, which a re-render would
           * have had to reproduce and would have got subtly wrong. */
          var held = document.createElement("canvas");
          held.width = W; held.height = H;
          var heldCtx = held.getContext("2d", { alpha: false });
          var haveHeld = false;

          var cursor = 0;
          var sent = -1;

          function paint() {
            var elapsed = (performance.now() - began) / 1000;
            if (elapsed >= runFor) {
              recorder.stop();
              videos.forEach(function (s) { try { s.el.pause(); } catch (e) {} });
              return;
            }
            var was = cursor;
            while (cursor < shots.length - 1
                   && elapsed >= shots[cursor].start + shots[cursor].dur) {
              cursor += 1;
            }
            if (cursor !== was) {
              // Freeze what is on screen before anything overwrites it. This
              // is the last frame of the shot that just ended.
              try { heldCtx.drawImage(canvas, 0, 0); haveHeld = true; }
              catch (e) { haveHeld = false; }
            }

            var shot = shots[cursor];
            var into = Math.min(1, Math.max(0, (elapsed - shot.start) / shot.dur));

            /* How the shot moves. `hold` genuinely does nothing, which is the
             * entry that did not exist: every shot used to get the same
             * linear zoom plus the same small pop, so the film had permanent
             * low-grade motion and no cut ever got to land. */
            var move = (global.auteurCutting.GESTURES[shot.gesture]
              || global.auteurCutting.GESTURES.hold).at(into, shot.arc);
            var zoom = shot.zoom * move.zoom;
            var dx = shot.dx + move.dx;
            var dy = shot.dy + move.dy;
            var offX = 0, offY = 0;

            /* How it arrives. `during` is false for most frames of most
             * shots — a transition is a handful of frames at the head of the
             * shot and nothing after that, and `cut` never sets it at all. */
            var kind = shot.transition;
            var span = shot.transitionFor;
            var tt = span > 0 ? (elapsed - shot.start) / span : 1;
            var move2 = global.auteurCutting.TRANSITIONS[kind];
            var during = haveHeld && !!move2 && span > 0 && tt < 1;

            if (during && move2.enter) {
              var arriving = move2.enter(tt, shot.arc);
              zoom *= arriving.zoom;
              dx += arriving.dx;
              dy += arriving.dy;
              if (arriving.slide) {
                offX = arriving.slide[0] * W;
                offY = arriving.slide[1] * H;
              }
            }

            /* A photograph arrived already graded; only live video pays the
             * per-frame filter. An accent is a frame or two that deliberately
             * does not match the film's grade — the blown-out or inverted
             * frame the references put on their hardest cuts — so it is paid
             * for over the first fraction of the shot and nowhere else. */
            var base = shot.source.graded ? "none" : filter;
            if (shot.accent && elapsed - shot.start < Math.min(0.12, shot.dur * 0.5)) {
              var extra = global.auteurStyle.ACCENTS[shot.accent];
              base = base === "none" ? extra.filter : base + " " + extra.filter;
            }
            ctx.filter = base;
            try {
              drawFramed(ctx, shot.source, W, H, zoom, dx, dy, offX, offY);
            } catch (e) { /* not ready yet */ }
            ctx.filter = "none";
            ctx.drawImage(vignette, 0, 0);

            // The graphics, on the cut. Drawn by the same module the
            // animation tab previews with, so what is chosen there is what
            // lands here.
            for (var layer = 0; layer < shot.graphics.length; layer++) {
              var cue = shot.graphics[layer];
              try {
                global.auteurOverlays.draw(ctx, cue, W, H, into);
              } catch (e) { /* a shape this build does not have */ }
            }
            if (overlayPlan.progress) {
              try {
                global.auteurOverlays.draw(ctx, {
                  kind: "progress", move: "none", anchor: [0.5, 0.955],
                  size: 0.6, color: "#e9a85c", opacity: 0.85
                }, W, H, elapsed / runFor);
              } catch (e) { /* likewise */ }
            }

            if (shot.title) {
              // In over four frames, out over the last four, so it lands.
              var fade = Math.min(1, into * 6, (1 - into) * 6);
              drawTitle(ctx, shot.title, W, H, Math.max(0, fade));
            }

            /* And last, what is left of the outgoing picture, on top of a
             * finished frame. Last rather than first because that is what the
             * transition means: the old picture is still there — with its own
             * vignette, its own graphics and its own grade, because it is
             * literally the frame that was on screen — and the new one is
             * arriving underneath it. Drawing it before the new shot would
             * simply paint over it. */
            if (during && move2.over) {
              try {
                move2.over(ctx, held, W, H, tt, shot.arc);
              } catch (e) { /* a transition this build does not have */ }
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
    // Exposed so the grade can be measured against the ungraded photograph
    // rather than judged by reading the filter strings, which is how a look
    // that moves the picture by 3 parts in 255 survived being called a grade.
    looks: function () { return LOOKS; },
    lookGrades: function () { return LOOK_GRADES; },
    titlesFrom: titlesFrom
  };
})(window);
