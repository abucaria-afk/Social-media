"""Read every file somebody has attached to this project, and say what happened.

A project accumulates material faster than anyone re-checks it. Reels arrive in
batches, exports arrive in batches, and the question that stops being asked is
whether the program can still read all of it — or whether something was dropped
silently three batches ago and nobody noticed because the tests use synthetic
footage.

This opens every attachment with the part of the program meant to read it —
reels through the template reader, photographs through the frame reader,
exports through the insight loader — and prints one line each. A file that
cannot be read is news. A file that is *deliberately* rejected, like a reel too
short to cut to, is not a failure and is reported as its own outcome, because
counting it as one trains people to ignore the report.

    python3 tools/artifact/check_attachments.py <folder>
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

FOLDER = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

REELS = {".mp4", ".mov", ".m4v", ".webm"}
STILLS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
TABLES = {".csv", ".jsonl", ".json"}

#: Matching `make_templates.py`, so this reports the same verdict the library
#: would reach rather than a second opinion nobody acts on.
LEAST_SECONDS = 4.0
LEAST_SHOTS = 8


def read_reel(path: Path) -> tuple[str, str]:
    from auteur.insight.template import read

    template = read(path, name=path.stem[:12])
    if template is None:
        return "unreadable", "would not open as video"
    if template.under_resolved:
        return "unresolved", "cut too fast for the sample rate to describe"
    if template.seconds < LEAST_SECONDS or template.shots < LEAST_SHOTS:
        return (
            "too short",
            f"{template.seconds:.1f}s, {template.shots} shots — kept out of the library",
        )
    return "template", (
        f"{template.shots} shots, {template.shot_seconds:.3f}s median, " f"{template.seconds:.1f}s"
    )


def read_still(path: Path) -> tuple[str, str]:
    from PIL import Image

    with Image.open(path) as picture:
        picture.load()
        width, height = picture.size
        mode = picture.mode
    if width < 64 or height < 64:
        return "too small", f"{width}x{height}"
    return "photo", f"{width}x{height} {mode}"


def read_table(path: Path) -> tuple[str, str]:
    from auteur.insight.dataset import load

    rows = load([path])
    if not rows:
        return "no rows", "opened, and nothing in it the loader recognised"
    return "export", f"{len(rows)} rows"


def main() -> int:
    if not FOLDER.is_dir():
        print(f"{FOLDER} is not a folder")
        return 1

    files = sorted(p for p in FOLDER.iterdir() if p.is_file())
    print(f"{len(files)} attachment(s) in {FOLDER}\n")

    tally: Counter[str] = Counter()
    trouble: list[str] = []

    for path in files:
        kind = path.suffix.lower()
        try:
            if kind in REELS:
                verdict, detail = read_reel(path)
            elif kind in STILLS:
                verdict, detail = read_still(path)
            elif kind in TABLES:
                verdict, detail = read_table(path)
            else:
                verdict, detail = "ignored", "not a kind this program reads"
        except Exception as exc:  # noqa: BLE001 - an unreadable file is the finding
            verdict, detail = "FAILED", f"{type(exc).__name__}: {exc}"

        tally[verdict] += 1
        if verdict in {"FAILED", "unreadable", "no rows"}:
            trouble.append(f"{path.name}: {detail}")
        print(f"  {verdict:11s} {path.name[:34]:36s} {detail}")

    print("\n" + "=" * 60)
    for verdict, count in sorted(tally.items(), key=lambda row: -row[1]):
        print(f"  {count:4d}  {verdict}")
    print("=" * 60)

    if trouble:
        print(f"\n{len(trouble)} attachment(s) this program could not read:")
        for line in trouble:
            print(f"  {line}")
        return 1
    print("\nevery attachment was read by the part of the program meant to read it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
