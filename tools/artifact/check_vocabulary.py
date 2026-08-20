"""Which joins each style actually reaches, over many plans.

A transition that exists in a bag and never survives the length ceiling is dead
code that reads as a feature. This plans — it does not render — a film per
style, many times over, and tallies what came out. The number that matters is
not the average: it is whether a join ever appears at all.

    python3 tools/artifact/check_vocabulary.py <folder of photos> [runs]
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

FOLDER = Path(sys.argv[1]).resolve()
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 6

PROMPTS = {
    "hypercut": 'a 90s hypercut, "SUMMER", 12 seconds',
    "montage": "fast neon montage, 20 seconds",
    "cinematic": "slow and cinematic, 20 seconds",
    "warm": "warm summer memories, gentle, 20 seconds",
}


def main() -> int:
    from auteur.analysis import build_dossiers
    from auteur.config import Settings
    from auteur.director import parse_brief
    from auteur.director import plan as plan_module
    from auteur.ingest import ingest

    bin_ = ingest([FOLDER])
    settings = Settings()
    dossiers = build_dossiers(
        bin_.visuals,
        analysis_fps=settings.quality.analysis_fps,
        analysis_width=settings.quality.analysis_width,
    )
    print(f"  {len(bin_.visuals)} asset(s), {RUNS} plans per style\n")

    missing = []
    for style, prompt in PROMPTS.items():
        joins: collections.Counter = collections.Counter()
        moves: collections.Counter = collections.Counter()
        holds: list[float] = []
        for run in range(RUNS):
            settings = Settings(seed=run)
            brief = parse_brief(prompt)
            if brief.duration:
                settings.target_duration = brief.duration
            edl = plan_module.direct(brief, dossiers, settings).edl
            for shot in edl.shots[1:]:
                joins["cut" if shot.transition_in.is_cut else shot.transition_in.kind] += 1
            for shot in edl.shots:
                moves[shot.motion.kind] += 1
                holds.append(shot.duration)
        total = sum(joins.values()) or 1
        spread = ", ".join(f"{k} {100 * v / total:.0f}%" for k, v in joins.most_common())
        print(f"  {style:<10} median hold {sorted(holds)[len(holds) // 2]:.3f}s")
        print(f"  {'':<10} joins  {spread}")
        print(f"  {'':<10} moves  {', '.join(f'{k} {v}' for k, v in moves.most_common())}")
        if len(joins) < 2:
            missing.append(f"{style}: only ever produced '{next(iter(joins))}'")
        print()

    if missing:
        print("  a style with one join is a style with no decisions in it:")
        for line in missing:
            print(f"    ✗ {line}")
        return 1
    print("  every style reaches more than one join")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
