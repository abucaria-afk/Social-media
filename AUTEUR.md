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

### Signing in

The first run creates one account and prints its username. Everything behind
the sign-in page is closed without it — including finished films and production
notes, which are your own footage.

- **Forgot your password?** The sign-in page has a link. It asks for your
  username or email and sends a link that works once, for 30 minutes. With no
  mail server configured the link is printed in the window where `serve` is
  running, which for a tool on your own machine is the right default: whoever
  can read that console is the person who owns the account. To send real email
  instead, set `AUTEUR_SMTP_HOST`, `AUTEUR_SMTP_PORT`, `AUTEUR_SMTP_USER`,
  `AUTEUR_SMTP_PASSWORD` and `AUTEUR_SMTP_FROM`.
- Five wrong passwords lock the account for fifteen minutes.
- Changing a password signs every other device out.
- The reply to "I forgot my password" is identical whether or not the account
  exists, so the page cannot be used to discover which addresses have one.
- The reset link is built from the server's own address, never from the
  request's `Host` header. Behind a proxy, set `AUTEUR_PUBLIC_URL` to name the
  address yourself.
- Films and production notes belong to the account that made them. Being signed
  in is not permission to read somebody else's footage.

```bash
python -m auteur account            # who can sign in
python -m auteur account password   # change one (asks, never echoes)
python -m auteur account add        # another person
```

These take effect immediately — the server re-reads the file when it changes,
so there is no restart and no window where the old password still works.

Passwords are stored as salted scrypt hashes (n=2¹⁵, ~0.1s and 32MB a guess) in
`<serve folder>/accounts.json` — outside the repository, and gitignored. Session
tokens are stored hashed too, so a copy of that file cannot be replayed.

#### The first password

There isn't one in the repository. The first `serve` against an empty folder
mints a random password, creates the account with it, and prints it once:

```
     Sign in as        streetlightseason
     Password          cobalt-swallow-amber-kestrel-marram-5567

     That password was generated just now and is shown once.
     Change it when you are in:  python -m auteur account password
```

Five words drawn from a 64-word list plus four digits — about 43 bits, which is
past reach online (five wrong answers locks the account for fifteen minutes) and
thousands of years of work offline against scrypt. Copy it, sign in, change it.
It exists only in that console and in the hash, so if it scrolls away before you
read it, delete `accounts.json` and start the server again.

To choose your own and have nothing generated at all:

```bash
AUTEUR_USERNAME=me AUTEUR_EMAIL=me@example.com AUTEUR_PASSWORD=... python -m auteur serve
```

A password has to be at least 12 characters, off the usual guessing lists, made
of more than a handful of distinct characters, and not built out of your own
username or email. Four unrelated words in a row satisfies all four and is
easier to type on a phone than anything clever.

### Light, dark, or whatever the phone is doing

The bottom of every screen has an **Appearance** switch: *Automatic*, *Light*,
*Dark*. Automatic is the default and follows the phone's own setting; the other
two override it and are remembered. The choice is applied by a small script in
`<head>` before the stylesheet loads, so the page never flashes the wrong theme
on the way in.

Both palettes come from the same photographs — the dark one from the shadow, the
light one from the torch-lit side — so switching changes the exposure rather than
the identity. A test asserts every text/background pair in *both* clears WCAG AA.

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
python -m auteur workflow list      # the places it can cut a post for
python -m auteur media scan ./rushes
python -m auteur schedule
```

If you installed the package (`pip install -e .`), `auteur` works everywhere
`python -m auteur` does.

Everything lands in the working directory: the masters, `production-notes.md`
(what it saw, what it decided, what it fixed), `edl.json` for every pass, and
`analysis.json`.

## Workflows: making a post, not just a film

`edit` gives you a film. Posting it is a separate job, and it is the boring
half: cut it to a length the surface accepts, keep the words out from under the
app's own interface, choose a cover frame, write a caption that fits, and note
down what you made so you can find it next week. A **workflow** is that whole
run, named after where it is going.

```bash
python -m auteur workflow list
python -m auteur workflow run instagram-reel ./rushes "harbour at dusk"
python -m auteur workflow run tiktok ./rushes "harbour at dusk" --schedule next
python -m auteur workflow run youtube-short ./rushes "how it was built" -l 45
```

| Workflow | Destination | Frame | Runtime | Caption |
|---|---|---|---|---|
| `instagram-reel` | Instagram Reels | 1080×1920 | 3–180s (aim 25) | 2200 chars, 30 tags |
| `instagram-post` | Instagram feed | 1080×1350 | 3–60s (aim 20) | 2200 chars, 30 tags |
| `instagram-story` | Instagram Stories | 1080×1920 | 1–60s (aim 15) | on-frame only |
| `tiktok` | TikTok | 1080×1920 | 3–600s (aim 25) | 2200 chars, 20 tags |
| `tiktok-photo` | TikTok photo mode | 1080×1920 | 3–60s (aim 12) | 2200 chars, 20 tags |
| `youtube-short` | YouTube Shorts | 1080×1920 | 1–180s (aim 30) | 5000 chars, 15 tags |

Aliases work: `reel`, `tiktok`, `shorts`, `story`, `feed`.

**What a workflow adds over `edit`:**

- **Safe areas.** Every surface draws its own buttons over the frame — TikTok
  covers the bottom 22% with a caption block and the right 16% with the action
  rail. The director places titles for the composition and cannot know that, so
  a plan hook pulls each title inside the readable box before anything renders.
  It only ever moves text *inward*; a title already in a safe place is left
  exactly where the director put it.
- **Length, in the right order.** A `--length` flag beats the prompt, the prompt
  beats the platform's house length, and the platform's hard limits beat all
  three. Asking for 12 seconds and being handed 25 because Reels prefers 25 is
  the tool overruling you, which it has no business doing while 12 seconds is a
  legal length for a Reel.
- **A cover frame that is not the first frame.** The first frame of a cut is the
  least representative one in it and is often a fade-up from black — which is
  why so many posts have a black thumbnail. It takes one a fifth of the way in.
- **A caption to rewrite.** Assembled by rules from the brief and what the edit
  turned out to be, trimmed to the character limit here rather than by the
  platform, which truncates mid-word and does not say so. It is a first line, and
  it says so. Alt text is written too.
- **A check against the platform.** The render is probed, not trusted: a runtime
  the critic was happy with can still be under TikTok's three-second floor. A
  mismatch is a warning printed next to the file, not an exception — the film
  exists either way.

Each run leaves a `post/` folder: the video, `cover.jpg`, `caption.txt`,
`alt-text.txt` and `post.json`.

**Nothing here posts anything.** There is no Instagram or TikTok API call in this
repository. Posting for you needs your credentials, and this runs on a laptop on
a home wifi.

> The platform numbers live in `auteur/workflows/platforms.py` with the date they
> were last checked, and they will go stale. Nothing fetches them at runtime; a
> video rejected as too long is the first sign one has moved. Treat them as a
> default to correct.

### The media manager

Pointing the editor at a folder works until there is more than a folder.

```bash
python -m auteur media scan ./footage       # index it, once
python -m auteur media list --kind video    # what is known
python -m auteur media duplicates           # the same clip, saved twice
python -m auteur media tag ./footage/a.mp4 --label keepers
```

The second scan of a folder only looks at what changed — size and modification
time decide that — so it costs a second rather than a minute. Duplicates are
found by **content**, not by name: a fingerprint of the file's size and both
ends, then a full byte comparison before anything is called a copy, because
telling somebody to delete footage on the strength of a partial hash would be
careless. The older file is reported as the original; the newer one is the copy.

Nothing is ever deleted. The index is one JSON file beside your media, and
deleting it costs a rescan and nothing else.

### Scheduling

```bash
python -m auteur workflow run tiktok ./rushes "harbour" --schedule "2026-08-20 18:00"
python -m auteur workflow run tiktok ./rushes "harbour" --schedule next

python -m auteur schedule            # what is queued
python -m auteur schedule due        # what should go out now
python -m auteur schedule done 4f2a1c9d
python -m auteur schedule export > posts.csv
```

Two rules, both adjustable (`--gap`, `--per-day`): a minimum gap between posts to
the same service (4 hours), and a ceiling per service per rolling day (3).
Everything else about posting times is folklore that changes by audience, so it
is not baked in. `--schedule next` takes the first slot the rules allow; an
explicit time that breaks a rule is refused with the reason, rather than queued
quietly.

Times are stored as UTC and printed in your own timezone. A bare `18:00` means
six in the evening where you are, not in Greenwich — a queue that assumed
otherwise would post at four in the morning.

## Insight: what your numbers say

`auteur insight` reads performance exports and reports what the posts that
travelled have in common. Eleven column shapes are recognised by their headers
rather than their filenames — short-form video, carousels, threads, film /
colour / music theory, algorithmic buckets, a multimodal matrix, and emulated
metadata:

```bash
auteur insight fit ./exports/*.csv       # what the winners have in common
auteur insight fit --json                # the same, machine-readable
auteur insight simulate --rows 5000 -o practice.csv
```

Three objectives, taken from the brief and used everywhere:

| | measure | target |
|---|---|---|
| **hook** | `three_second_watch_rate` | > 0.80 |
| **share** | `share_to_view_ratio` | > 0.05 |
| **loop** | `loop_count` | > 1.5 |

Underneath them are the three drivers: **velocity** (what happens in the first
ten minutes, not the first day), **retention** (the second attention stops, not
the completion rate that summarises it), and **amplification** (shares, saves,
reposts and audio re-use, weighted far above likes — a like does not move a post
and a share does).

### What it refuses to pretend

Fitting a model to performance data is easy to do dishonestly, so this reports
the ways it could be wrong before it reports the answer:

- **Derived fields never claim to have been measured.** Most exports have no
  three-second column, so one is inferred — from completion, from swipe-through,
  from stop-scroll. `Signal.has()` says which numbers were observed, and
  `derived_from` records what each inference came from. That provenance is what
  stops a correlation being run between a number and itself: the first version
  reported *stop-scroll → three-second watch, r = 1.00* as its strongest
  finding, which was a number correlated with its own source.
- **Exports that look generated are down-weighted to a tenth.** Real
  performance data is noisy; contrast ratio and tempo are set by different
  people on different days and do not track each other. A file where the median
  correlation between unrelated columns is above 0.95 is a curve somebody drew.
  It is still a useful target and it must not outvote a smaller observed file.
- **Disagreements are surfaced, not averaged.** When two exports disagree about
  the *direction* of an effect, that is the most useful thing in the corpus.
- **Implausible magnitudes are called out.** A corpus where the median post is
  watched to completion has no drop-off anywhere in it, so nothing in it can
  teach an agent about pacing, whatever its correlations say.
- **A corpus of nothing but winners says so.** Without a single stalled or
  killed post it can describe what success looks like and not what separates it
  from anything else.

With no exports at all it will fit on simulated rows so the machinery can be
rehearsed — and it says so in every report. Do not quote those numbers as
evidence about a platform.

## Agents

Three agents, one objective each, run against the planned timeline before a
frame is rendered:

```bash
auteur workflow run tiktok ./clips "crate day" --agents supervised --data ./exports/*.csv
```

| mode | what it means |
|---|---|
| `off` | no agents (default) |
| `manual` | every proposal waits for you |
| `supervised` | small changes apply themselves, structural ones ask |
| `autonomous` | editing rounds run uninterrupted |

**The hook agent** shortens the opening to where the winners cut, lands a title
before the first cut, and will argue for opening on the best shot rather than
saving it. **The share agent** argues about runtime and pace, because a share
grows out of completion and completion falls with length. **The loop agent**
removes end cards, returns the last shot to the opening frame, and shortens the
tail — an ending that resolves is an ending people leave at.

Each proposal is a real operation on the timeline plus a sentence of reasoning.
The crew applies each one to a *copy*, scores it, and keeps it only if the
overall prediction improved — so an agent can be confidently wrong and the worst
it costs is a round.

### The gate

**No mode lets an agent publish.** `Gate.may_publish` requires a person in every
mode including `autonomous`, and a gate with nobody to ask returns no rather
than assuming yes — a gate that approves when unattended is not a gate, it is a
delay. Autonomy here means an agent may restructure a cut without being asked.
It does not mean it may post one.

Safe areas still win: the agents move titles to win the first three seconds, and
the platform's safe area gets the last word on where a title may actually sit.

## Studio

`auteur serve` now has a second page at **/studio**: pick a destination, see
what your data says, plan a cut, and approve or reject each proposal with the
predicted gain next to it. The retention curve is drawn with the steepest
drop-off marked. Point it at your exports with `AUTEUR_EXPORTS=./exports`.

Same palette as the films — every colour is a variable generated from
`auteur/theme.py`, so the interface cannot drift away from what it produces.

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

**Authentication fails closed.** A missing account store denies every request
rather than admitting them. The other direction — treating "auth is not set up"
as "everyone is allowed" — turns one missing line of start-up into a server
quietly handing out the user's footage, with nothing in the log to say so.

**One bad clip costs one shot, not the film.** A shot whose source window holds
no frames — a fraction of a second of low-frame-rate footage, say — used to take
the whole render down. It is dropped now, named in plain words, and the timeline
is rebuilt around the gap. A folder of zero-byte files, truncated containers,
text renamed to .mp4, 4K, 16x16 and 1fps clips still produces a film.

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

```bash
python tests/fuzz.py            # ten thousand randomised cases
```

Separate from the suite, and deliberately: it checks *properties* rather than
examples, and its job is to find the next thing worth a named test. Ten
thousand cases put roughly 314,000 assertions through the EDL repairer, the
ramp maths, the grammar passes, brief parsing, the upload parser, the static
route and the account store. Everything it has caught has a test in
`test_auteur.py` — a runtime that was only range-checked on one of its two
routes in, a transition cap that rounded to nearest instead of down, and a
static path that resolved one folder up.

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
- **The phone app is for your own network.** It has a sign-in, hashed passwords
  and a lockout, but no TLS: on a plain `http://` LAN address the password and
  the session cookie cross the wifi in the clear. That is fine for your own
  network and not fine for a public one. `--host 127.0.0.1` keeps it to the one
  machine; anything wider should sit behind a reverse proxy with a certificate.
- **A password that was once in git history stays in git history.** Nothing in
  the working tree carries credential material any more, but earlier commits
  shipped a scrypt hash for the seeded account. Anything that was ever that
  password should be considered burnt and never reused.
- **Workflows do not post.** They make the folder and the queue; a person or
  another tool does the posting. Adding uploads would mean holding platform
  credentials, which is a different project with a different threat model.
- **The platform rules are a snapshot, not a feed.** Frame sizes, runtime
  ceilings, caption limits and safe areas are written down in
  `auteur/workflows/platforms.py` with the date they were checked. They change,
  nothing here notices, and a rejected upload is how you find out.
- **The safe areas are approximate.** TikTok's caption block is taller when the
  caption is longer, and every one of these apps has redesigned its player at
  least once. The insets err generous: a slightly tighter composition costs
  less than a title nobody can read.
- **Captions are assembled, not written.** No model is involved in drafting one
  even when an API key is present. It is a first line to rewrite, and both the
  file and the CLI say so.
- **A virality score is a prediction, not a forecast.** It is read entirely off
  the timeline — shot lengths, where the titles land, how the first and last
  frames relate — because that is what an agent can change. It knows nothing
  about whether the footage is any good, what the subject is, or who follows
  you, and those decide more than any of this does.
- **An agent optimises the number it was given.** All three will happily produce
  something that scores well and is not what you meant. That is what the gate is
  for, and it is why no mode lets one publish.
- **The music bed is synthesised, not licensed.** `demo/make_track.py` writes an
  original instrumental in a named style, because a tool that downloads the song
  everybody is using and bakes it into your upload is handing you a copyright
  strike. Cut against the bed, then swap the real track in inside the app when
  you post — which is where trending audio is licensed anyway, and where the
  platform gives a post distribution weight for using it.
- **Simulated metrics teach the simulator.** With no real export the model fits
  on invented rows. Everything downstream still works, and none of it is
  evidence. The same applies to any supplied corpus with no failures in it.
