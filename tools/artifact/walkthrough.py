"""Walk the whole app as a person would, and photograph every step.

Not a test — a test tells you a selector matched. This opens the app the way
somebody who has just been handed the link opens it, makes an account, picks
photographs, chooses a template and an era, waits for the film, visits every
tab and connects two platforms. The screenshots are the deliverable: if a
screen is wrong, it is wrong in the picture, which is not true of a green tick.

    python3 tools/artifact/walkthrough.py <base-url> <folder of photos> [outdir]

Everything is 390x844 at 2x, which is an iPhone, because that is the only
screen this app has ever been designed for.
"""

import secrets
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791"
PHOTOS = (
    [
        str(p)
        for p in sorted(Path(sys.argv[2]).iterdir())
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    if len(sys.argv) > 2
    else []
)
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.mkdtemp(prefix="auteur-walk-"))
CHROME = "/opt/pw-browsers/chromium"

#: The account this walkthrough makes. The password is generated per run and
#: never written to the repository — a fixed one in source is a credential in
#: source however loudly the file says it is only for a walkthrough.
WHO = "firsttimeuser"
WORD = (
    "-".join(
        secrets.choice(
            ["cavern", "zenith", "juniper", "ferret", "compass", "harbour", "willow", "quartz"]
        )
        for _ in range(5)
    )
    + f"-{secrets.randbelow(9000) + 1000}"
)

steps: list[tuple[str, str]] = []


def shot(page, name: str, note: str) -> None:
    page.wait_for_timeout(500)
    path = OUT / f"{len(steps):02d}-{name}.png"
    page.screenshot(path=str(path))
    steps.append((path.name, note))
    print(f"  {path.name:34s} {note}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"walking {BASE} -> {OUT}\n")
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
        broke: list[str] = []
        page.on("pageerror", lambda e: broke.append(str(e)))

        # 1. The link, opened cold. Nobody has an account yet.
        page.goto(BASE + "/login")
        page.wait_for_selector("#username", timeout=15000)
        shot(page, "opened-the-link", "first open — the app is unclaimed")

        # 2. Making the account.
        #
        # The claim button is only shown when nobody has claimed the app yet,
        # so its absence is the signal that this is a returning install and
        # the walkthrough should sign in instead. Filling the sign-in form and
        # pressing submit — which is what this did first — types a password
        # into the wrong form and waits twenty seconds for a navigation that
        # was never going to happen.
        claiming = page.is_visible("#to-signup")
        if claiming:
            page.click("#to-signup")
            page.wait_for_selector("#new-username", state="visible", timeout=10000)
            page.fill("#new-username", WHO)
            page.fill("#signup-password", WORD)
            shot(page, "making-an-account", "claiming the app — name and password typed")
            page.click("#signup-go")
        else:
            page.fill("#username", WHO)
            page.fill("#password", WORD)
            shot(page, "signing-in", "already claimed, so this is a sign-in")
            page.click("#signin-go")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        page.wait_for_selector("#go", timeout=20000)
        shot(page, "signed-in", "signed in, on the first screen")

        # 3. Picking photographs.
        if PHOTOS:
            page.set_input_files("#clips", PHOTOS)
            page.wait_for_timeout(900)
            shot(page, "photos-picked", f"{len(PHOTOS)} photographs from the camera roll")

        # 4. Saying what kind of film, and picking a template.
        page.fill("#prompt", 'a 90s hypercut, "SUMMER", 12 seconds')
        page.wait_for_timeout(300)
        shot(page, "said-what-i-want", "the prompt, with a decade and a word on screen")

        card = page.query_selector("#template-card")
        if card and not card.is_hidden():
            card.scroll_into_view_if_needed()
            shot(page, "template-choices", "the reference reels, as timelines to cut to")
            chip = page.query_selector("#template .choice:not([data-value=''])")
            if chip:
                chip.click()
                page.wait_for_timeout(300)
                shot(page, "template-chosen", "cutting to one reel's actual timeline")

        era = page.query_selector("#era")
        if era:
            era.scroll_into_view_if_needed()
            shot(page, "era-choices", "which decade it should look like")

        # 5. Making it.
        page.click("#go")
        page.wait_for_timeout(2500)
        shot(page, "making-the-film", "it says what it is doing while it works")

        # The ffmpeg renderer is slow enough that waiting for it can outlast the
        # walk. Photograph what there is and carry on to the rest of the app
        # rather than abandoning eight screens because one of them is still
        # working — a walkthrough that stops at the slowest step tells you
        # nothing about the steps after it.
        try:
            page.wait_for_selector("#screen-done:not([hidden])", timeout=240000)
            shot(page, "film-is-ready", "the film, and what it decided")
            heard = page.inner_text("#heard")
            facts = page.inner_text("#facts").replace("\n", " · ")
            print(f"\n    heard: {heard}\n    facts: {facts}\n")
        except Exception:
            shot(page, "still-rendering", "still rendering when the walk moved on")
            print("\n    the render had not finished — see the note above\n")

        # 6. Every tab.
        for name, path, ready, note in [
            ("animation-tab", "/overlays", "#kinds .overlay-chip", "the shapes over the cut"),
            ("studio-tab", "/studio", "#platforms .platform", "what the numbers say"),
            ("scholar-tab", "/ask", "#question", "asking the Scholar"),
            ("connect-tab", "/connect", 'input[placeholder*="handle"]', "where the film goes"),
        ]:
            page.goto(BASE + path)
            try:
                page.wait_for_selector(ready, timeout=15000)
            except Exception:
                print(f"    {name}: never became ready")
            shot(page, name, note)

        # 7. Connecting a platform, which is the last thing a person does.
        page.goto(BASE + "/connect")
        page.wait_for_timeout(800)
        # Located by what is on the screen rather than by a data attribute the
        # markup does not carry. Guessing selectors is how the first run of
        # this reported "connect-tab: never became ready" about a page that
        # had rendered perfectly.
        boxes = page.query_selector_all('input[placeholder*="handle"]')
        buttons = [b for b in page.query_selector_all("button") if b.inner_text().strip() == "Link"]
        for n, name in enumerate(("instagram", "tiktok")):
            if n < len(boxes) and n < len(buttons):
                boxes[n].fill("@" + WHO)
                buttons[n].click()
                page.wait_for_timeout(1400)
                shot(page, f"linked-{name}", f"{name} linked to the account")
        shot(page, "connections", "both platforms, and what linking does and does not mean")

        browser.close()

    print(f"\n{len(steps)} screens -> {OUT}")
    if broke:
        print("\npage errors:")
        for problem in broke[:6]:
            print("  " + problem[:140])
        return 1
    print("no page errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
