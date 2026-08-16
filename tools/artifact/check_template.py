"""Does a film cut from a template land its cuts where the reference did?

The claim is frame-by-frame reproduction of a reel's *timing* using somebody
else's pictures. That is checkable: read the reel, cast the template against a
folder of photographs, render it, then measure the rendered file the same way
the reel was measured and compare the two cut lists.

The comparison is against the reference's own numbers, not against the
template's — reading the template back would be the template checking itself.

    python3 tools/artifact/check_template.py <reel.mp4> <folder of photos>
"""

import sys
import tempfile
from pathlib import Path

from auteur.insight import template as tpl


def main() -> int:
    reel = Path(sys.argv[1]).resolve()
    photos = sorted(
        p
        for p in Path(sys.argv[2]).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    work = Path(tempfile.mkdtemp(prefix="auteur-template-"))

    original = tpl.read(reel)
    if original is None:
        print("that reel would not open")
        return 1
    print("reference: ", original.describe())
    if original.under_resolved:
        print("  (the decode could not resolve this cutting — numbers are a floor)")

    film = tpl.cast(original, photos, words=["PORTLAND", "JULY"])
    print(f"cast:       {len(film.shots)} shots from {len(photos)} photographs")
    print(f"            {film.rationale}")

    from auteur.config import FORMATS, Settings, Workspace
    from auteur.render import render

    result = render(
        film,
        Workspace(work / "render"),
        Settings(),
        formats=(FORMATS["reel"],),
        name="from-template",
    )
    made = result.primary
    if made is None:
        print("nothing rendered")
        return 1
    print(f"rendered:   {made.name}")

    replay = tpl.read(made, name="the film it made")
    if replay is None:
        print("the rendered film would not open")
        return 1
    print("made:      ", replay.describe())

    # Cut for cut. Both lists are measured by the same code off real files, so
    # the difference is the error in reproducing the timing — not a comparison
    # of a measurement against the plan that produced it.
    wanted = [beat.start for beat in original.beats]
    got = [beat.start for beat in replay.beats]
    print(f"\ncuts: reference {len(wanted)}, made {len(got)}")

    # Matched to the nearest cut, not pairwise by index. Two shots of the same
    # subject either side of a cut are sometimes invisible to any detector, and
    # one cut missed that way shifts every index after it — comparing by
    # position then reports seconds of drift for a film that is actually in
    # step. The unmatched cuts are counted separately, which is the honest
    # place for that cost.
    if wanted and got:
        drift = sorted(min(abs(g - w) for g in got) for w in wanted)
        missed = sum(1 for d in drift if d > 0.25)
        print(f"  median cut lands {drift[len(drift) // 2] * 1000:.0f}ms from the reference's")
        print(f"  90th percentile: {drift[int(len(drift) * 0.9)] * 1000:.0f}ms")
        print(f"  reference cuts with no match within 250ms: {missed} of {len(wanted)}")

    holds_a = sorted(beat.duration for beat in original.beats)
    holds_b = sorted(beat.duration for beat in replay.beats)
    print(
        f"  median hold: reference {holds_a[len(holds_a) // 2]:.3f}s, "
        f"made {holds_b[len(holds_b) // 2]:.3f}s"
    )
    print(
        f"  cuts per 10s: reference {original.cuts_per_10s:.1f}, " f"made {replay.cuts_per_10s:.1f}"
    )

    close = abs(replay.cuts_per_10s - original.cuts_per_10s) < original.cuts_per_10s * 0.25
    print("\nreproduces the cutting:", "yes" if close else "NO — the rate does not match")
    print(f"work kept in {work}")
    return 0 if close else 1


if __name__ == "__main__":
    sys.exit(main())
