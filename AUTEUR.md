# 🎬 auteur — an autonomous cinematic editor

Point it at a pile of unsorted clips, give it a sentence of direction, and it
returns a finished, graded, beat-cut, sound-designed short film.

```bash
auteur edit ./rushes --prompt 'moody neon chase, 20 seconds, ends on "AFTER DARK"'
```

```
« Moody Neon Chase, 20 Seconds »
19.98s · 28 shots · 1080x1920 @ 30fps · look: neon
  montage on a hook-drop arc; 28 shots averaging 0.71s, cut to 120 BPM

  1.   0.00–  0.50  C04 [3.88–4.51] ramp 1.08x→2.05x  — close/push-in @e0.88
  2.   0.50–  1.00  C01 [4.53–5.19] ramp 0.79x→3.35x  — medium/push-in @e0.88
  3.   1.00–  1.50  C05 [0.53–1.22] 1.20x (whip-left 0.20s)  — close/push-in @e0.85
  ...
```

It is built around a simple claim: **most of what separates an amateur montage
from a professional one is measurable.** Cuts landing on the beat, exposure
matched across a join, shot lengths that vary with the energy of the section,
the strongest frame first — none of that is taste, it is craft, and craft can be
automated. What is left over — which frames carry the idea, what order reveals
it — is where a model earns its place.

---

## How it works

```
ingest → analyse → direct → craft → render → critique → re-cut
                     ↑                                      │
                     └──────────────────────────────────────┘
```

| Stage | What happens |
|---|---|
| **Ingest** | Any container, any orientation, any frame rate. Rotation metadata is resolved so a phone clip is analysed the way it will be watched. |
| **Analyse** | Every clip is decoded to a small proxy and measured frame by frame: motion, camera movement (pan / tilt / push-in, fitted by optical flow), focus, exposure, clipping, colour, subject position, and the cuts the clip already contains. Audio gets an RMS envelope, spectral-flux onsets, tempo, and a **beat grid with phase**. |
| **Direct** | The brief is parsed into pace, energy shape, palette and runtime. Claude picks the shots when an API key is present — otherwise a full algorithmic director does, and the film still gets made. |
| **Craft** | Film grammar is enforced on whatever the director wrote: beat snapping, no clip cutting back to itself, transition density capped, hook guaranteed, J/L cuts on dialogue. |
| **Render** | Each shot is conformed to its own segment, then assembled with transitions, grade, grain, type and a mixed soundtrack. |
| **Critique** | The finished file is played back through the same analysis and scored. Dead air, flash frames, exposure jumps across cuts, cuts drifting off the grid, a weak opening — each becomes a revision, and the film is cut again. |

---

## Installation

```bash
pip install numpy pillow ffmpeg-binaries   # ffmpeg-binaries ships ffmpeg + ffprobe
pip install anthropic                      # optional: lets Claude direct
```

A system ffmpeg works too. Discovery order is `$AUTEUR_FFMPEG` → a wheel-bundled
static build → `PATH` → `imageio-ffmpeg`. Wheel builds are preferred because
distro packages sometimes ship without `libx264`, `xfade` or `loudnorm`.

Try it with no footage of your own:

```bash
python demo/make_footage.py ./rushes      # synthesises clips + a 120 BPM track
auteur edit ./rushes --prompt "fast neon montage, 15 seconds"
```

---

## Usage

```bash
auteur edit ./rushes ./b-roll ./music.mp3 \
    --prompt 'warm nostalgic summer, 30 seconds, "ONE LAST SUMMER"' \
    --format reel,square,wide \
    --quality master \
    --duration 30 \
    --rounds 2
```

| Option | |
|---|---|
| `--prompt` | The direction. Quoted phrases become on-screen text. |
| `--duration` | Target runtime. Also readable from the prompt ("20 seconds"). |
| `--format` | `reel` (9:16), `square`, `wide`, `cinema` (2.35:1), `portrait` (4:5), or `1080x1920`. Comma-separated for several. |
| `--quality` | `draft`, `standard`, `master`. `master` adds optical-flow slow motion. |
| `--rounds` | How many times to watch it back and re-cut. Each round is a full re-render. |
| `--seed` | A different cut of the same brief. |
| `--no-llm` | Algorithmic director only. |

```bash
auteur analyse ./rushes      # what the agent sees in your footage
auteur looks                 # the film emulations and transitions available
```

Everything lands in the working directory: the masters, `production-notes.md`
(what it saw, what it decided, what it fixed), `edl.json` for every pass, and
`analysis.json`.

### As a library

```python
from auteur.agent import direct

production = direct(["./rushes"], 'cinematic travel montage, 25 seconds')

print(production.edl.describe())        # the timeline, shot by shot
print(production.final_critique)        # what it thought of its own work
print(production.primary)               # the finished file
```

---

## What it actually does to the picture

**Beat-cut, not beat-adjacent.** Tempo comes from autocorrelating a spectral-flux
onset envelope; phase comes from sliding a pulse train across that envelope until
it fits. Shot lengths are then chosen from *musical multiples* — one beat, two,
four — biased by the energy curve. Quantising to the nearest beat instead would
make every shot exactly one beat long, which is its own kind of monotony.

**Shot matching before grading.** Every shot is nudged toward a shared exposure
and white balance before the look is applied. This is the least visible part of
the whole system and the reason a bin of clips shot on different days reads as
one film instead of a collage.

**Subject-aware reframing.** Horizontal footage delivered vertical is cropped
around the measured subject track, not the middle of the frame.

**Speed ramps that keep time.** A ramp is realised by slicing the shot into short
constant-speed pieces, so screen time stays exact and the next cut still lands on
its beat. Below 0.5× at `master` quality, optical flow synthesises the missing
frames instead of repeating them.

**Motion-blurred whip transitions.** Written as `xfade` custom expressions —
a directional slide with a three-tap smear that peaks mid-transition. Support is
probed once against the actual binary; if the build refuses, the nearest built-in
is used and nothing downstream notices.

**Typography rendered properly.** `drawtext` cannot letter-space, wrap, or reveal
a line one word at a time, so type is drawn with Pillow into RGBA plates and
composited. That is what makes kinetic captions possible.

**Sound designed, not sampled.** Whooshes, impacts, risers and sub-drops are
synthesised — a state-variable filter sweeping over noise, a pitch-dropping sine
with a click transient. Music is ducked under dialogue by sidechain compression
and the whole mix is normalised to −14 LUFS, which is what the platforms expect.

---

## The critic

The second viewing is what makes this an agent rather than a script.

```
critic score 0.81
  · [metronomic] only 2 distinct shot lengths in beats
  · [weak-hook @0.00s] the opening is quieter than the rest of the film

re-cutting: opened on C04 instead — it has more life in it;
            broke up the metronomic cutting
```

| Rule | Fires when | Fix |
|---|---|---|
| `dead-air` | ≥1.6s where the picture is effectively frozen | drop the shot |
| `flash-frame` | a shot too short to register | lengthen it |
| `exposure` | brightness jumps hard across a cut | flagged for the grade |
| `off-beat` | fewer than 55% of cuts land on the grid | re-snap |
| `metronomic` | too few distinct shot lengths *in beats* | vary the pacing |
| `weak-hook` | the opening is quieter than the rest | promote a livelier shot |
| `runtime` | drifted off the target | trim from the middle |
| `black` | too much of the film is near black | flagged |

---

## Design notes

**The model is never trusted.** Whatever comes back is clamped to footage that
actually exists, then run through the grammar passes. A director asking for
frames past the end of a clip produces a legal edit, not a crash. If the model is
unreachable, refuses, or returns nothing usable, the algorithmic director takes
over and the film still gets made.

**Structured outputs, not JSON parsing.** The EDL schema is enforced by the API,
so there is no prose to strip and no repair to attempt.

**Every extra format is a full re-render**, not a crop of the master — the
reframe has to see the original footage to know what to keep.

**Determinism.** Same seed, brief and footage produce the same cut.

---

## Testing

```bash
python -m pytest tests/ -q                 # everything, including a render
python -m pytest tests/ -q -m "not slow"   # 54 tests, ~4 seconds
```

The suite synthesises its own footage, so it needs no fixtures on disk.

---

## Limitations

- **No semantic understanding without a model.** The algorithmic director knows a
  shot is sharp, moving and well exposed; it does not know it contains a face, a
  logo, or the moment the story turns. Shot size is estimated from detail density
  and is a proxy, not a depth estimate.
- **Speech is detected, not transcribed.** There is a speech-likelihood measure
  used for ducking and music selection, but no ASR, so there are no automatic
  subtitles and no cutting to what was said.
- **Rendering is CPU-bound.** A 15-second reel is roughly two minutes at draft
  quality; `master` with optical flow is considerably slower. No hardware encoder
  is used.
- **The critic measures, it does not watch.** It can tell that a shot is frozen or
  that the film is off the beat. It cannot tell that the edit is boring.
