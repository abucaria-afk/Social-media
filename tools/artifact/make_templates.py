"""Turn reference reels into templates the browser renderer can cut to.

A template is a reel's *timeline* — where its cuts fall and what each shot is
doing tonally — kept so a different set of photographs can be cut to the same
shape. It is not the reel's footage and it carries none: what comes out the
other side is a list of numbers.

`auteur.insight.template` has been able to read one since it was written, and
nothing could use it. It was a library function with a CLI in front of it,
which meant the measurements existed and the app cut to a generic cadence
anyway. This writes them where the published page can reach them.

    python3 tools/artifact/make_templates.py <folder of reels> [out.json]

Every distinct reel becomes a template, so a person can cut to any of them
frame for frame rather than to one representative of each speed.

Beats are written as flat arrays rather than objects. Fifty shots times seven
named fields is most of the file size and none of the information, and the
page reads them back by position.
"""

import json
import sys
from pathlib import Path

from auteur.insight.template import read

REELS = Path(sys.argv[1])
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "templates.json"

#: Shortest reel worth keeping as a template. Under this there is not enough
#: timeline to cut anything else to.
LEAST_SECONDS = 4.0
#: And the fewest shots. A four-shot template is a cadence, not a shape.
LEAST_SHOTS = 8


def character(median: float, per_ten: float) -> tuple[str, str]:
    """A name a person can choose between, from what the reel measures as.

    Named after the cutting rather than after the file, because the file is a
    hash from somebody's camera roll and "ceded785" is not a choice anyone can
    make. The thresholds are the measured spread of the references.

    Both numbers are stated because they disagree and the disagreement is the
    interesting part. The name comes from the median hold, which is what a
    shot is *usually*; the rate comes from the mean, which a handful of long
    shots drags down. Describing a reel by one and naming it by the other
    produced "Razor — 25 cuts every ten seconds", which reads as a mistake and
    was one.
    """
    rate = f"{median:.2f}s a shot, {per_ten:.0f} cuts every ten seconds on average"
    if median <= 0.14:
        return "Razor", f"{rate} — as fast as it gets"
    if median <= 0.20:
        return "Hypercut", f"{rate} — three to a beat"
    if median <= 0.40:
        return "Quick", f"{rate} — room to see each one"
    return "Held", f"{rate} — it lets shots land"


def main() -> int:
    found = sorted(
        p for p in REELS.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
    )
    if not found:
        print(f"no reels in {REELS}")
        return 1

    out: list[dict] = []
    seen: set[str] = set()
    for path in found:
        template = read(path, name=path.stem[:8])
        if template is None:
            print(f"  {path.name[:24]:26s} would not read")
            continue
        # Two copies of one file under different names are one template. The
        # upload folder is full of them and a library that lists the same reel
        # five times is a library nobody trusts.
        if template.fingerprint in seen:
            print(f"  {path.name[:24]:26s} duplicate of one already read")
            continue
        if template.seconds < LEAST_SECONDS or template.shots < LEAST_SHOTS:
            print(
                f"  {path.name[:24]:26s} too short to cut to "
                f"({template.seconds:.1f}s, {template.shots} shots)"
            )
            continue
        if template.under_resolved:
            print(f"  {path.name[:24]:26s} decode could not resolve the cutting")
            continue
        seen.add(template.fingerprint)
        name, note = character(template.shot_seconds, template.cuts_per_10s)
        out.append(
            {
                "id": template.fingerprint[:10],
                "label": name,
                "note": note,
                "seconds": round(template.seconds, 3),
                "shots": template.shots,
                "hold": round(template.shot_seconds, 4),
                # [duration, luma, contrast, saturation, warmth, motion]
                "beats": [
                    [
                        round(b.duration, 4),
                        round(b.luma, 3),
                        round(b.contrast, 3),
                        round(b.saturation, 3),
                        round(b.warmth, 3),
                        round(b.motion, 3),
                    ]
                    for b in template.beats
                ],
            }
        )
        print(
            f"  {path.name[:24]:26s} {name:9s} {template.shots:3d} shots, "
            f"median {template.shot_seconds:.3f}s"
        )

    # Every distinct reel, not one per character.
    #
    # Keeping only the fastest example of each character collapsed eighteen
    # measured reels into four, which is the opposite of what a template
    # library is for: two reels can both be hypercuts and still be completely
    # different edits, because a template is a *timeline* — where every cut
    # falls across the whole runtime — and not a cadence. Reducing them to
    # their median hold threw away the only part that was worth keeping.
    #
    # Duplicates are still collapsed, on the fingerprint, because the upload
    # folder genuinely does hold the same file under several names.
    kept = sorted(out, key=lambda e: (e["hold"], -e["shots"]))

    # Names have to be distinguishable once there are several of each kind.
    # "Hypercut" five times is a list nobody can choose from, so each carries
    # its own shot count and length — the two things that actually differ.
    counts: dict[str, int] = {}
    for entry in kept:
        counts[entry["label"]] = counts.get(entry["label"], 0) + 1
    numbered: dict[str, int] = {}
    for entry in kept:
        label = entry["label"]
        if counts[label] > 1:
            numbered[label] = numbered.get(label, 0) + 1
            entry["label"] = f"{label} {numbered[label]}"
        # Short enough to read on a chip. The long form — the cuts-per-ten-
        # seconds sentence — turned every chip into a nine-line paragraph and
        # nineteen of them into a wall nobody would scroll past on a phone.
        # The two numbers that actually tell one template from another are how
        # many shots it has and how long each one holds.
        entry["note"] = f"{entry['shots']} shots · {entry['hold']:.2f}s each"

    OUT.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    print(f"\n{len(kept)} template(s) -> {OUT} ({OUT.stat().st_size} bytes)")
    for entry in kept:
        print(f"  {entry['label']:9s} {entry['shots']:3d} shots  {entry['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
