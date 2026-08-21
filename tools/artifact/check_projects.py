"""Drive a project end to end: the album, and the map.

The map is the only screen in this app that is not a list, so it is the only
one where "it renders" and "it works" are genuinely different claims. This
drags a node and checks the *stored* position moved; joins two and checks the
line is drawn between the boxes rather than near them; zooms and checks the
world transform changed; reloads and checks all of it survived.

    python3 tools/artifact/check_projects.py [outdir]
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

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="auteur-proj-"))
CHROME = "/opt/pw-browsers/chromium"
WHO, WORD = "you", "a-long-enough-password"


def settle(page, expression: str, timeout: float = 15.0) -> None:
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
    from auteur.projects import Projects
    from auteur.web import assets, oidc, server as web
    from auteur.web.auth import Accounts
    from auteur.web.profiles import Profiles
    from auteur.web.safety import Reports
    from auteur.web.social import Films, Messages

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(workspace)
    web.Handler.accounts = Accounts(workspace / "accounts.json")
    web.Handler.accounts.add(WHO, "you@example.com", WORD, born=1990)
    web.Handler.films = Films(workspace / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(workspace / "messages.json")
    web.Handler.profiles = Profiles(workspace / "profiles.json", workspace / "pictures")
    web.Handler.reports = Reports(workspace / "reports.json")
    web.Handler.board = Board(Board.default_path(workspace))
    web.Handler.projects = Projects(Projects.default_path(workspace))
    web.Handler.sign_in_with = oidc.load(workspace)
    web.Handler.attempts = oidc.Attempts()

    clips = workspace / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    for index, colour in enumerate(("0x2a6b78", "0x8a4a2a")):
        web.Handler.films.add(
            owner=WHO,
            prompt=f"something {index}",
            video=str(a_film(clips, f"c{index}", colour)),
        )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", web.Handler


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="auteur-proj-ws-"))
    httpd, base, handler = start(workspace)
    print(f"serving {base}")
    notes: list[str] = []
    broke: list[str] = []
    violations: list[str] = []

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
                else broke.append(m.text) if m.type == "error" else None
            ),
        )

        page.goto(base + "/login")
        page.wait_for_selector("#username", timeout=20000)
        page.fill("#username", WHO)
        page.fill("#password", WORD)
        page.click("#signin-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=25000)

        # -- starting one --------------------------------------------------
        page.goto(base + "/projects")
        page.wait_for_selector("#blank:not([hidden])", timeout=20000)
        say("empty state: " + page.inner_text(".blank-line"))
        page.click("#blank-new")
        page.wait_for_selector("#new-name", state="visible", timeout=10000)
        page.fill("#new-name", "Portugal, June")
        page.fill("#new-place", "Lisbon and the coast")
        page.fill("#new-starts", "2026-06-02")
        page.fill("#new-ends", "2026-06-14")
        page.fill("#new-note", "Slow mornings, the harbour at six, nothing posed.")
        page.screenshot(path=str(OUT / "01-starting-one.png"), full_page=True)
        page.click("#new-go")
        page.wait_for_url(lambda u: "/project/" in u, timeout=20000)
        say("straight into it: " + page.url.split("/project/")[1][:12])
        settle(page, "document.getElementById('big-name').textContent !== '…'")
        say("it says: " + page.inner_text("#project-when"))

        # -- the map -------------------------------------------------------
        kinds = page.eval_on_selector_all(
            "#adders .adder", "ns => ns.map(n => n.textContent.trim())"
        )
        say(f"kinds you can add: {kinds}")
        say("empty map says: " + page.inner_text("#map-blank"))

        page.click('[data-add="idea"]')
        page.wait_for_selector("#node-sheet .sheet-body", state="visible", timeout=10000)
        page.fill("#node-text", "start on the water")
        page.click("#node-save")
        page.wait_for_selector("#node-sheet", state="hidden", timeout=10000)

        page.click('[data-add="shot"]')
        page.wait_for_selector("#node-sheet .sheet-body", state="visible", timeout=10000)
        page.fill("#node-text", "ferry leaving, from the rail")
        page.click("#node-save")
        page.wait_for_selector("#node-sheet", state="hidden", timeout=10000)
        say(f"nodes on the map: {len(page.query_selector_all('.node'))}")
        page.screenshot(path=str(OUT / "02-two-nodes.png"))

        # -- dragging one, and checking the *stored* position moved --------
        project_id = page.url.split("/project/")[1]

        def stored(node_id):
            found = handler.projects.get(project_id, WHO)
            return next(n["x"] for n in found.nodes if n["id"] == node_id)

        # Bring everything on screen first. Without this the node may be
        # clipped by the map's own overflow, and `bounding_box` still reports
        # where it geometrically is — so the press lands on the page behind
        # the map and starts a pan instead of a drag.
        page.click("#zoom-fit")
        page.wait_for_timeout(300)
        # The element actually under the press, not the first one the DOM
        # hands back — two runs of this check disagreed with the store because
        # `querySelectorAll` order and creation order are not the same thing
        # once a node has been re-drawn in place.
        node = page.evaluate_handle(
            "() => document.elementFromPoint("
            "  document.querySelectorAll('.node')[0].getBoundingClientRect().left + 20,"
            "  document.querySelectorAll('.node')[0].getBoundingClientRect().top + 20"
            ").closest('.node')"
        ).as_element()
        # By its id, not by its position in either list. The first run of this
        # dragged the node the DOM happened to hand back first and then read
        # the *store's* first node, which is a different one — and reported a
        # working drag as broken.
        which = node.get_attribute("data-node")
        before = stored(which)
        box = node.bounding_box()
        say(f"  the node is at {[round(box['x']), round(box['y'])]} on screen")
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        # Separate moves rather than one call with `steps`: a single stepped
        # move is dispatched fast enough that the page can coalesce it, and
        # the first version of this check reported a drag that had not
        # happened — which was the check being wrong, not the map.
        for step in range(1, 9):
            page.mouse.move(box["x"] + 20 + step * 12, box["y"] + 20 + step * 10)
        page.mouse.up()
        page.wait_for_timeout(900)
        after = stored(which)
        moved = page.evaluate(
            "id => document.querySelector('[data-node=\"' + id + '\"]').style.left", which
        )
        say(f"dragged: stored x {round(before)} -> {round(after)}, drawn at {moved}")

        # -- joining two ---------------------------------------------------
        page.click(".node")
        page.wait_for_selector("#node-sheet .sheet-body", state="visible", timeout=10000)
        page.click("#node-link")
        say("linking hint: " + page.inner_text("#map-hint"))
        page.query_selector_all(".node")[1].click()
        page.wait_for_timeout(800)
        lines = page.eval_on_selector_all(
            ".map-link",
            "ns => ns.map(n => [n.getAttribute('x1'), n.getAttribute('y1'),"
            " n.getAttribute('x2'), n.getAttribute('y2')].map(Number).map(Math.round))",
        )
        say(f"links drawn: {lines}")
        say(f"links stored: {len(handler.projects.get(project_id, WHO).links)}")
        page.screenshot(path=str(OUT / "03-joined.png"))

        # A line has to end at the middle of a node, not near it.
        middles = page.eval_on_selector_all(
            ".node",
            "ns => ns.map(function (n) {"
            " return [Math.round(parseFloat(n.style.left) + n.offsetWidth / 2),"
            "         Math.round(parseFloat(n.style.top) + n.offsetHeight / 2)]; })",
        )
        say(f"node middles: {middles}")
        if lines:
            ends = {(lines[0][0], lines[0][1]), (lines[0][2], lines[0][3])}
            say("the line joins the two middles: " + str(ends == {tuple(m) for m in middles}))

        # -- zoom ----------------------------------------------------------
        was = page.evaluate("getComputedStyle(document.getElementById('world')).transform")
        page.click("#zoom-in")
        page.wait_for_timeout(200)
        now = page.evaluate("getComputedStyle(document.getElementById('world')).transform")
        say("zoom changed the world transform: " + str(was != now))

        # -- a shot becomes a real plan ------------------------------------
        page.click("#zoom-fit")
        page.wait_for_timeout(300)
        shot = page.query_selector('[class*="node-shot"]')
        shot.click()
        page.wait_for_selector("#node-sheet .sheet-body", state="visible", timeout=10000)
        say("a shot offers the board: " + str(page.is_visible("#node-plan")))
        page.click("#node-plan")
        page.wait_for_selector("#node-sheet", state="hidden", timeout=15000)
        page.wait_for_timeout(900)
        plans = handler.board.by(WHO)
        say(f"plans on the board: {len(plans)}, project set: {bool(plans and plans[0].project)}")
        page.screenshot(path=str(OUT / "04-on-the-board.png"))

        # -- the album -----------------------------------------------------
        page.click("#face-album")
        page.wait_for_selector("#album-face:not([hidden])", timeout=10000)
        page.wait_for_timeout(900)
        loose = len(page.query_selector_all("[data-gather]"))
        say(f"films not yet in a project: {loose}")
        if loose:
            page.click("[data-gather]")
            page.wait_for_timeout(1200)
        say(f"films in the album now: {len(handler.films.in_project(project_id))}")
        say("plans shown in the album: " + str(len(page.query_selector_all("#plans .inset-row"))))
        page.screenshot(path=str(OUT / "05-the-album.png"), full_page=True)

        # -- and it all survives a reload ----------------------------------
        page.reload()
        settle(page, "document.querySelectorAll('.node').length === 2")
        say(
            f"after a reload: {len(page.query_selector_all('.node'))} nodes, "
            f"{len(page.query_selector_all('.map-link'))} link"
        )

        # -- the create screen offers it -----------------------------------
        page.goto(base + "/")
        page.wait_for_selector("#go", timeout=20000)
        page.wait_for_timeout(900)
        say("the create screen offers the project: " + str(page.is_visible("#project-card")))
        say(
            "  options: "
            + str(
                page.eval_on_selector_all(
                    "#project .choice", "ns => ns.map(n => n.textContent.trim())"
                )
            )
        )
        page.screenshot(path=str(OUT / "06-part-of.png"), full_page=True)

        # -- the list, with a cover ----------------------------------------
        page.goto(base + "/projects")
        page.wait_for_selector(".album", timeout=20000)
        page.wait_for_timeout(600)
        say("the album row says: " + page.inner_text(".album-facts"))
        say(
            "  and has a cover: "
            + str(
                bool(page.evaluate("document.querySelector('.album-cover').style.backgroundImage"))
            )
        )
        page.screenshot(path=str(OUT / "07-the-list.png"), full_page=True)

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
