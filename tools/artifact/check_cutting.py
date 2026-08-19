"""Does the film actually make more than one move, and do the joins carry?

Three claims, all of which were false and none of which is visible from
reading the code:

1. **There is a transition at all.** Every cut in every film was a hard cut —
   `paint()` resolved the clock to exactly one shot and drew it, so no frame
   of any film ever had two pictures on it.
2. **The joins carry part of the outgoing picture onto the incoming one.**
   The portal, the carry, the whip, the slice and the luma dissolve all do;
   a hard cut and a match do not. At least some joins have to be the former
   or the feature is decorative.
3. **The shots do not all move the same way.** Every shot used to get the
   same linear zoom and the same drift, which is the "no personality"
   complaint stated as a number: one gesture, every shot, every film.

Measured off the rendered file and off the page's own tally, which are two
independent answers to the same question — the tally says what the edit
*planned*, the frames say what came out. Where they disagree the frames win
and something is wrong with the painting.

    python3 tools/artifact/check_cutting.py <page.html> <folder of photos>
"""

import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

PAGE = Path(sys.argv[1]).resolve()
PHOTOS = [
    str(p)
    for p in sorted(Path(sys.argv[2]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
]
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.mkdtemp(prefix="auteur-cut-"))
CHROME = "/opt/pw-browsers/chromium"

READ_BLOB = """
async (url) => {
  const blob = await (await fetch(url)).blob();
  return await new Promise((ok) => {
    const reader = new FileReader();
    reader.onload = () => ok(reader.result);
    reader.readAsDataURL(blob);
  });
}
"""

#: One prompt per style, so the run covers the whole vocabulary rather than
#: whichever corner of it the default lands in. A check that only ever
#: exercises one style cannot notice a style that draws nothing.
RUNS = [
    ("hypercut", "a hypercut, hard to the beat, 12 seconds"),
    ("hype", "gym energy, punchy, 12 seconds"),
    ("dreamy", "soft nostalgic summer memories, 12 seconds"),
    ("story", "a travel story, cinematic, 12 seconds"),
]


def frames_of(film: Path, into: Path, width: int = 96) -> np.ndarray:
    """Every frame of the film, small, as one array."""
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-i",
            str(film),
            "-vf",
            f"scale={width}:-1",
            "-vsync",
            "0",
            str(into / "%04d.png"),
        ],
        check=False,
    )
    shots = sorted(into.glob("*.png"))
    if not shots:
        return np.zeros((0, 1, 1))
    return np.stack([np.asarray(Image.open(s).convert("L"), dtype=np.float32) for s in shots])


def cuts_in(frames: np.ndarray) -> list[int]:
    """Frame indices where the picture changed hard enough to be a join."""
    if len(frames) < 3:
        return []
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    floor = max(6.0, float(np.median(diff)) * 3.0)
    found: list[int] = []
    for i, value in enumerate(diff):
        if value > floor and (not found or i - found[-1] > 1):
            found.append(i)
    return found


def gradual(frames: np.ndarray, cuts: list[int]) -> int:
    """How many joins take more than one frame to happen.

    A hard cut is one frame of large difference with quiet either side. A
    transition — any of them — spreads that difference over several frames,
    because for those frames both pictures are on screen. Counting the joins
    whose difference is *smeared* is therefore the frame-level answer to "is
    there a transition here at all", and it needs no knowledge of which
    transition was chosen.
    """
    if len(frames) < 3:
        return 0
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    spread = 0
    for at in cuts:
        near = diff[max(0, at - 3) : min(len(diff), at + 4)]
        if len(near) < 3:
            continue
        peak = float(near.max())
        if peak <= 0:
            continue
        # Neighbours carrying a real share of the peak means the change was
        # happening across several frames rather than between two of them.
        shoulders = sorted(float(v) for v in near)[-4:-1]
        if sum(shoulders) / 3 > peak * 0.30:
            spread += 1
    return spread


def run(page, name: str, prompt: str) -> dict:
    page.goto(PAGE.as_uri())
    page.set_input_files("#clips", PHOTOS)
    page.fill("#prompt", prompt)
    page.click("#go")
    page.wait_for_selector("#screen-done:not([hidden])", timeout=240000)
    url = page.eval_on_selector("#player", "el => el.src")
    raw = base64.b64decode(page.evaluate(READ_BLOB, url).split(",", 1)[1])
    film = OUT / f"{name}.webm"
    film.write_bytes(raw)
    # The page's own account of what it planned. `window.auteurLastEdit` is
    # set by the demo wiring when a film finishes.
    tally = page.evaluate("() => window.auteurLastEdit || null")
    return {"film": film, "tally": tally}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as play:
        browser = play.chromium.launch(
            executable_path=CHROME,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
        )
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        for name, prompt in RUNS:
            print(f"\n=== {name}: {prompt}")
            made = run(page, name, prompt)
            frames = frames_of(made["film"], OUT / f"frames-{name}")
            joins = cuts_in(frames)
            smeared = gradual(frames, joins)
            tally = made["tally"] or {}
            print(f"  {len(frames)} frames, {len(joins)} joins measured")
            print(f"  planned: {json.dumps(tally.get('transitions', {}))}")
            print(f"  gestures: {json.dumps(tally.get('gestures', {}))}")
            print(
                f"  carrying joins planned: {tally.get('carrying')}   "
                f"kinds: {tally.get('kinds')}   accents: {tally.get('accents')}"
            )
            print(
                f"  longest run of anything: {tally.get('longestRun')}   "
                f"of one loud move: {tally.get('longestLoudRun')}"
            )
            print(f"  joins that take more than one frame: {smeared}/{len(joins)}")
            results.append((name, tally, joins, smeared, frames))
        browser.close()

    print("\n" + "=" * 66)
    bad = 0
    for name, tally, joins, smeared, _frames in results:
        if not tally:
            print(f"FAIL {name}: the page reported no edit at all")
            bad += 1
            continue
        if tally.get("kinds", 0) < 3:
            print(f"FAIL {name}: only {tally.get('kinds')} transition kind(s) in the whole film")
            bad += 1
        if tally.get("moves", 0) < 2:
            print(f"FAIL {name}: only {tally.get('moves')} gesture(s) — every shot moves the same")
            bad += 1
        if tally.get("carrying", 0) < 1:
            print(f"FAIL {name}: no join carries part of one picture onto the next")
            bad += 1
        # A run of hard cuts is not a fault — the references hard-cut most of
        # their joins, and a stretch of them is exactly what leaves room for a
        # portal to mean something when it arrives. A run of the same *loud*
        # move is the fault. Counting both together, which the first version of
        # this check did, called a perfectly healthy hypercut broken.
        if tally.get("longestLoudRun", 99) > 3:
            print(
                f"FAIL {name}: {tally.get('longestLoudRun')} of the same "
                "non-cut transition in a row"
            )
            bad += 1
        if smeared < 1 and joins:
            print(
                f"FAIL {name}: every measured join happens between two frames — "
                "nothing is transitioning"
            )
            bad += 1
        if not bad:
            print(
                f"ok   {name}: {tally.get('kinds')} transition kinds, "
                f"{tally.get('moves')} gestures, {tally.get('carrying')} carrying joins, "
                f"{smeared} multi-frame joins"
            )
    if problems:
        print("\npage errors:", problems[:4])
        bad += 1
    print("=" * 66)
    print(OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
