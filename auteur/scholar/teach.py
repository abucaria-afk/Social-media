"""Teaching layer — how the Scholar shares what it knows with other agents.

The Scholar does not directly modify other agents. Instead it produces
`TeachingBrief` objects — structured context that the crew can incorporate
into its next run — and `WorkflowPatch` objects that propose changes to
the editing workflow itself.

A TeachingBrief is *context*, not a command. It surfaces relevant learnings
before the crew runs so agents can make better decisions with knowledge they
did not have before.

A WorkflowPatch is a *proposal* to change how something is done going forward.
It goes through the gate like any other proposal — the Scholar cannot rewrite
the workflow unilaterally.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .knowledge import Confidence, Discipline, KnowledgeStore, Learning

log = logging.getLogger("auteur.scholar.teach")


@dataclass
class TeachingBrief:
    """Context the Scholar surfaces to the crew before an editing run.

    This is read by the agents as additional knowledge — techniques, principles,
    and tool-specific workflows that might improve the current edit. It is
    structured so agents can decide whether a learning is relevant to their
    objective.
    """

    #: Which agent(s) this is addressed to. Empty means all agents.
    target_agents: list[str] = field(default_factory=list)
    #: The learnings being surfaced, ranked by relevance.
    learnings: list[Learning] = field(default_factory=list)
    #: A natural-language summary of what the Scholar thinks is relevant.
    summary: str = ""
    #: The discipline context these learnings apply to.
    discipline_context: list[Discipline] = field(default_factory=list)
    #: What several independent sources agree on, stated once.
    #:
    #: Measured learnings come one per film, so a brief built straight from
    #: them reads "abc123.mp4 measures 0.034 inter-frame motion" twelve times —
    #: an opaque filename and a number that generalises to nothing. An agent
    #: cannot act on that. What it can act on is the consensus: what the *set*
    #: does, with the spread, which is also the only thing several sources
    #: agreeing actually licenses you to say.
    consensus: list[str] = field(default_factory=list)
    #: When this brief was generated.
    generated_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "target_agents": self.target_agents,
            "learnings": [learning.to_json() for learning in self.learnings],
            "summary": self.summary,
            "consensus": list(self.consensus),
            "discipline_context": [d.value for d in self.discipline_context],
            "generated_at": self.generated_at,
        }

    def describe(self) -> str:
        targets = ", ".join(self.target_agents) if self.target_agents else "all agents"
        lines = [f"[Scholar → {targets}] {self.summary}"]
        if self.consensus:
            lines.extend(f"    • {line}" for line in self.consensus)
            return "\n".join(lines)
        lines.append(f"  {len(self.learnings)} relevant learnings surfaced:")
        for learning in self.learnings[:5]:
            lines.append(f"    • {learning.technique}: {learning.insight}")
        if len(self.learnings) > 5:
            lines.append(f"    … and {len(self.learnings) - 5} more")
        return "\n".join(lines)


@dataclass
class WorkflowPatch:
    """A proposed change to how the editing workflow operates.

    Unlike a Proposal (which changes an edit), a WorkflowPatch changes *how
    future edits are made*. Examples:
    - "Use 1.2s hooks instead of 2s — every tutorial on retention agrees"
    - "In DaVinci Resolve, apply colour wheels before curves for this palette"
    - "The animation timing principle says ease-out on 12 frames, not 8"

    These go through the gate and require human approval.
    """

    #: What part of the workflow this changes.
    target_workflow: str
    #: What the patch proposes to change.
    title: str
    #: The reasoning — which learnings support this change.
    reason: str
    #: The specific parameter or behavior being changed.
    parameter: str
    #: The current value/behavior.
    current_value: str
    #: The proposed new value/behavior.
    proposed_value: str
    #: The learnings that back this up.
    supporting_learnings: list[str] = field(default_factory=list)
    #: Confidence — only SUPPORTED or VALIDATED patches should be proposed.
    confidence: Confidence = Confidence.SUPPORTED
    #: Whether this has been approved by the gate.
    approved: bool = False
    approved_by: str = ""

    def to_json(self) -> dict:
        return {
            "target_workflow": self.target_workflow,
            "title": self.title,
            "reason": self.reason,
            "parameter": self.parameter,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "supporting_learnings": self.supporting_learnings,
            "confidence": self.confidence.value,
            "approved": self.approved,
            "approved_by": self.approved_by,
        }

    def describe(self) -> str:
        return (
            f"[Scholar workflow patch] {self.title}\n"
            f"  Workflow: {self.target_workflow}\n"
            f"  Change: {self.parameter}: {self.current_value!r} → {self.proposed_value!r}\n"
            f"  Reason: {self.reason}\n"
            f"  Backed by {len(self.supporting_learnings)} learnings ({self.confidence.value})"
        )


#: What a measured property is called when it is said out loud to an agent, and
#: how to render its value. Keyed on the `measurements` keys `library` writes.
MEASURED: dict[str, tuple[str, str]] = {
    "cuts_per_10s": ("cut {v} times per ten seconds", "{:.1f}"),
    "shot_seconds": ("hold a shot for {v}s", "{:.3f}"),
    "first_cut": ("cut away from the opening shot after {v}s", "{:.2f}"),
    "motion": ("measure {v} inter-frame motion", "{:.3f}"),
    "luma": ("sit at luma {v}", "{:.2f}"),
    "contrast": ("run {v} contrast", "{:.2f}"),
    "hue_spread": ("spread their hues over {v}°", "{:.0f}"),
    "clipped_black": ("put {v} of the frame at true black", "{:.0%}"),
}

#: Below this many independent sources, a "consensus" is one opinion with a
#: median taken over it.
AGREEING = 2


def consensus_from(learnings: list[Learning]) -> list[str]:
    """Collapse per-source measurements into what the set agrees on.

    Measured learnings arrive one per film. Listed individually a brief reads
    "abc123.mp4 measures 0.034 inter-frame motion" a dozen times — an opaque
    name and a number that generalises to nothing. Several sources agreeing
    licenses exactly one statement, so this makes exactly one: the median, the
    spread, and how many independent sources it rests on.
    """
    gathered: dict[str, list[tuple[float, str]]] = {}
    for learning in learnings:
        for key, value in (learning.measurements or {}).items():
            if key in MEASURED and isinstance(value, (int, float)):
                gathered.setdefault(key, []).append((float(value), learning.source_channel))

    lines: list[str] = []
    for key, (phrase, form) in MEASURED.items():
        seen = gathered.get(key) or []
        sources = {channel for _, channel in seen if channel}
        if len(sources) < AGREEING:
            continue
        values = sorted(value for value, _ in seen)
        middle = values[len(values) // 2]
        spread = ""
        if len(values) > 2 and values[-1] > values[0]:
            spread = f" (from {form.format(values[0])} to {form.format(values[-1])})"
        lines.append(
            f"Across {len(sources)} films they "
            + phrase.format(v=form.format(middle))
            + spread
            + "."
        )
    return lines


class Teacher:
    """Generates teaching briefs and workflow patches from the knowledge store.

    The Teacher observes patterns across learnings — when multiple sources agree
    on a technique, it gains confidence. When a validated learning consistently
    improves output, it proposes a workflow patch.
    """

    def __init__(self, store: KnowledgeStore):
        self._store = store

    def brief_for_agent(self, agent_name: str, *, max_learnings: int = 10) -> TeachingBrief:
        """Generate a teaching brief targeted at a specific agent."""
        # Map agent names to relevant disciplines
        agent_disciplines: dict[str, list[Discipline]] = {
            "hook": [
                Discipline.PSYCHOLOGY,
                Discipline.PATTERN_RECOGNITION,
                Discipline.HUMAN_BEHAVIOR,
                Discipline.CINEMATOGRAPHY,
            ],
            "share": [
                Discipline.HUMAN_CONDITION,
                Discipline.PSYCHOLOGY,
                Discipline.CONTENT_CREATION,
                Discipline.PHILOSOPHY,
            ],
            "loop": [
                Discipline.MUSIC_THEORY,
                Discipline.PATTERN_RECOGNITION,
                Discipline.ANIMATION,
                Discipline.CINEMATOGRAPHY,
            ],
            "gaze": [
                Discipline.COLOR_THEORY,
                Discipline.ART_BASICS,
                Discipline.ART_THEORY,
                Discipline.PHOTOGRAPHY,
                Discipline.CINEMATOGRAPHY,
                Discipline.ART_HISTORY,
            ],
            "style": [
                Discipline.DIRECTING,
                Discipline.MOVIE_MAKING,
                Discipline.CINEMATOGRAPHY,
                Discipline.ART_THEORY,
            ],
        }

        disciplines = agent_disciplines.get(agent_name, list(Discipline))
        relevant: list[Learning] = []
        for d in disciplines:
            relevant.extend(self._store.by_discipline(d))

        # Deduplicate and sort by confidence then recency
        seen: set[str] = set()
        unique: list[Learning] = []
        for learning in relevant:
            if learning.learning_id not in seen:
                seen.add(learning.learning_id)
                unique.append(learning)

        # Prefer validated > supported > tentative, then most recent
        confidence_order = {
            Confidence.VALIDATED: 0,
            Confidence.SUPPORTED: 1,
            Confidence.TENTATIVE: 2,
        }
        unique.sort(
            key=lambda learning: (
                confidence_order.get(learning.confidence, 3),
                -learning.learned_at,
            )
        )

        selected = unique[:max_learnings]
        agreed = consensus_from(unique)
        return TeachingBrief(
            target_agents=[agent_name],
            learnings=selected,
            consensus=agreed,
            summary=(
                f"{len(agreed)} thing(s) the studied films agree on, for the {agent_name} agent"
                if agreed
                else f"{len(selected)} learnings relevant to the {agent_name} agent's objective"
            ),
            discipline_context=disciplines,
        )

    def brief_for_product(self, *, max_learnings: int = 12) -> TeachingBrief:
        """What the Scholar has learned about the thing that delivers the work.

        Web design, building, accessibility, conversion, the shop. These are
        deliberately separate from every other brief, because they cannot
        become proposals: no change to an edit decision list follows from a
        rule about tap targets, and an agent handed this brief would have
        nothing to do with it.

        So it is addressed to a person. A film nobody can reach is a film
        nobody watches — if the app is unusable none of the rest counts — which
        is exactly why these learnings need somewhere to land rather than
        sitting in a store nothing reads.
        """
        from .knowledge import PRODUCT_DISCIPLINES

        seen: set[str] = set()
        found: list[Learning] = []
        for discipline in sorted(PRODUCT_DISCIPLINES, key=lambda d: d.value):
            for learning in self._store.by_discipline(discipline):
                if learning.learning_id not in seen:
                    seen.add(learning.learning_id)
                    found.append(learning)

        order = {Confidence.VALIDATED: 0, Confidence.SUPPORTED: 1, Confidence.TENTATIVE: 2}
        found.sort(key=lambda item: (order.get(item.confidence, 3), -item.learned_at))
        selected = found[:max_learnings]
        return TeachingBrief(
            target_agents=[],  # nobody in the crew: this one is for a person
            learnings=selected,
            summary=(
                f"{len(selected)} learning(s) about the app, the site and the shop"
                if selected
                else "nothing learned about the product yet — the Scholar has not studied it"
            ),
            discipline_context=sorted(PRODUCT_DISCIPLINES, key=lambda d: d.value),
        )

    def brief_for_all(self, *, max_learnings: int = 20) -> TeachingBrief:
        """Generate a general teaching brief for the whole crew."""
        validated = self._store.by_confidence(Confidence.VALIDATED)
        supported = self._store.by_confidence(Confidence.SUPPORTED)

        selected = (validated + supported)[:max_learnings]
        # Consensus over everything corroborated, not just the slice shown —
        # a median taken over a truncated list is a median of a truncation.
        agreed = consensus_from(validated + supported)
        return TeachingBrief(
            target_agents=[],
            learnings=selected,
            consensus=agreed,
            summary=(
                f"{len(agreed)} thing(s) the studied films agree on"
                if agreed
                else f"{len(selected)} high-confidence learnings for the crew"
            ),
        )

    def propose_patches(self) -> list[WorkflowPatch]:
        """Identify workflow changes backed by multiple validated learnings.

        A patch is proposed only when:
        1. At least 3 learnings agree on the same technique.
        2. At least one of them is validated (proven to help scoring).
        """
        patches: list[WorkflowPatch] = []

        # Group learnings by technique
        by_technique: dict[str, list[Learning]] = {}
        for learning in self._store.by_confidence(Confidence.SUPPORTED):
            key = learning.technique.lower().strip()
            by_technique.setdefault(key, []).append(learning)

        for _technique, learnings in by_technique.items():
            if len(learnings) < 3:
                continue

            has_validated = any(
                learning.confidence == Confidence.VALIDATED for learning in learnings
            )
            if not has_validated:
                continue

            # Build a patch proposal from the consensus
            representative = learnings[0]
            patches.append(
                WorkflowPatch(
                    target_workflow="editing",
                    title=f"Apply {representative.technique} consistently",
                    reason=(
                        f"{len(learnings)} sources agree on this technique, "
                        f"and it has been validated against output scoring."
                    ),
                    parameter=representative.technique,
                    current_value="not applied",
                    proposed_value=representative.application,
                    supporting_learnings=[learning.learning_id for learning in learnings],
                    confidence=Confidence.VALIDATED if has_validated else Confidence.SUPPORTED,
                )
            )

        return patches
