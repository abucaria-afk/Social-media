"""Drive the age gate and the content restriction in a real browser.

The App Store rating is 13+, and a reviewer checks a rating by using the app:
they will try to sign up, and they will look for whether the restriction is
real and whether it can be turned off by the person it applies to. So this does
both — signs up too young and is refused, signs up old enough, then watches a
restricted account lose the sensitive and unreviewed films, fail to lift the
restriction with the wrong code, and succeed with the right one.

    python3 tools/artifact/check_age.py [outdir]

Everything is measured: how many films are on screen before and after, what the
server says the account's state is, whether the code is anywhere in the page.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="auteur-age-"))
CHROME = "/opt/pw-browsers/chromium"

WHO, WORD = "grown", "a-long-enough-password"
THEM, THEIR_WORD = "grace", "another-long-password"


def a_film(folder: Path, name: str, colour: str) -> Path:
    from auteur.ffmpeg import run

    out = folder / f"{name}.mp4"
    run(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:s=540x960:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(out),
        ]
    )
    return out


def start(workspace: Path, *, empty: bool):
    from auteur.manager import Board
    from auteur.web import assets, oidc, server as web
    from auteur.web.auth import Accounts
    from auteur.web.profiles import Profiles
    from auteur.web.safety import Reports
    from auteur.web.social import Films, Messages

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(workspace)
    web.Handler.accounts = Accounts(workspace / "accounts.json")
    web.Handler.films = Films(workspace / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(workspace / "messages.json")
    web.Handler.profiles = Profiles(workspace / "profiles.json", workspace / "pictures")
    web.Handler.reports = Reports(workspace / "reports.json")
    web.Handler.board = Board(Board.default_path(workspace))
    web.Handler.sign_in_with = oidc.load(workspace)
    web.Handler.attempts = oidc.Attempts()

    if not empty:
        year = time.gmtime().tm_year
        web.Handler.accounts.add(WHO, "grown@example.com", WORD, born=year - 30)
        web.Handler.accounts.add(THEM, "grace@example.com", THEIR_WORD, born=year - 26)
        clips = workspace / "clips"
        clips.mkdir(parents=True, exist_ok=True)
        made = {
            "an ordinary one": ("0x2a6b78", None),
            "marked sensitive": ("0x8a4a2a", "sensitive"),
            "reported, not looked at": ("0x6b4a78", "reported"),
        }
        for index, (prompt, (colour, state)) in enumerate(made.items()):
            film = web.Handler.films.add(
                owner=THEM, prompt=prompt, video=str(a_film(clips, f"c{index}", colour))
            )
            if state == "sensitive":
                web.Handler.films.mark(film.id, True)
            elif state == "reported":
                web.Handler.reports.file(
                    by=WHO, kind="film", about=film.id, about_who=THEM, reason="sexual"
                )
        web.Handler.profiles.edit(THEM, name="Grace")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", web.Handler


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    broke: list[str] = []
    violations: list[str] = []
    expected: list[str] = []

    def say(line: str) -> None:
        print(line)
        notes.append(line)

    def watch(page):
        page.on("pageerror", lambda e: broke.append(str(e)))
        page.on(
            "console",
            lambda m: (
                violations.append(m.text)
                if "Content Security Policy" in m.text
                else (
                    (expected if expected and m.type == "error" else broke).append(m.text)
                    if m.type == "error"
                    else None
                )
            ),
        )

    year = time.gmtime().tm_year

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)

        # -- the gate, on an instance nobody has claimed yet ----------------
        gate_ws = Path(tempfile.mkdtemp(prefix="auteur-age-gate-"))
        httpd, base, handler = start(gate_ws, empty=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
        watch(page)
        page.goto(base + "/login")
        page.wait_for_selector("#to-signup", timeout=20000)
        page.click("#to-signup")
        page.wait_for_selector("#new-username", state="visible", timeout=10000)
        say("the field says: " + page.inner_text('label[for="signup-born"]'))
        page.screenshot(path=str(OUT / "01-sign-up.png"), full_page=True)

        expected.append("the deliberate under-age attempt")
        page.fill("#new-username", "tooyoung")
        page.fill("#new-email", "t@example.com")
        page.fill("#signup-password", "a-long-enough-password")
        page.fill("#signup-born", str(year - 9))
        page.click("#signup-go")
        page.wait_for_selector("#signup-error", state="visible", timeout=15000)
        say("nine years old: " + page.inner_text("#signup-error"))
        page.screenshot(path=str(OUT / "02-too-young.png"))
        page.wait_for_timeout(400)
        expected.clear()
        handler.accounts.refresh()
        say("  and no account was made: " + str(handler.accounts.get("tooyoung") is None))

        page.fill("#signup-born", str(year - 30))
        page.click("#signup-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        handler.accounts.refresh()
        made = handler.accounts.get("tooyoung")
        say(f"thirty years old: account made, age {made.age}, restriction {made.restriction}")
        page.close()
        httpd.shutdown()
        httpd.server_close()

        # -- the restriction, on an instance with something to hide ---------
        workspace = Path(tempfile.mkdtemp(prefix="auteur-age-ws-"))
        httpd, base, handler = start(workspace, empty=False)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
        watch(page)
        page.goto(base + "/login")
        page.wait_for_selector("#username", timeout=20000)
        page.fill("#username", WHO)
        page.fill("#password", WORD)
        page.click("#signin-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=25000)

        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=25000)
        page.wait_for_timeout(900)
        before = page.eval_on_selector_all(".reel-prompt", "ns => ns.map(n => n.textContent)")
        say(f"feed unrestricted: {sorted(before)}")

        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=20000)
        page.wait_for_timeout(700)
        say("the row reads: " + page.inner_text("#restriction-state"))
        page.click("#restriction-row")
        page.wait_for_selector("#restriction-sheet .sheet-body", state="visible", timeout=10000)
        page.check("#restriction-want-lock")
        page.fill("#restriction-new-lock", "4821")
        page.screenshot(path=str(OUT / "03-turning-it-on.png"))
        page.click("#restriction-go")
        page.wait_for_selector(".said", timeout=10000)
        say("after turning it on: " + page.inner_text(".said"))

        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=25000)
        page.wait_for_timeout(900)
        after = page.eval_on_selector_all(".reel-prompt", "ns => ns.map(n => n.textContent)")
        say(f"feed restricted:   {sorted(after)}")
        page.screenshot(path=str(OUT / "04-feed-restricted.png"))

        # -- and it cannot be lifted by whoever it applies to ---------------
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=20000)
        page.wait_for_timeout(700)
        page.click("#restriction-row")
        page.wait_for_selector("#restriction-off:not([hidden])", state="visible", timeout=10000)
        say("it says: " + page.inner_text("#restriction-off-note"))
        say("a code is asked for: " + str(page.is_visible("#restriction-code")))
        page.screenshot(path=str(OUT / "05-locked.png"))

        expected.append("the deliberate wrong code")
        page.fill("#restriction-code", "0000")
        page.click("#restriction-lift")
        page.wait_for_selector("#restriction-off-error", state="visible", timeout=10000)
        say("the wrong code: " + page.inner_text("#restriction-off-error"))
        page.wait_for_timeout(400)
        expected.clear()

        # The code must not be anywhere the page could have compared it.
        markup = page.content()
        say("the code is nowhere in the page: " + str("4821" not in markup))

        page.fill("#restriction-code", "4821")
        page.click("#restriction-lift")
        page.wait_for_selector(".said", timeout=10000)
        say("the right code: " + page.inner_text(".said"))
        page.screenshot(path=str(OUT / "06-lifted.png"))

        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=25000)
        page.wait_for_timeout(900)
        say(f"feed after lifting: {len(page.query_selector_all('.reel'))} films")

        # -- marking one of your own ---------------------------------------
        clips = workspace / "clips"
        mine = handler.films.add(
            owner=WHO, prompt="one of mine", video=str(a_film(clips, "mine", "0x3f4a2e"))
        )
        page.reload()
        page.wait_for_selector(f'[data-mark="{mine.id}"]', timeout=25000)
        page.click(f'[data-mark="{mine.id}"]')
        page.wait_for_selector(".said", timeout=10000)
        say("marking my own: " + page.inner_text(".said"))
        say("  stored: " + str(handler.films.get(mine.id).sensitive))
        page.screenshot(path=str(OUT / "07-marked-my-own.png"))

        browser.close()

    httpd.shutdown()
    httpd.server_close()

    say(f"console errors: {len(broke)}")
    for line in broke[:10]:
        say("  " + line)
    say(f"CSP violations: {len(violations)}")
    for line in violations[:10]:
        say("  " + line)

    (OUT / "notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    print(f"\nscreenshots in {OUT}")
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(gate_ws, ignore_errors=True)
    return 1 if broke or violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
