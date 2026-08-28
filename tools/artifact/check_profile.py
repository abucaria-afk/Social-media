"""Drive the profile tab in a real browser, at a real phone viewport.

Reading the markup proves nothing about a page. This starts a real server with
two accounts and two films in it, signs in on a 390x844 screen, and then does
the things somebody would do: set a picture, write a bio, follow the other
person, narrow the feed to them, and turn the text up.

Everything it claims, it measures — the rendered font size before and after the
text control, the pixels in the profile picture as served, the number of films
in each feed scope. Console errors and CSP violations are collected the whole
way through and printed at the end, because a page that looks right and logs a
violation is a page that is broken on the next browser.

    python3 tools/artifact/check_profile.py [outdir]
"""

import io
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="auteur-profile-"))
CHROME = "/opt/pw-browsers/chromium"

YOU, YOUR_WORD = "you", "a-long-enough-password"
THEM, THEIR_WORD = "grace", "another-long-password"


def settle(page, expression: str, timeout: float = 15.0) -> None:
    """Poll until an expression is true in the page.

    Not `wait_for_function`, which injects a poller the page then refuses to
    run: this app sends `script-src 'self' 'unsafe-inline'` with no
    `unsafe-eval`, so a string compiled inside the page is blocked. That is the
    header doing its job, and the check has to work around it rather than the
    other way round. `evaluate` goes through the debugger protocol and is not
    subject to it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never became true: {expression}")


def a_film(folder: Path, name: str, colour: str) -> Path:
    """One real mp4, so the poster route has a frame to pull."""
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


def a_photograph() -> bytes:
    """Something recognisable to upload, so a wrong crop is visible."""
    art = Image.new("RGB", (1200, 800), (24, 24, 30))
    pen = ImageDraw.Draw(art)
    pen.ellipse((450, 150, 750, 450), fill=(236, 166, 105))
    pen.rectangle((450, 470, 750, 700), fill=(114, 188, 203))
    raw = io.BytesIO()
    art.save(raw, "PNG")
    return raw.getvalue()


def start(workspace: Path):
    from auteur.manager import Board
    from auteur.web import assets, oidc, server as web
    from auteur.web.auth import Accounts
    from auteur.web.profiles import Profiles
    from auteur.web.social import Films, Messages

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(workspace)
    web.Handler.accounts = Accounts(workspace / "accounts.json")
    web.Handler.accounts.add(YOU, "you@example.com", YOUR_WORD)
    web.Handler.accounts.add(THEM, "grace@example.com", THEIR_WORD)
    web.Handler.films = Films(workspace / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(workspace / "messages.json")
    web.Handler.profiles = Profiles(workspace / "profiles.json", workspace / "pictures")
    # The studio asks for these two, and a store left as None is a 500 with an
    # empty body — which reaches the browser as a console error and nothing
    # else, so it has to be set here or the sweep at the end reports faults
    # that belong to this file rather than to the app.
    web.Handler.board = Board(Board.default_path(workspace))
    web.Handler.sign_in_with = oidc.load(workspace)
    web.Handler.attempts = oidc.Attempts()

    clips = workspace / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    web.Handler.films.add(
        owner=THEM,
        prompt="the harbour at six",
        video=str(a_film(clips, "harbour", "0x2a6b78")),
        facts=["12 shots", "cut to 118 bpm"],
        heard="the harbour at six",
    )
    web.Handler.films.add(
        owner=YOU,
        prompt="kitchen table, sunday",
        video=str(a_film(clips, "kitchen", "0x8a4a2a")),
        facts=["9 shots"],
        heard="kitchen table, sunday",
    )
    # Something in the inbox, so the picture and the chosen name can be checked
    # where they matter most — a list of people.
    web.Handler.messages.send(THEM, YOU, text="did you see the harbour one")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="auteur-profile-ws-"))
    httpd, base = start(workspace)
    print(f"serving {base}")
    notes: list[str] = []
    broke: list[str] = []
    violations: list[str] = []
    # One step here deliberately posts a wrong code and expects a 400, which
    # the browser reports as a console error. Counting it would mean this file
    # can never reach zero, so the one refusal it asks for is marked as such
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

        # -- signing in ----------------------------------------------------
        page.goto(base + "/login")
        page.wait_for_selector("#username", timeout=15000)
        page.fill("#username", YOU)
        page.fill("#password", YOUR_WORD)
        page.click("#signin-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)

        # -- the tab bar ---------------------------------------------------
        page.goto(base + "/feed")
        page.wait_for_selector(".tabbar", timeout=15000)
        labels = page.eval_on_selector_all(".tab-label", "ns => ns.map(n => n.textContent)")
        say(f"tab bar: {labels}")
        bar = page.query_selector(".tabbar").bounding_box()
        say(f"tab bar sits at y={bar['y']:.0f} on an 844px screen (visible without scrolling)")

        # -- your own profile, empty --------------------------------------
        page.click('.tab[data-tab="profile"]')
        page.wait_for_url("**/profile", timeout=15000)
        page.wait_for_selector("#big-name", timeout=15000)
        settle(page, "document.getElementById('big-name').textContent !== '…'")
        say("profile opens as: " + page.inner_text("#big-name"))
        say("settings visible on your own profile: " + str(page.is_visible("#settings")))
        say("studio row on your own profile: " + str(page.is_visible('a[href="/studio"]')))
        page.screenshot(path=str(OUT / "01-your-profile-before.png"), full_page=True)

        # -- a picture -----------------------------------------------------
        page.set_input_files(
            "#picture-file",
            files=[{"name": "me.png", "mimeType": "image/png", "buffer": a_photograph()}],
        )
        page.wait_for_selector("#picture-img:not([hidden])", timeout=15000)
        served = page.evaluate("document.getElementById('picture-img').src")
        got = page.request.get(served)
        picture = Image.open(io.BytesIO(got.body()))
        say(f"picture served as {picture.format} {picture.size} in {len(got.body())} bytes")

        # -- a name and a bio ---------------------------------------------
        page.click("#edit-open")
        page.wait_for_selector("#edit-name", state="visible", timeout=10000)
        page.fill("#edit-name", "You, Actually")
        page.fill("#edit-bio", "cuts things\n\nin   the kitchen")
        page.fill("#edit-link", "example.com/reels")
        page.screenshot(path=str(OUT / "02-editing-your-profile.png"))
        page.click("#edit-save")
        page.wait_for_selector("#edit-sheet", state="hidden", timeout=10000)
        say("name now: " + page.inner_text("#big-name"))
        say("bio now: " + repr(page.inner_text("#bio")))
        say("link href: " + page.get_attribute("#link", "href"))
        # The count on the header after a save, not only on first load: an
        # answer that leaves it out reads as "your films were deleted".
        say("films counted on the header after saving: " + page.inner_text("#count-films"))
        page.screenshot(path=str(OUT / "03-your-profile-after.png"), full_page=True)

        # -- somebody else's ----------------------------------------------
        page.goto(base + "/u/" + THEM)
        settle(page, "document.getElementById('big-name').textContent !== '…'")
        say("their page shows settings: " + str(page.is_visible("#settings")))
        say("their page shows an edit button: " + str(page.is_visible("#edit-open")))
        say("films in their grid: " + str(len(page.query_selector_all(".grid-cell"))))
        page.screenshot(path=str(OUT / "04-their-profile.png"), full_page=True)

        page.screenshot(path=str(OUT / "05-their-profile-top.png"))

        # -- the empty Following tab, before there is anybody in it ---------
        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=20000)
        page.click('.feed-scope[data-scope="following"]')
        page.wait_for_timeout(800)
        say("empty Following says: " + page.inner_text("#feed-empty-line"))
        say("  and offers: " + page.inner_text("#feed-empty-go"))
        page.screenshot(path=str(OUT / "05b-following-nobody.png"))

        # -- following them, then the feed, narrowed ------------------------
        page.goto(base + "/u/" + THEM)
        settle(page, "document.getElementById('big-name').textContent !== '…'")
        page.click("#follow")
        settle(page, "document.getElementById('count-followers').textContent === '1'")
        say("after following, their followers: " + page.inner_text("#count-followers"))
        say("the button now says: " + page.inner_text("#follow"))
        page.screenshot(path=str(OUT / "05c-following-them.png"))

        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=20000)
        everyone = len(page.query_selector_all(".reel"))
        # Measured on Everyone, where the one person with a picture appears.
        say(
            "author disc is a real picture: "
            + str(bool(page.query_selector(".reel-who .avatar img")))
        )
        page.click('.feed-scope[data-scope="following"]')
        page.wait_for_timeout(800)
        followed = page.query_selector_all(".reel")
        # The last text node, not the whole link: the disc in front of the name
        # carries the initial as its own text, so `textContent` reads "ggrace".
        owners = page.eval_on_selector_all(
            ".reel-who", "ns => ns.map(n => n.lastChild.textContent.trim())"
        )
        say(f"feed: everyone {everyone} films, following {len(followed)} — {owners}")
        page.screenshot(path=str(OUT / "06-feed-following.png"))
        page.click('.feed-scope[data-scope="all"]')
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "06b-feed-everyone.png"))

        # -- the inbox knows them too -------------------------------------
        page.goto(base + "/inbox")
        page.wait_for_selector(".thread-row", timeout=15000)
        say("inbox row says: " + page.inner_text(".thread-name"))
        page.screenshot(path=str(OUT / "07-inbox.png"))

        # -- two-step, which moved here from the edit room ------------------
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        settle(page, "document.getElementById('two-step-state').textContent === 'Off'")
        page.click("#two-step-row")
        page.wait_for_selector("#two-step-setup", state="visible", timeout=10000)
        # The key arrives a round trip after the sheet opens, so wait for it
        # rather than reading the placeholder and computing a code from "".
        settle(page, "/^[A-Z2-7 ]+$/.test(document.getElementById('two-step-secret').textContent)")
        secret = page.inner_text("#two-step-secret")
        say(f"two-step offers a {len(secret.replace(' ', ''))}-character key")
        say(
            "and a link the authenticator opens: "
            + page.get_attribute("#two-step-uri", "href")[:22]
        )
        page.screenshot(path=str(OUT / "11-two-step.png"))
        # A wrong code has to be refused, or the dialog is decoration.
        expected.append("the deliberate wrong code below")
        page.fill("#two-step-code", "000000")
        page.click("#two-step-confirm")
        page.wait_for_selector("#two-step-error", state="visible", timeout=10000)
        say("a wrong code is refused: " + page.inner_text("#two-step-error"))
        page.wait_for_timeout(300)
        expected.clear()
        # And the right one turns it on.
        import base64 as _b64
        import hmac as _hmac
        import hashlib as _hashlib
        import struct as _struct

        key = _b64.b32decode(secret.replace(" ", "") + "=" * (-len(secret.replace(" ", "")) % 8))
        counter = int(time.time() // 30)
        digest = _hmac.new(key, _struct.pack(">Q", counter), _hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        chunk = _struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        page.fill("#two-step-code", str(chunk % 1000000).zfill(6))
        page.click("#two-step-confirm")
        page.wait_for_selector("#two-step-recovery", state="visible", timeout=10000)
        codes = page.eval_on_selector_all("#recovery-list li", "ns => ns.length")
        say(f"turning it on hands over {codes} recovery codes")
        page.screenshot(path=str(OUT / "12-recovery-codes.png"))
        page.click("#two-step-close")
        settle(page, "document.getElementById('two-step-state').textContent.indexOf('On') === 0")
        say("the row now reads: " + page.inner_text("#two-step-state"))

        # -- accessibility, measured --------------------------------------
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        before = page.evaluate("getComputedStyle(document.querySelector('.inset-label')).fontSize")
        page.click('[data-setting="text"] .choice[data-value="largest"]')
        page.wait_for_timeout(200)
        after = page.evaluate("getComputedStyle(document.querySelector('.inset-label')).fontSize")
        say(f"text size: body label {before} -> {after}")

        page.click('[data-setting="contrast"] .choice[data-value="more"]')
        page.click('[data-setting="motion"] .choice[data-value="still"]')
        page.wait_for_timeout(200)
        stamped = page.evaluate(
            "JSON.stringify({"
            "  text: document.documentElement.dataset.text,"
            "  motion: document.documentElement.dataset.motion,"
            "  contrast: document.documentElement.dataset.contrast,"
            "  root: getComputedStyle(document.documentElement).fontSize"
            "})"
        )
        say("stamped on the root: " + stamped)
        page.screenshot(path=str(OUT / "08-accessibility.png"), full_page=True)
        # And one at the real viewport: a full-page screenshot paints a sticky
        # bar wherever the viewport was, which looks like a layout fault and is
        # not one.
        page.evaluate(
            "document.querySelector('[data-setting=\"text\"]')" ".scrollIntoView({block: 'center'})"
        )
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / "08b-accessibility-on-screen.png"))

        # It has to survive a reload, or it is not a setting.
        page.reload()
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        kept = page.evaluate("getComputedStyle(document.querySelector('.inset-label')).fontSize")
        say(f"after a reload the text size is still {kept}")

        # And the feed stays dark with the app set to light.
        page.click('.appearance .choice[data-value="light"]')
        page.wait_for_timeout(150)
        page.screenshot(path=str(OUT / "09-light-mode-profile.png"), full_page=True)
        page.goto(base + "/feed")
        page.wait_for_selector(".reel", timeout=20000)
        ground = page.evaluate("getComputedStyle(document.body).backgroundColor")
        say(f"with the app in Light, the feed's ground is {ground}")
        page.screenshot(path=str(OUT / "10-feed-stays-dark.png"))

        # -- back to normal, and one last sweep for violations -------------
        page.goto(base + "/profile")
        page.wait_for_selector("#settings:not([hidden])", timeout=15000)
        page.click('.appearance .choice[data-value="system"]')
        page.click('[data-setting="text"] .choice[data-value="default"]')
        for path in (
            "/",
            "/templates",
            "/studio",
            "/inbox",
            "/feed",
            "/connect",
            "/overlays",
            "/ask",
            "/profile",
            "/u/" + THEM,
            "/privacy",
        ):
            page.goto(base + path)
            page.wait_for_timeout(500)

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
