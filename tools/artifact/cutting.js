/* The vocabulary a cut is made of: what the eye is doing, and what happens
 * between two pictures.
 *
 * This exists because the films the renderer was making had exactly one
 * transition — a hard cut — and exactly one camera move: a slow linear zoom
 * applied identically to every shot. Both are defensible choices *once*. Made
 * on every shot of every film they are not choices at all, and what comes out
 * reads as a slideshow with a wobble on it. The reference reels do something
 * else, and it is measurable: they hold each frame almost perfectly still
 * (median frame-to-frame difference of 0.15-2.0 out of 255) and put all of
 * their energy into the cut itself, which spikes to 100-200. Ours does the
 * opposite — permanent low-grade motion, then nothing at the cut.
 *
 * So this module owns three things:
 *
 *   salience   where the subject is in a picture, so a transition can be
 *              built around it rather than around the middle of the frame
 *   gestures   what the frame does during a shot, including doing nothing,
 *              which is the most common answer in the references and was
 *              not previously reachable
 *   transitions how one picture becomes the next, including the ones that
 *              leave part of the outgoing picture on top of the incoming one
 *
 * Nothing here draws a film. It answers questions and returns numbers; the
 * renderer decides what to do with them.
 */
(function (global) {
  "use strict";

  // ------------------------------------------------------------------ easing

  function clamp01(t) { return t < 0 ? 0 : t > 1 ? 1 : t; }

  /* Easing is most of the difference between "moved" and "was moved by a
   * script". A linear ramp is the one curve nothing in the physical world
   * follows, and it was the only curve this program had. */
  var EASE = {
    linear: function (t) { return t; },
    out: function (t) { var c = 1 - clamp01(t); return 1 - c * c * c; },
    // Quintic: nearly all the travel in the first fifth, then it sits still.
    snap: function (t) { var c = 1 - clamp01(t); return 1 - c * c * c * c * c; },
    inOut: function (t) {
      t = clamp01(t);
      return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    },
    // Overshoots and comes back — an arrival, not a switch being thrown.
    back: function (t) {
      var c1 = 1.70158, c3 = c1 + 1, u = clamp01(t) - 1;
      return 1 + c3 * u * u * u + c1 * u * u;
    },
    // Starts fast, stops hard. For anything that is thrown rather than moved.
    thrown: function (t) { var c = 1 - clamp01(t); return 1 - c * c * c * c; }
  };

  // --------------------------------------------------------------- salience

  /* Where the eye goes in a picture, and how big the thing it goes to is.
   *
   * Not segmentation and it must not be described as segmentation — there is
   * no model here, no network and no notion of a person. It is edge energy
   * plus colour departure from the frame's own average, blurred into regions
   * and weighted very slightly toward the middle, which is where photographs
   * put their subjects. That is enough to place a portal over the thing in
   * the picture instead of over the centre of the rectangle, and it fails
   * gracefully — on a picture with no subject it returns the middle, which is
   * what centring would have done anyway.
   *
   * Returned in *source* coordinates, 0..1 across the picture as supplied.
   * Mapping that onto the film frame is `focusInFrame`, because a shot is
   * cropped and pushed and the subject does not stay where it started.
   */
  var GRID = 48;

  function readSource(source) {
    var flat = {
      strength: 0.5, focus: [0.5, 0.5], radius: 0.32,
      bright: 0.5, busy: 0.5
    };
    var c = document.createElement("canvas");
    c.width = GRID; c.height = GRID;
    var g = c.getContext("2d", { alpha: false, willReadFrequently: true });
    var data;
    try {
      g.drawImage(source.el, 0, 0, GRID, GRID);
      data = g.getImageData(0, 0, GRID, GRID).data;
    } catch (e) {
      return flat;                       // tainted or not decoded: no opinion
    }

    var n = GRID * GRID;
    var luma = new Float32Array(n);
    var chroma = new Float32Array(n);
    var mean = 0, meanR = 0, meanG = 0, meanB = 0;
    for (var p = 0; p < n; p++) {
      var r = data[p * 4], gg = data[p * 4 + 1], b = data[p * 4 + 2];
      luma[p] = (0.2126 * r + 0.7152 * gg + 0.0722 * b) / 255;
      mean += luma[p];
      meanR += r; meanG += gg; meanB += b;
    }
    mean /= n; meanR /= n * 255; meanG /= n * 255; meanB /= n * 255;

    var spread = 0;
    for (p = 0; p < n; p++) { spread += (luma[p] - mean) * (luma[p] - mean); }
    spread = Math.sqrt(spread / n);

    // How far each pixel's colour departs from the picture's own average. A
    // red coat in a grey street scores; a grey street in a grey street does
    // not. Relative rather than absolute, so it works on a monochrome frame.
    for (p = 0; p < n; p++) {
      var dr = data[p * 4] / 255 - meanR;
      var dg = data[p * 4 + 1] / 255 - meanG;
      var db = data[p * 4 + 2] / 255 - meanB;
      chroma[p] = Math.sqrt(dr * dr + dg * dg + db * db);
    }

    // Edges: a discrete Laplacian. Texture, focus and boundaries all show up
    // here, and it is near zero on sky, on a wall and on anything defocused.
    var sal = new Float32Array(n);
    var detail = 0, counted = 0;
    for (var y = 1; y < GRID - 1; y++) {
      for (var x = 1; x < GRID - 1; x++) {
        var k = y * GRID + x;
        var edge = Math.abs(
          4 * luma[k] - luma[k - 1] - luma[k + 1] - luma[k - GRID] - luma[k + GRID]
        );
        detail += edge; counted += 1;
        // A gentle centre bias — 1.0 in the middle falling to 0.72 at the
        // edge. Strong enough to break ties, too weak to beat a real subject
        // sitting off to one side, which is where a composed frame puts it.
        var ox = (x / (GRID - 1) - 0.5), oy = (y / (GRID - 1) - 0.5);
        var middle = 1 - 0.56 * (ox * ox + oy * oy);
        sal[k] = (edge * 3.2 + chroma[k] * 1.1) * middle;
      }
    }
    detail /= Math.max(counted, 1);

    // Blur it into regions. A per-pixel maximum is a speck of noise; what a
    // portal needs is the middle of the area that is interesting.
    var soft = new Float32Array(n);
    var R = 3;
    for (y = 0; y < GRID; y++) {
      for (x = 0; x < GRID; x++) {
        var sum = 0, seen = 0;
        for (var j = -R; j <= R; j++) {
          var yy = y + j;
          if (yy < 0 || yy >= GRID) { continue; }
          for (var i = -R; i <= R; i++) {
            var xx = x + i;
            if (xx < 0 || xx >= GRID) { continue; }
            sum += sal[yy * GRID + xx]; seen += 1;
          }
        }
        soft[y * GRID + x] = sum / Math.max(seen, 1);
      }
    }

    var peak = 0, at = Math.floor(n / 2);
    for (p = 0; p < n; p++) { if (soft[p] > peak) { peak = soft[p]; at = p; } }

    // How far the interesting region reaches, as a share of the frame. Every
    // cell above half the peak counted, turned back into a radius — so a
    // single face gives a small portal and a busy street gives a wide one.
    var above = 0;
    for (p = 0; p < n; p++) { if (soft[p] > peak * 0.5) { above += 1; } }
    var radius = Math.sqrt((above / n) / Math.PI);

    return {
      strength: Math.min(1, spread * 1.6 + detail * 6),
      focus: [(at % GRID) / (GRID - 1), Math.floor(at / GRID) / (GRID - 1)],
      radius: Math.max(0.14, Math.min(0.5, radius)),
      bright: mean,
      busy: Math.min(1, detail * 9)
    };
  }

  /* Where a source-space point lands on the film frame, given the framing the
   * shot is drawn at. Same arithmetic as `drawFramed`, kept next to it in
   * spirit: a portal centred on the subject's position *in the photograph* is
   * centred somewhere else entirely once the photograph has been cropped to a
   * 9:16 frame and pushed in 20%. */
  function focusInFrame(sourceSize, W, H, zoom, dx, dy, focus) {
    var scale = Math.max(W / sourceSize[0], H / sourceSize[1]) * zoom;
    var w = sourceSize[0] * scale, h = sourceSize[1] * scale;
    var x = (W - w) / 2 + dx * Math.max(0, (w - W) / 2);
    var y = (H - h) / 2 + dy * Math.max(0, (h - H) / 2);
    return [
      Math.max(0.06, Math.min(0.94, (x + focus[0] * w) / W)),
      Math.max(0.06, Math.min(0.94, (y + focus[1] * h) / H))
    ];
  }

  // --------------------------------------------------------------- gestures

  /* What the frame does for the length of one shot.
   *
   * The important entry is `hold`, which does nothing at all. It was not
   * previously possible to make a shot that sits still, and stillness is what
   * the references are full of — it is how a cut gets to land. A film where
   * every shot drifts has no cuts in it, only dissolves between wobbles.
   *
   * Each returns the framing at `t` through the shot: a multiplier on the
   * shot's zoom and an offset added to its position, both in the same units
   * `drawFramed` already takes.
   */
  var GESTURES = {
    hold: {
      label: "held",
      moves: false,
      at: function () { return { zoom: 1, dx: 0, dy: 0 }; }
    },
    snap: {
      // Arrives a few percent wide and is at rest by a fifth of the way in.
      // Reads as the cut landing rather than as a move.
      label: "snapped in",
      moves: false,
      at: function (t, a) {
        var e = EASE.snap(clamp01(t / 0.22));
        return { zoom: 1 + a.push * (1 - e), dx: 0, dy: 0 };
      }
    },
    press: {
      label: "pressed in",
      moves: true,
      at: function (t, a) {
        var e = EASE.out(t);
        return { zoom: 1 + a.push * e, dx: 0, dy: 0 };
      }
    },
    release: {
      label: "pulled back",
      moves: true,
      at: function (t, a) {
        var e = EASE.out(t);
        return { zoom: 1 + a.push * (1 - e), dx: 0, dy: 0 };
      }
    },
    swing: {
      // Travels across and overshoots very slightly at the end of the travel.
      label: "swung across",
      moves: true,
      at: function (t, a) {
        var e = EASE.back(clamp01(t / 0.8));
        return { zoom: 1 + a.push * 0.25, dx: a.travel * e, dy: 0 };
      }
    },
    fall: {
      label: "fell through",
      moves: true,
      at: function (t, a) {
        var e = EASE.inOut(t);
        return { zoom: 1 + a.push * 0.4 * e, dx: 0, dy: a.travel * 0.7 * e };
      }
    },
    settle: {
      // Comes in off-centre and moving, and stops. The most "filmed" of them.
      label: "settled",
      moves: false,
      at: function (t, a) {
        var e = EASE.thrown(clamp01(t / 0.45));
        return {
          zoom: 1 + a.push * 0.5 * (1 - e),
          dx: a.travel * (1 - e),
          dy: a.travel * 0.4 * (1 - e)
        };
      }
    }
  };

  var GESTURE_NAMES = ["hold", "snap", "press", "release", "swing", "fall", "settle"];

  // ------------------------------------------------------------ transitions

  /* How one picture becomes the next.
   *
   * A transition has two halves and either may be absent:
   *
   *   `enter(t, a)`  what the *incoming* frame does while it arrives — a
   *                  multiplier and an offset, folded into the draw it was
   *                  going to do anyway, so it costs nothing
   *   `over(g, held, W, H, t, a)`  what is drawn on top afterwards, where
   *                  `held` is the last frame of the outgoing shot, frozen
   *
   * Freezing the outgoing frame rather than continuing to render it is a
   * deliberate simplification and an honest one at this speed: these run for
   * two to five frames, over which a 0.13s shot moves imperceptibly, and a
   * freeze on the way out of a shot is itself something editors do. It costs
   * one canvas-to-canvas copy per cut instead of a second full render per
   * frame, which is the difference between this being affordable on a phone
   * and not.
   *
   * `a` carries the accents: which way this one goes, where the subject was,
   * and how hard to play it.
   */

  function drawSmear(g, image, W, H, ox, oy, alpha, steps) {
    // A smear built from repeated draws rather than `filter: blur()`, which
    // costs 60ms a frame at this size — the same measurement that forced
    // photographs to be graded once instead of per frame.
    var each = alpha / steps;
    g.save();
    g.globalAlpha = each;
    for (var i = 0; i < steps; i++) {
      var f = i / steps;
      g.drawImage(image, ox * f, oy * f, W, H);
    }
    g.restore();
  }

  var TRANSITIONS = {

    /* Nothing. Has to stay the commonest thing in any film — a reel that
     * transitions every cut is mush, and the references hard-cut most of
     * theirs. It is in the table so it can be chosen rather than defaulted
     * to. */
    cut: {
      label: "a straight cut",
      seconds: 0,
      enter: null,
      over: null
    },

    /* The portal. An aperture opens in the outgoing frame, centred on
     * whatever the outgoing picture's subject was, and the new picture is
     * behind it — so for two or three frames part of the previous photograph
     * is still on screen, with a hole in it, and the eye goes through the
     * hole. The incoming frame flies in slightly to sell the passage.
     *
     * This is the one the whole module was written for. */
    portal: {
      label: "a portal through the subject",
      seconds: 0.20,
      enter: function (t, a) {
        return { zoom: 1 + 0.10 * (1 - EASE.out(t)), dx: 0, dy: 0 };
      },
      over: function (g, held, W, H, t, a) {
        /* Accelerating, not decelerating. An ease-*out* aperture is 47% open
         * one frame in and 90% open by the third, so the portal spends almost
         * none of its life being a portal — drawn out on a contact sheet it
         * was a hole that had already finished opening in every cell but the
         * first. Squared: it holds small, then goes. */
        var e = clamp01(t) * clamp01(t);
        var here = a.focus || [0.5, 0.5];
        var cx = here[0] * W, cy = here[1] * H;
        // Grows from the subject's own size to past the far corner.
        var far = Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy));
        var r = (a.radius || 0.3) * Math.min(W, H) * 0.5 + e * far;

        g.save();
        // Everything except the aperture — `evenodd` on a rect plus a circle
        // is the hole. The held frame keeps its own edges, so what is left is
        // recognisably the picture that was just there.
        g.beginPath();
        g.rect(0, 0, W, H);
        g.arc(cx, cy, r, 0, Math.PI * 2);
        g.clip("evenodd");
        // Pushing the held frame outward as it opens: passing through, not a
        // hole being cut in a still.
        var grow = 1 + 0.16 * e;
        var ow = W * grow, oh = H * grow;
        g.globalAlpha = 1 - e * e * 0.35;
        g.drawImage(held, (W - ow) / 2, (H - oh) / 2, ow, oh);
        g.restore();

        // A rim of light on the aperture edge, brightest as it opens. Without
        // it the hole reads as a mask; with it, it reads as an opening.
        if (e < 0.85) {
          g.save();
          g.globalCompositeOperation = "lighter";
          var glow = g.createRadialGradient(cx, cy, r * 0.86, cx, cy, r * 1.14);
          glow.addColorStop(0, "rgba(0,0,0,0)");
          glow.addColorStop(0.5, "rgba(255,248,235," + (0.30 * (1 - e)).toFixed(3) + ")");
          glow.addColorStop(1, "rgba(0,0,0,0)");
          g.fillStyle = glow;
          g.fillRect(0, 0, W, H);
          g.restore();
        }
      }
    },

    /* The carry. The subject of the outgoing frame — a soft-edged patch of
     * it, not a cutout, because nothing here segments anything — stays on top
     * of the incoming picture and drifts off it. Literally part of the
     * previous photo sitting on the next one. Cheap, and the single most
     * recognisable move in the reference reels. */
    carry: {
      label: "the last subject carried over",
      seconds: 0.24,
      enter: null,
      over: function (g, held, W, H, t, a) {
        /* Linear, and a patch big enough to recognise.
         *
         * Both numbers here were wrong the first time and the contact sheet
         * showed it in one glance: a salience radius of 0.14 gives a 34px
         * blob on a 270px frame, which is a smudge rather than a subject, and
         * an eased-out alpha was down to a fifth by the second frame. What
         * has to survive the join is enough of the previous picture to be
         * *recognised as* the previous picture. */
        var e = clamp01(t);
        var here = a.focus || [0.5, 0.5];
        var cx = here[0] * W, cy = here[1] * H;
        var r = (0.34 + (a.radius || 0.3)) * Math.min(W, H) * 0.8;
        // Drifts the way the cut is going, and a little up.
        var travel = (a.way || 1) * W * 0.22 * e;

        g.save();
        g.globalAlpha = Math.max(0, 1 - e * e);
        // A radial fade for the edge instead of a hard boundary: a hard edge
        // announces that a shape was pasted on, which is the look this is
        // supposed to avoid.
        var mask = document.createElement("canvas");
        mask.width = W; mask.height = H;
        var m = mask.getContext("2d");
        m.drawImage(held, 0, 0);
        m.globalCompositeOperation = "destination-in";
        var fade = m.createRadialGradient(cx, cy, r * 0.45, cx, cy, r);
        fade.addColorStop(0, "rgba(0,0,0,1)");
        fade.addColorStop(1, "rgba(0,0,0,0)");
        m.fillStyle = fade;
        m.fillRect(0, 0, W, H);
        var scale = 1 + 0.22 * e;
        g.drawImage(
          mask,
          travel - (W * scale - W) / 2,
          -H * 0.05 * e - (H * scale - H) / 2,
          W * scale, H * scale
        );
        g.restore();
      }
    },

    /* Whip. Both frames thrown sideways with a smear on them, the incoming
     * overshooting and coming back. Two to three frames — any longer and it
     * is a slide, which is a different and much duller thing. */
    whip: {
      label: "a whip across",
      seconds: 0.13,
      enter: function (t, a) {
        var e = EASE.thrown(t);
        return { zoom: 1 + 0.06 * (1 - e), dx: (a.way || 1) * -1.5 * (1 - e), dy: 0 };
      },
      over: function (g, held, W, H, t, a) {
        var e = EASE.thrown(t);
        if (e >= 1) { return; }
        drawSmear(g, held, W, H, (a.way || 1) * W * 1.25 * e, 0, 1 - e, 4);
      }
    },

    /* Push. The plain one, and it earns its place by being plain — a film
     * needs somewhere for the eye to rest between the loud transitions. */
    push: {
      label: "a push",
      seconds: 0.17,
      enter: function (t, a) {
        var e = EASE.inOut(t);
        return { zoom: 1, dx: 0, dy: 0, slide: [(a.way || 1) * (1 - e), 0] };
      },
      over: function (g, held, W, H, t, a) {
        var e = EASE.inOut(t);
        g.drawImage(held, (a.way || 1) * -W * e, 0, W, H);
      }
    },

    /* Dissolving through the outgoing frame's own light: the bright parts go
     * first and the shadows hold on, so a window or a sky opens into the next
     * picture. The mask is built at 96px and scaled up, which is both cheap
     * and the reason the edge is soft rather than a threshold. */
    luma: {
      label: "a dissolve through the light",
      seconds: 0.22,
      enter: null,
      over: function (g, held, W, H, t, a) {
        var e = EASE.inOut(t);
        var mw = 96, mh = Math.max(1, Math.round(96 * H / W));
        var small = document.createElement("canvas");
        small.width = mw; small.height = mh;
        var s = small.getContext("2d", { alpha: true, willReadFrequently: true });
        var pixels;
        try {
          s.drawImage(held, 0, 0, mw, mh);
          pixels = s.getImageData(0, 0, mw, mh);
        } catch (err) {
          // Cannot read it: fall back to a plain dissolve rather than nothing.
          g.save(); g.globalAlpha = 1 - e; g.drawImage(held, 0, 0, W, H); g.restore();
          return;
        }
        var d = pixels.data;
        // Everything brighter than the moving threshold has gone; a band
        // either side of it is part way, which is the soft edge.
        var edge = 1.25 * e - 0.12;
        for (var i = 0; i < d.length; i += 4) {
          var l = (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255;
          var gone = clamp01((edge - (1 - l)) / 0.26);
          d[i + 3] = Math.round(255 * (1 - gone));
        }
        s.putImageData(pixels, 0, 0);

        var keep = document.createElement("canvas");
        keep.width = W; keep.height = H;
        var k = keep.getContext("2d");
        k.drawImage(held, 0, 0, W, H);
        k.globalCompositeOperation = "destination-in";
        k.drawImage(small, 0, 0, W, H);      // upscaled: the edge comes out soft
        g.drawImage(keep, 0, 0);
      }
    },

    /* Slices. Horizontal bands of the two pictures interleaved, collapsing
     * over three or four frames. Digital rather than photographic, and the
     * high-contrast references use it constantly. */
    slice: {
      label: "sliced across",
      seconds: 0.15,
      enter: null,
      over: function (g, held, W, H, t, a) {
        var e = EASE.out(t);
        var bands = 7;
        g.save();
        for (var i = 0; i < bands; i++) {
          // Alternating bands leave at different rates, so it shears apart
          // rather than closing like a blind.
          var lag = (i % 2 === 0 ? 0.0 : 0.35);
          var gone = clamp01((e - lag) / (1 - lag));
          if (gone >= 1) { continue; }
          var y = Math.floor(i * H / bands);
          var h = Math.ceil(H / bands) + 1;
          var shove = (i % 2 === 0 ? 1 : -1) * (a.way || 1) * W * gone;
          g.save();
          g.beginPath();
          g.rect(0, y, W, h);
          g.clip();
          g.globalAlpha = 1 - gone * 0.4;
          g.drawImage(held, shove, 0, W, H);
          g.restore();
        }
        g.restore();
      }
    },

    /* A frame or two of blown-out white, or of the outgoing frame inverted.
     * Directly observed in the references — a single inverted frame on the
     * cut — and it is the cheapest thing here by a distance. The incoming
     * shot arrives already running underneath it. */
    flash: {
      label: "a flash frame",
      seconds: 0.09,
      enter: null,
      over: function (g, held, W, H, t, a) {
        var e = clamp01(t);
        // Spikes almost immediately and is gone: a flash that fades in is a
        // dissolve to white, which is a different and much slower idea.
        var strength = Math.pow(1 - e, 1.8);
        g.save();
        if (a.invert) {
          g.globalAlpha = strength;
          g.globalCompositeOperation = "difference";
          g.fillStyle = "#ffffff";
          g.fillRect(0, 0, W, H);
        } else {
          g.globalAlpha = strength * 0.92;
          g.fillStyle = a.ink || "#ffffff";
          g.fillRect(0, 0, W, H);
        }
        g.restore();
      }
    },

    /* The match cut. Nothing is drawn over anything — the incoming shot
     * simply starts at the outgoing shot's framing and eases to its own, so
     * the two pictures share a composition for a moment and the join
     * disappears. The seamless one, and the only one here that is invisible
     * when it works. */
    match: {
      label: "a match on the framing",
      seconds: 0.26,
      enter: function (t, a) {
        var e = EASE.out(t);
        var from = a.from || { zoom: 1, dx: 0, dy: 0 };
        return {
          zoom: 1 + (from.zoom - 1) * (1 - e),
          dx: from.dx * (1 - e),
          dy: from.dy * (1 - e)
        };
      },
      over: null
    }
  };

  var TRANSITION_NAMES = Object.keys(TRANSITIONS);

  /* Transitions that put part of the outgoing picture on top of the incoming
   * one. Named because "how often does the film do the thing that was asked
   * for" is a question the Gaze agent and the checks both need to ask, and
   * counting them by hand in two places is how the two answers drift apart. */
  var CARRYING = { portal: true, carry: true, whip: true, slice: true, luma: true };

  function carries(kind) { return CARRYING[kind] === true; }

  /* The most a transition may eat of the shot it opens.
   *
   * A 0.20s portal on a 0.13s shot means the shot is over before the portal
   * finishes and the film never resolves into a picture — which is precisely
   * the "uncoordinated" complaint, arrived at from the other direction. Half
   * the shot, and never more than the transition's own length. */
  function transitionSeconds(kind, shotSeconds) {
    var t = TRANSITIONS[kind];
    if (!t || !t.seconds) { return 0; }
    return Math.max(0.05, Math.min(t.seconds, shotSeconds * 0.5));
  }

  global.auteurCutting = {
    EASE: EASE,
    GESTURES: GESTURES,
    GESTURE_NAMES: GESTURE_NAMES,
    TRANSITIONS: TRANSITIONS,
    TRANSITION_NAMES: TRANSITION_NAMES,
    readSource: readSource,
    focusInFrame: focusInFrame,
    transitionSeconds: transitionSeconds,
    carries: carries,
    clamp01: clamp01
  };
})(window);
