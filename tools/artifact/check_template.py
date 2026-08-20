"""Does a film cut to a template actually land where the template cuts?

The claim is specific and worth checking rather than trusting: choosing a
reference reel should give the reference reel's *rhythm* applied to your own
photographs. Not its ratios stretched to fit — its actual durations, because
a rhythm stretched to fit is a different rhythm, and the number a person
means by "cut it like that" is 0.167 seconds a shot.

So this renders with a template chosen, measures where the cuts fell in the
file that came out, and compares that to the timeline the template carries.
Nearest-neighbour matching, not index-by-index: one missed or one extra cut
shifts every later pairing and reports a film that is nearly perfect as
wildly wrong — a fault this repository has already made once.

    python3 tools/artifact/check_template.py <page.html> <folder of photos>
"""

import base64
import json
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
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.mkdtemp(prefix="auteur-tpl-"))
CHROME = "/opt/pw-browsers/chromium"
TEMPLATES = json.loads((Path(__file__).parent / "templates.json").read_text())

#: How far a cut may land from where the template puts it. Generous, and it
#: has to be: the browser renders in wall time on a software rasteriser, so
#: every shot carries the jitter of whatever the paint loop managed.
SLACK = 0.10
SECONDS = 10.0

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


def cuts_of(film: Path, into: Path) -> tuple[list[float], float]:
    """When the picture changed, in seconds, and the film's length."""
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(film), "-vf", "scale=96:-1,fps=30",
         "-vsync", "0", str(into / "%04d.png")],
        check=False,
    )
    shots = sorted(into.glob("*.png"))
    if len(shots) < 4:
        return [], 0.0
    frames = np.stack(
        [np.asarray(Image.open(s).convert("L"), dtype=np.float32) for s in shots]
    )
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    floor = max(6.0, float(np.median(diff)) * 3.0)
    found: list[float] = []
    for i, value in enumerate(diff):
        if value > floor and (not found or (i + 1) / 30 - found[-1] > 0.05):
            found.append((i + 1) / 30)
    return found, len(shots) / 30


def wanted_cuts(template: dict, seconds: float) -> list[float]:
    """Where the template puts its cuts, cycled to fill the film."""
    at, out = 0.0, []
    i = 0
    while at < seconds:
        at += max(0.05, template["beats"][i % len(template["beats"])][0])
        if at < seconds:
            out.append(at)
        i += 1
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    bad = 0
    with sync_playwright() as play:
        browser = play.chromium.launch(
            executable_path=CHROME, args=["--autoplay-policy=no-user-gesture-required"]
        )
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(str(e)))

        for template in TEMPLATES:
            page.goto(PAGE.as_uri())
            page.set_input_files("#clips", PHOTOS)
            page.fill("#prompt", f"a montage, {SECONDS:.0f} seconds")
            page.click(f'#template .choice[data-value="{template["id"]}"]')
            page.click("#go")
            page.wait_for_selector("#screen-done:not([hidden])", timeout=240000)
            heard = page.inner_text("#heard")
            url = page.eval_on_selector("#player", "el => el.src")
            raw = base64.b64decode(page.evaluate(READ_BLOB, url).split(",", 1)[1])
            film = OUT / f"{template['label']}.webm"
            film.write_bytes(raw)

            got, ran = cuts_of(film, OUT / f"frames-{template['label']}")
            want = wanted_cuts(template, ran)
            if not got or not want:
                print(f"FAIL {template['label']}: no cuts measured")
                bad += 1
                continue
            # Measured from the template's cuts outward: for each place the
            # template cuts, is there a cut in the film near it?
            #
            # The other direction is the wrong question and answering it
            # failed two healthy templates. A transition is extra visual
            # change on purpose — a portal or a whip spreads its difference
            # over several frames and a frame-difference detector reports the
            # start and the end of it — so a film with transitions in it will
            # always show more edges than it has cuts. Penalising that is
            # penalising the feature.
            drift = [min(abs(w - g) for g in got) for w in want]
            near = sum(1 for d in drift if d <= SLACK)
            median = float(np.median(drift))
            print(f"{template['label']:9s} template median {template['hold']:.3f}s  "
                  f"film {ran:.1f}s  {len(want)} template cuts, {len(got)} edges in the film  "
                  f"median drift {median * 1000:.0f}ms  {near}/{len(want)} within "
                  f"{SLACK * 1000:.0f}ms")
            print(f"          heard: {heard[:120]}")
            if median > SLACK:
                print(f"FAIL {template['label']}: the cutting does not follow the template")
                bad += 1
            if template["label"] not in heard:
                print(f"FAIL {template['label']}: the page never said which template it used")
                bad += 1
        browser.close()
    if problems:
        print("page errors:", problems[:4])
        bad += 1
    print(OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
