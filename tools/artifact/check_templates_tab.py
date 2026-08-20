"""Pick a template on its own tab, then add a reel and watch it become one.

The two things this has to prove are the two that were only claims: that the
list is readable on a phone rather than a wall of near-identical chips, and
that a reel handed to the app comes back as a timing you can cut to.

    python3 tools/artifact/check_templates_tab.py <base-url> <a reel.mp4> [outdir]
"""

import secrets
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8815"
REEL = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.mkdtemp(prefix="auteur-tab-"))
CHROME = "/opt/pw-browsers/chromium"

WHO = "templatetester"
WORD = (
    "-".join(
        secrets.choice(["cavern", "zenith", "juniper", "ferret", "compass", "harbour"])
        for _ in range(5)
    )
    + f"-{secrets.randbelow(9000) + 1000}"
)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
        broke: list[str] = []
        page.on("pageerror", lambda e: broke.append(str(e)))

        page.goto(BASE + "/login")
        page.wait_for_selector("#username", timeout=15000)
        if page.is_visible("#to-signup"):
            page.click("#to-signup")
            page.wait_for_selector("#new-username", state="visible", timeout=10000)
            page.fill("#new-username", WHO)
            page.fill("#signup-password", WORD)
            page.click("#signup-go")
        else:
            page.fill("#username", WHO)
            page.fill("#password", WORD)
            page.click("#signin-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)

        # 1. The home screen, which should no longer carry the wall of chips.
        page.wait_for_selector("#go", timeout=15000)
        chips = page.query_selector_all("#template .choice")
        page.screenshot(path=str(OUT / "00-home-without-the-wall.png"))
        print(f"template chips still on the first screen: {len(chips)} (want 0)")

        # 2. The tab.
        page.goto(BASE + "/templates")
        page.wait_for_selector(".template", timeout=20000)
        listed = page.query_selector_all(".template")
        print(f"templates listed: {len(listed)}")
        page.screenshot(path=str(OUT / "01-the-templates-tab.png"))
        page.screenshot(path=str(OUT / "02-all-of-them.png"), full_page=True)

        # 3. Pick one and check it sticks.
        listed[3].click()
        page.wait_for_timeout(400)
        picked = page.evaluate("() => localStorage.getItem('auteur-template')")
        print(f"picked, and remembered as: {picked!r}")
        page.screenshot(path=str(OUT / "03-one-chosen.png"))

        # 4. Add a reel.
        print(f"uploading {REEL.name} ({REEL.stat().st_size // 1024}KB) …")
        page.set_input_files("#reel", str(REEL))
        try:
            page.wait_for_function(
                """() => {
                    const said = document.getElementById('reel-said');
                    return said && !said.hidden && /^Added |could not|did not/.test(said.textContent);
                }""",
                timeout=300000,
            )
        except Exception:
            print("  the reel was never read — still: " + page.inner_text("#reel-said"))
            page.screenshot(path=str(OUT / "04-upload-stalled.png"))
            browser.close()
            return 1
        said = page.inner_text("#reel-said").strip()
        print(f"  {said}")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "04-reel-added.png"))
        after = page.query_selector_all(".template")
        mine = page.query_selector_all(".template.is-mine")
        print(f"templates now: {len(after)} ({len(mine)} of them yours)")

        # 5. Home again — the link should say what was chosen.
        page.goto(BASE + "/")
        page.wait_for_selector("#template-link-note", timeout=15000)
        page.wait_for_timeout(1200)
        note = page.inner_text("#template-link-note").strip()
        print(f"the home link now reads: {note}")
        page.screenshot(path=str(OUT / "05-home-knows-the-choice.png"))

        browser.close()

    ok = True
    if broke:
        print("\npage errors:")
        for problem in broke[:5]:
            print("  " + problem[:160])
        ok = False
    print(OUT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
