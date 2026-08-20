/* Taste. Which of the available moves this particular film makes, and when.
 *
 * Kept apart from `cutting.js` deliberately. That module is the vocabulary —
 * what a portal *is*, how a whip behaves, what easing a snap uses. This one is
 * the choosing, and the choosing is the part that was missing. A program that
 * owns a rich vocabulary and applies one entry of it uniformly has not made a
 * single decision; it has a default. That is what the films looked like.
 *
 * Two things have to be true at once and they pull against each other:
 *
 *   variety     a film that portals every cut is as monotonous as one that
 *               hard-cuts every cut, just louder. The moves have to be mixed
 *               and the mixing has to avoid repeating itself
 *   coherence   the mix has to be recognisably *one* film's mix. A different
 *               transition on every cut, drawn at random, is noise — the
 *               thing that reads as "no personality" even though every frame
 *               is different from the last
 *
 * A style is the answer to both: a weighted bag of moves narrow enough to
 * have a character, plus a shape for the film that says where the loud ones
 * are allowed to go. Everything is seeded, so the same clips and the same
 * words give the same film twice and two films can be compared.
 */
(function (global) {
  "use strict";

  /* Roles. A shot's job in the film, decided before anything about how it
   * looks. This is the layer that was entirely absent — every shot was
   * interchangeable with every other shot, which is why the result read as a
   * sequence rather than an edit. */
  var ROLES = {
    hook: "the first frame, and the one it comes back to",
    run: "part of a burst — quick, and not meant to be dwelt on",
    accent: "the one the burst was building to",
    rest: "held, so the eye catches up",
    turn: "a change of direction; a new movement starts here",
    close: "the return to the hook, so the reel loops"
  };

  /* The styles.
   *
   * `transitions` and `gestures` are weighted bags, not lists: `cut` is heavy
   * everywhere because most cuts in a good reel are hard cuts, and the moves
   * that carry part of one picture onto the next are seasoning. The measured
   * references hard-cut the large majority of their joins; what makes them
   * feel rich is that the remainder is *varied*, not that it is frequent.
   *
   * `bars` are shot-length patterns in multiples of the base hold. A film
   * picks a different bar per movement, so its rhythm develops instead of
   * repeating a four-beat loop for thirty seconds.
   */
  var STYLES = {

    hypercut: {
      label: "hypercut",
      note: "hard, fast, and cut on the frame — flashes and slices, almost no drift",
      transitions: { cut: 10, flash: 4, slice: 3, whip: 2, portal: 2, carry: 1 },
      gestures: { hold: 6, snap: 5, settle: 2, press: 1, swing: 1 },
      bars: [[1, 1, 1, 1], [0.5, 0.5, 1, 2], [0.5, 0.5, 0.5, 0.5, 2], [1, 1, 2], [2, 0.5, 0.5, 1]],
      accents: 0.22,
      invert: 0.30,
      push: 0.10,
      travel: 0.35
    },

    dreamy: {
      label: "dreamy",
      note: "dissolves through the light, subjects carried over, long easy moves",
      transitions: { cut: 6, luma: 4, carry: 3, portal: 3, match: 3, push: 2 },
      gestures: { press: 5, release: 4, fall: 3, settle: 3, hold: 2, swing: 1 },
      bars: [[1, 1, 2], [2, 1, 1], [1, 2, 1, 2], [1.5, 1.5], [2, 2, 1]],
      accents: 0.10,
      invert: 0.0,
      push: 0.26,
      travel: 0.55
    },

    story: {
      label: "story",
      note: "matched framings and portals — the cuts try to be invisible",
      transitions: { cut: 8, match: 5, portal: 3, push: 3, carry: 2, luma: 1 },
      gestures: { settle: 4, press: 4, hold: 3, release: 2, swing: 2, fall: 1 },
      bars: [[1, 1, 1.5], [1, 1.5, 1, 2], [1.5, 1, 1], [1, 1, 1, 2]],
      accents: 0.14,
      invert: 0.0,
      push: 0.20,
      travel: 0.45
    },

    hype: {
      label: "hype",
      note: "whips, portals and impacts — every join is meant to be felt",
      transitions: { cut: 7, whip: 5, portal: 4, flash: 3, slice: 3, carry: 2 },
      gestures: { snap: 6, hold: 4, swing: 3, settle: 2, press: 2, fall: 1 },
      bars: [[0.5, 0.5, 1, 1], [1, 1, 0.5, 0.5, 2], [0.5, 0.5, 0.5, 0.5, 1, 2], [1, 1, 2]],
      accents: 0.26,
      invert: 0.18,
      push: 0.14,
      travel: 0.50
    },

    gallery: {
      label: "gallery",
      note: "held frames, matched light, and very little else — the pictures do the work",
      transitions: { cut: 10, match: 4, luma: 3, portal: 1 },
      gestures: { hold: 7, press: 4, release: 3, settle: 2 },
      bars: [[2, 2], [2, 1, 2], [1.5, 2, 1.5], [2, 2, 1, 3]],
      accents: 0.07,
      invert: 0.0,
      push: 0.18,
      travel: 0.30
    }
  };

  /* Which style a prompt is asking for.
   *
   * Words people actually type, same principle as the look and cadence
   * matching: a prompt is somebody describing a feeling. Anything unmatched
   * gets `story`, which is the one that tries hardest to be invisible and is
   * therefore the safest thing to be wrong about.
   */
  /* Matched at a word boundary, without which every one of these also fires
   * inside longer words. That is not hypothetical: `rave` matched *travel*,
   * so "a travel story, cinematic" was cut as a rave — measured, not
   * imagined. A leading boundary only, because the entries are word-initial
   * stems (`nostalg`, `energet`) that are supposed to match longer words
   * starting with them. */
  function wordy(pattern) {
    return new RegExp("\\b(?:" + pattern.source + ")", pattern.flags || "");
  }

  var STYLE_WORDS = [
    ["hypercut", /hypercut|hard\s*to\s*the\s*beat|rapid|machine\s*gun|frantic|blitz|glitch|edit\s*heavy|tiktok/],
    ["hype", /hype|gym|workout|sport|training|aggressive|energy|energet|punchy|drop|rave|edm|techno|club|party|fast/],
    ["dreamy", /dream|soft|nostalg|memor|hazy|ethereal|gentle|calm|slow|romantic|wedding|love|sunset|golden/],
    ["gallery", /gallery|minimal|still|portrait|photo|quiet|editorial|fashion|clean|austere|documentary/],
    ["story", /story|narrative|journey|trip|travel|day\s*in|vlog|cinematic|film/]
  ].map(function (entry) { return [entry[0], wordy(entry[1])]; });

  function styleFor(prompt) {
    var p = (prompt || "").toLowerCase();
    for (var i = 0; i < STYLE_WORDS.length; i++) {
      if (STYLE_WORDS[i][1].test(p)) { return STYLES[STYLE_WORDS[i][0]]; }
    }
    return STYLES.story;
  }

  /* Draw from a weighted bag, never returning what was returned last time.
   *
   * The "never twice running" rule does most of the work here. Weighted
   * sampling alone produces runs — three portals in a row is entirely likely
   * when portals are a quarter of the bag — and a run of the same loud move
   * is worse than not having the move at all, because it stops reading as a
   * choice and starts reading as a tic.
   */
  function pickFrom(bag, roll, avoid) {
    var names = [], weights = [], total = 0;
    for (var name in bag) {
      if (!Object.prototype.hasOwnProperty.call(bag, name)) { continue; }
      if (name === avoid) { continue; }
      names.push(name);
      weights.push(bag[name]);
      total += bag[name];
    }
    if (!names.length) { return avoid || "cut"; }
    var want = roll * total;
    for (var i = 0; i < names.length; i++) {
      want -= weights[i];
      if (want <= 0) { return names[i]; }
    }
    return names[names.length - 1];
  }

  /* The rhythm of the whole film, as multiples of the base hold.
   *
   * A different bar per movement, and each bar repeated until the movement is
   * full. The bars are written so their mean is near 1, which keeps the median
   * shot length near the cadence the words asked for — otherwise "a hypercut"
   * silently becomes something slower, which is the failure the fixed
   * `[1, 1, 1, 1.5]` pattern was covering up by never varying at all.
   *
   * Returns a list of {beats, role}.
   */
  function arrange(style, movements, roughly, roll) {
    var out = [];
    var bars = style.bars;
    // One bar per movement, walked with a stride so a five-movement film does
    // not use bar 0 twice before bar 3 once.
    var stride = 1 + Math.floor(roll() * (bars.length - 1));
    var perMovement = Math.max(1, Math.ceil(roughly / Math.max(movements, 1)));

    for (var m = 0; m < movements; m++) {
      var bar = bars[(m * stride) % bars.length];
      var placed = 0;
      var turned = false;
      while (placed < perMovement) {
        for (var i = 0; i < bar.length && placed < perMovement; i++) {
          var beats = bar[i];
          var role;
          if (m > 0 && !turned) { role = "turn"; turned = true; }
          else if (beats >= 2) { role = "rest"; }
          else if (beats <= 0.6) { role = "run"; }
          // The shot right after a burst, at full length, is what the burst
          // was for. Naming it is what lets it be treated differently.
          else if (i > 0 && bar[i - 1] <= 0.6) { role = "accent"; }
          else { role = "run"; }
          out.push({ beats: beats, role: role });
          placed += 1;
        }
      }
    }
    if (out.length) { out[0].role = "hook"; }
    return out;
  }

  /* Everything about one shot that is a matter of taste rather than of
   * physics: how it moves, how it arrives, and whether the grade does
   * anything unusual on it.
   *
   * `previous` is what the shot before chose, so runs can be broken.
   */
  function choices(style, role, roll, previous) {
    previous = previous || {};

    // Transitions first, because the gesture has to agree with the arrival.
    var transition;
    if (role === "hook") {
      transition = "cut";                       // nothing to come from
    } else if (role === "run" && roll() > 0.28) {
      // The body of a burst is hard cuts. This is the single most important
      // weighting in the file: it is what leaves room for the loud moves to
      // mean something when they do arrive.
      transition = "cut";
    } else {
      transition = pickFrom(style.transitions, roll(), previous.transition);
    }

    var gesture;
    if (transition === "match") {
      // A match cut resolves into its own framing; a gesture on top of that
      // fights it. Hold, and let the arrival be the movement.
      gesture = "hold";
    } else if (role === "run") {
      // Bursts hold or snap. A drift inside a 0.1s shot is invisible motion
      // that costs the cut its edge — measured on the references as the thing
      // that most distinguishes them from what this program was making.
      gesture = roll() < 0.62 ? "hold" : "snap";
    } else if (role === "rest") {
      gesture = pickFrom(
        { press: 4, release: 3, fall: 2, hold: 2, swing: 1 }, roll(), previous.gesture
      );
    } else {
      gesture = pickFrom(style.gestures, roll(), previous.gesture);
    }

    // Grade accents. A frame or two that does not match the film's grade,
    // placed on an accent, is the difference between a look and a wash. Only
    // on accents, and only in the styles that have asked for it.
    var accent = null;
    if ((role === "accent" || role === "turn") && roll() < style.accents) {
      accent = roll() < 0.45 ? "blowout" : (roll() < 0.6 ? "crush" : "drain");
    }

    return {
      transition: transition,
      gesture: gesture,
      accent: accent,
      invert: transition === "flash" && roll() < style.invert
    };
  }

  /* How the grade accents change a shot's filter. Applied on top of the
   * film's look rather than replacing it, so an accented frame still belongs
   * to the same film. */
  var ACCENTS = {
    blowout: { label: "blown out", filter: "brightness(1.5) contrast(1.25) saturate(0.7)" },
    crush: { label: "crushed", filter: "brightness(0.62) contrast(1.7) saturate(1.2)" },
    drain: { label: "drained", filter: "saturate(0.12) contrast(1.35)" }
  };

  global.auteurStyle = {
    ROLES: ROLES,
    STYLES: STYLES,
    ACCENTS: ACCENTS,
    styleFor: styleFor,
    arrange: arrange,
    choices: choices,
    pickFrom: pickFrom
  };
})(window);
