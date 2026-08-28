"""Does the published page cut a montage at the pace the reels are cut at?

The app has two cutting engines, and only one of them is Python. A published
page has no server behind it, so `browser-render.js` carries its own copy of
the pace table — and a copy of a number drifts. It had: `montage` sat at 0.5s
against the app's 0.334s, and the no-pace-word fallback was still 0.9s, the
invented number the app itself had already stopped using. Somebody opening the
link got a different film from the same words.

A unit test now holds the two tables to each other, which catches the numbers
disagreeing. It cannot catch the number being right and the film still coming
out slow, because that lives in the painting rather than in the table. This
opens the real page in a real browser at a real phone viewport, makes two
films, and counts the cuts in the frames that came out.

    python3 tools/artifact/check_montage.py <page.html> <folder of photos>
"""

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from auteur.ffmpeg import probe  # noqa: E402

PAGE = Path(sys.argv[1]).resolve()
PHOTOS = [
    str(p)
    for p in sorted(Path(sys.argv[2]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
]
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.mkdtemp(prefix="auteur-mont-"))
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

#: What the thirteen montage reels in `templates.json` measure at, and what
#: `brief.py` now cuts a montage at. 10 / 0.334 overstates the rate — reels
#: also hold an opener — so the bar is the measured 20.1 cuts per ten seconds,
#: with room either side for a film made of eight photos rather than footage.
CORPUS_RATE = 20.1

RUNS = [
    ("montage", "a montage, cut at the pace of the reels, 12 seconds"),
    # The case nobody types: no pace word at all. This is the fallback, and it
    # is the one most films go through.
    ("default", "summer, 12 seconds"),
]


def frames_of(film: Path, into: Path, width: int = 96) -> np.ndarray:
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
            str(into / "%04d.png"),
        ],
        check=True,
    )
    shots = sorted(into.glob("*.png"))
    if not shots:
        return np.zeros((0, 1, 1), dtype=np.float32)
    return np.stack([np.asarray(Image.open(p).convert("L"), dtype=np.float32) for p in shots])


def cuts_in(frames: np.ndarray) -> list[int]:
    """One entry per join, counting a transition as the one join it is.

    A hard cut is a single frame of large difference. Every other join in the
    vocabulary — the portal, the carry, the whip, the slice, the luma dissolve
    — puts both pictures on screen for several frames, so the difference is
    spread across a run of them. Taking each above-floor frame as its own join
    counted a six-frame portal as three, which reported this page cutting 38.9
    times per ten seconds when it cuts 25.9. A run of change is one join, so
    the run is what gets counted.
    """
    if len(frames) < 3:
        return []
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    floor = max(6.0, float(np.median(diff)) * 3.0)
    runs: list[int] = []
    last = None
    for i, value in enumerate(diff):
        if value <= floor:
            continue
        # Two frames of quiet ends a run. Any less and a transition whose
        # middle frame dips below the floor splits into two joins.
        if last is None or i - last > 2:
            runs.append(i)
        last = i
    return runs


def run(page, prompt: str, name: str) -> Path:
    page.goto(PAGE.as_uri())
    page.set_input_files("#clips", PHOTOS)
    page.fill("#prompt", prompt)
    page.click("#go")
    page.wait_for_selector("#screen-done:not([hidden])", timeout=240000)
    url = page.eval_on_selector("#player", "el => el.src")
    raw = base64.b64decode(page.evaluate(READ_BLOB, url).split(",", 1)[1])
    film = OUT / f"{name}.webm"
    film.write_bytes(raw)
    return film


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
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
            film = run(page, prompt, name)
            frames = frames_of(film, OUT / f"frames-{name}")
            joins = cuts_in(frames)
            # The recorder writes at whatever rate it managed, so the film's
            # length has to be read rather than assumed. `ffprobe` is not on
            # PATH in this repository — `auteur.ffmpeg.probe` is the way in.
            seconds = float(probe(film)["format"]["duration"])
            rate = len(joins) / seconds * 10 if seconds else 0.0
            print(f"  {len(frames)} frames over {seconds:.2f}s")
            print(f"  {len(joins)} cuts measured = {rate:.1f} per ten seconds")
            print(f"  the corpus cuts a montage {CORPUS_RATE} times per ten seconds")

            # The bar either side. Wide enough that a film made of eight
            # photos is not held to a reel cut from footage, narrow enough to
            # catch the failure this exists for: the browser reverting to a
            # pace nobody measured. 0.9s a shot is 11 per ten seconds, so the
            # low bar sits above it; a hypercut is 60, so the high bar sits
            # well below that.
            if rate < CORPUS_RATE * 0.7:
                bad.append(
                    f"{name}: {rate:.1f} cuts per ten seconds against the corpus's "
                    f"{CORPUS_RATE} — the browser is cutting slower than the reels"
                )
            if rate > CORPUS_RATE * 1.5:
                bad.append(
                    f"{name}: {rate:.1f} per ten seconds against the corpus's "
                    f"{CORPUS_RATE} — that is closer to a hypercut than a montage"
                )

        browser.close()

    if problems:
        bad.append(f"the page threw: {problems[0]}")

    print("\n" + "=" * 66)
    if bad:
        for line in bad:
            print("  ✗", line)
        return 1
    print("  ✓ the published page cuts a montage at the pace the reels are cut at")
    print(f"  films and frames under {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
