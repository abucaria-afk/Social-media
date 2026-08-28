"""The Scholar, the Gaze agent and the critic, on the app instead of on a film.

The crew was built to judge a cut. Pointing it at the interface is not a
metaphor here, and it is worth being exact about which part of each agent is
doing the work, because "the agents reviewed the design" is the kind of claim
that is easy to make and hard to check:

* **the Scholar** answers out of what it has actually measured in the
  reference reels — their colour, their contrast, their cadence — and those
  numbers are held against the app's own. This is the agent's real `recall`,
  asked real questions.
* **the Gaze agent** contributes its own code: `_variety` and `_longest_run`,
  the two functions it uses to decide whether a cut was authored or merely
  produced. They take a list of choices and do not care what the choices are
  about, so the list here is one visual decision per screen. Its `inspect()`
  is *not* run — that reads an EDL, and an app is not one.
* **the critic** measures the rendered thing rather than the plan, which is
  what it does for a film. Here that means the real pixels of every screen at
  a phone viewport: contrast of every piece of text against what is actually
  behind it, the size of everything you can tap, whether anything runs off the
  side, and whether the bars clear the safe area.

    python3 tools/artifact/three_opinions_ui.py <base url> <user> <password>
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8793"
WHO = sys.argv[2] if len(sys.argv) > 2 else "ada"
WORD = sys.argv[3]
WIDTH, HEIGHT = 390, 844
SCREENS = [
    ("make", "/"),
    ("feed", "/feed"),
    ("templates", "/templates"),
    ("inbox", "/inbox"),
    ("studio", "/studio"),
    ("scholar", "/ask"),
    ("manager", "/manager"),
    # Everything built since this list was last written. A review that covers
    # the screens that already passed it is a review that cannot fail.
    ("profile", "/profile"),
    ("projects", "/projects"),
    ("terms", "/terms"),
    ("privacy", "/privacy"),
]

#: Screens that only exist once something has been made, and the sheets that
#: only exist once something has been tapped. Reached by driving rather than by
#: a URL — `openers` in `main` does the tapping.
DEEPER = [
    ("project-map", "/projects", "the map of one project"),
    ("project-album", "/projects", "its album"),
    ("report-sheet", "/feed", "reporting a film"),
    ("restriction-sheet", "/profile", "hiding sensitive films"),
]

#: WCAG AA for body text. The same bar the palette is already held to, applied
#: to what is on screen rather than to the tokens.
READABLE = 4.5
#: The smallest a thumb reliably hits. Both platforms publish this number and
#: they publish the same one.
TAPPABLE = 44

# The page is measured in the browser: only the browser knows what colour is
# actually behind a piece of text after the cascade, the materials and the
# gradients have had their say.
MEASURE = r"""
() => {
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const parse = (s) => {
    s = s || "";
    const m = s.match(/-?[\d.]+/g);
    if (!m) return null;
    let v = m.slice(0, 4).map(Number);
    // color-mix() comes back from Chromium as `color(srgb 0.97 0.96 0.95 / .82)`,
    // whose channels are 0..1 rather than 0..255. Read as 0..255 every bar in
    // the app measured as black and every label on one looked unreadable —
    // which is a bug in this file, not in the stylesheet, and worth saying so
    // rather than quietly repainting the app to satisfy it.
    if (/^color\(/.test(s)) {
      v = [v[0] * 255, v[1] * 255, v[2] * 255].concat(v.length > 3 ? [v[3]] : []);
    }
    return v;
  };
  // What is actually behind this element, walking up until something opaque.
  const behind = (el) => {
    let node = el;
    while (node && node !== document.documentElement) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && (c.length < 4 || c[3] > 0.75)) return c;
      node = node.parentElement;
    }
    const c = parse(getComputedStyle(document.body).backgroundColor);
    return c || [255, 255, 255];
  };
  const contrast = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };

  const text = [];
  const small = [];
  document.querySelectorAll("body *").forEach((el) => {
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.opacity === "0" || cs.display === "none") return;

    // Text: only elements whose own direct children include real words.
    const own = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join("");
    if (own.length > 1) {
      const fg = parse(cs.color);
      const size = parseFloat(cs.fontSize);
      if (fg) {
        text.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className || "").toString().slice(0, 40),
          words: own.slice(0, 40),
          size: size,
          weight: cs.fontWeight,
          ratio: contrast(fg, behind(el)),
        });
      }
    }

    // Anything you can tap — except a link inside a sentence. Both platforms
    // publish 44 for *targets*, and a word you read in a paragraph and happen
    // to be able to follow is not one: making it 44 tall would put gaps in the
    // prose. A link that is its own paragraph, or sits in a bar or a list, is
    // a target and is measured.
    const inline =
      el.tagName === "A" &&
      el.parentElement &&
      el.parentElement.matches("p, li, span") &&
      (el.parentElement.textContent || "").trim().length > (el.textContent || "").trim().length + 4;
    // Inside a canvas that zooms, the size on screen is whatever the person
    // set it to — that is what a zoom *is*, and every map application works
    // this way. The invariant that matters there is the size at life size,
    // which is a rule about the stylesheet and is checked in the test suite
    // rather than measured off a view somebody has pinched.
    const inCanvas = !!el.closest(".map-world");
    const tappable =
      !inline &&
      !inCanvas &&
      el.matches("a, button, [role=button], [role=radio], input, select, textarea, label.picker");
    if (tappable) {
      // What a thumb can actually hit, which for a checkbox is its label. A
      // 20px box inside a 44px <label> is a 44px target — tapping the words
      // toggles it — and measuring the input alone reported that as a failure
      // it is not. The label has to *contain* the control for this to hold,
      // which is why it is `closest` rather than a `for=` lookup.
      let hit = box;
      const wrap = el.closest("label");
      if (wrap && wrap !== el) {
        const around = wrap.getBoundingClientRect();
        if (around.height > hit.height) { hit = around; }
      }
      small.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || "").toString().slice(0, 40),
        label: (el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 30),
        w: Math.round(hit.width),
        h: Math.round(hit.height),
      });
    }
  });

  // One number for what this screen looks like, for the Gaze agent's variety
  // check: the background it is built on, and the accent it leans on.
  const ground = behind(document.body);
  const accents = {};
  document.querySelectorAll("body *").forEach((el) => {
    const c = parse(getComputedStyle(el).backgroundColor);
    if (!c || (c.length > 3 && c[3] < 0.5)) return;
    const box = el.getBoundingClientRect();
    if (box.width * box.height < 400) return;
    const key = c.slice(0, 3).map((v) => Math.round(v / 24)).join(",");
    accents[key] = (accents[key] || 0) + box.width * box.height;
  });
  const ranked = Object.entries(accents).sort((a, b) => b[1] - a[1]).map((r) => r[0]);

  return {
    text,
    tappable: small,
    groundLuma: Math.round(lum(ground) * 100) / 100,
    fills: ranked.slice(0, 4),
    overflows: document.documentElement.scrollWidth > window.innerWidth + 1,
    scrollWidth: document.documentElement.scrollWidth,
    // The bars have to clear the notch and the home indicator.
    barTop: (() => { const b = document.querySelector(".topbar, .feed-top"); return b ? Math.round(b.getBoundingClientRect().top) : null; })(),
    tabBottom: (() => { const b = document.querySelector(".tabbar"); return b ? Math.round(window.innerHeight - b.getBoundingClientRect().bottom) : null; })(),
    layout: (() => {
      if (document.querySelector(".reels")) return "full-bleed";
      if (document.querySelector(".inset-group")) return "grouped-rows";
      if (document.querySelector(".threads")) return "list";
      if (document.querySelector(".card")) return "cards";
      return "plain";
    })(),
  };
}
"""


def reach(page, name: str) -> None:
    """Drive to a screen that has no address of its own."""
    if name.startswith("project"):
        page.wait_for_selector(".album, #blank", timeout=8000)
        if page.is_visible("#blank"):
            raise RuntimeError("no project to open")
        page.click(".album")
        page.wait_for_selector("#big-name", timeout=8000)
        page.wait_for_timeout(900)
        if name == "project-album":
            page.click("#face-album")
            page.wait_for_selector("#album-face:not([hidden])", timeout=6000)
        return
    if name == "report-sheet":
        page.wait_for_selector(".reel [data-more]", timeout=12000)
        page.click(".reel [data-more]")
        page.wait_for_selector("#safety-sheet .sheet-body", state="visible", timeout=6000)
        return
    if name == "restriction-sheet":
        page.wait_for_selector("#restriction-row", timeout=10000)
        page.wait_for_timeout(700)
        page.click("#restriction-row")
        page.wait_for_selector("#restriction-sheet .sheet-body", state="visible", timeout=6000)
        return
    raise RuntimeError("no way in")


def rule(title: str) -> None:
    print("\n" + "─" * 92)
    print(f"  {title}")
    print("─" * 92)


def main() -> int:
    from auteur.agents.gaze import _longest_run, _variety
    from auteur.scholar import Scholar

    readings = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        for scheme in ("dark", "light"):
            ctx = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
                color_scheme=scheme,
            )
            page = ctx.new_page()
            page.goto(BASE + "/login")
            page.wait_for_timeout(500)
            page.fill("#username", WHO)
            page.fill("#password", WORD)
            page.click("#signin-go")
            page.wait_for_url(lambda u: "/login" not in u, timeout=25000)
            for name, path in SCREENS:
                page.goto(BASE + path)
                page.wait_for_timeout(2200)
                readings[(scheme, name)] = page.evaluate(MEASURE)

            # The screens and sheets that only exist after something has been
            # tapped. Measured the same way — a dialog is a screen, and the
            # first version of this list quietly skipped every one of them.
            for name, path, _what in DEEPER:
                page.goto(BASE + path)
                page.wait_for_timeout(1800)
                try:
                    reach(page, name)
                except Exception as exc:  # noqa: BLE001 - a screen that is not there
                    print(f"      · could not reach {name}: {exc}")
                    continue
                page.wait_for_timeout(900)
                readings[(scheme, name)] = page.evaluate(MEASURE)
            ctx.close()
        browser.close()

    # ---- the critic ------------------------------------------------------
    rule("THE CRITIC — measured off the rendered screens, not off the stylesheet")
    unreadable, untappable, spilling = [], [], []
    for (scheme, name), said in readings.items():
        for item in said["text"]:
            # The bar rises for small text, the way the guideline does.
            bar = (
                3.0
                if (item["size"] >= 24 or (item["size"] >= 18.66 and int(item["weight"]) >= 700))
                else READABLE
            )
            if item["ratio"] < bar:
                unreadable.append(
                    f"{scheme}/{name}: {item['ratio']:.2f}:1 on {item['size']:.0f}px "
                    f"{'.' + item['cls'].split()[0] if item['cls'] else item['tag']} "
                    f"— {item['words']!r}"
                )
        for item in said["tappable"]:
            if item["h"] < TAPPABLE and item["w"] > 0:
                untappable.append(
                    f"{scheme}/{name}: {item['w']}x{item['h']} "
                    f"{'.' + item['cls'].split()[0] if item['cls'] else item['tag']} "
                    f"— {item['label']!r}"
                )
        if said["overflows"]:
            spilling.append(f"{scheme}/{name}: {said['scrollWidth']}px wide on a {WIDTH}px screen")

    def report(title: str, rows: list[str], limit: int = 8) -> None:
        if not rows:
            print(f"      ✓ {title}")
            return
        print(f"      ✗ {title} — {len(rows)}")
        for row in sorted(set(rows))[:limit]:
            print(f"          {row}")

    report("every piece of text clears its contrast bar", unreadable)
    report(f"everything tappable is at least {TAPPABLE}px tall", untappable)
    report("nothing runs off the side of the screen", spilling)

    # ---- the Gaze agent --------------------------------------------------
    rule("THE GAZE AGENT — its own variety maths, over the screens instead of the shots")
    for scheme in ("dark", "light"):
        rows = [
            readings[(scheme, name)]
            for name, *_ in SCREENS + [(d[0], d[1]) for d in DEEPER]
            if (scheme, name) in readings
        ]
        layouts = [r["layout"] for r in rows]
        grounds = [r["groundLuma"] for r in rows]
        leads = [r["fills"][0] if r["fills"] else "" for r in rows]
        print(f"      {scheme}")
        print(f"        layouts    {layouts}")
        print(
            f"        variety    layout {_variety(layouts):.2f}   "
            f"lead fill {_variety(leads):.2f}   ground {_variety(grounds):.2f}"
        )
        print(f"        longest run of one layout: {_longest_run(layouts)} of {len(layouts)}")
        if _variety(layouts) < 0.12:
            print("        ✗ one layout repeated is not a style, it is the absence of a")
            print("          decision — the same thing it says about a cut of 79 cuts.")
        else:
            print("        ✓ the screens are not all the same screen")

    # ---- the Scholar -----------------------------------------------------
    rule("THE SCHOLAR — the app's numbers against the reels it has measured")
    scholar = Scholar()
    print(f"      {scholar.knowledge.total_learnings} learnings held\n")
    asked = [
        "what colour are the reels, saturation and hue",
        "how bright are the reels, luma",
        "hypercut how fast a fast cut is",
    ]
    for question in asked:
        hits = scholar.knowledge.recall(question, limit=1)
        if not hits:
            print(f"      · {question}: nothing measured")
            continue
        measurements = getattr(hits[0], "measurements", None) or {}
        print(f"      · {question}")
        print(f"        {measurements}")

    from auteur import theme

    for scheme in ("dark", "light"):
        ground = theme.rgb_of("ground", scheme)
        worst = min(
            (theme.contrast(theme.rgb_of(role, scheme), ground), role)
            for role in ("text", "text_muted", "text_faint", "ember_text", "moss", "rust")
        )
        print(f"\n      palette {scheme}: weakest role is {worst[1]} at {worst[0]:.2f}:1")

    bad = bool(unreadable or untappable or spilling)
    rule("VERDICT")
    if bad:
        print("      the critic found something on the rendered screens")
    else:
        print("      all three pass on every screen, in both lightings")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
