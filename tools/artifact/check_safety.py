"""Drive reporting, blocking and account deletion in a real browser.

Guideline 1.2 and guideline 5.1.1(v) are the two an app like this is most
likely to be rejected under, and both are checked by a reviewer *using the
app*, not by reading its source. So this does what they would do: sign in,
report a film, block the person, watch them leave the feed and the inbox,
unblock them again, and then delete an account and confirm there is nothing
left of it.

    python3 tools/artifact/check_safety.py [outdir]

Everything it claims is measured — how many films are in the feed before and
after, whether the conversation still answers, whether the account file still
has the row in it. Console errors and CSP violations are collected throughout.
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

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="auteur-safety-"))
CHROME = "/opt/pw-browsers/chromium"

WHO, WORD = "you", "a-long-enough-password"
THEM, THEIR_WORD = "grace", "another-long-password"
SPARE, SPARE_WORD = "leaving", "a-third-long-password"


def settle(page, expression: str, timeout: float = 15.0) -> None:
    """Poll until an expression is true. Not `wait_for_function`, which
    compiles a string inside the page and is refused by the CSP — correctly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never became true: {expression}")


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


def start(workspace: Path):
    from auteur.manager import Board
    from auteur.web import assets, oidc, server as web
    from auteur.web.auth import Accounts
    from auteur.web.profiles import Profiles
    from auteur.web.safety import Reports
    from auteur.web.social import Films, Messages

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(workspace)
    web.Handler.accounts = Accounts(workspace / "accounts.json")
    for name, word in ((WHO, WORD), (THEM, THEIR_WORD), (SPARE, SPARE_WORD)):
        web.Handler.accounts.add(name, f"{name}@example.com", word)
    web.Handler.films = Films(workspace / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(workspace / "messages.json")
    web.Handler.profiles = Profiles(workspace / "profiles.json", workspace / "pictures")
    web.Handler.reports = Reports(workspace / "reports.json")
    web.Handler.board = Board(Board.default_path(workspace))
    web.Handler.sign_in_with = oidc.load(workspace)
    web.Handler.attempts = oidc.Attempts()

    clips = workspace / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    web.Handler.films.add(
        owner=THEM, prompt="the harbour at six", video=str(a_film(clips, "theirs", "0x2a6b78"))
    )
    web.Handler.films.add(
        owner=WHO, prompt="kitchen table", video=str(a_film(clips, "mine", "0x8a4a2a"))
    )
    web.Handler.films.add(
        owner=SPARE, prompt="one last thing", video=str(a_film(clips, "spare", "0x4a4a6b"))
    )
    web.Handler.messages.send(THEM, WHO, text="did you see the harbour one")
    web.Handler.profiles.edit(THEM, name="Grace")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", web.Handler


def sign_in(page, base: str, who: str, word: str) -> None:
    page.goto(base + "/login")
    page.wait_for_selector("#username", timeout=20000)
    page.fill("#username", who)
    page.fill("#password", word)
    page.click("#signin-go")
    page.wait_for_url(lambda u: "/login" not in u, timeout=25000)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="auteur-safety-ws-"))
    httpd, base, handler = start(workspace)
    print(f"serving {base}")
    notes: list[str] = []
    broke: list[str] = []
    violations: list[str] = []
    # Two steps here deliberately post something the server must refuse, and
    # the browser reports each refusal as a console error. Counting them would
    # mean this file can never reach zero, so the ones it asks for are marked
    # rather than the whole category being ignored.
    expected: list[str] = []

    def say(line: str) -> None:
        print(line)
        notes.append(line)

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
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

        # -- the terms, before anybody has an account ----------------------
        page.goto(base + "/terms")
        page.wait_for_selector("h1", timeout=15000)
        body = page.inner_text("body")
        say("terms reachable signed out: " + str("no tolerance" in body))
        say("  and they say how to delete an account: " + str("Delete my account" in body))
        page.screenshot(path=str(OUT / "01-terms.png"), full_page=True)

        page.goto(base + "/login")
        page.wait_for_selector("#username", timeout=15000)
        say("sign-up links the terms: " + str(page.locator('a[href="/terms"]').count() > 0))

        # -- reporting a film, and blocking in the same step ---------------
        sign_in(page, base, WHO, WORD)
        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=20000)
        before = len(page.query_selector_all(".reel"))
        say(f"feed before: {before} films")

        # Theirs specifically, not "the first film in the feed" — the feed is
        # newest first, and the first run of this reported a third person's
        # film by accident and then reported that the block had not worked.
        page.click(f'.reel [data-more][data-owner="{THEM}"]')
        page.wait_for_selector("#safety-sheet .sheet-body", state="visible", timeout=10000)
        reasons = page.eval_on_selector_all(
            "#safety-reasons .choice", "ns => ns.map(n => n.textContent)"
        )
        say(f"reasons offered: {len(reasons)}")
        say("blocking is offered in the same step: " + str(page.is_checked("#safety-block")))
        page.screenshot(path=str(OUT / "02-report-sheet.png"))

        # Sending with nothing picked has to be refused, or the reason is
        # decoration.
        page.click("#safety-send")
        page.wait_for_selector("#safety-error", state="visible", timeout=8000)
        say("a report with no reason is refused: " + page.inner_text("#safety-error"))

        page.click('#safety-reasons .choice[data-value="harassment"]')
        page.fill("#safety-note", "kept sending this after I asked them to stop")
        page.click("#safety-send")
        page.wait_for_selector(".said", timeout=10000)
        say("after reporting: " + page.inner_text(".said"))
        page.screenshot(path=str(OUT / "03-reported.png"))

        page.wait_for_timeout(1200)
        after = len(page.query_selector_all(".reel"))
        say(f"feed after: {after} films (theirs should be gone)")

        # -- what the block did everywhere else ----------------------------
        page.goto(base + "/inbox")
        page.wait_for_timeout(1000)
        rows = len(page.query_selector_all(".thread-row"))
        say(f"conversations left: {rows}")
        page.screenshot(path=str(OUT / "04-inbox-empty.png"))

        page.goto(base + "/u/" + THEM)
        settle(page, "document.getElementById('big-name').textContent !== '…'")
        say("their profile says: " + page.inner_text("#blocked-note")[:70] + "…")
        say("  follow button hidden: " + str(not page.is_visible("#follow")))
        say("  their films hidden: " + str(len(page.query_selector_all(".grid-cell")) == 0))
        page.screenshot(path=str(OUT / "05-blocked-profile.png"))

        # -- and what the operator sees ------------------------------------
        waiting = handler.reports.open_ones()
        say(f"reports waiting for the operator: {len(waiting)}")
        if waiting:
            one = waiting[0]
            say(f"  a {one.kind} by {one.about_who}, reported as {one.reason}")

        # -- seeing what came of it, and undoing the block -----------------
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        settle(page, "document.getElementById('reports-state').textContent !== ''")
        say("the account row says: " + page.inner_text("#reports-state"))
        page.click("#my-reports")
        page.wait_for_selector("#reports-sheet .sheet-body", state="visible", timeout=10000)
        page.wait_for_timeout(600)
        say("reported list: " + page.inner_text("#reported-list").replace("\n", " · "))
        page.screenshot(path=str(OUT / "06-what-i-reported.png"))

        handler.reports.decide(waiting[0].id, "removed", "took it down")
        page.click('[data-close="reports"]')
        page.click("#my-reports")
        page.wait_for_timeout(700)
        say("after the operator acted: " + page.inner_text(".reported-state"))

        page.click("[data-unblock]")
        page.wait_for_timeout(800)
        say("unblocked from the list: " + str(not handler.profiles.blocks(WHO, THEM)))
        page.click('[data-close="reports"]')

        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=20000)
        page.wait_for_timeout(800)
        say(f"feed after unblocking: {len(page.query_selector_all('.reel'))} films")

        # -- deleting an account -------------------------------------------
        sign_in(page, base, SPARE, SPARE_WORD)
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        page.click("#delete-account")
        page.wait_for_selector("#delete-sheet .sheet-body", state="visible", timeout=10000)
        say("what it says goes: " + page.inner_text("#delete-list").replace("\n", " · "))
        page.screenshot(path=str(OUT / "07-delete-account.png"), full_page=True)

        expected.append("the two deliberate refusals below")
        page.fill("#delete-password", "wrong")
        page.fill("#delete-confirm", "delete")
        page.click("#delete-go")
        page.wait_for_selector("#delete-error", state="visible", timeout=10000)
        say("a wrong password is refused: " + page.inner_text("#delete-error"))

        page.fill("#delete-password", SPARE_WORD)
        page.fill("#delete-confirm", "yes")
        page.click("#delete-go")
        page.wait_for_timeout(700)
        say("the wrong word is refused: " + page.inner_text("#delete-error"))

        page.wait_for_timeout(300)
        expected.clear()
        page.fill("#delete-confirm", "delete")
        page.click("#delete-go")
        page.wait_for_url(lambda u: "/login" in u, timeout=20000)
        handler.accounts.refresh()
        say("account gone: " + str(handler.accounts.get(SPARE) is None))
        say("their films gone: " + str(handler.films.by(SPARE) == []))
        left = list((workspace / "clips").glob("spare.mp4"))
        say("their file gone: " + str(not left))
        page.screenshot(path=str(OUT / "08-back-at-the-login.png"))

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
    return 1 if broke or violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
