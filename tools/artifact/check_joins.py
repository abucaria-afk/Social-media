"""Render one join, look at the frames, and say what the join actually did.

A transition expression that compiles is not a transition that reads. The two
that this program most needed — a portal opening through the outgoing frame,
and a carry where part of the last shot is still standing over the next — are
both defined as "some of A and some of B are on screen at the same time, in
different places". That is a measurable claim, so it is measured here rather
than described: the middle frame of each join is compared against both source
frames, per region, and the answer is which source each region came from.

    python3 tools/artifact/check_joins.py [kind ...]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from auteur import ffmpeg  # noqa: E402
from auteur.craft import transitions  # noqa: E402

WIDTH, HEIGHT, FPS = 360, 640, 25
HOLD, JOIN = 1.0, 0.6
KINDS = sys.argv[1:] or ["portal", "carry", "dissolve", "whip-left"]


def sources(folder: Path) -> tuple[Path, Path]:
    """Two flat, very different frames, so "which one is this" is unambiguous."""
    first, second = folder / "a.mp4", folder / "b.mp4"
    for path, colour in ((first, "red"), (second, "blue")):
        subprocess.run(
            [
                str(ffmpeg.ffmpeg_path()),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s={WIDTH}x{HEIGHT}:r={FPS}:d={HOLD + JOIN}",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
    return first, second


def render(first: Path, second: Path, kind: str, out: Path) -> None:
    spec = transitions.xfade_spec(kind, JOIN, HOLD - JOIN)
    subprocess.run(
        [
            str(ffmpeg.ffmpeg_path()),
            "-v",
            "error",
            "-y",
            "-i",
            str(first),
            "-i",
            str(second),
            "-filter_complex",
            f"[0:v][1:v]{spec},format=yuv420p[v]",
            "-map",
            "[v]",
            "-r",
            str(FPS),
            str(out),
        ],
        check=True,
    )


def frames(video: Path, folder: Path) -> list[Path]:
    pattern = folder / "f_%03d.png"
    subprocess.run(
        [
            str(ffmpeg.ffmpeg_path()),
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-vsync",
            "0",
            str(pattern),
        ],
        check=True,
    )
    return sorted(folder.glob("f_*.png"))


def regions(frame: Path) -> dict[str, str]:
    """Which source each region of this frame came from: red (A) or blue (B)."""
    from PIL import Image

    with Image.open(frame) as picture:
        image = picture.convert("RGB")
        width, height = image.size
        spots = {
            "centre": (width // 2, height // 2),
            "corner": (width // 12, height // 12),
            "edge": (width // 2, height // 14),
        }
        out = {}
        for name, (x, y) in spots.items():
            r, g, b = image.getpixel((x, y))
            if r > b + 40:
                out[name] = "A"
            elif b > r + 40:
                out[name] = "B"
            else:
                out[name] = "mix"
        return out


def main() -> int:
    scratch = Path(tempfile.mkdtemp())
    first, second = sources(scratch)
    print(f"  custom expressions: {'yes' if transitions.supports_custom() else 'no'}\n")
    print(f"  {'join':<12} {'frame':<7} {'centre':<8} {'edge':<8} {'corner':<8}")
    print("  " + "─" * 46)

    trouble = []
    for kind in KINDS:
        work = scratch / kind
        work.mkdir()
        out = work / "join.mp4"
        render(first, second, kind, out)
        shots = frames(out, work)
        # The join runs from HOLD-JOIN to HOLD. Look a third and two thirds in.
        start = int((HOLD - JOIN) * FPS)
        marks = [start + int(JOIN * FPS * share) for share in (0.33, 0.66)]
        seen = []
        for mark in marks:
            if mark >= len(shots):
                continue
            where = regions(shots[mark])
            seen.append(where)
            print(
                f"  {kind:<12} {mark:<7} {where['centre']:<8} "
                f"{where['edge']:<8} {where['corner']:<8}"
            )
        # A join worth the name has to put both sources on screen at once,
        # somewhere, at some point. A dissolve does it by mixing; portal and
        # carry do it by *place*, which is the whole difference.
        placed = any(len({w["centre"], w["edge"], w["corner"]} - {"mix"}) > 1 for w in seen)
        if kind in ("portal", "carry") and not placed:
            trouble.append(f"{kind}: never had A and B in different places at once")
        print()

    if trouble:
        print("\n".join("  ✗ " + line for line in trouble))
        return 1
    print("  every join put both shots on screen the way it claims to")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
