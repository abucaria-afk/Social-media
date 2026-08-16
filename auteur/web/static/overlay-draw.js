/* The graphics vocabulary, drawn in a browser.
 *
 * A port of `auteur/craft/graphics.py` — the same eight shapes and the same
 * seven movements the OverlayAgent actually places, with the same easing and
 * the same reveal curves. Deliberately one module used by two callers: the
 * overlays tab previews with it, and the in-browser renderer draws the film
 * with it. A preview that draws its own approximation of the real thing is a
 * picture of a feature rather than the feature.
 *
 * Where the Python differs it is noted. Nothing here reads a file or a
 * network; a cue is a plain object and the canvas is the caller's.
 */
(function (global) {
  "use strict";

  //: What a graphic can be. Same set as `GRAPHIC_KINDS`, minus `sticker`,
  //: which is a PNG somebody supplied rather than a shape drawn from nothing.
  var KINDS = [
    { name: "circle", label: "Ring", note: "marks the subject, drawn by hand not by a compass" },
    { name: "bracket", label: "Viewfinder", note: "corners around the subject; covers nothing" },
    { name: "arrow", label: "Arrow", note: "points from somewhere to somewhere, with a bow in it" },
    { name: "underline", label: "Underline", note: "a pen stroke that lifts as it runs" },
    { name: "highlight", label: "Marker", note: "a soft swipe that sits behind type" },
    { name: "burst", label: "Impact", note: "radiating lines on a hit; it must not linger" },
    { name: "progress", label: "Progress", note: "tells the viewer the end is reachable" },
    { name: "tape", label: "Tape", note: "a torn strip for type to sit on" }
  ];

  //: How it arrives and behaves. Same set as `GRAPHIC_MOVES`.
  var MOVES = [
    { name: "pop", label: "Pop", note: "overshoots in and settles — arrived, not switched on" },
    { name: "draw", label: "Draw on", note: "drawn in over the first third" },
    { name: "sweep", label: "Sweep", note: "wipes on left to right" },
    { name: "pulse", label: "Pulse", note: "breathes on the beat" },
    { name: "wiggle", label: "Wiggle", note: "rotational jitter, sticker-ish" },
    { name: "drift", label: "Drift", note: "travels slowly across" },
    { name: "none", label: "Still", note: "no movement at all" }
  ];

  /* Kinds whose whole point is that they change over their own life, so the
   * reveal comes from the cue's clock rather than from the movement's curve.
   * Same set as `graphics.SELF_TIMED`. Leaving `burst` out of it — which the
   * first version of this port did — pins its reveal at 1.0, and a burst at
   * full reveal has already burnt out: it drew nothing at all. */
  var SELF_TIMED = { progress: true, burst: true };

  //: Kinds that run between two points rather than sitting at one.
  var SPANNING = { arrow: true, underline: true, highlight: true, tape: true };

  var TAU = Math.PI * 2;

  function easeOut(t) {
    var c = 1 - Math.min(1, Math.max(0, t));
    return 1 - c * c * c;
  }

  /* (scale, rotation°, dx, dy, alpha) at `progress` through the cue, 0..1.
     dx and dy are fractions of the graphic's own size. */
  function motion(move, progress) {
    var p = Math.min(1, Math.max(0, progress));

    if (move === "pop") {
      if (p < 0.2) {
        var t = easeOut(p / 0.2);
        return [0.55 + 0.55 * t, 0, 0, 0, Math.min(1, t * 1.6)];
      }
      var settle = Math.min(1, (p - 0.2) / 0.12);
      return [1.10 - 0.10 * easeOut(settle), 0, 0, 0, 1];
    }
    if (move === "pulse") {
      return [1 + 0.055 * Math.sin(p * TAU * 3), 0, 0, 0, 1];
    }
    if (move === "drift") {
      return [1, 0, 0.10 * p, -0.06 * p, 1];
    }
    if (move === "wiggle") {
      return [1 + 0.03 * Math.cos(p * TAU * 2.5), 7 * Math.sin(p * TAU * 2.5), 0, 0, 1];
    }
    return [1, 0, 0, 0, 1];
  }

  /* How much of a progressive shape is drawn yet, 0..1. */
  function reveal(move, progress) {
    if (move === "draw") { return easeOut(Math.min(1, progress / 0.35)); }
    if (move === "sweep") { return easeOut(Math.min(1, progress / 0.3)); }
    return 1;
  }

  function alphaOf(cue, extra) {
    return Math.max(0, Math.min(1, (extra === undefined ? 1 : extra) * (cue.opacity || 1)));
  }

  function strokeOf(cue, base) {
    return Math.max(2, base * 0.010 * Math.sqrt(cue.size || 1));
  }

  // ------------------------------------------------------------- the shapes
  // Each draws into a w×h box with its own origin at 0,0. The caller has
  // already placed and transformed it.

  function drawCircle(g, cue, shown, w, h, base) {
    // Radius-jittered, because a perfect ellipse reads as a user interface
    // element and gets skipped; a ring with a tremble in it reads as somebody
    // marking up the frame, which is what holds the eye for the half second
    // it needs to.
    var span = Math.max(0.03, shown * 1.06);
    var steps = Math.max(24, Math.round(96 * span));
    var cx = w / 2, cy = h / 2, rx = w / 2 * 0.92, ry = h / 2 * 0.92;
    g.lineWidth = strokeOf(cue, base);
    g.lineJoin = "round";
    g.lineCap = "round";
    g.beginPath();
    for (var i = 0; i <= steps; i++) {
      var t = (i / steps) * span * TAU - Math.PI * 0.35;
      var jitter = 1 + 0.035 * Math.sin(t * 3.1 + 0.7) * 0.5;
      var x = cx + Math.cos(t) * rx * jitter;
      var y = cy + Math.sin(t) * ry * jitter;
      if (i === 0) { g.moveTo(x, y); } else { g.lineTo(x, y); }
    }
    g.stroke();
  }

  function drawBracket(g, cue, shown, w, h, base) {
    var width = strokeOf(cue, base);
    var armX = w * 0.26 * shown, armY = h * 0.26 * shown;
    var i = width;
    g.lineWidth = width;
    g.lineCap = "round";
    var corners = [
      [[i, i], [i + armX, i], [i, i + armY]],
      [[w - i, i], [w - i - armX, i], [w - i, i + armY]],
      [[i, h - i], [i + armX, h - i], [i, h - i - armY]],
      [[w - i, h - i], [w - i - armX, h - i], [w - i, h - i - armY]]
    ];
    g.beginPath();
    corners.forEach(function (c) {
      g.moveTo(c[0][0], c[0][1]); g.lineTo(c[1][0], c[1][1]);
      g.moveTo(c[0][0], c[0][1]); g.lineTo(c[2][0], c[2][1]);
    });
    g.stroke();
  }

  function drawArrow(g, cue, shown, w, h, base) {
    var pad = base * 0.06 * (cue.size || 1);
    var tail = [pad, h - pad];
    var full = [w - pad, pad];
    var head = [tail[0] + (full[0] - tail[0]) * shown, tail[1] + (full[1] - tail[1]) * shown];
    var width = strokeOf(cue, base);
    g.lineWidth = width;
    g.lineCap = "round";

    // A bow in the shaft: a ruler-straight arrow reads as a diagram.
    var mx = (tail[0] + head[0]) / 2, my = (tail[1] + head[1]) / 2;
    var dx = head[0] - tail[0], dy = head[1] - tail[1];
    var run = Math.sqrt(dx * dx + dy * dy) || 1;
    var bow = run * 0.12;
    g.beginPath();
    g.moveTo(tail[0], tail[1]);
    g.quadraticCurveTo(mx - (dy / run) * bow, my + (dx / run) * bow, head[0], head[1]);
    g.stroke();

    if (shown > 0.35) {
      var angle = Math.atan2(head[1] - my, head[0] - mx);
      var barb = Math.max(width * 3, run * 0.16);
      g.beginPath();
      g.moveTo(head[0], head[1]);
      g.lineTo(head[0] - Math.cos(angle - 0.5) * barb, head[1] - Math.sin(angle - 0.5) * barb);
      g.moveTo(head[0], head[1]);
      g.lineTo(head[0] - Math.cos(angle + 0.5) * barb, head[1] - Math.sin(angle + 0.5) * barb);
      g.stroke();
    }
  }

  function drawUnderline(g, cue, shown, w, h, base) {
    var width = strokeOf(cue, base);
    var pad = base * 0.04 * (cue.size || 1);
    var y = h / 2;
    g.lineWidth = width;
    g.lineCap = "round";
    g.beginPath();
    // A slight rise across the run, like a pen stroke that lifted.
    g.moveTo(pad, y + width * 0.4);
    g.lineTo(pad + (w - pad * 2) * shown, y - width * 0.3);
    g.stroke();
  }

  function roundRect(g, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    g.beginPath();
    g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r);
    g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r);
    g.arcTo(x, y, x + w, y, r);
    g.closePath();
  }

  function drawHighlight(g, cue, shown, w, h, base) {
    // Sits *behind* type, so it is deliberately soft and never full strength.
    var band = h * 0.62;
    var pad = base * 0.04 * (cue.size || 1) * 0.4;
    g.globalAlpha *= 0.55;
    roundRect(g, pad, (h - band) / 2, Math.max(2, (w - pad * 2) * shown), band, band * 0.18);
    g.fill();
  }

  function drawBurst(g, cue, shown, w, h, base) {
    var cx = w / 2, cy = h / 2;
    var side = Math.min(w, h);
    var inner = side * (0.16 + 0.24 * shown);
    var outer = side * (0.22 + 0.28 * shown);
    // Full strength while it expands, then gone. Fading from the first frame
    // made it a smudge for its whole life and legible for none of it.
    var fade = 1 - Math.max(0, shown - 0.45) / 0.55 * 0.95;
    g.globalAlpha *= Math.max(0, fade);
    g.lineWidth = strokeOf(cue, base);
    g.lineCap = "round";
    g.beginPath();
    for (var spoke = 0; spoke < 12; spoke++) {
      var angle = spoke / 12 * TAU + 0.13;
      var end = outer * (spoke % 2 === 0 ? 1 : 0.72);
      g.moveTo(cx + Math.cos(angle) * inner, cy + Math.sin(angle) * inner);
      g.lineTo(cx + Math.cos(angle) * end, cy + Math.sin(angle) * end);
    }
    g.stroke();
  }

  function drawProgress(g, cue, shown, w, h) {
    var radius = h / 2;
    g.globalAlpha *= 0.22;
    roundRect(g, 0, 0, w, h, radius);
    g.fill();
    g.globalAlpha = g.globalAlpha / 0.22;
    roundRect(g, 0, 0, Math.max(h, w * shown), h, radius);
    g.fill();
  }

  function drawTape(g, cue, shown, w, h) {
    var right = Math.max(8, w * shown);
    var teeth = 9;
    // Proportional to the strip: a 4px notch on a 150px strip is a rounding
    // error, not a torn edge.
    var bite = h * 0.13;
    g.globalAlpha *= 0.82;
    g.beginPath();
    for (var x = 0; x <= teeth; x++) {
      var px = x / teeth * right, py = bite * (x % 2 ? 0.9 : 0.1);
      if (x === 0) { g.moveTo(px, py); } else { g.lineTo(px, py); }
    }
    for (x = teeth; x >= 0; x--) {
      g.lineTo(x / teeth * right, h - bite * (x % 2 ? 0.15 : 0.95));
    }
    g.closePath();
    g.fill();
  }

  var DRAWERS = {
    circle: drawCircle,
    bracket: drawBracket,
    arrow: drawArrow,
    underline: drawUnderline,
    highlight: drawHighlight,
    burst: drawBurst,
    progress: drawProgress,
    tape: drawTape
  };

  /* The box a kind wants, as a fraction of the frame's short side. Spanning
     kinds are wide and shallow; the rest are roughly square. */
  function boxOf(cue) {
    var size = cue.size || 1;
    if (cue.kind === "progress") { return [1.5 * size, 0.035 * size]; }
    if (SPANNING[cue.kind]) { return [0.62 * size, 0.16 * size]; }
    return [0.4 * size, 0.4 * size];
  }

  /* Draw one cue onto a canvas context.
   *
   * `cue` is {kind, move, anchor:[x,y] 0..1, size, color, opacity}. `p` is how
   * far through the cue's life it is, 0..1. Everything is in fractions of the
   * frame, so the same cue draws correctly at 1080x1920 and in a 120px chip.
   */
  function draw(g, cue, W, H, p) {
    var drawer = DRAWERS[cue.kind];
    if (!drawer) { return; }

    var base = Math.min(W, H);
    var move = cue.move || "pop";
    // Only the *reveal* comes from the cue's own clock for a self-timed kind;
    // the movement still applies, exactly as `graphics.py` line 560 has it.
    var shown = SELF_TIMED[cue.kind] ? Math.min(1, Math.max(0, p)) : reveal(move, p);
    var m = motion(move, p);

    var box = boxOf(cue);
    var w = box[0] * base, h = box[1] * base;
    var anchor = cue.anchor || [0.5, 0.5];
    var cx = anchor[0] * W + m[2] * w;
    var cy = anchor[1] * H + m[3] * h;

    g.save();
    g.globalAlpha = alphaOf(cue, m[4]);
    g.strokeStyle = cue.color || "#ffffff";
    g.fillStyle = cue.color || "#ffffff";
    g.translate(cx, cy);
    g.rotate(m[1] * Math.PI / 180);
    g.scale(m[0], m[0]);
    g.translate(-w / 2, -h / 2);
    // A halo, so a white line survives a white frame underneath it.
    g.shadowColor = "rgba(0,0,0,0.45)";
    g.shadowBlur = base * 0.012;
    drawer(g, cue, shown, w, h, base);
    g.restore();
  }

  global.auteurOverlays = {
    KINDS: KINDS,
    MOVES: MOVES,
    SPANNING: SPANNING,
    SELF_TIMED: SELF_TIMED,
    motion: motion,
    reveal: reveal,
    draw: draw
  };
})(window);
