"""One cut, and what the Scholar, the Gaze agent and the critic each say about it.

They are three different kinds of judgement and it is worth seeing them
together, because they disagree in useful ways:

* **the Scholar** holds the cut against films it has measured. Every finding
  names a number, the number it is held against, and where that number came
  from. It cannot say "it feels slow".
* **the Gaze agent** is the curator's eye — composition, exposure, colour
  continuity, and whether the film is varied enough to have been authored at
  all. It reads the relationships between shots rather than any shot.
* **the critic** watches the finished thing back and scores it, which is the
  only one of the three that looks at what actually came out rather than at
  the plan.

Run against the same timeline so the three columns are opinions about one
film rather than three films.

    python3 tools/artifact/three_opinions.py <folder of photos> [prompt]
"""

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

FOLDER = Path(sys.argv[1]).resolve()
PROMPT = sys.argv[2] if len(sys.argv) > 2 else 'a 90s hypercut, "SUMMER", 12 seconds'
WIDTH = 96


def rule(title: str) -> None:
    print("\n" + "─" * WIDTH)
    print(f"  {title}")
    print("─" * WIDTH)


def wrapped(text: str, indent: str = "      ") -> None:
    for line in textwrap.wrap(str(text), WIDTH - len(indent)):
        print(indent + line)


def main() -> int:
    from auteur.agents.gaze import GazeAgent
    from auteur.agent import direct
    from auteur.insight import fit
    from auteur.config import Settings
    from auteur.insight import predict
    from auteur.scholar import Scholar

    print(f"  footage : {FOLDER}")
    print(f"  prompt  : {PROMPT!r}")
    print("  rendering — the critic watches the finished file, so there has to be one\n")

    settings = Settings(target_duration=12.0, revision_rounds=1)
    production = direct(
        [FOLDER],
        PROMPT,
        settings=settings,
        workspace=Path("/tmp/three-opinions"),
        duration=None,
    )
    edl = production.edl

    # ---- the cut ---------------------------------------------------------
    rule("THE CUT — what a person actually receives")
    holds = sorted(shot.duration for shot in edl.shots)
    joins: dict[str, int] = {}
    for shot in edl.shots[1:]:
        kind = "cut" if shot.transition_in.is_cut else shot.transition_in.kind
        joins[kind] = joins.get(kind, 0) + 1
    moves: dict[str, int] = {}
    for shot in edl.shots:
        moves[shot.motion.kind] = moves.get(shot.motion.kind, 0) + 1
    looks = sorted({shot.look.preset for shot in edl.shots if shot.look.preset})
    print(f"      {len(edl.shots)} shots over {edl.duration:.1f}s")
    if holds:
        print(f"      median hold {holds[len(holds) // 2]:.3f}s")
    print(f"      joins   {joins}")
    print(f"      moves   {moves}")
    print(f"      graded  {looks or ['(none)']}")
    print(f"      file    {production.primary}")

    # ---- the Scholar -----------------------------------------------------
    rule("THE SCHOLAR — this cut against films it has measured")
    scholar = Scholar()
    print(f"      {scholar.knowledge.total_learnings} learnings held\n")
    findings = scholar.critique(edl)
    if not findings:
        print("      nothing it has studied contradicts this cut")
    for finding in findings[:6]:
        wrapped(getattr(finding, "note", None) or getattr(finding, "reason", str(finding)))
        print()

    # ---- the Gaze agent --------------------------------------------------
    rule("THE GAZE AGENT — the curator's eye across the whole wall")
    # The real prediction for this timeline, not a blank one. The Gaze agent
    # is handed what the scoring model actually says about the cut, because
    # several of its judgements are relative to it.
    model = fit([])
    prediction = predict(edl, model)
    proposals = GazeAgent().inspect(edl, prediction, model)
    if not proposals:
        print("      nothing it would change")
    for proposal in proposals:
        print(f"      · {proposal.title}   [{proposal.risk.value} risk]")
        wrapped(proposal.reason, "        ")
        print()

    # ---- the critic ------------------------------------------------------
    rule("THE CRITIC — what it says having watched the finished file")
    critique = production.final_critique
    if critique is None:
        print("      the render produced no critique")
    else:
        print(f"      score {critique.score:.2f}\n")
        if not critique.notes:
            print("      nothing to fix")
        for note in sorted(critique.notes, key=lambda n: -n.severity)[:8]:
            wrapped(str(note), "      · ")
        if critique.measured:
            print("\n      measured off the frames:")
            for key, value in sorted(critique.measured.items())[:10]:
                print(f"        {key:30s} {value:.3f}")

    # ---- side by side ----------------------------------------------------
    rule("SIDE BY SIDE")
    score = f"{critique.score:.2f}" if critique else "—"
    notes = len(critique.notes) if critique else 0
    print(
        f"      {'the Scholar':18s} {len(findings):3d} finding(s)   each naming a measured number"
    )
    print(
        f"      {'the Gaze agent':18s} {len(proposals):3d} proposal(s)  about how it reads as one piece"
    )
    print(
        f"      {'the critic':18s} {notes:3d} note(s)      score {score}, off the rendered frames"
    )
    print()
    print("      They are not redundant, and the difference is the point. The Scholar")
    print("      can only speak about things it has measured elsewhere. The Gaze agent")
    print("      only about relationships between shots. The critic only about what")
    print("      actually came out. A cut all three pass is a stronger claim than a")
    print("      cut any one of them passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
