"""Cut four different films from the same photographs and read the frames.

Four prompts that should each produce something different: one hypercut with
words on screen, one slow, one black and white, and one that matches no
keyword at all — the case that used to come back ungraded at a flat 0.9s a
shot and looked exactly like the camera roll it came from.

    python3 tools/artifact/check_photos.py <page.html> <folder of photos>

Photographs only. Recording a canvas with a *playing video* drawn into it
comes back corrupt in a headless Chromium built on SwiftShader — reproducibly,
in twenty lines that never touch this renderer — so the video path has to be
checked on a real device instead. That same build has no H.264 either.
"""

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
HERE = Path(tempfile.mkdtemp(prefix="auteur-check-"))
CHROME = "/opt/pw-browsers/chromium"

TRIALS = [
    ("hypercut + titles", 'a hypercut, 6 seconds, "PORTLAND" and "JULY"'),
    ("slow and cinematic", "slow and cinematic, 8 seconds"),
    ("black and white", "gritty black and white, fast, 6 seconds"),
    ("no keyword at all", "my trip last weekend, 6 seconds"),
]


def frames(path):
    """Every frame of the finished film, as PNGs.

    `ffprobe` is not on PATH here, and playing the film back in the same
    browser that wrote it is a probe measuring itself — an earlier version did
    that and reported pairs of frames as identical because a file MediaRecorder
    has just written carries no seek index.
    """
    out = HERE / "frames"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path), "-vsync", "0", str(out / "%03d.png")])
    return sorted(out.glob("*.png"))


def look(shots):
    """Read the frames: how many cuts, how warm, how grey, how many broken."""
    corrupt = warm = grey = cuts = 0
    before = None
    for shot in shots:
        frame = Image.open(shot).convert("RGB").resize((64, 114))
        stat = ImageStat.Stat(frame)
        # Colour bars: a decoder that lost its reference frame.
        if sum(stat.stddev) / 3 > 90:
            corrupt += 1
        warm += stat.mean[0] - stat.mean[2]
        if max(stat.mean) - min(stat.mean) < 2:
            grey += 1
        now = list(frame.getdata())[::37]
        if before is not None:
            gap = sum(
                abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                for a, b in zip(now, before, strict=True)
            ) / len(now)
            # Within a shot the picture only drifts; a cut jumps.
            if gap > 40:
                cuts += 1
        before = now
    count = max(len(shots), 1)
    return {
        "frames": len(shots),
        "corrupt": corrupt,
        "red-minus-blue": round(warm / count, 1),
        "grey frames": grey,
        "cuts": cuts,
    }


def main():
    with sync_playwright() as play:
        browser = play.chromium.launch(
            executable_path=CHROME,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        broken = 0
        for name, prompt in TRIALS:
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
            )
            page.goto(PAGE.as_uri())
            page.set_input_files("#clips", PHOTOS)
            page.fill("#prompt", prompt)
            page.click("#go")
            page.wait_for_selector("#screen-done:not([hidden])", timeout=120000)

            url = page.eval_on_selector("#player", "el => el.src")
            raw = page.evaluate(
                "async u => Array.from(new Uint8Array(" "await (await fetch(u)).arrayBuffer()))",
                url,
            )
            film = HERE / f"{name.split()[0]}.webm"
            film.write_bytes(bytes(raw))

            print(f"\n--- {name}: {prompt!r}")
            print("   heard:", page.inner_text("#heard"))
            report = look(frames(film))
            print("  ", report)
            broken += report["corrupt"]
            page.close()
        browser.close()
    print(f"\nwork kept in {HERE}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
