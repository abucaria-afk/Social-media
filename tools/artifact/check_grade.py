"""Does the grade actually change the picture?

Written because the answer turned out to be "barely". Every look is a CSS
filter string, and a filter string is easy to write and impossible to judge by
reading: `saturate(1.12) contrast(1.1) brightness(1.01)` looks like a grade in
source and is a 3-in-255 nudge on screen — under the threshold where anyone
would call it graded at all. That string is `house`, and `house` is what every
prompt that matches no keyword falls through to, which is most prompts.

So this measures rather than asserts. It draws a photograph raw, draws it
again through each look, and reports the distance between them in units a
person can argue with: mean absolute channel change out of 255, and how far
the saturation and the contrast actually moved.

    python3 tools/artifact/check_grade.py <folder of photos>

A look under `FAINT` is reported as a failure. The number is not arbitrary —
below about 8/255 a change is invisible against JPEG noise on a phone screen,
which is precisely the complaint that prompted this.
"""

import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PHOTOS = [
    p
    for p in sorted(Path(sys.argv[1]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
][:4]
CHROME = "/opt/pw-browsers/chromium"

#: Mean absolute channel change, out of 255, below which nobody would call the
#: picture graded.
FAINT = 8.0

RENDER = Path("tools/artifact/browser-render.js").read_text()
ERAS = Path("tools/artifact/era.js")

PAGE = """
<!doctype html><meta charset=utf-8><body style="margin:0">
<script>%(era)s</script>
<script>%(render)s</script>
<script>
window.ready = (async function () {
  const load = (src) => new Promise((ok) => {
    const i = new Image(); i.onload = () => ok(i); i.src = src;
  });
  const looks = window.auteurCut.looks();
  const eras = window.auteurEra ? window.auteurEra.names() : [];
  const shots = %(photos)s;
  const out = {};

  function pixels(draw, w, h) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d", { alpha: false, willReadFrequently: true });
    draw(g, c);
    return g.getImageData(0, 0, w, h).data;
  }

  function stats(d) {
    let mean = 0, sat = 0, n = d.length / 4;
    const lumas = [];
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2];
      const l = 0.2126*r + 0.7152*g + 0.0722*b;
      lumas.push(l); mean += l;
      const mx = Math.max(r,g,b), mn = Math.min(r,g,b);
      sat += mx ? (mx - mn) / mx : 0;
    }
    mean /= n; sat /= n;
    let v = 0;
    for (const l of lumas) { v += (l - mean) * (l - mean); }
    return { mean: mean, sat: sat, contrast: Math.sqrt(v / n) };
  }

  function apart(a, b) {
    let sum = 0;
    for (let i = 0; i < a.length; i += 4) {
      sum += Math.abs(a[i]-b[i]) + Math.abs(a[i+1]-b[i+1]) + Math.abs(a[i+2]-b[i+2]);
    }
    return sum / (a.length / 4 * 3);
  }

  for (const src of shots) {
    const img = await load(src);
    const W = 240, H = Math.round(240 * img.naturalHeight / img.naturalWidth);
    const raw = pixels((g) => { g.drawImage(img, 0, 0, W, H); }, W, H);
    const base = stats(raw);

    // Looks are measured the way a photograph actually gets them — through
    // the real grader — not through the filter string, which is only what a
    // clip can afford. Measuring the filter string was measuring the
    // fallback path and calling it the grade.
    const grades = window.auteurCut.lookGrades();
    for (const name of Object.keys(looks)) {
      const got = pixels((g, c) => {
        g.drawImage(img, 0, 0, W, H);
        window.auteurEra.apply(c, grades[name]);
      }, W, H);
      const s = stats(got);
      const key = "look:" + name;
      (out[key] = out[key] || []).push({
        apart: apart(raw, got),
        dSat: s.sat - base.sat,
        dContrast: s.contrast - base.contrast,
        dMean: s.mean - base.mean
      });
    }

    for (const name of eras) {
      const got = pixels((g, c) => {
        g.drawImage(img, 0, 0, W, H);
        window.auteurEra.grade(c, name);
      }, W, H);
      const s = stats(got);
      const key = "era:" + name;
      (out[key] = out[key] || []).push({
        apart: apart(raw, got),
        dSat: s.sat - base.sat,
        dContrast: s.contrast - base.contrast,
        dMean: s.mean - base.mean
      });
    }
  }
  return JSON.stringify(out);
})();
</script>
"""


def main() -> int:
    def uri(p: Path) -> str:
        kind = "png" if p.suffix.lower() == ".png" else "jpeg"
        return f"data:image/{kind};base64," + base64.b64encode(p.read_bytes()).decode()

    html = PAGE % {
        "render": RENDER,
        "era": ERAS.read_text() if ERAS.exists() else "",
        "photos": json.dumps([uri(p) for p in PHOTOS]),
    }
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.set_content(html)
        data = json.loads(page.evaluate("() => window.ready"))
        browser.close()
    if errs:
        print("page errors:", errs[:3])

    print(f"{len(PHOTOS)} photographs, mean absolute channel change out of 255\n")
    print(f"{'look':<22}{'apart':>8}{'Δsat':>9}{'Δcontrast':>11}{'Δbright':>9}")
    print("-" * 59)
    weak = []
    for name, runs in sorted(data.items()):
        apart = sum(r["apart"] for r in runs) / len(runs)
        dsat = sum(r["dSat"] for r in runs) / len(runs)
        dcon = sum(r["dContrast"] for r in runs) / len(runs)
        dmean = sum(r["dMean"] for r in runs) / len(runs)
        flag = "" if apart >= FAINT else "   <- invisible"
        if apart < FAINT:
            weak.append(name)
        print(f"{name:<22}{apart:>8.1f}{dsat:>+9.3f}{dcon:>+11.2f}{dmean:>+9.1f}{flag}")
    print("-" * 59)
    if weak:
        print(
            f"\n{len(weak)} look(s) below {FAINT}/255 — a person would say the "
            "photo was not graded:"
        )
        for name in weak:
            print(f"  {name}")
        return 1
    print("\nevery look moves the picture by more than the visible threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
