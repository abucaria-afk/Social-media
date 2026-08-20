"""Do the two renderers agree about what a decade looks like?

There are two graders in this program and they are built out of completely
different parts. The browser walks the pixels of a photograph once, because a
photograph is graded once and can afford it. ffmpeg pays per frame, so the same
look has to come out of `curves`, `colorbalance`, `eq`, a thresholded bloom and
`rgbashift` instead. Nothing makes them agree except care, and two things
called "1990s" that do not match is worse than having only one of them —
somebody picks a decade in the app, gets one answer on the published page and a
different answer from the desktop render, and neither is wrong-looking enough
to report.

Measured the first time this ran: the ffmpeg side came out 50 to 78 levels
darker across every era. The cause was not the eras at all — `_vignette`
mapped its amount to ffmpeg's lens angle backwards, so every look in the whole
program asked for a gentle vignette and got a near-maximum one.

    python3 tools/artifact/check_eras_match.py <folder of photos>

The two will never be identical. Grain in particular is `overlay`-composited
noise on one side and ffmpeg's `noise` filter on the other, and it shows up in
the contrast figure for the two grainiest decades. Mean luma is the thing that
has to match, because that is what "darker" means.
"""

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from auteur.craft.color import LOOKS, level_chain, level_for  # noqa: E402

PHOTOS = [
    p
    for p in sorted(Path(sys.argv[1]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
][:4]
OUT = Path(tempfile.mkdtemp(prefix="auteur-eras-"))
CHROME = "/opt/pw-browsers/chromium"

#: Browser name -> ffmpeg look name. Both sides carry the same numbers; this is
#: only the difference in what each side calls them.
PAIRS = {
    "seventies": "1970s",
    "eighties": "1980s",
    "nineties": "1990s",
    "y2k": "2000s",
    "tens": "2010s",
    "now": "2020s",
}

#: How far apart the two may be in mean luma, out of 255. Below about 8 a
#: change is invisible against JPEG noise, which is the same threshold the
#: grade check uses to decide a look is doing anything at all.
APART = 8.0

ERA_JS = (Path(__file__).resolve().parent / "era.js").read_text(encoding="utf-8")

PAGE = """<!doctype html><meta charset=utf8><body><script>%(era)s</script><script>
window.ready = (async function () {
  const load = (src) => new Promise((ok) => {
    const i = new Image(); i.onload = () => ok(i); i.src = src;
  });
  const names = %(pairs)s;
  const out = {};
  for (const src of %(photos)s) {
    const img = await load(src);
    for (const key of Object.keys(names)) {
      const W = 320, H = Math.round(320 * img.naturalHeight / img.naturalWidth);
      const c = document.createElement("canvas");
      c.width = W; c.height = H;
      const g = c.getContext("2d", { alpha: false, willReadFrequently: true });
      g.drawImage(img, 0, 0, W, H);
      window.auteurEra.grade(c, key);
      const d = g.getImageData(0, 0, W, H).data;
      let m = 0; const ls = [];
      for (let i = 0; i < d.length; i += 4) {
        const l = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
        ls.push(l); m += l;
      }
      m /= ls.length;
      let v = 0; for (const l of ls) { v += (l - m) * (l - m); }
      const name = names[key];
      (out[name] = out[name] || []).push({ mean: m, contrast: Math.sqrt(v / ls.length) });
    }
  }
  return JSON.stringify(out);
})();
</script>"""


def uri(path: Path) -> str:
    kind = "png" if path.suffix.lower() == ".png" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


def levelling_for(photo: Path) -> str:
    """The level this photograph would get inside a real render.

    The browser levels a picture from its own histogram before grading it, and
    the render pipeline now does the same per shot — so a check that grades
    unlevelled footage on one side is comparing two things the program itself
    never compares. The numbers come from the same percentiles the analysis
    measures: 1st and 99th, not the extremes.
    """
    frame = np.asarray(Image.open(photo).convert("RGB"), dtype=np.float32) / 255.0
    lum = 0.2126 * frame[..., 0] + 0.7152 * frame[..., 1] + 0.0722 * frame[..., 2]
    low, high = np.percentile(lum, (1.0, 99.0))
    black, white, gamma = level_for(
        float(np.clip(low, 0.0, 0.5)), float(np.clip(high, 0.2, 1.0)), float(lum.mean())
    )
    return level_chain(black, white, gamma)


def through_ffmpeg(photo: Path, look: str) -> tuple[float, float]:
    out = OUT / "probe.png"
    level = levelling_for(photo)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(photo),
            "-vf",
            ",".join(filter(None, ["scale=320:-1", level, LOOKS[look].build(1.0)])),
            "-frames:v",
            "1",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise SystemExit(f"{look}: ffmpeg refused the chain — {proc.stderr.strip()[-200:]}")
    frame = np.asarray(Image.open(out).convert("RGB"), dtype=np.float32)
    lum = 0.2126 * frame[..., 0] + 0.7152 * frame[..., 1] + 0.0722 * frame[..., 2]
    return float(lum.mean()), float(lum.std())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    html = PAGE % {
        "era": ERA_JS,
        "pairs": json.dumps(PAIRS),
        "photos": json.dumps([uri(p) for p in PHOTOS]),
    }
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        broke: list[str] = []
        page.on("pageerror", lambda e: broke.append(str(e)))
        page.set_content(html)
        in_browser = json.loads(page.evaluate("() => window.ready"))
        browser.close()
    if broke:
        print("page errors:", broke[:3])

    print(f"{len(PHOTOS)} photographs, mean luma out of 255\n")
    print(f"{'era':10}{'browser':>10}{'ffmpeg':>10}{'apart':>9}")
    print("-" * 39)
    bad = []
    for look in PAIRS.values():
        theirs = in_browser[look]
        want = sum(r["mean"] for r in theirs) / len(theirs)
        got = sum(through_ffmpeg(p, look)[0] for p in PHOTOS) / len(PHOTOS)
        gap = abs(got - want)
        flag = "" if gap <= APART else "   <- they disagree"
        if gap > APART:
            bad.append((look, gap))
        print(f"{look:10}{want:10.1f}{got:10.1f}{gap:9.1f}{flag}")
    print("-" * 39)
    if bad:
        print(
            f"\n{len(bad)} era(s) more than {APART}/255 apart — the app and the "
            "desktop render would show different films:"
        )
        for look, gap in bad:
            print(f"  {look}: {gap:.1f}")
        print(
            "\nKnown cause, measured per photograph: the browser levels a picture\n"
            "from its own histogram before grading it and the ffmpeg path does not.\n"
            "The gap tracks how underexposed the source is — a photograph at mean\n"
            "38 is levelled to 74 before any look touches it and lands 30 apart,\n"
            "while one at mean 120 is barely levelled and lands 2 apart.\n"
            "\n"
            "ffmpeg has no per-frame adaptive gamma to match it with. `normalize`\n"
            "is the closest filter and does nothing here, because these pictures\n"
            "already span the full range — a night shot with one bright light in\n"
            "it reaches both ends while being dark everywhere that matters.\n"
            "\n"
            "The fix is to compute the level from the footage this program has\n"
            "already analysed and bake a per-shot gamma into the correction pass,\n"
            "which is a change to the render pipeline rather than to these look\n"
            "chains. Until then this check is expected to fail on any set with an\n"
            "underexposed photograph in it, and that is worth more than a\n"
            "threshold widened until it passes."
        )
        return 1
    print(f"\nboth renderers agree on every decade, within {APART}/255")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
