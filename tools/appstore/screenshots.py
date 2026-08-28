"""The App Store screenshots, captured from the running app.

Not mock-ups. This starts a real server with real content in it and photographs
the actual screens at the exact pixel sizes App Store Connect accepts, so what
is on the product page is what the app looks like — which is both the honest
thing to do and the thing Apple asks for (screenshots must show the app in use,
not a design of it).

    python3 tools/appstore/screenshots.py [outdir]

The sizes are not a preference. Apple's upload form takes a fixed set of
dimensions and refuses anything else without saying which dimension is wrong:

* **1290 x 2796** — the 6.9"/6.7" iPhone slot, which every iPhone submission
  needs. That is a 430 x 932 CSS viewport at three device pixels per point,
  which is why the browser is driven at that and not at 1290 wide.
* **2048 x 2732** — the 12.9" iPad slot, needed only if the app is offered on
  iPad, which this one is.

`tools/appstore/preflight.py` checks what this writes against the same list, so
a screenshot at the wrong size fails here rather than at the upload.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Playwright is imported where it is used rather than here. `DEVICES` below is
# the list of sizes App Store Connect accepts, and the preflight reads it to
# check a screenshot's dimensions — a question that needs no browser. Importing
# playwright at module scope made reading that table impossible without one,
# which failed CI on every run while passing anywhere a browser was installed.

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "appstore" / "screenshots"
CHROME = "/opt/pw-browsers/chromium"

WHO, WORD = "you", "a-long-enough-password"
THEM, THEIR_WORD = "grace", "another-long-password"

#: (folder, css width, css height, device pixel ratio) -> the pixel size Apple
#: wants. Kept as the CSS size because that is what the browser is driven at;
#: the assertion below is what ties the two together.
DEVICES = (
    ("iphone-6.9", 430, 932, 3, (1290, 2796)),
    # 13", not 12.9". Apple's page marks the 13" slot "required if app runs on
    # iPad" and scales the smaller classes up from it, so producing 12.9" was
    # filling the legacy slot and leaving the required one empty. 1032 x 1376
    # points at two pixels per point is 2064 x 2752.
    ("ipad-13", 1032, 1376, 2, (2064, 2752)),
)


#: What the demo films are pictures of. Not photographs of anybody — a
#: screenshot set ships in a repository and nobody's actual footage belongs in
#: one — but not a flat rectangle of colour either. The first version of this
#: used `color=c=0x2a6b78`, and the product page it produced showed the app's
#: chrome around a plain purple block, which sells nothing and reads as a
#: render that failed. These are horizons: a graded sky, a low sun, a
#: waterline and a silhouette, which is enough to look like a frame of
#: something at the size a product page shows it.
SCENES = {
    "harbour": ((30, 52, 80), (236, 172, 108), (14, 22, 34), 0.62),
    "kitchen": ((88, 54, 38), (248, 216, 170), (40, 24, 16), 0.70),
    "road": ((44, 58, 44), (216, 228, 178), (20, 28, 22), 0.58),
    "market": ((74, 44, 82), (240, 178, 154), (28, 18, 34), 0.66),
}


def _frame(scene: str, width: int = 1080, height: int = 1920) -> Path:
    """One still, built rather than photographed."""
    top, glow, ground, horizon = SCENES[scene]
    art = Image.new("RGB", (width, height))
    pen = ImageDraw.Draw(art)

    # Sky: a ramp from the deep tone at the top to the glow at the horizon,
    # eased so the light gathers near the waterline the way it does.
    line = int(height * horizon)
    for y in range(line):
        blend = (y / max(1, line)) ** 1.6
        pen.line(
            [(0, y), (width, y)],
            fill=tuple(int(a + (b - a) * blend) for a, b in zip(top, glow, strict=True)),
        )

    # A low sun: discs of falling radius, each a little closer to white.
    cx, cy = int(width * 0.62), int(line * 0.84)
    for step in range(30, 0, -1):
        radius = step * 13
        near = 1 - step / 30
        pen.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=tuple(
                int(a + (b - a) * (near * 0.6)) for a, b in zip(glow, (255, 248, 234), strict=True)
            ),
        )

    # Ground, and a skyline, so the frame has an edge in it.
    pen.rectangle((0, line, width, height), fill=ground)
    for at, tall in ((0.08, 0.15), (0.24, 0.09), (0.38, 0.20), (0.70, 0.12), (0.86, 0.17)):
        x = int(width * at)
        pen.rectangle((x, line - int(height * tall), x + int(width * 0.055), line), fill=ground)

    art = art.filter(ImageFilter.GaussianBlur(1.4))
    out = Path(tempfile.mkdtemp()) / f"{scene}.png"
    art.save(out)
    return out


def a_film(folder: Path, name: str, scene: str, seconds: int = 3) -> Path:
    """A real mp4: a slow push into a built frame.

    A `zoompan` over a still, which is the same gesture the app's own renderer
    puts on a photograph — so the screenshot shows the shape of what the app
    actually makes rather than a placeholder that happens to be video.
    """
    from auteur.ffmpeg import run

    still = _frame(scene)
    frames = seconds * 25
    out = folder / f"{name}.mp4"
    run(
        [
            "-loop",
            "1",
            "-t",
            str(seconds),
            "-i",
            str(still),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            f"[0:v]scale=1080:1920,zoompan=z='1+0.09*on/{frames}':d={frames}"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=540x960:fps=25,"
            "noise=alls=5:allf=t,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-y",
            str(out),
        ]
    )
    return out


def a_face(hue: tuple[int, int, int], mark: tuple[int, int, int]) -> bytes:
    art = Image.new("RGB", (900, 900), hue)
    pen = ImageDraw.Draw(art)
    pen.ellipse((300, 190, 600, 490), fill=mark)
    pen.rounded_rectangle((250, 540, 650, 900), radius=90, fill=mark)
    raw = io.BytesIO()
    art.save(raw, "PNG")
    return raw.getvalue()


def start(workspace: Path):
    from auteur.manager import Board
    from auteur.web import assets, oidc, server as web
    from auteur.web.auth import Accounts
    from auteur.web.profiles import Profiles
    from auteur.web.safety import Reports
    from auteur.web.social import Films, Messages
    from auteur.projects import Projects
    from auteur.social.accounts import Connections
    from auteur.web.watching import Watching

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(workspace)
    web.Handler.accounts = Accounts(workspace / "accounts.json")
    web.Handler.accounts.add(WHO, "you@example.com", WORD)
    web.Handler.accounts.add(THEM, "grace@example.com", THEIR_WORD)
    web.Handler.films = Films(workspace / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(workspace / "messages.json")
    web.Handler.profiles = Profiles(workspace / "profiles.json", workspace / "pictures")
    web.Handler.reports = Reports(workspace / "reports.json")
    web.Handler.board = Board(Board.default_path(workspace))
    # /api/projects raised AttributeError on every screenshot run because this
    # was never set — a 500 on a route the shipped app serves, in the harness
    # that photographs the app for the store.
    web.Handler.projects = Projects(Projects.default_path(workspace))
    # Linked platform accounts. Unset, the Schedule screen renders nothing at
    # all rather than the honest "needs setting up" it is supposed to show —
    # the third store in a row this harness forgot.
    web.Handler.connections = Connections(Connections.default_path(workspace))
    web.Handler.sign_in_with = oidc.load(workspace)
    web.Handler.attempts = oidc.Attempts()
    # The feed ranks by what gets watched, so a harness that leaves this unset
    # photographs a shuffle and calls it the product. The plays below are put in
    # deliberately rather than left to whatever the browser happens to play.
    web.Handler.watching = Watching(workspace / "watching")

    clips = workspace / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    made = [
        (THEM, "the harbour at six", "harbour", ["14 shots", "cut to 118 bpm", "90s grade"]),
        (WHO, "kitchen table, sunday", "kitchen", ["9 shots", "cut to your words"]),
        (WHO, "the long way home", "road", ["11 shots", "carry on every join"]),
        (THEM, "market, early", "market", ["12 shots", "80s grade"]),
    ]
    for index, (owner, prompt, scene, facts) in enumerate(made):
        film = web.Handler.films.add(
            owner=owner,
            prompt=prompt,
            video=str(a_film(clips, f"clip{index}", scene)),
            facts=facts,
            heard=prompt,
        )
        if owner == THEM:
            web.Handler.films.like(film.id, WHO)

    # A reception, so the feed on the screenshot is ordered by something. The
    # harbour film is the one people finish; the market one they scroll past.
    for film in web.Handler.films.feed(limit=99):
        finishers, part = (7, 3.0) if film.owner == THEM else (4, 3.0)
        for n in range(finishers):
            web.Handler.watching.played(f"viewer{n}", film.id, seconds=part, runtime=3.0)
    web.Handler.profiles.edit(WHO, name="You, Actually", bio="cuts things in the kitchen")
    web.Handler.profiles.edit(THEM, name="Grace", bio="harbours, mostly")
    web.Handler.profiles.follow(WHO, THEM)
    web.Handler.messages.send(THEM, WHO, text="did you see the harbour one")
    web.Handler.messages.send(WHO, THEM, text="watching it now")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", web.Handler


def shoot(page, base: str, folder: Path, wide: bool) -> list[str]:
    """The screens, in the order somebody would meet them."""
    taken: list[str] = []

    def snap(name: str) -> None:
        page.wait_for_timeout(700)
        path = folder / f"{name}.png"
        page.screenshot(path=str(path))
        taken.append(name)

    page.goto(base + "/")
    page.wait_for_selector("#go", timeout=20000)
    snap("01-say-what-you-want")

    page.goto(base + "/feed")
    page.wait_for_selector(".reel", timeout=20000)
    page.wait_for_timeout(1200)
    snap("02-the-feed")

    page.goto(base + "/templates")
    page.wait_for_selector(".tabbar", timeout=20000)
    snap("03-cut-to-a-reel-you-like")

    page.goto(base + "/plan")
    page.wait_for_selector(".tabbar", timeout=20000)
    snap("04-plan-it-before-you-shoot-it")

    page.goto(base + "/studio")
    page.wait_for_selector(".tabbar", timeout=20000)
    snap("05-the-crew-and-what-they-know")

    page.goto(base + "/profile")
    page.wait_for_selector("#settings:not([hidden])", timeout=20000)
    page.wait_for_timeout(500)
    snap("06-yours-and-how-it-looks")

    if not wide:
        # The accessibility block, scrolled to. Worth a slot: it is a real
        # feature and it is the one reviewers check by hand.
        page.evaluate(
            "document.querySelector('[data-setting=\"text\"]')" ".scrollIntoView({block: 'center'})"
        )
        snap("07-set-it-to-suit-your-eyes")

    return taken


def main() -> int:
    from playwright.sync_api import sync_playwright

    if OUT.exists():
        shutil.rmtree(OUT)
    workspace = Path(tempfile.mkdtemp(prefix="auteur-shots-"))
    httpd, base, handler = start(workspace)
    print(f"serving {base}")
    bad: list[str] = []

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        for name, width, height, ratio, wanted in DEVICES:
            folder = OUT / name
            folder.mkdir(parents=True, exist_ok=True)
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=ratio,
                is_mobile=(width < 700),
            )
            page.on("pageerror", lambda e: bad.append(str(e)))
            page.goto(base + "/login")
            page.wait_for_selector("#username", timeout=20000)
            page.fill("#username", WHO)
            page.fill("#password", WORD)
            page.click("#signin-go")
            page.wait_for_url(lambda u: "/login" not in u, timeout=25000)

            taken = shoot(page, base, folder, wide=width > 700)
            page.close()

            for shot in taken:
                with Image.open(folder / f"{shot}.png") as art:
                    if art.size != wanted:
                        bad.append(f"{name}/{shot}.png is {art.size}, wanted {wanted}")
            print(f"  {name}: {len(taken)} at {wanted[0]}x{wanted[1]}")
        browser.close()

    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(workspace, ignore_errors=True)

    if bad:
        print("\n  problems:")
        for line in bad:
            print(f"    - {line}")
        return 1
    print(f"\n  screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
