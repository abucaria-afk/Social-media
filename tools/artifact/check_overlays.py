"""Do the graphics chosen on the animation tab actually land on the film?

Renders the same photographs twice — once with the overlays off, once with
them busy — and counts the pixels that could only have come from a drawn
shape. A tab that changes a setting nothing reads is the failure mode being
ruled out here, and it is invisible from the tab itself.

    python3 tools/artifact/check_overlays.py <page.html> <folder of photos>
"""

import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PAGE = Path(sys.argv[1]).resolve()
PHOTOS = [
    str(p)
    for p in sorted(Path(sys.argv[2]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
]
HERE = Path(tempfile.mkdtemp(prefix="auteur-overlays-"))
CHROME = "/opt/pw-browsers/chromium"

#: A hypercut, so there are many cuts to hang graphics on, and neon, whose ink
#: is cyan. Black and white was the first choice and it cannot work: a black
#: and white film's ink is *white*, and the grade blows its own highlights, so
#: 79% of frames carried near-white pixels with the graphics switched off.
#: Cyan appears nowhere in this footage, so it can only have been drawn.
PROMPT = "a neon hypercut, 8 seconds"

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


def render(browser, choice):
    page = browser.new_page(
        viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
    )
    page.add_init_script(
        "localStorage.setItem('auteur-overlays', " + json.dumps(json.dumps(choice)) + ")"
    )
    page.goto(PAGE.as_uri())
    page.set_input_files("#clips", PHOTOS)
    page.fill("#prompt", PROMPT)
    page.click("#go")
    page.wait_for_selector("#screen-done:not([hidden])", timeout=180000)
    url = page.eval_on_selector("#player", "el => el.src")
    raw = base64.b64decode(page.evaluate(READ_BLOB, url).split(",", 1)[1])
    # What the renderer itself says it placed. Reported alongside the pixels
    # rather than instead of them: a counter is the renderer marking its own
    # homework, and the pixels are the film.
    drawn = page.evaluate("window.__lastFilm ? window.__lastFilm.graphics : -1")
    page.close()
    return raw, drawn


def marked(film):
    """Frames, and how many pixels in them are the ink's cyan.

    Bright and blue-dominant: `#7ef0ff` and nothing in a photograph of
    woodland. Two earlier versions of this measured the wrong thing — pixels
    that are merely coloured, on a film graded to have no colour, and then
    pixels that are merely bright, on a grade that blows its highlights.
    """
    out = HERE / "frames"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    path = HERE / "film.webm"
    path.write_bytes(film)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-i",
            str(path),
            "-vf",
            "scale=120:-1",
            "-vsync",
            "0",
            str(out / "%04d.png"),
        ]
    )
    frames = sorted(out.glob("*.png"))
    touched = 0
    total = 0
    for shot in frames:
        pixels = list(Image.open(shot).convert("RGB").getdata())
        inked = sum(1 for r, g, b in pixels if b > 170 and b - r > 55 and g > r + 25)
        total += inked
        if inked > 12:
            touched += 1
    return len(frames), touched, total


def main():
    with sync_playwright() as play:
        browser = play.chromium.launch(
            executable_path=CHROME, args=["--autoplay-policy=no-user-gesture-required"]
        )
        results = {}
        for label, choice in (
            ("off", {"kinds": ["circle", "bracket", "burst"], "move": "pop", "density": "off"}),
            (
                "busy",
                {
                    "kinds": ["circle", "bracket", "burst", "arrow", "underline"],
                    "move": "pop",
                    "density": "busy",
                },
            ),
        ):
            raw, drawn = render(browser, choice)
            frames, touched, total = marked(raw)
            results[label] = (frames, touched, total)
            print(
                f"{label:5s}: {frames} frames, the renderer says it placed {drawn} "
                f"graphics; {touched} frames carry cyan ink "
                f"({total} pixels in all)"
            )
        browser.close()

    off_frames, off_touched, _ = results["off"]
    on_frames, on_touched, _ = results["busy"]
    off_share = off_touched / max(off_frames, 1)
    on_share = on_touched / max(on_frames, 1)
    print(f"\nwith them off:  {off_share:.0%} of frames")
    print(f"with them busy: {on_share:.0%} of frames")
    good = on_share > 0.25 and on_share > off_share * 3
    print("the tab reaches the film:", "yes" if good else "NO — the setting changed nothing")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
