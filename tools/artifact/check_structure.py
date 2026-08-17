"""Does a long reel go somewhere, and does it come back?

Two claims worth checking, because both were false before and neither is
visible from reading the code:

1. A thirty second film is not a five second loop played six times. The shot
   list used to be `sources[i % sources.length]`, which for five photographs
   and thirty seconds is literally the same cycle forty times.
2. The last frame matches the first, so the reel loops without a seam.

Both are measured off the rendered file rather than asserted.

    python3 tools/artifact/check_structure.py <page.html> <folder of photos>
"""

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright

PAGE = Path(sys.argv[1]).resolve()
PHOTOS = [
    str(p)
    for p in sorted(Path(sys.argv[2]).iterdir())
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
]
HERE = Path(tempfile.mkdtemp(prefix="auteur-structure-"))
CHROME = "/opt/pw-browsers/chromium"

#: Pull the finished film out of the page as one base64 string.
READ_BLOB = """
async (url) => {
  const blob = await (await fetch(url)).blob();
  return await new Promise((ok) => {
    const reader = new FileReader();
    reader.onload = () => ok(reader.result);
    reader.readAsDataURL(blob);
  });
}
"""

PROMPT = "a punchy montage, 30 seconds"


#: Every Nth frame. The repeat search below is quadratic, and at 30fps a
#: thirty second film is 900 frames, which is 3 million comparisons and several
#: minutes of waiting to learn something a sixth of the frames also show.
STRIDE = 5


def fingerprints(folder):
    """A coarse signature per sampled frame: 4x4 luma cells, quantised."""
    out = []
    for shot in sorted(folder.glob("*.png"))[::STRIDE]:
        frame = Image.open(shot).convert("L").resize((4, 4))
        out.append(tuple(round(v / 16) for v in frame.getdata()))
    return out


def distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def main():
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
        page.goto(PAGE.as_uri())
        page.set_input_files("#clips", PHOTOS)
        page.fill("#prompt", PROMPT)
        page.click("#go")
        page.wait_for_selector("#screen-done:not([hidden])", timeout=180000)
        print("heard:", page.inner_text("#heard"))
        print("facts:", page.inner_text("#facts").replace("\n", " | "))

        url = page.eval_on_selector("#player", "el => el.src")
        # Base64, not an array of numbers. A thirty second film is 25MB, and
        # `Array.from(new Uint8Array(...))` hands the debugging protocol
        # twenty-five million separate JSON numbers to serialise — which is
        # slower than rendering the film was.
        raw = base64.b64decode(page.evaluate(READ_BLOB, url).split(",", 1)[1])
        film = HERE / "long.webm"
        film.write_bytes(raw)
        browser.close()

    frames = HERE / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir()
    # Scaled on the way out. Nine hundred frames of 1080x1920 PNG is most of
    # two gigabytes written to disk to answer a question about composition
    # that survives being 64 pixels wide.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-i",
            str(film),
            "-vf",
            "scale=64:-1",
            "-vsync",
            "0",
            str(frames / "%04d.png"),
        ]
    )
    marks = fingerprints(frames)
    if len(marks) < 30:
        print(f"only {len(marks)} frames came out — nothing to say about structure")
        return 1
    print(f"\n{len(marks)} frames")

    # 1. Repetition, over the span the complaint was about: "a five second
    # loop over thirty seconds". So the window is five seconds of film, not a
    # handful of frames — with five photographs and fifty shots every single
    # frame recurs somewhere by necessity, and a one-shot window only ever
    # asks "does this picture appear twice", which is trivially yes and says
    # nothing about whether the reel develops. A *sequence* five seconds long
    # matching another one is the thing that reads as a loop.
    window = max(6, round(5.0 * 30 / STRIDE))
    half = len(marks) // 2
    if len(marks) < half + window + 1:
        window = max(4, (len(marks) - half) // 2)

    def closest_repeat(series):
        best = (999.0, 0, 0)
        stop = len(series) // 2
        for start in range(stop, len(series) - window):
            for against in range(0, stop - window):
                gap = (
                    sum(distance(series[start + k], series[against + k]) for k in range(window))
                    / window
                )
                if gap < best[0]:
                    best = (gap, against, start)
        return best

    closest = closest_repeat(marks)
    print(
        f"closest {window * STRIDE / 30:.1f}s stretch that repeats: {closest[0]:.2f} "
        f"(at frame {closest[2] * STRIDE} against {closest[1] * STRIDE})"
    )

    # The control. A test for looping that no film ever fails is not a test,
    # so run the identical measurement over this film's own first five seconds
    # played on repeat — which is precisely the thing being ruled out. If the
    # control does not come back near zero, the number above means nothing.
    looped = [marks[k % window] for k in range(len(marks))]
    control = closest_repeat(looped)
    print(f"  the same measure on a real 5s loop: {control[0]:.2f}  (control)")

    threshold = 0.6
    repeats = closest[0] < threshold
    if control[0] >= threshold:
        print("  CONTROL FAILED — this measure cannot tell a loop from a film")
        return 1

    # 2. The loop. The last frame should be the first frame again.
    seam = distance(marks[-1], marks[0])
    middles = [distance(marks[len(marks) // 2], marks[0]), distance(marks[half // 2], marks[0])]
    print(f"first frame to last frame: {seam:.2f}")
    print(f"first frame to the middle: {min(middles):.2f}  (for scale)")
    closes = seam < min(middles)

    # 3. Development: the film should not sit at one framing throughout.
    shots = sorted(frames.glob("*.png"))
    tightness = [
        ImageStat.Stat(Image.open(f).convert("L").resize((32, 32))).stddev[0]
        for f in shots[:: max(1, len(shots) // 8)]
    ]
    print("texture through the film:", " ".join(f"{t:.0f}" for t in tightness))

    print()
    print("repeats itself: ", "YES — still a loop" if repeats else "no")
    print("closes the loop:", "yes" if closes else "NO — last frame is not the first")
    return 0 if (not repeats and closes) else 1


if __name__ == "__main__":
    sys.exit(main())
