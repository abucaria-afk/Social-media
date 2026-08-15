# 🎬 Social-media — auteur, an autonomous cinematic editor

![Python CI](https://github.com/abucaria-afk/Social-media/actions/workflows/python-ci.yml/badge.svg?branch=main)
![CodeQL](https://github.com/abucaria-afk/Social-media/actions/workflows/codeql.yml/badge.svg?branch=main)
![License](https://img.shields.io/github/license/abucaria-afk/Social-media)

Point it at a pile of unsorted clips, give it a sentence of direction, and it
returns a finished, graded, beat-cut, sound-designed short film — from the
command line, or from your phone.

> An earlier README described a C++ Social Media Manager. That project is not in
> this repository; what is here is the Python package **auteur** and a demo that
> synthesises its own test footage.

---

## Quick start

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt

python -m auteur demo                        # makes practice clips, then edits them
python -m auteur edit ./rushes 'moody neon chase, 20 seconds, "AFTER DARK"'
python -m auteur serve                       # then open the printed address on your phone

python -m auteur media scan ./rushes         # index your footage once
python -m auteur workflow run tiktok ./rushes 'harbour at dusk' --schedule next
python -m auteur schedule due                # what to post now

python -m auteur insight fit ./exports/*.csv # what your numbers say
python -m auteur workflow run tiktok ./rushes 'crate day' \
    --agents supervised --data ./exports/*.csv
```

`demo` needs no footage, no arguments and no API key: it synthesises clips and a
120 BPM track, edits them, and shows you the whole pipeline working.

`serve` puts the same agent behind a mobile web app you can add to the iPhone
home screen — or install from Chrome on desktop and Android. Pick clips from the
camera roll, say what you want, and save the finished film back to Photos. It
has its own sign-in with password reset, and a light/dark/automatic switch.

**Requirements:** Python 3.10+ and ffmpeg. A system ffmpeg works; the
`ffmpeg-binaries` wheel in `requirements.txt` is preferred because distro builds
sometimes ship without `libx264`, `xfade` or `loudnorm`.

---

## What it does

It measures every clip frame by frame — motion, camera move, focus, exposure,
colour, subject position — derives a beat grid from the music, cuts to it, grades
and matches the shots, mixes the sound, and then **watches its own output back
and re-cuts what it got wrong**. Claude directs when an API key is present; a
full algorithmic director takes over when there isn't one, so the film always
gets made.

See **[AUTEUR.md](AUTEUR.md)** for the full documentation: every command, how
the edit is planned, what the critic measures, and the limitations.

---

## Workflows

`edit` makes a film. A **workflow** makes a *post*: it cuts to the length the
surface accepts, keeps the titles out from under the app's own buttons, pulls a
cover frame that is not the first frame, drafts a caption inside the character
limit, and writes a `post.json` you or a scheduler can read.

| workflow | where it goes | shape | runtime |
| --- | --- | --- | --- |
| `instagram-reel` | Instagram Reels | 1080×1920 | 3–180s |
| `instagram-post` | Instagram feed | 1080×1350 | 3–60s |
| `instagram-story` | Instagram Stories | 1080×1920 | 1–60s |
| `tiktok` | TikTok | 1080×1920 | 3–600s |
| `tiktok-photo` | TikTok photo mode | 1080×1920 | 3–60s |
| `youtube-short` | YouTube Shorts | 1080×1920 | 1–180s |

Alongside them, `media` is a footage index — scan a folder once, and afterwards
only what changed is re-read; duplicates are found by content and confirmed byte
for byte before anything is called a copy. `schedule` is a queue with the two
rules that matter: a minimum gap between posts to the same service, and a
ceiling per day.

**Nothing posts for you.** There is no Instagram or TikTok API call anywhere in
this repository. A workflow produces a folder you can post from in a minute, and
the queue says which one is next.

---

## Insight and agents

`auteur insight` reads performance exports — eleven column shapes, recognised by
their headers — and reports what the posts that travelled had in common. Three
agents then work on the planned timeline before a frame renders, one objective
each:

| | measure | target | what the agent does |
| --- | --- | --- | --- |
| **hook** | `three_second_watch_rate` | > 0.80 | shortens the opening, lands the title before the first cut |
| **share** | `share_to_view_ratio` | > 0.05 | argues about runtime and pace, because shares grow out of completion |
| **loop** | `loop_count` | > 1.5 | removes end cards, returns the last shot to the opening frame |

Every proposal is applied to a copy and kept only if the overall prediction
improves, so an agent can be confidently wrong and lose nothing but a round.

**No mode lets an agent publish.** A gate with nobody to ask returns *no* rather
than assuming yes. Autonomy means an agent may restructure a cut without being
asked; it never means it may post one.

The scoring is deliberately suspicious of its own inputs: derived fields never
claim to have been measured, exports whose columns track each other too neatly
to be observations are down-weighted, disagreements between sources are surfaced
rather than averaged away, and a corpus with no failures in it is told it has
none. With no data at all it fits on simulated rows and says so every time.

`auteur serve` exposes all of this at **/studio** — pick a destination, see the
prediction and the retention curve, approve or reject each proposal.

---

## Development

```bash
pytest -q                    # the suite; synthesises its own footage
python tests/fuzz.py         # ten thousand randomised property checks
ruff check auteur tests      # lint

pip install pre-commit && pre-commit install
```

### Layout

```
auteur/            the package
  analysis/        what it sees in the footage
  director/        who decides the shots (Claude, or the built-in editor)
  craft/           grammar, motion, colour, transitions, sound, titles
  workflows/       platforms, the media index, post packaging, the queue
  insight/         performance schemas, the loader, the simulator, the scorer
  agents/          hook / share / loop agents, and the human approval gate
  web/             the phone app: stdlib-only server and static front end
  theme.py         the one palette, read by the app, the icons and the terminal
demo/              make_footage.py — synthetic clips for a first run
tests/             test_auteur.py (pytest) and fuzz.py (property campaign)
.github/workflows/ CI: tests, python-ci, lint, coverage, CodeQL, pip-audit
```
