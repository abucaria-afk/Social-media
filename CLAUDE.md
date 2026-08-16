# Working in this repository

## Formatting: black, never `ruff format`

CI runs **both** `ruff check .` and `black --check .`. They are not the same
formatter and they disagree — running `ruff format` here reformats files black
then wants changed back, and the lint job goes red.

```
python3 -m black .        # the formatter CI checks
python3 -m ruff check .   # the linter CI checks
```

## Tests

```
python3 -m pytest -q
```

The whole suite is one file, `tests/test_auteur.py`, and it is expected to stay
green on every push. Tests are named as sentences describing the behaviour they
protect, not after the function they call.

## CodeQL

Code scanning runs on every pull request and has found real bugs the suite
missed — most notably six CLI commands that raised `TypeError` on their
*success* path, because nothing unit-tests a success message. Read its
findings rather than dismissing them; when one is a false positive, prefer
restructuring the code so the invariant is visible locally over ignoring it.

## Verifying

Claims about behaviour are checked by running the thing, not by reading it: a
real render and a look at the frames for anything visual, a real browser at a
real phone viewport for anything in `auteur/web/`. `ffprobe` is not on `PATH` —
use `auteur.ffmpeg.probe`.
