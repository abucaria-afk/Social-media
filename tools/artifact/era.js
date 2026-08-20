/* Grading a picture like it came from a particular decade.
 *
 * This exists because the grades this program had were CSS filter strings,
 * and a filter string is easy to write, impossible to judge by reading, and
 * capable of almost nothing. Measured against the ungraded photograph, the
 * house look — the one every prompt that matches no keyword falls through to,
 * which is most prompts — moved the picture by 6.6 parts in 255. Below about
 * 8 that is invisible against JPEG noise on a phone. So the complaint that
 * "the actual photo never changed" was not an impression. It was correct, and
 * it was correct for the majority of prompts.
 *
 * `filter` cannot fix that, because the things that actually make a picture
 * look like a year are not in its vocabulary:
 *
 *   tone curves          film does not scale brightness, it rolls off
 *   split toning         cold shadows and warm highlights, separately
 *   channel crosstalk    the reason cross-processed film goes cyan-green
 *   halation             light spilling out of highlights, tinted red-orange
 *   grain                structure, not noise, and it sits in the midtones
 *   chroma bleed         colour lagging behind luma, which is what VHS is
 *   scanlines            interlaced video, which is what 80s footage is
 *
 * All of it is per-pixel work and none of it is affordable thirty times a
 * second. It does not have to be: a photograph does not change, so it is
 * graded once when it loads and drawn from the result. That is the same
 * observation that made `gradeStill` possible, taken as far as it goes — at
 * one pass per photograph there is no reason to be cheap about it.
 *
 * Video is the exception and is treated as one, honestly: a clip pays per
 * frame, so it gets the tone curve as a filter string and the cheap overlays,
 * and does not get grain or halation. That is stated where it happens rather
 * than quietly delivering a different look for clips than for photographs.
 */
(function (global) {
  "use strict";

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  /* An era, as numbers.
   *
   * `lift` and `gain` are where black and white land, per channel — a 90s
   * print has blacks that never reach zero and that is most of why it reads
   * as a print. `splitShadow` and `splitHigh` are tints applied by luminance,
   * which is the difference between "blue" and "blue shadows with warm
   * skin". Everything is 0..1 and gets scaled where it is used.
   */
  var ERAS = {

    seventies: {
      label: "1970s",
      note: "Super 8 — orange, heavy grain, soft blacks, and it breathes",
      years: "1965–1979",
      gamma: [0.94, 1.0, 1.08],
      lift: [0.085, 0.055, 0.030],
      gain: [1.0, 0.965, 0.885],
      contrast: 0.82,
      saturation: 0.92,
      splitShadow: [0.030, 0.010, -0.010],
      splitHigh: [0.055, 0.022, -0.030],
      halation: 0.55, halationTint: [255, 156, 92],
      grain: 0.30, grainSize: 2,
      chroma: 0, scan: 0,
      vignette: 0.46,
      weave: 0.0018
    },

    eighties: {
      label: "1980s",
      note: "VHS — chroma bleeding sideways, scanlines, and blown highlights",
      years: "1980–1991",
      gamma: [1.0, 1.02, 0.96],
      lift: [0.055, 0.045, 0.078],
      /* Below 1. VHS blows its highlights, but it blows them by clipping a
       * signal that was already dim — not by being brighter overall, which
       * is what a gain above 1 does. Stacked on the S-curve and the halation
       * it made every frame white. */
      gain: [0.96, 0.92, 0.97],
      contrast: 1.14,
      saturation: 1.26,
      splitShadow: [0.006, -0.008, 0.038],
      splitHigh: [0.020, 0.014, -0.004],
      halation: 0.30, halationTint: [255, 214, 168],
      grain: 0.13, grainSize: 1,
      // The one thing that says VHS more than anything else: colour drawn a
      // couple of pixels to the right of the thing it belongs to.
      chroma: 2.6,
      scan: 0.22,
      vignette: 0.34,
      weave: 0.0
    },

    nineties: {
      label: "1990s",
      note: "Kodak Gold — golden, milky blacks, grain you can see",
      years: "1990–2001",
      gamma: [0.96, 1.0, 1.06],
      lift: [0.070, 0.062, 0.048],
      gain: [1.02, 0.99, 0.925],
      contrast: 0.90,
      saturation: 1.05,
      splitShadow: [0.008, 0.018, 0.010],
      splitHigh: [0.048, 0.028, -0.018],
      halation: 0.40, halationTint: [255, 178, 120],
      grain: 0.22, grainSize: 2,
      chroma: 0, scan: 0,
      vignette: 0.30,
      weave: 0.0008
    },

    y2k: {
      label: "2000s",
      note: "point-and-shoot flash — hard, cool, and clipped",
      years: "2000–2009",
      gamma: [1.06, 1.04, 1.0],
      lift: [0.012, 0.016, 0.030],
      // A flash lights the near thing and loses the far one. That is a
      // contrast story, not a brightness one.
      gain: [0.99, 0.98, 0.97],
      contrast: 1.24,
      saturation: 1.14,
      splitShadow: [-0.012, 0.004, 0.030],
      splitHigh: [0.020, 0.020, 0.012],
      halation: 0.18, halationTint: [220, 232, 255],
      grain: 0.07, grainSize: 1,
      chroma: 0.8, scan: 0,
      vignette: 0.20,
      weave: 0.0
    },

    tens: {
      label: "2010s",
      note: "the filter era — faded blacks, teal shadows, warm skin",
      years: "2010–2019",
      gamma: [1.0, 1.0, 1.0],
      lift: [0.090, 0.098, 0.104],
      gain: [0.985, 0.985, 0.965],
      contrast: 1.06,
      saturation: 0.94,
      splitShadow: [-0.030, 0.012, 0.046],
      splitHigh: [0.050, 0.026, -0.020],
      halation: 0.22, halationTint: [255, 196, 150],
      grain: 0.06, grainSize: 2,
      chroma: 0, scan: 0,
      vignette: 0.42,
      weave: 0.0
    },

    now: {
      label: "2020s",
      note: "phone HDR — everything visible, nothing hidden",
      years: "2020–",
      gamma: [0.98, 0.98, 0.98],
      lift: [0.0, 0.0, 0.004],
      gain: [1.04, 1.04, 1.05],
      contrast: 1.16,
      saturation: 1.16,
      splitShadow: [-0.008, 0.0, 0.016],
      splitHigh: [0.012, 0.010, 0.008],
      halation: 0.10, halationTint: [255, 236, 214],
      grain: 0.0, grainSize: 1,
      chroma: 0, scan: 0,
      vignette: 0.16,
      weave: 0.0
    }
  };

  var ORDER = ["seventies", "eighties", "nineties", "y2k", "tens", "now"];

  /* Words people type for a decade. `y2k`, `vhs` and `camcorder` matter more
   * than the digits do — nobody types "2000s", they type "y2k" or "old
   * digital camera". Boundary-matched, for the same reason every other word
   * list here is: without it `80s` matches inside `1980s` fine but `film`
   * matches inside `filmic`, and the failures are silent. */
  var WORDS = [
    ["seventies", /70s|1970s|seventies|super\s*8|8mm|s(?:uper)?8|grindhouse|kodachrome/],
    ["eighties", /80s|1980s|eighties|vhs|vcr|camcorder|betamax|synthwave|retrowave|tracking/],
    ["nineties", /90s|1990s|nineties|disposable|kodak\s*gold|point\s*and\s*shoot|film\s*camera|handycam/],
    ["y2k", /y2k|2000s|00s|noughties|early\s*digital|digicam|flash\s*photo|myspace/],
    ["tens", /2010s|10s|filter\s*era|tumblr|vsco|early\s*insta|indie\s*sleaze|faded/],
    ["now", /2020s|modern|today|hdr|iphone|current|clean\s*digital/]
  ].map(function (entry) {
    return [entry[0], new RegExp("\\b(?:" + entry[1].source + ")", "i")];
  });

  function eraFor(prompt) {
    var p = (prompt || "").toLowerCase();
    for (var i = 0; i < WORDS.length; i++) {
      if (WORDS[i][1].test(p)) { return WORDS[i][0]; }
    }
    return null;                       // no era asked for is a real answer
  }

  // ------------------------------------------------------------------- luts

  /* One 256-entry table per channel, built once per era.
   *
   * Everything that can be decided from a single channel value goes in here,
   * which turns the expensive part of the pixel loop into three array reads.
   * Saturation and split toning cannot — they need the whole pixel — so they
   * stay in the loop. */
  var CACHE = {};

  function tablesFor(era) {
    /* Keyed on the numbers that go into the table, not on the name.
     *
     * Keying on `label` worked for as long as every recipe was a named era.
     * The moment the base looks started coming through here — partial
     * recipes, most with no label at all — every one of them would have hit
     * the same empty-string key and been handed the first look's curve. A
     * cache that returns the wrong answer is worse than no cache, and this
     * one would have done it silently, in the direction of making everything
     * look identical: exactly the fault being repaired. */
    var key = [era.gamma, era.lift, era.gain, era.contrast].join("|");
    if (CACHE[key]) { return CACHE[key]; }
    var out = [new Uint8Array(256), new Uint8Array(256), new Uint8Array(256)];
    for (var c = 0; c < 3; c++) {
      for (var v = 0; v < 256; v++) {
        var x = v / 255;
        // Gamma first, so the curve below acts on a linearised-ish signal.
        x = Math.pow(x, era.gamma[c]);
        /* An S-curve rather than a multiply. `contrast(1.3)` scales around
         * the midpoint and clips both ends flat; film compresses toward the
         * ends and holds detail in them, which is the whole reason a print
         * looks different from a slider. */
        var k = era.contrast;
        if (k !== 1) {
          var s = 1 / (1 + Math.exp(-(x - 0.5) * 6 * k));
          var lo = 1 / (1 + Math.exp(0.5 * 6 * k));
          var hi = 1 / (1 + Math.exp(-0.5 * 6 * k));
          var curved = (s - lo) / (hi - lo);
          // Blended rather than replaced, so a mild contrast stays mild.
          x = x + (curved - x) * clamp(Math.abs(k - 1) * 2.2, 0, 1);
        }
        // Where black and white actually land. Lifted blacks are most of what
        // makes a print look like a print rather than like a screenshot.
        x = era.lift[c] + x * (era.gain[c] - era.lift[c]);
        out[c][v] = clamp(Math.round(x * 255), 0, 255);
      }
    }
    CACHE[key] = out;
    return out;
  }

  // ------------------------------------------------------------- the passes

  function tonePass(image, era) {
    var lut = tablesFor(era);
    var d = image.data;
    var sat = era.saturation;
    var sr = era.splitShadow, hr = era.splitHigh;
    for (var i = 0; i < d.length; i += 4) {
      var r = lut[0][d[i]], g = lut[1][d[i + 1]], b = lut[2][d[i + 2]];

      var l = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      if (sat !== 1) {
        r = l + (r - l) * sat;
        g = l + (g - l) * sat;
        b = l + (b - l) * sat;
      }

      /* Split toning. The tint applied to the shadows and the tint applied to
       * the highlights are different tints, weighted by how dark the pixel
       * is — which is why "teal and orange" is a look and "add blue" is not. */
      var dark = 1 - l / 255, light = l / 255;
      r += (sr[0] * dark + hr[0] * light) * 255;
      g += (sr[1] * dark + hr[1] * light) * 255;
      b += (sr[2] * dark + hr[2] * light) * 255;

      d[i] = clamp(r, 0, 255);
      d[i + 1] = clamp(g, 0, 255);
      d[i + 2] = clamp(b, 0, 255);
    }
  }

  /* Light spilling out of the highlights, tinted.
   *
   * Real halation is light passing through the emulsion, bouncing off the
   * film base and coming back — which is why it is red-orange and why it only
   * happens around bright things. Built by isolating the highlights, blurring
   * them by drawing small and back up, and adding the result.
   */
  function halationPass(canvas, era) {
    if (!era.halation) { return; }
    var W = canvas.width, H = canvas.height;
    var small = Math.max(24, Math.round(W / 14));
    var tall = Math.max(24, Math.round(H / 14));

    var bright = document.createElement("canvas");
    bright.width = small; bright.height = tall;
    var b = bright.getContext("2d", { alpha: true, willReadFrequently: true });
    b.drawImage(canvas, 0, 0, small, tall);
    var pixels = b.getImageData(0, 0, small, tall);
    var d = pixels.data;
    var tint = era.halationTint;
    for (var i = 0; i < d.length; i += 4) {
      var l = (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255;
      // Only the top of the range, and rising steeply inside it.
      // Only the genuinely bright end. At 0.62 most of a daylight photograph
      // qualifies and the "glow" becomes an exposure increase.
      var glow = l <= 0.76 ? 0 : Math.pow((l - 0.76) / 0.24, 1.8);
      d[i] = tint[0]; d[i + 1] = tint[1]; d[i + 2] = tint[2];
      d[i + 3] = Math.round(255 * glow);
    }
    b.putImageData(pixels, 0, 0);

    var g = canvas.getContext("2d");
    g.save();
    g.globalCompositeOperation = "lighter";
    g.globalAlpha = era.halation * 0.40;
    // Drawn back up from a fourteenth of the size: the upscale is the blur.
    g.imageSmoothingEnabled = true;
    g.drawImage(bright, 0, 0, W, H);
    g.restore();
  }

  /* Colour drawn slightly to the side of the thing it belongs to.
   *
   * Composite video carries chroma at a fraction of luma's bandwidth, so the
   * colour smears sideways and lags. This is the single most recognisable
   * artefact of the format.
   *
   * Done in the pixel domain, on the buffer the tone pass already has open.
   * The first version drew two offset copies with `lighter`, which is not a
   * displacement — it is adding two more exposures of the picture to itself,
   * and it made every 1980s frame a pink wash. Displacement moves colour
   * without changing how much light is in the frame, which is what the real
   * artefact does: red pulled one way, blue the other, green left alone
   * because that is where most of the luminance lives and moving it would
   * put the picture out of focus.
   */
  function chromaShift(image, era, W, H) {
    if (!era.chroma) { return; }
    var shift = Math.max(1, Math.round(era.chroma * (W / 1080)));
    var d = image.data;
    var copy = new Uint8ClampedArray(d);
    for (var y = 0; y < H; y++) {
      var row = y * W;
      for (var x = 0; x < W; x++) {
        var i = (row + x) * 4;
        var red = (row + Math.min(W - 1, x + shift)) * 4;
        var blue = (row + Math.max(0, x - shift)) * 4;
        d[i] = copy[red];
        d[i + 2] = copy[blue + 2];
      }
    }
  }

  /* Interlaced video: alternate lines darker. Drawn as a tiled two-pixel
   * pattern rather than a loop over every row. */
  function scanPass(canvas, era) {
    if (!era.scan) { return; }
    var W = canvas.width, H = canvas.height;
    var tile = document.createElement("canvas");
    tile.width = 1; tile.height = 3;
    var t = tile.getContext("2d");
    t.fillStyle = "rgba(0,0,0," + (era.scan * 0.5).toFixed(3) + ")";
    t.fillRect(0, 0, 1, 1);
    var g = canvas.getContext("2d");
    g.save();
    g.fillStyle = g.createPattern(tile, "repeat");
    g.fillRect(0, 0, W, H);
    g.restore();
  }

  /* Grain. Structure in the midtones, not noise everywhere.
   *
   * A tile generated once and repeated: a full-frame noise field costs a
   * getImageData and a putImageData the size of the picture, and at this size
   * the repeat is invisible. Drawn with `overlay`, which leaves black and
   * white alone and works hardest in the middle of the range — where film
   * grain actually lives.
   */
  var GRAIN = {};

  function grainTile(size, amount) {
    var key = size + ":" + amount.toFixed(2);
    if (GRAIN[key]) { return GRAIN[key]; }
    var side = 128;
    var tile = document.createElement("canvas");
    tile.width = side; tile.height = side;
    var g = tile.getContext("2d", { alpha: true, willReadFrequently: true });
    var pixels = g.createImageData(side, side);
    var d = pixels.data;
    for (var i = 0; i < d.length; i += 4) {
      // Mid-grey plus noise: `overlay` treats 128 as "leave alone".
      var n = 128 + (Math.random() - 0.5) * 255 * amount;
      d[i] = d[i + 1] = d[i + 2] = clamp(n, 0, 255);
      d[i + 3] = 255;
    }
    g.putImageData(pixels, 0, 0);
    GRAIN[key] = tile;
    return tile;
  }

  function grainPass(canvas, era) {
    if (!era.grain) { return; }
    var g = canvas.getContext("2d");
    var tile = grainTile(era.grainSize, era.grain);
    g.save();
    g.globalCompositeOperation = "overlay";
    g.globalAlpha = 0.55;
    var pattern = g.createPattern(tile, "repeat");
    if (era.grainSize > 1) {
      // Coarser grain by scaling the pattern rather than by making a second
      // tile: older stock has bigger grain, not more of it.
      g.scale(era.grainSize, era.grainSize);
      g.fillStyle = pattern;
      g.fillRect(0, 0, canvas.width / era.grainSize, canvas.height / era.grainSize);
    } else {
      g.fillStyle = pattern;
      g.fillRect(0, 0, canvas.width, canvas.height);
    }
    g.restore();
  }

  function vignettePass(canvas, era) {
    if (!era.vignette) { return; }
    var W = canvas.width, H = canvas.height;
    var g = canvas.getContext("2d");
    var grad = g.createRadialGradient(
      W / 2, H / 2, Math.min(W, H) * 0.30,
      W / 2, H / 2, Math.max(W, H) * 0.74
    );
    grad.addColorStop(0, "rgba(0,0,0,0)");
    grad.addColorStop(1, "rgba(0,0,0," + era.vignette.toFixed(2) + ")");
    g.save();
    g.fillStyle = grad;
    g.fillRect(0, 0, W, H);
    g.restore();
  }

  // ------------------------------------------------------------------ apply

  /* A recipe with every field present.
   *
   * Recipes are written partially — a look that only wants a tone curve
   * should not have to spell out that it has no scanlines — and every pass
   * below reads its fields unconditionally. Merging against a neutral set is
   * what lets both be true. Neutral here means *identity*: gain 1, lift 0,
   * contrast 1, saturation 1, and every effect off, so an empty recipe is a
   * grade that does nothing rather than a grade that does something
   * arbitrary. */
  var NEUTRAL = {
    label: "", note: "",
    gamma: [1, 1, 1], lift: [0, 0, 0], gain: [1, 1, 1],
    contrast: 1, saturation: 1,
    splitShadow: [0, 0, 0], splitHigh: [0, 0, 0],
    halation: 0, halationTint: [255, 255, 255],
    grain: 0, grainSize: 1,
    chroma: 0, scan: 0, vignette: 0, weave: 0
  };

  function full(recipe) {
    var out = {};
    for (var key in NEUTRAL) {
      if (Object.prototype.hasOwnProperty.call(NEUTRAL, key)) {
        out[key] = Object.prototype.hasOwnProperty.call(recipe, key)
          ? recipe[key] : NEUTRAL[key];
      }
    }
    return out;
  }

  /* Grade a canvas in place from a recipe. Every pass, in the order the
   * physics happens: the film responds to light, then light spills, then the
   * format mangles it, then it is projected through a lens.
   *
   * Public in its own right, not only via `grade`, because the base looks —
   * warm, noir, house — need the same engine. They were CSS filter strings,
   * and two of them moved the picture by less than the eye can see. Running
   * them through this instead is the difference between a grade and a
   * rounding error. */
  function apply(canvas, recipe) {
    if (!recipe) { return false; }
    var era = full(recipe);
    var g = canvas.getContext("2d", { alpha: false, willReadFrequently: true });
    var pixels;
    try {
      pixels = g.getImageData(0, 0, canvas.width, canvas.height);
    } catch (e) {
      return false;                     // tainted canvas: leave it untouched
    }
    // Level first, grade second. Grading an underexposed photograph without
    // this makes it black rather than making it look like 1994.
    if (recipe.level !== false) { levelPass(pixels); }
    tonePass(pixels, era);
    chromaShift(pixels, era, canvas.width, canvas.height);
    g.putImageData(pixels, 0, 0);
    halationPass(canvas, era);
    scanPass(canvas, era);
    grainPass(canvas, era);
    vignettePass(canvas, era);
    return true;
  }

  /* Bring a photograph to a usable exposure before grading it.
   *
   * A grade is a look applied to a picture, not a rescue. Applied to a night
   * photograph straight off a phone — of which a camera roll is full — a look
   * that lifts blacks and adds a vignette produces a black rectangle, and a
   * reel of black rectangles is what "it looks worse than anything a human
   * would make" looks like from the inside. Measured on a real set: eight
   * photographs, three of them night shots, and the night shots came out of
   * the grade at a mean luma under 0.08.
   *
   * So the picture is levelled first, from its own histogram, and only then
   * graded. Deliberately partial — `PULL` well under 1 — because a picture
   * that is dark *on purpose* should stay darker than one that is not, and
   * full auto-levels flattens the difference between every photograph ever
   * taken. This corrects; it does not normalise.
   */
  var PULL = 0.72;
  //: Where a mid-grey subject should sit once the picture is levelled.
  var TARGET = 0.44;

  function levelPass(image) {
    var d = image.data;
    var n = d.length / 4;
    if (!n) { return; }

    // A 64-bin luma histogram is enough to find the ends of the picture.
    var bins = new Float32Array(64);
    var mean = 0;
    for (var i = 0; i < d.length; i += 4) {
      var l = (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255;
      bins[Math.min(63, Math.floor(l * 64))] += 1;
      mean += l;
    }
    mean /= n;

    // The 1st and 99th percentiles rather than the extremes: one blown
    // specular highlight or one dead pixel should not set the white point.
    var low = 0, high = 1, seen = 0;
    var floorAt = n * 0.01, ceilAt = n * 0.99;
    for (var b = 0; b < 64; b++) {
      var was = seen;
      seen += bins[b];
      if (was < floorAt && seen >= floorAt) { low = b / 64; }
      if (was < ceilAt && seen >= ceilAt) { high = (b + 1) / 64; }
    }
    var span = Math.max(0.06, high - low);

    // Stretch what the picture actually uses to fill the range, then put the
    // midtone where a viewer expects it. Both pulled back toward doing
    // nothing, so a correctly exposed photograph is barely touched.
    var stretch = 1 + (1 / span - 1) * PULL;
    var lifted = (mean - low) * stretch;
    var gamma = lifted > 0.01 && lifted < 0.99
      ? 1 + (Math.log(TARGET) / Math.log(lifted) - 1) * PULL
      : 1;
    gamma = clamp(gamma, 0.45, 2.2);

    var lut = new Uint8Array(256);
    for (var v = 0; v < 256; v++) {
      var x = clamp((v / 255 - low) * stretch, 0, 1);
      lut[v] = clamp(Math.round(Math.pow(x, gamma) * 255), 0, 255);
    }
    for (i = 0; i < d.length; i += 4) {
      d[i] = lut[d[i]];
      d[i + 1] = lut[d[i + 1]];
      d[i + 2] = lut[d[i + 2]];
    }
  }

  //: Grade by era name. The named-recipe front door onto `apply`.
  function grade(canvas, name) {
    return ERAS[name] ? apply(canvas, ERAS[name]) : false;
  }

  /* What a clip gets, which is less, and it is worth being plain about why:
   * every pass above is per-pixel and a clip pays for it thirty times a
   * second. The tone curve survives as a filter string — approximately, since
   * `filter` has no curves — and the rest does not. A film cut from clips
   * therefore looks like the era in colour but not in texture. */
  function filterFor(name) {
    var era = ERAS[name];
    if (!era) { return "none"; }
    var mid = (era.gain[0] + era.gain[1] + era.gain[2]) / 3;
    var warm = era.splitHigh[0] - era.splitHigh[2];
    return [
      "saturate(" + era.saturation.toFixed(2) + ")",
      "contrast(" + era.contrast.toFixed(2) + ")",
      "brightness(" + clamp(mid + era.lift[1] * 0.6, 0.7, 1.3).toFixed(2) + ")",
      warm > 0.02 ? "sepia(" + clamp(warm * 3.2, 0, 0.45).toFixed(2) + ")" : ""
    ].filter(Boolean).join(" ");
  }

  function names() { return ORDER.slice(); }
  function about(name) { return ERAS[name] || null; }

  global.auteurEra = {
    ERAS: ERAS,
    // Exposed so the levelling can be measured on its own. Inferring it from
    // the finished grade measured nothing: `full()` drops keys it does not
    // know, so an opt-out passed in the recipe never reached the check and
    // both arms of the comparison ran the same code.
    levelPass: levelPass,
    NEUTRAL: NEUTRAL,
    apply: apply,
    names: names,
    about: about,
    eraFor: eraFor,
    grade: grade,
    filterFor: filterFor
  };
})(window);
