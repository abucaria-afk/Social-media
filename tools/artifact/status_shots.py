"""Photograph every screen of the app, in both themes, at a phone's size.

Run after any change to the front end. It is one command rather than a
remembered ritual because a screenshot pass that depends on somebody
remembering it is a pass that stops happening — and the whole point is to see
what changed rather than to be told about it.

    python3 tools/artifact/status_shots.py <base-url> <user> <password> [outdir]

Every shot is 390x844 at 2x, which is an iPhone, because that is the only
screen size this app has ever been designed for.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
USER = sys.argv[2] if len(sys.argv) > 2 else ""
WORD = sys.argv[3] if len(sys.argv) > 3 else ""
OUT = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(tempfile.mkdtemp(prefix="auteur-status-"))
CHROME = "/opt/pw-browsers/chromium"

#: Every screen worth looking at, and what has to be on it before the shutter
#: goes. Waiting on a selector rather than a sleep: a page photographed while
#: it is still fetching is a picture of a spinner.
SCREENS = [
    ("home", "/", "#go"),
    ("animation", "/overlays", "#kinds .overlay-chip"),
    ("studio", "/studio", "#platforms .platform"),
    ("scholar", "/ask", "#question"),
]


def shoot(page, name, path, ready, theme, into):
    page.goto(BASE + path)
    try:
        page.wait_for_selector(ready, timeout=15000)
    except Exception as exc:  # noqa: BLE001 - a screen that will not load is news
        print(f"  {name} ({theme}): never became ready — {str(exc).splitlines()[0][:90]}")
    page.wait_for_timeout(700)
    shot = into / f"{name}-{theme}.png"
    page.screenshot(path=str(shot))
    return shot


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        for theme in ("dark", "light"):
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
                color_scheme=theme,
            )
            broken: list[str] = []
            # Bound explicitly: a lambda closing over the loop variable would
            # report the last theme's errors against every theme.
            page.on("pageerror", lambda e, into=broken: into.append(str(e)))

            if USER:
                page.goto(BASE + "/login")
                page.fill("#username", USER)
                page.fill("#password", WORD)
                page.click("button[type=submit]")
                try:
                    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
                except Exception:  # noqa: BLE001 - report it, do not stop
                    print("  could not sign in — the shots will be of the login page")

            for name, path, ready in SCREENS:
                made.append(shoot(page, name, path, ready, theme, OUT))
            if broken:
                print(f"  javascript errors in {theme}: {broken[:3]}")
            page.close()
        browser.close()

    # One strip per theme, so a glance covers the whole app rather than four
    # separate files nobody opens.
    for theme in ("dark", "light"):
        row = [str(OUT / f"{name}-{theme}.png") for name, _, _ in SCREENS]
        strip = OUT / f"status-{theme}.png"
        if shutil.which("ffmpeg"):
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "quiet",
                    "-y",
                    *sum((["-i", f] for f in row), []),
                    "-filter_complex",
                    f"hstack=inputs={len(row)}",
                    str(strip),
                ],
                check=False,
            )
            if strip.exists():
                made.append(strip)

    print(f"{len(made)} shots in {OUT}")
    for shot in made:
        print(f"  {shot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
