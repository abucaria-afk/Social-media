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

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence

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
    #: When this brief was generated.
    generated_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "target_agents": self.target_agents,
            "learnings": [l.to_json() for l in self.learnings],
            "summary": self.summary,
            "discipline_context": [d.value for d in self.discipline_context],
            "generated_at": self.generated_at,
        }

    def describe(self) -> str:
        targets = ", ".join(self.target_agents) if self.target_agents else "all agents"
        lines = [
            f"[Scholar → {targets}] {self.summary}",
            f"  {len(self.learnings)} relevant learnings surfaced:",
        ]
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
                Discipline.PSYCHOLOGY, Discipline.PATTERN_RECOGNITION,
                Discipline.HUMAN_BEHAVIOR, Discipline.CINEMATOGRAPHY,
            ],
            "share": [
                Discipline.HUMAN_CONDITION, Discipline.PSYCHOLOGY,
                Discipline.CONTENT_CREATION, Discipline.PHILOSOPHY,
            ],
            "loop": [
                Discipline.MUSIC_THEORY, Discipline.PATTERN_RECOGNITION,
                Discipline.ANIMATION, Discipline.CINEMATOGRAPHY,
            ],
            "gaze": [
                Discipline.COLOR_THEORY, Discipline.ART_BASICS,
                Discipline.ART_THEORY, Discipline.PHOTOGRAPHY,
                Discipline.CINEMATOGRAPHY, Discipline.ART_HISTORY,
            ],
            "style": [
                Discipline.DIRECTING, Discipline.MOVIE_MAKING,
                Discipline.CINEMATOGRAPHY, Discipline.ART_THEORY,
            ],
        }

        disciplines = agent_disciplines.get(agent_name, list(Discipline))
        relevant: list[Learning] = []
        for d in disciplines:
            relevant.extend(self._store.by_discipline(d))

        # Deduplicate and sort by confidence then recency
        seen: set[str] = set()
        unique: list[Learning] = []
        for l in relevant:
            if l.learning_id not in seen:
                seen.add(l.learning_id)
                unique.append(l)

        # Prefer validated > supported > tentative, then most recent
        confidence_order = {Confidence.VALIDATED: 0, Confidence.SUPPORTED: 1, Confidence.TENTATIVE: 2}
        unique.sort(key=lambda l: (confidence_order.get(l.confidence, 3), -l.learned_at))

        selected = unique[:max_learnings]
        return TeachingBrief(
            target_agents=[agent_name],
            learnings=selected,
            summary=f"{len(selected)} learnings relevant to the {agent_name} agent's objective",
            discipline_context=disciplines,
        )

    def brief_for_all(self, *, max_learnings: int = 20) -> TeachingBrief:
        """Generate a general teaching brief for the whole crew."""
        validated = self._store.by_confidence(Confidence.VALIDATED)
        supported = self._store.by_confidence(Confidence.SUPPORTED)

        selected = (validated + supported)[:max_learnings]
        return TeachingBrief(
            target_agents=[],
            learnings=selected,
            summary=f"{len(selected)} high-confidence learnings for the crew",
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

        for technique, learnings in by_technique.items():
            if len(learnings) < 3:
                continue

            has_validated = any(l.confidence == Confidence.VALIDATED for l in learnings)
            if not has_validated:
                continue

            # Build a patch proposal from the consensus
            representative = learnings[0]
            patches.append(WorkflowPatch(
                target_workflow="editing",
                title=f"Apply {representative.technique} consistently",
                reason=(
                    f"{len(learnings)} sources agree on this technique, "
                    f"and it has been validated against output scoring."
                ),
                parameter=representative.technique,
                current_value="not applied",
                proposed_value=representative.application,
                supporting_learnings=[l.learning_id for l in learnings],
                confidence=Confidence.VALIDATED if has_validated else Confidence.SUPPORTED,
            ))

        return patches
