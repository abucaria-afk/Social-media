"""The editing agents, one objective each.

Each one owns a single number and is deliberately not allowed to care about the
others. That is what makes the crew work: the hook agent will happily propose a
cut that hurts the share score, the crew scores the *overall* prediction, and
the proposal is dropped. An agent that tried to balance all three itself would
be a worse version of the crew, arrived at privately.

Every change here is a real operation on the timeline — trim a shot, move a
title, reorder, drop a tail — because a proposal that cannot be applied is an
opinion, and the edit already has enough of those.
"""

from __future__ import annotations

import copy

from ..edl import EditDecisionList, TextCue, Transition
from ..insight import FitReport, Prediction
from .base import Proposal, Risk

# ---------------------------------------------------------------------------
# What each agent steers toward
# ---------------------------------------------------------------------------
#
# Every number below is also written in prose in `docs/agent-briefs.md`, which
# is the document somebody reads to argue with the crew. Two copies of a
# threshold is one threshold and one stale number, and which is which is
# invisible from either side — so these are named here, the briefs state them,
# and `test_the_agent_briefs_state_the_numbers_the_agents_actually_use` holds
# the two together.
#
# Where a number came from is marked, because they did not all come from the
# same place and treating them alike is how a preference gets cited as
# evidence.

#: MEASURED. The three-second watch rate the winning rows clear, and the
#: midpoint between the two medians below which a film reads as a failure.
HOOK_TARGET = 0.80
HOOK_FAILURE_BOUNDARY = 0.69

#: MEASURED. Where the winners' first cut lands. The opening-length/watch
#: correlation behind it is r = -0.83, the strongest observed finding in the
#: corpus.
HOOK_FIRST_CUT_SECONDS = 1.2

#: MEASURED. Share-to-view among winners, and the completion boundary that
#: separates the two populations.
SHARE_TARGET = 0.05
SHARE_COMPLETION_BOUNDARY = 0.56

#: CHOSEN, not measured — said plainly for the same reason `pricing.TRIAL_DAYS`
#: says it. The corpus has nothing about absolute runtime; these are a form
#: convention. An edit longer than the trigger gets shortened toward the
#: target, from the tail.
SHARE_RUNTIME_TRIGGER = 22.0
SHARE_RUNTIME_TARGET = 18.0

#: CHOSEN. How much of each middle shot the "tighten the middle" proposal
#: removes.
#:
#: It was `0.78` inline — a 22% trim — while the proposal's own title, the one
#: a person reads before approving it, said "Tighten the middle by a fifth",
#: and so did the brief. Neither 0.78 nor a fifth has a source in the corpus;
#: the difference is not that one was measured and the other guessed, it is
#: that the number and the sentence describing it had drifted apart by two
#: points and nothing compared them. A fifth, once, in the place the words say.
MIDDLE_TIGHTEN = 0.20

#: MEASURED. Rewatches among winners, and the failure boundary.
LOOP_TARGET = 1.5
LOOP_FAILURE_BOUNDARY = 1.72


# ---------------------------------------------------------------------------
# 1. Hook — survive the first three seconds
# ---------------------------------------------------------------------------


class HookAgent:
    """Owns `three_second_watch_rate`.

    Trained on the rows that clear `HOOK_TARGET`. What they have in common
    is not a
    style so much as a *shape*: the first cut arrives early, something moves
    or is said before it, and the strongest frame is not being saved for later.
    """

    name = "hook"
    objective = "three_second_watch_rate"

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if not edl.shots:
            return []
        proposals: list[Proposal] = []
        ideal = model.best_hook_duration or 1.6
        opening = edl.shots[0].duration

        if opening > ideal + 0.35:

            def trim(target: EditDecisionList, ideal: float = ideal) -> None:
                shot = target.shots[0]
                # Trim from the *front*, not the back: the end of a shot is
                # usually where the movement has resolved, and the resolution
                # is the interesting frame.
                keep = ideal * (shot.source_duration / max(shot.duration, 1e-6))
                shot.start = max(shot.start, shot.end - keep)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Cut the opening from {opening:.1f}s to {ideal:.1f}s",
                    reason=(
                        f"Hooks that clear {model.elite_three_second:.0%} at three seconds cut "
                        f"by {ideal:.1f}s. This one holds for {opening:.1f}s, which is long "
                        "enough for a thumb to decide against it."
                    ),
                    change=trim,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        if not any(cue.start < max(opening, 0.6) for cue in edl.texts):

            def add_text(target: EditDecisionList) -> None:
                # The *first* cue is not necessarily a title. Taking texts[0]
                # blindly picked up the end card and dragged it to the front,
                # which both wrecked the ending and left a cue styled
                # "end-card" playing over the opening frame — and because it no
                # longer sat at the end, the loop agent could not find it to
                # remove it either.
                movable = next(
                    (cue for cue in target.texts if cue.style not in ("end-card", "chapter")),
                    None,
                )
                if movable is not None:
                    # Move the title the director already wrote rather than
                    # inventing a second one; two titles in three seconds is
                    # worse than a late one.
                    movable.start = 0.0
                    movable.duration = max(HOOK_FIRST_CUT_SECONDS, min(movable.duration, 2.0))
                else:
                    target.texts.insert(
                        0,
                        TextCue(text=target.title.upper(), start=0.0, duration=1.6, style="title"),
                    )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Land the title before the first cut",
                    reason=(
                        "Text on screen inside the first second is the single strongest "
                        "three-second signal in the data — 'Text Over Blank Screen' tops the "
                        "style ranking. Right now nothing is said until after the cut."
                    ),
                    change=add_text,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # The strongest shot leading is worth more than the strongest shot
        # landing. "Strongest" here is the longest held take the director chose,
        # which is the only quality signal available on the timeline itself.
        if len(edl.shots) > 3:
            best = max(range(1, len(edl.shots)), key=lambda i: edl.shots[i].source_duration)
            if edl.shots[best].source_duration > edl.shots[0].source_duration * 1.6:

                def lead_with_best(target: EditDecisionList, best: int = best) -> None:
                    # The index was chosen against the timeline as it was when
                    # this proposal was written. An earlier proposal in the same
                    # round may have removed shots since — the style agent
                    # thins them — so it has to be re-checked rather than
                    # trusted. It used to raise IndexError and lose the round.
                    if best >= len(target.shots) or len(target.shots) < 2:
                        return
                    shot = target.shots.pop(best)
                    shot.transition_in = Transition(kind="cut")
                    target.shots.insert(0, shot)
                    if target.shots[1].transition_in.is_cut is False:
                        target.shots[1].transition_in = Transition(kind="cut")

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title=f"Open on shot {best + 1} instead",
                        reason=(
                            "The best take is being saved for the middle. Nothing is saved in "
                            "short form — the audience that would have seen it has already gone."
                        ),
                        change=lead_with_best,
                        objective=self.objective,
                        risk=Risk.HIGH,
                    )
                )
        return proposals


# ---------------------------------------------------------------------------
# 2. Share — earn the action that leaves the seed pool
# ---------------------------------------------------------------------------


class ShareAgent:
    """Owns `share_to_view_ratio`.

    A share is the only engagement that costs the person something, which is
    why the ranking systems weight it so far above a like and why this agent
    exists separately from the hook agent. Shares grow out of *completion*: you
    do not send somebody a video you did not finish. So this agent mostly
    argues about length and pace.
    """

    name = "share"
    objective = "share_to_view_ratio"

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if len(edl.shots) < 3:
            return []
        proposals: list[Proposal] = []
        runtime = edl.duration

        if runtime > SHARE_RUNTIME_TRIGGER:
            # Drop from the tail: the end of an over-long edit is where
            # attention has already gone, so it is the cheapest thing to lose.
            surplus = runtime - SHARE_RUNTIME_TARGET

            def shorten(target: EditDecisionList, surplus: float = surplus) -> None:
                removed = 0.0
                while len(target.shots) > 3 and removed < surplus:
                    # Keep the last shot — the loop agent needs it — and drop
                    # the one before it.
                    victim = target.shots.pop(-2)
                    removed += victim.duration

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Take it from {runtime:.0f}s to about 18s",
                    reason=(
                        f"Predicted completion is {prediction.share.note.split()[-1]}, and "
                        "completion is what a share grows out of. Past twenty seconds every "
                        "extra second costs completion faster than it adds anything."
                    ),
                    change=shorten,
                    objective=self.objective,
                    risk=Risk.HIGH,
                )
            )

        pace = len(edl.shots) / max(runtime, 1.0)
        if pace < 0.8 and len(edl.shots) >= 4:

            def quicken(target: EditDecisionList) -> None:
                # Tighten the middle, leave the hook and the ending alone: both
                # are load-bearing for the other two objectives.
                for shot in target.shots[1:-1]:
                    keep = shot.source_duration * (1.0 - MIDDLE_TIGHTEN)
                    shot.end = shot.start + max(keep, 0.35)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Tighten the middle by a fifth",
                    reason=(
                        f"{pace:.1f} cuts a second is slow for the form. The middle is where "
                        "people leave, and it is the part nobody remembers being shorter."
                    ),
                    change=quicken,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )
        return proposals


# ---------------------------------------------------------------------------
# 3. Loop — hand the ending back to the beginning
# ---------------------------------------------------------------------------


class LoopAgent:
    """Owns `loop_count`.

    A seamless loop is the cheapest multiplier in short form: the second watch
    costs nothing to produce and counts as much as the first. The two things
    that break one are an ending that resolves and a fade to black, and both
    are what a normal edit does by default.
    """

    name = "loop"
    objective = "loop_count"

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if len(edl.shots) < 2:
            return []
        proposals: list[Proposal] = []
        first, last = edl.shots[0], edl.shots[-1]

        end_cards = [
            cue for cue in edl.texts if cue.style == "end-card" and cue.end >= edl.duration - 0.35
        ]
        if end_cards:

            def drop_end_card(target: EditDecisionList) -> None:
                target.texts = [
                    cue
                    for cue in target.texts
                    if not (cue.style == "end-card" and cue.end >= target.duration - 0.35)
                ]

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Remove the end card",
                    reason=(
                        "An end card is a full stop. It tells the viewer the video is over at "
                        "exactly the moment you want them to fall back into the first frame."
                    ),
                    change=drop_end_card,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        if last.clip_id != first.clip_id:

            def close_the_loop(target: EditDecisionList) -> None:
                opener = target.shots[0]
                tail = copy.deepcopy(opener)
                # A short return to the opening frame: long enough to register
                # as the same place, short enough that the join is invisible.
                #
                # Measured against the film rather than fixed at 0.9s. That
                # constant was written when a montage held a shot for 0.9s, so
                # the return was one shot long and did read as invisible. The
                # montage default is 0.334s now, which made this tail 2.7 holds
                # — and on a hypercut 5.4 — so the shot that was supposed to
                # slip past unnoticed became the longest one in the film, sat
                # at the end where a loop is meant to snap round. Two of the
                # film's own holds registers as the same place without stopping
                # the reel dead.
                holds = sorted(shot.duration for shot in target.shots)
                typical = holds[len(holds) // 2] if holds else 0.45
                span = min(max(typical * 2.0, 0.2), 0.9, opener.source_duration)
                tail.start = opener.start
                tail.end = opener.start + span
                tail.transition_in = Transition(kind="cut")
                tail.note = "loop return"
                target.shots[-1] = tail

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="End on the frame it opened on",
                    reason=(
                        "The last shot is somewhere else entirely, so the loop is a jump cut "
                        "back to a place the viewer left ten seconds ago. Returning to the "
                        "opening frame makes the second watch start before they notice."
                    ),
                    change=close_the_loop,
                    objective=self.objective,
                    risk=Risk.HIGH,
                )
            )
        elif not last.transition_in.is_cut:

            def hard_cut(target: EditDecisionList) -> None:
                target.shots[-1].transition_in = Transition(kind="cut")

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Hard cut into the last shot",
                    reason=(
                        "A dissolve into the final shot softens the join the loop depends on. "
                        "Loops want an edge to catch on."
                    ),
                    change=hard_cut,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        if last.duration > 2.0:

            def shorten_tail(target: EditDecisionList) -> None:
                tail = target.shots[-1]
                keep = 1.2 * (tail.source_duration / max(tail.duration, 1e-6))
                tail.end = tail.start + max(0.4, min(tail.source_duration, keep))

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Shorten the last shot from {last.duration:.1f}s",
                    reason=(
                        "A long final shot reads as an ending. A short one hands you back to "
                        "the top before you have decided to leave."
                    ),
                    change=shorten_tail,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )
        return proposals


def default_crew():
    """The five, in the order they should argue.

    Hook first because it is cheapest and everything else depends on somebody
    still being there; share second because it decides the runtime the loop has
    to work with; loop third because it only needs the two ends; gaze fourth
    so the visual curator can read the assembled cut; final_check last because
    it is the QC gate before export.

    The crew runs multiple rounds. That means gaze naturally re-inspects
    whatever final_check changed in the previous round — final_check applies
    its fixes, and the next pass lets gaze confirm the visual coherence still
    holds. If it does not, gaze proposes corrections and the hill-climber
    keeps only what improves the overall prediction.
    """
    from .finalcheck import FinalCheckAgent
    from .gaze import GazeAgent

    return (HookAgent(), ShareAgent(), LoopAgent(), GazeAgent(), FinalCheckAgent())
