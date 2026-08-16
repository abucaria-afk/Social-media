"""Agents that change the edit, and the gate that stops them shipping it.

An agent here is small and boring on purpose: it looks at a planned timeline and
a prediction about it, and returns *proposals* — each one a named change, a
sentence of reasoning, and a function that performs it. It does not render, it
does not post, and it cannot decide that its own work is finished.

**The gate is the point.** Autonomy in this package means an agent may
restructure a cut without being asked, run several rounds, and keep only what
scores better. It does not mean an agent may publish. Everything that leaves
the machine — a render, a queue entry, a post — goes through `Gate`, and in
every mode except one there is a person on the other side of it. The one
exception, `Mode.AUTONOMOUS`, still refuses to schedule or publish; it only
lets the editing rounds run without interruption.

That is not caution for its own sake. An agent optimising a predicted number
will happily produce something that scores well and is not what you meant, and
the only reliable check on that is a person who looks at it.
"""

from __future__ import annotations

import copy
import enum
import time
from dataclasses import dataclass, field
from typing import Protocol
from collections.abc import Callable, Sequence

from ..edl import EditDecisionList
from ..insight import FitReport, Prediction, predict
from .preview import changes_the_picture

#: A change to an edit. Takes the EDL and mutates it in place.
Change = Callable[[EditDecisionList], None]


class Mode(enum.Enum):
    """How much rope the agents get."""

    #: Every proposal is shown and waits for a person. Nothing is applied
    #: without a yes.
    MANUAL = "manual"
    #: Low-risk proposals apply themselves; anything above the risk threshold
    #: waits. The default, and the one worth using.
    SUPERVISED = "supervised"
    #: Editing rounds run to completion without interruption. Publishing and
    #: scheduling still require a person — there is no mode in which they do not.
    AUTONOMOUS = "autonomous"


class Risk(enum.IntEnum):
    """How much of the film a proposal is willing to move."""

    #: Nudges a number. Reversible, local, hard to get wrong.
    LOW = 1
    #: Changes structure — reorders shots, drops one, moves a title.
    MEDIUM = 2
    #: Changes what the film *is*: runtime, subject order, the ending.
    HIGH = 3


@dataclass
class Proposal:
    """One change an agent wants to make."""

    agent: str
    title: str
    reason: str
    change: Change
    objective: str
    risk: Risk = Risk.LOW
    #: A direct instruction rather than an optimisation. Binding proposals skip
    #: the crew's "does this improve the prediction?" test, because the answer
    #: is irrelevant: somebody pointed at footage and said make it like that.
    #: They still go to the gate — binding means the *model* does not get a
    #: veto, not that the person does not.
    binding: bool = False
    #: Filled in by the crew once the change has been tried against the model.
    predicted_gain: float = 0.0
    #: How much better or worse the picture got, when the crew could see it.
    #: Zero means it was never looked at, not that nothing changed.
    craft_gain: float = 0.0
    #: source / baseline / candidate, for anything that wants to show its work.
    comparison: object | None = field(default=None, repr=False)
    applied: bool = False
    decided_by: str = ""
    decision_note: str = ""

    def to_json(self) -> dict:
        return {
            "agent": self.agent,
            "title": self.title,
            "reason": self.reason,
            "objective": self.objective,
            "risk": self.risk.name.lower(),
            "binding": self.binding,
            "predicted_gain": round(self.predicted_gain, 4),
            "craft_gain": round(self.craft_gain, 4),
            "comparison": self.comparison.to_json() if self.comparison is not None else None,
            "applied": self.applied,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
        }

    def describe(self) -> str:
        arrow = "+" if self.predicted_gain >= 0 else ""
        return (
            f"[{self.agent}] {self.title} "
            f"({arrow}{self.predicted_gain:.1%} {self.objective}, {self.risk.name.lower()} risk)"
            f"\n    {self.reason}"
        )


class Agent(Protocol):
    """Anything that can look at an edit and suggest changes."""

    name: str
    objective: str

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]: ...


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: Answers a human can give. `EDIT` means "not like that" and sends the
#: proposal back with a note, which is the answer people actually want to give
#: and which most approval flows do not offer.
Decision = str  # "approve" | "reject" | "edit"


class Gate:
    """The thing standing between an agent and the outside world.

    `ask` is called for every proposal that needs a person, and for every
    publish. The default implementation refuses anything it cannot ask about,
    because a gate that silently approves when nobody is listening is not a
    gate — it is a delay.
    """

    def __init__(
        self,
        mode: Mode = Mode.SUPERVISED,
        *,
        on_ask: Callable[[Proposal], tuple[Decision, str]] | None = None,
        auto_below: Risk = Risk.MEDIUM,
    ):
        self.mode = mode
        self.on_ask = on_ask
        self.auto_below = auto_below
        self.log: list[tuple[str, str, str]] = []

    def needs_a_person(self, proposal: Proposal) -> bool:
        if self.mode is Mode.MANUAL:
            return True
        if self.mode is Mode.AUTONOMOUS:
            return False
        return proposal.risk >= self.auto_below

    def review(self, proposal: Proposal) -> bool:
        """Decide a proposal. Returns whether to apply it."""
        if not self.needs_a_person(proposal):
            proposal.decided_by = f"auto ({self.mode.value})"
            self.log.append((proposal.title, "auto-approved", ""))
            return True

        if self.on_ask is None:
            # Nobody to ask. Refuse rather than assume — the alternative is an
            # unattended process quietly making every decision for itself.
            proposal.decided_by = "held"
            proposal.decision_note = "no reviewer available"
            self.log.append((proposal.title, "held", "no reviewer available"))
            return False

        answer, note = self.on_ask(proposal)
        proposal.decided_by = "human"
        proposal.decision_note = note
        self.log.append((proposal.title, answer, note))
        return answer == "approve"

    def may_publish(self, what: str) -> bool:
        """Nothing reaches a platform or a queue without this returning True.

        There is deliberately no mode that short-circuits it. An agent that
        could schedule its own output would be one bad objective away from
        posting a fortnight of it.
        """
        if self.on_ask is None:
            self.log.append((what, "blocked", "publishing always needs a person"))
            return False
        answer, note = self.on_ask(
            Proposal(
                agent="crew",
                title=f"Publish: {what}",
                reason="This leaves the machine. Nothing here can approve it for you.",
                change=lambda edl: None,
                objective="publish",
                risk=Risk.HIGH,
            )
        )
        self.log.append((what, answer, note))
        return answer == "approve"


# ---------------------------------------------------------------------------
# The crew
# ---------------------------------------------------------------------------


@dataclass
class Round:
    """One pass of every agent over the edit."""

    index: int
    before: float
    after: float
    proposals: list[Proposal] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return self.after - self.before


@dataclass
class CrewResult:
    """What the crew did, and what it thinks of the result."""

    edl: EditDecisionList
    baseline: Prediction
    final: Prediction
    rounds: list[Round] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def applied(self) -> list[Proposal]:
        return [p for round_ in self.rounds for p in round_.proposals if p.applied]

    @property
    def rejected(self) -> list[Proposal]:
        return [p for round_ in self.rounds for p in round_.proposals if not p.applied]

    @property
    def gain(self) -> float:
        return self.final.overall - self.baseline.overall

    def to_json(self) -> dict:
        return {
            "baseline": self.baseline.to_json(),
            "final": self.final.to_json(),
            "gain": round(self.gain, 4),
            "seconds": round(self.seconds, 2),
            "rounds": [
                {
                    "index": round_.index,
                    "before": round(round_.before, 4),
                    "after": round(round_.after, 4),
                    "proposals": [p.to_json() for p in round_.proposals],
                }
                for round_ in self.rounds
            ],
        }

    def describe(self) -> str:
        lines = [
            f"predicted {self.baseline.overall:.0%} → {self.final.overall:.0%} "
            f"({self.gain:+.0%}) over {len(self.rounds)} round(s)",
            "",
        ]
        for proposal in self.applied:
            lines.append(f"  applied  {proposal.title}  ({proposal.predicted_gain:+.1%})")
        for proposal in self.rejected:
            why = proposal.decision_note or "no gain"
            lines.append(f"  skipped  {proposal.title}  — {why}")
        lines.append("")
        lines.append(self.final.describe())
        return "\n".join(lines)


class Crew:
    """Runs the agents until they stop finding improvements.

    Hill climbing, and honest about it: each proposal is applied to a *copy*,
    scored, and kept only if the overall prediction actually improved. An agent
    can therefore be confidently wrong without damaging the edit — the worst it
    can do is waste a round.
    """

    def __init__(
        self,
        agents: Sequence[Agent],
        model: FitReport,
        *,
        gate: Gate | None = None,
        max_rounds: int = 3,
        min_gain: float = 0.005,
        ledger=None,
        previewer=None,
        sources=None,
        helpdesk=None,
    ):
        self.agents = list(agents)
        self.model = model
        self.gate = gate or Gate()
        self.max_rounds = max(1, max_rounds)
        self.min_gain = min_gain
        # What previous runs found to be worth doing. Advisory: it changes the
        # order things are tried in, never whether they may be tried.
        if ledger is None:
            from .ledger import NullLedger

            ledger = NullLedger()
        self.ledger = ledger

        # Lets the crew look at what it is proposing rather than only computing
        # about it. Off by default because it costs renders; when it is on, a
        # change that improves the structure and ruins the picture is visible as
        # exactly that.
        if previewer is None:
            from .preview import NullPreviewer

            previewer = NullPreviewer()
        self.previewer = previewer
        #: The footage as it arrived, for the untouched end of the comparison.
        self.sources = list(sources or [])
        # Where a stuck agent goes. Optional, and an agent that has it still has
        # to turn the answer into a proposal — nothing here reaches a frame
        # without going through the gate like everything else.
        self.helpdesk = helpdesk

    def run(self, edl: EditDecisionList) -> CrewResult:
        started = time.perf_counter()
        baseline = predict(edl, self.model)
        current = copy.deepcopy(edl)
        current_score = baseline.overall
        rounds: list[Round] = []
        #: (agent, title) pairs already turned down this run, by the model or by
        #: the person. Kept across rounds so nobody is asked twice.
        declined: set[tuple[str, str]] = set()
        #: The untouched footage is the same for every proposal, so it is read
        #: once and carried, not re-read thirteen times.
        seen_source = False

        for index in range(self.max_rounds):
            prediction = predict(current, self.model)
            round_ = Round(index=index, before=current_score, after=current_score)

            proposals: list[Proposal] = []
            for agent in self.agents:
                # Hand the help desk to anything that knows how to use it,
                # before it inspects — so a question raised this round can be
                # answered from what the Scholar already knows, this round.
                if self.helpdesk is not None and hasattr(agent, "helpdesk"):
                    agent.helpdesk = self.helpdesk
                try:
                    proposals.extend(agent.inspect(current, prediction, self.model))
                except Exception as exc:  # noqa: BLE001 - one bad agent must not stop the crew
                    round_.proposals.append(
                        Proposal(
                            agent=getattr(agent, "name", "unknown"),
                            title="agent failed",
                            reason=str(exc),
                            change=lambda edl: None,
                            objective="none",
                        )
                    )

            # Try the changes that have earned their place first. Each applied
            # change alters the timeline the next one is scored against, so a
            # marginal proposal judged after the reliable ones is judged against
            # a better cut than it would have been.
            proposals = self.ledger.order(proposals)

            improved = False
            # A binding change is applied even where it costs prediction, so
            # the round must not be judged only on whether the score went up.
            for proposal in proposals:
                # An agent that inspects the same timeline twice offers the same
                # advice twice. Most of them have no memory of the last round,
                # so a rejected proposal came back every round until the rounds
                # ran out — recomputed, rescored, and printed again each time.
                # The answer has not changed: the cut it objected to is still
                # there precisely *because* the objection was turned down.
                fingerprint = (proposal.agent, proposal.title)
                if fingerprint in declined:
                    continue

                candidate = copy.deepcopy(current)
                try:
                    proposal.change(candidate)
                except Exception as exc:  # noqa: BLE001
                    proposal.decision_note = f"change failed: {exc}"
                    round_.proposals.append(proposal)
                    continue

                scored = predict(candidate, self.model).overall
                proposal.predicted_gain = scored - current_score
                round_.proposals.append(proposal)

                # If the change touches a pixel, look at it. The structural
                # score cannot move when a grade changes — no shot got longer —
                # so without this a proposal that turns the film magenta reads
                # as perfectly neutral and gets applied on a coin flip.
                if self.previewer.enabled and changes_the_picture(current, candidate):
                    comparison = self.previewer.compare(
                        current, candidate, sources=self.sources if not seen_source else None
                    )
                    seen_source = True
                    proposal.comparison = comparison
                    proposal.craft_gain = comparison.gain
                    if comparison.candidate.craft is not None and comparison.gain < -0.02:
                        # Not a veto on taste — a veto on damage. The picture
                        # got measurably worse and the structure did not pay for
                        # it, so there is nothing here to weigh against.
                        proposal.decision_note = (
                            f"the picture gets worse by {abs(comparison.gain):.2f} — "
                            f"{comparison.candidate.craft.describe()}"
                        )
                        declined.add(fingerprint)
                        continue

                # A binding proposal is applied on the strength of the
                # instruction behind it, not on the strength of the model's
                # opinion of it. Gating these on predicted gain let a
                # correlation across a population overrule a person pointing
                # at their own reference footage — which is the exact thing
                # the style agent exists to prevent.
                if not proposal.binding and proposal.predicted_gain < self.min_gain:
                    # Unless the eye disagrees with the model. A change the
                    # structural score is blind to — a grade, a reframe — can
                    # still be worth making, and this is the only place that
                    # evidence exists.
                    if proposal.craft_gain > 0.02:
                        proposal.decision_note = (
                            f"no structural gain, but the picture improves by "
                            f"{proposal.craft_gain:.2f}"
                        )
                    else:
                        proposal.decision_note = proposal.decision_note or "no predicted gain"
                        declined.add(fingerprint)
                        continue
                if not self.gate.review(proposal):
                    declined.add(fingerprint)
                    continue

                current, current_score = candidate, scored
                proposal.applied = True
                improved = improved or not proposal.binding

            round_.after = current_score
            rounds.append(round_)
            if not improved:
                break

        # Remember what this run found, so the next one starts from somewhere.
        self.ledger.record([p for round_ in rounds for p in round_.proposals])

        return CrewResult(
            edl=current,
            baseline=baseline,
            final=predict(current, self.model),
            rounds=rounds,
            seconds=time.perf_counter() - started,
        )
