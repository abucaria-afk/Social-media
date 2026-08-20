"""Ask the Scholar real questions through the app, and read what it says back.

The tab renders, the header counts learnings, and none of that tells you
whether the answers are worth reading. This types questions into the real page
the way a person would — the four suggested ones and a handful it has never
been prompted with, including one it should refuse — and prints every answer in
full so the wording can be judged rather than assumed.

    python3 tools/artifact/ask_scholar.py <base-url> [outdir]

An answer is graded on three things a person actually cares about: that it
arrived at all, that it is specific rather than a restatement of the question,
and that it says where it got the answer instead of asserting.
"""

import secrets
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="auteur-ask-"))
CHROME = "/opt/pw-browsers/chromium"

WHO = "askingtoday"
WORD = (
    "-".join(
        secrets.choice(
            ["cavern", "zenith", "juniper", "ferret", "compass", "harbour", "willow", "quartz"]
        )
        for _ in range(5)
    )
    + f"-{secrets.randbelow(9000) + 1000}"
)

#: The questions, and what a good answer would contain. `expect` is a hint for
#: the reader rather than an assertion — the point of this tool is to put the
#: wording in front of somebody, not to grade it automatically.
QUESTIONS = [
    ("How fast do the reels cut?", "a measured number, not an adjective"),
    ("What about grading?", "something about the look, from what it studied"),
    ("Why does a hypercut work?", "a reason, not a definition"),
    ("What next?", "a thing to try"),
    ("Should the first shot be the strongest one I have?", "an opinion with a reason"),
    ("How long should a reel be?", "a number and what it depends on"),
    ("What is the capital of France?", "it should decline — this is not its subject"),
]

#: Phrases that mean it had nothing and said so. Not failures in themselves —
#: a Scholar that admits ignorance is behaving correctly — but they have to be
#: counted, because an app where every answer is a shrug is not working.
EMPTY = (
    "i do not know",
    "i don't know",
    "nothing yet",
    "have not studied",
    "haven't studied",
    "no learnings",
    "cannot answer",
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

        page.goto(BASE + "/ask")
        page.wait_for_selector("#question", timeout=15000)
        header = page.inner_text(".sbar-note") if page.query_selector(".sbar-note") else ""
        print(f"scholar header: {header.strip()}\n")

        shrugs = 0
        for n, (question, wanted) in enumerate(QUESTIONS):
            # Count the blocks *now* rather than assuming one per exchange.
            # Indexing by question number assumed each round adds exactly one
            # `.said`, and when it adds two the wait passes instantly on the
            # previous round's answer — which is how this tool reported the app
            # giving wrong answers that the API, asked the same questions in the
            # same order, answered correctly.
            # Messages, not paragraphs. `say()` splits an answer on blank
            # lines into one `.said` per part, so counting `.said` counts
            # paragraphs — and taking the last one hands back the *final
            # bullet* of an answer rather than its first. That is why this
            # tool reported the tab giving different answers from the API,
            # asked the same questions in the same order.
            before = page.evaluate("() => document.querySelectorAll('li.says.scholar').length")
            page.fill("#question", question)
            page.keyboard.press("Enter")
            # The answer arrives asynchronously; wait for the count of said
            # blocks to grow rather than for a fixed time.
            # Two waits, not one. The block appears immediately carrying
            # "thinking…" and is replaced when the answer lands, so waiting
            # only for the block to exist reads the placeholder back — which
            # is what the first version of this did, and it reported seven
            # real answers when it had read none.
            try:
                page.wait_for_function(
                    "n => document.querySelectorAll('li.says.scholar').length > n",
                    arg=before,
                    timeout=30000,
                )
                page.wait_for_function(
                    """() => {
                        const all = document.querySelectorAll('li.says.scholar');
                        if (!all.length) { return false; }
                        const last = all[all.length - 1].innerText.trim().toLowerCase();
                        return last && !last.startsWith('thinking');
                    }""",
                    timeout=90000,
                )
            except Exception:
                answers = page.query_selector_all(".said")
                stuck = answers[-1].inner_text().strip() if answers else "(no block at all)"
                print(f"  Q: {question}\n  A: (still {stuck!r} after 90s)\n")
                shrugs += 1
                continue
            answers = page.query_selector_all("li.says.scholar")
            said = answers[-1].inner_text().strip()
            flat = said.lower()
            if any(mark in flat for mark in EMPTY):
                shrugs += 1
            print(f"  Q: {question}")
            print(f"     (wanted: {wanted})")
            for line in said.splitlines():
                print(f"  A: {line}")
            print()
            page.screenshot(path=str(OUT / f"{n:02d}-{question[:28].replace(' ', '-')}.png"))

        page.screenshot(path=str(OUT / "99-the-whole-conversation.png"), full_page=True)
        browser.close()

    print("=" * 60)
    print(f"{len(QUESTIONS) - shrugs} of {len(QUESTIONS)} questions got a real answer")
    if broke:
        print("page errors:", broke[:4])
        return 1
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
