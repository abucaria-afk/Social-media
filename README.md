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
  web/             the phone app: stdlib-only server and static front end
  theme.py         the one palette, read by the app, the icons and the terminal
demo/              make_footage.py — synthetic clips for a first run
tests/             test_auteur.py (pytest) and fuzz.py (property campaign)
.github/workflows/ CI: tests, python-ci, lint, coverage, CodeQL, pip-audit
```
