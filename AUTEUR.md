# 🎬 auteur — an autonomous cinematic editor

Point it at a pile of unsorted clips, give it a sentence of direction, and it
returns a finished, graded, beat-cut, sound-designed short film.

```bash
python -m auteur edit ./rushes 'moody neon chase, 20 seconds, ends on "AFTER DARK"'
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

---

## Running it

### The shortest possible first run

No footage, no arguments, no API key. This makes its own practice clips and
edits them, so you can see the whole thing work before committing anything of
your own to it:

```bash
python -m auteur demo
```

### On your own clips

```bash
python -m auteur edit ./my-clips "fast neon montage, 20 seconds"
```

Point it at a folder, or at individual files, and say what you want in your own
words. Put the music in the same folder and it will find it, work out the tempo,
and cut to the beat. The prompt can be the last argument like that, or `-p`.

### On your phone

The clips are on the phone; the command line is on the computer. This serves the
same agent as a web app on your local network:

```bash
python -m auteur serve
```

It prints two addresses. Open the second one on your iPhone — both devices need
to be on the same wifi — then **Share → Add to Home Screen** to keep it as a
real app that opens full-screen with no browser chrome. Pick clips from the
camera roll, type what you want, and it renders on the computer and hands the
film back to the phone, where **Save to my phone** puts it in Photos through the
normal share sheet.

**In Chrome** — desktop or Android — the same page offers an **Install this as
an app** button, and installs as a normal PWA: own window, own icon, no tab
strip. One caveat that is Chrome's rule rather than this program's: Chrome only
offers installation on a *secure* origin. `http://localhost:8000` counts as
secure; a plain `http://192.168.…` LAN address does not. So the phone can always
*use* the page, but installing from Chrome means either opening it on the
computer itself or putting it behind HTTPS. The page detects which case it is in
and says so rather than showing a button that would do nothing.

| Option | |
|---|---|
| `--port` | Default 8000. Try another if something already holds that one. |
| `--host` | `0.0.0.0` (default) lets the phone reach it; `127.0.0.1` keeps it to this computer. |
| `--quality` | `draft` (default) keeps phone renders quick. |
| `--out` | Where uploads and finished films go. Default `./auteur-web`. |

Renders run one at a time, and finished jobs are swept after six hours.

### Every option

```bash
python -m auteur edit ./rushes ./b-roll ./music.mp3 \
    -p 'warm nostalgic summer, 30 seconds, "ONE LAST SUMMER"' \
    --shape vertical,square,widescreen \
    --quality best \
    --length 30 \
    --revisions 2
```

| Option | |
|---|---|
| `-p`, `--prompt` | The direction. Quoted phrases become on-screen text. Can also just be the last argument. |
| `-l`, `--length` | Target runtime in seconds. Also readable from the prompt ("20 seconds"). |
| `-s`, `--shape` | `vertical` (default), `square`, `widescreen`, `cinematic`, `portrait`. Comma-separate for several at once. |
| `--quality` | `draft`, `standard` (default), `best`. `best` adds optical-flow slow motion. |
| `--revisions` | How many times to watch it back and re-cut. Each round is a full re-render. Default 1. |
| `-o`, `--out` | Where everything lands. Default `./auteur-work`. |
| `--seed` | A different cut of the same brief. |
| `--details` | Also print the full shot list. |
| `--no-ai` | Never call Claude; use the built-in editor. |
| `-q`, `--quiet` | Print nothing but the finished path. |
| `-v`, `-vv` | Show what it is doing internally. |

```bash
python -m auteur analyse ./rushes   # what the agent sees in your footage
python -m auteur looks              # the film looks and transitions available
```

If you installed the package (`pip install -e .`), `auteur` works everywhere
`python -m auteur` does.

Everything lands in the working directory: the masters, `production-notes.md`
(what it saw, what it decided, what it fixed), `edl.json` for every pass, and
`analysis.json`.

### Do I need an API key?

No. Claude directs when `ANTHROPIC_API_KEY` is set, and a full algorithmic
director takes over when it isn't — the film always gets made either way. The
run says which one cut it.

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

**Nothing may fail quietly.** ffmpeg exits 0 for a filter graph that produced no
frames, so a shot can render to a valid, empty file and only explode later, deep
in the assembly, with an error naming neither the shot nor the cause. Every
segment is probed for picture before it is allowed into the assembly, and the
frame reader asks ffprobe for the height rather than inferring it from the byte
count — an inference that was ambiguous, and silently reported a 15-second film
as 107 seconds.

**The phone app carries no dependencies.** `auteur/web/` is the standard library
and nothing else: no Flask, no build step, no bundler, no CDN. The page is three
static files, the icons are drawn at startup rather than checked in, and the
front end is the same `Reporter` interface the terminal uses, pointed at a JSON
endpoint instead of stdout.

**One palette, in `auteur/theme.py`.** It is sampled from the material this was
built for — torchlit night photography, where a warm subject sits in a
near-black frame. The dominant clusters in that footage are a near-neutral black
ground, a cream-amber subject around hue 30–36, low-saturation forest green, and
a silver highlight; those are the roles. The stylesheet is generated from the
module at startup and contains no hex values of its own, the icons read the same
constants, and the terminal uses 24-bit escapes from them where it can. A test
asserts every text/background pair clears WCAG AA, so a palette change cannot
quietly make the primary button unreadable.

---

## Where the time goes

**Shots render in parallel.** Each is its own ffmpeg process writing its own
file, so they are independent by construction. The pool is sized at one process
per core, from measurement rather than assumption: on a 4-core box a 21-shot
reel took 74s sequentially, 58s with two workers, 54s with four, and 77s with
six. Past the core count every segment slows down and the batch finishes later
than it would have with fewer. Optical flow is excluded — `minterpolate` is
memory-hungry enough that several at once can push a laptop into swap.

**The page is cheap to hold open.** Text is gzipped (the stylesheet goes 7.5 KB →
2.5 KB), shell assets revalidate by ETag so a reload costs a 304, connections
are kept alive, and polling backs off from 1s to 5s while nothing is changing and
stops entirely when the page is hidden.

**Video is served by range.** Not an optimisation but a requirement: iOS Safari
opens a video with `Range: bytes=0-1` and will not play anything answered with a
plain 200 and the whole file.

---

## Testing

```bash
python -m pytest tests/ -q                 # everything, including a render
python -m pytest tests/ -q -m "not slow"   # ~75 tests, a few seconds
```

The suite synthesises its own footage, so it needs no fixtures on disk. The web
tests bind a real socket and exercise the routes as served, including the
`Range` requests iOS Safari uses to open a video.

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
- **The phone app is for your own network.** There is no login, no TLS and no
  rate limiting: anyone who can reach the port can post a render. It is meant to
  be your laptop and your phone on the same wifi, and `--host 127.0.0.1` keeps it
  to the one machine. Do not put it on a public address.
