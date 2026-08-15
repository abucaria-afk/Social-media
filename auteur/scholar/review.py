"""Output review — the Scholar watches the final cut before it ships.

The editing crew optimises predicted metrics. The Scholar has a broader view:
it has watched hundreds of tutorials and reference videos, and can spot things
the narrow-objective agents miss:

- A colour grade that contradicts what the colour theory discipline says works.
- A cut rhythm that ignores what the animation/pacing tutorials demonstrated.
- A composition that violates principles the art basics discipline teaches.
- A sound design choice that music theory says will hurt retention.
- An NLE-specific rendering artefact it learned to recognise from tool tutorials.

The review produces `ReviewFinding` objects — each one a potential problem with
a suggested fix and the learnings that back it up. These go to the gate as
proposals, not as direct changes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections.abc import Sequence

from ..edl import EditDecisionList
from ..insight import FitReport, Prediction, predict
from ..agents.base import Proposal, Risk
from ..agents.gaze import (
    GazeAgent,
    _composition_score,
    _exposure_balance,
    _focal_weight,
    _palette_drift,
)
from .knowledge import Confidence, Discipline, KnowledgeStore, Learning

log = logging.getLogger("auteur.scholar.review")


@dataclass
class ReviewFinding:
    """One thing the Scholar noticed in the final output."""

    #: What category of issue this is.
    category: str
    #: A concise description of the problem.
    description: str
    #: What the Scholar suggests doing about it.
    suggestion: str
    #: How severe — does this hurt the piece or merely miss an opportunity?
    severity: str  # "critical" | "important" | "opportunity"
    #: Which learnings informed this finding.
    supporting_learnings: list[Learning] = field(default_factory=list)
    #: Which shot(s) this applies to, if specific.
    shot_indices: list[int] = field(default_factory=list)
    #: The discipline knowledge that backs this up.
    discipline: Discipline | None = None

    def to_json(self) -> dict:
        return {
            "category": self.category,
            "description": self.description,
            "suggestion": self.suggestion,
            "severity": self.severity,
            "supporting_learnings": [l.learning_id for l in self.supporting_learnings],
            "shot_indices": self.shot_indices,
            "discipline": self.discipline.value if self.discipline else None,
        }

    def describe(self) -> str:
        severity_icon = {"critical": "🔴", "important": "🟡", "opportunity": "🟢"}.get(
            self.severity, "⚪"
        )
        return (
            f"{severity_icon} [{self.category}] {self.description}\n"
            f"    Suggestion: {self.suggestion}\n"
            f"    Backed by {len(self.supporting_learnings)} learnings"
        )


class OutputReview:
    """The Scholar's final review pass — watches the output with everything it knows.

    This runs *after* the crew finishes and *before* the gate approves publishing.
    It combines the Gaze agent's perceptual analysis with the Scholar's accumulated
    knowledge to catch issues the narrow-objective agents are blind to.
    """

    def __init__(self, store: KnowledgeStore):
        self._store = store
        self._gaze = GazeAgent()

    def review(
        self,
        edl: EditDecisionList,
        prediction: Prediction,
        model: FitReport,
    ) -> list[ReviewFinding]:
        """Review the final output and report findings.

        This is the Scholar's most important job: it watches the final product
        with the eye of someone who has studied all sixteen disciplines and
        every NLE tool, and flags what the metric-optimising agents missed.
        """
        findings: list[ReviewFinding] = []

        # 1. Run the Gaze analysis as the perceptual foundation
        findings.extend(self._review_composition(edl))

        # 2. Apply colour theory knowledge
        findings.extend(self._review_colour(edl))

        # 3. Apply pacing/rhythm knowledge from music theory and animation
        findings.extend(self._review_rhythm(edl))

        # 4. Apply cinematography and directing knowledge
        findings.extend(self._review_cinematography(edl))

        # 5. Check for NLE-specific issues the Scholar has learned about
        findings.extend(self._review_tool_specific(edl))

        # 6. Apply psychology and human behavior knowledge to structure
        findings.extend(self._review_psychology(edl, prediction))

        return findings

    def review_as_proposals(
        self,
        edl: EditDecisionList,
        prediction: Prediction,
        model: FitReport,
    ) -> list[Proposal]:
        """Convert review findings into proposals that the gate can approve.

        This is how the Scholar's review integrates with the existing crew
        system — findings become proposals with the same shape as any other
        agent's suggestions.
        """
        findings = self.review(edl, prediction, model)
        proposals: list[Proposal] = []

        for finding in findings:
            risk = {
                "critical": Risk.HIGH,
                "important": Risk.MEDIUM,
                "opportunity": Risk.LOW,
            }.get(finding.severity, Risk.LOW)

            proposals.append(
                Proposal(
                    agent="scholar",
                    title=f"[Review] {finding.description}",
                    reason=f"{finding.suggestion} (backed by {len(finding.supporting_learnings)} learnings)",
                    change=lambda edl: None,  # Review findings are advisory
                    objective="knowledge_application",
                    risk=risk,
                    binding=False,
                )
            )

        return proposals

    def _review_composition(self, edl: EditDecisionList) -> list[ReviewFinding]:
        """Apply art basics and photography knowledge to composition."""
        findings: list[ReviewFinding] = []

        if not edl.shots:
            return findings

        # Check focal weight against art basics knowledge
        weights = _focal_weight(edl)
        weak_shots = [i for i, w in enumerate(weights) if w < 0.3]

        if weak_shots:
            art_learnings = self._store.by_discipline(Discipline.ART_BASICS)
            composition_learnings = [
                l
                for l in art_learnings
                if any(
                    kw in l.technique.lower() for kw in ("composition", "focal", "eye", "attention")
                )
            ]

            if composition_learnings:
                findings.append(
                    ReviewFinding(
                        category="composition",
                        description=f"{len(weak_shots)} shots lack a clear focal anchor",
                        suggestion=(
                            f"Art basics principle: {composition_learnings[0].technique}. "
                            f"{composition_learnings[0].application}"
                        ),
                        severity="important",
                        supporting_learnings=composition_learnings[:3],
                        shot_indices=weak_shots,
                        discipline=Discipline.ART_BASICS,
                    )
                )

        return findings

    def _review_colour(self, edl: EditDecisionList) -> list[ReviewFinding]:
        """Apply colour theory knowledge to the grade."""
        findings: list[ReviewFinding] = []

        if len(edl.shots) < 2:
            return findings

        drift = _palette_drift(edl)
        if drift > 0.4:
            colour_learnings = self._store.by_discipline(Discipline.COLOR_THEORY)
            palette_learnings = [
                l
                for l in colour_learnings
                if any(
                    kw in l.technique.lower()
                    for kw in ("palette", "harmony", "continuity", "temperature")
                )
            ]

            if palette_learnings:
                findings.append(
                    ReviewFinding(
                        category="colour",
                        description=f"Colour temperature drifts {drift:.0%} — breaks palette unity",
                        suggestion=(
                            f"Colour theory: {palette_learnings[0].technique}. "
                            f"{palette_learnings[0].application}"
                        ),
                        severity="important",
                        supporting_learnings=palette_learnings[:3],
                        discipline=Discipline.COLOR_THEORY,
                    )
                )

        return findings

    def _review_rhythm(self, edl: EditDecisionList) -> list[ReviewFinding]:
        """Apply music theory and animation knowledge to pacing."""
        findings: list[ReviewFinding] = []

        if len(edl.shots) < 3:
            return findings

        # Check cut rhythm — are the intervals too uniform or too chaotic?
        durations = [shot.duration for shot in edl.shots]
        avg_dur = sum(durations) / len(durations)
        variance = sum((d - avg_dur) ** 2 for d in durations) / len(durations)

        # Too uniform (variance near zero) — monotonous
        if variance < 0.01 and len(durations) > 4:
            music_learnings = self._store.by_discipline(Discipline.MUSIC_THEORY)
            rhythm_learnings = [
                l
                for l in music_learnings
                if any(
                    kw in l.technique.lower()
                    for kw in ("rhythm", "variation", "syncopation", "dynamic")
                )
            ]
            if rhythm_learnings:
                findings.append(
                    ReviewFinding(
                        category="rhythm",
                        description="Cut rhythm is too uniform — risks monotony",
                        suggestion=(
                            f"Music theory: {rhythm_learnings[0].technique}. "
                            "Vary shot duration to create rhythmic interest."
                        ),
                        severity="opportunity",
                        supporting_learnings=rhythm_learnings[:3],
                        discipline=Discipline.MUSIC_THEORY,
                    )
                )

        return findings

    def _review_cinematography(self, edl: EditDecisionList) -> list[ReviewFinding]:
        """Apply cinematography and directing knowledge."""
        findings: list[ReviewFinding] = []

        if not edl.shots:
            return findings

        # Check for held shots without motivation (directing knowledge)
        long_static = [
            i
            for i, shot in enumerate(edl.shots)
            if shot.duration > 3.0 and shot.motion.kind == "none"
        ]

        if long_static:
            cinema_learnings = self._store.by_discipline(Discipline.CINEMATOGRAPHY)
            directing_learnings = self._store.by_discipline(Discipline.DIRECTING)
            relevant = [
                l
                for l in cinema_learnings + directing_learnings
                if any(
                    kw in l.technique.lower()
                    for kw in ("movement", "pace", "rhythm", "hold", "static")
                )
            ]
            if relevant:
                findings.append(
                    ReviewFinding(
                        category="cinematography",
                        description=f"{len(long_static)} shots hold static for 3+ seconds without camera motivation",
                        suggestion=(
                            f"Cinematography: {relevant[0].technique}. "
                            "Consider subtle movement or justify the stillness with content."
                        ),
                        severity="opportunity",
                        supporting_learnings=relevant[:3],
                        shot_indices=long_static,
                        discipline=Discipline.CINEMATOGRAPHY,
                    )
                )

        return findings

    def _review_tool_specific(self, edl: EditDecisionList) -> list[ReviewFinding]:
        """Check for issues the Scholar learned about from NLE tutorials.

        These are things like: render artefacts at certain settings, export
        codec issues, transition rendering bugs, colour space mismatches —
        the kind of thing you only know if you've watched the tutorial.
        """
        findings: list[ReviewFinding] = []

        # Look for validated tool-specific learnings about common issues
        for tool_discipline in (
            Discipline.PREMIERE_PRO,
            Discipline.DAVINCI_RESOLVE,
            Discipline.CAPCUT,
            Discipline.AFTER_EFFECTS,
        ):
            tool_learnings = self._store.by_discipline(tool_discipline)
            issue_learnings = [
                l
                for l in tool_learnings
                if l.confidence in (Confidence.VALIDATED, Confidence.SUPPORTED)
                and any(
                    kw in l.insight.lower()
                    for kw in ("avoid", "issue", "bug", "artefact", "problem")
                )
            ]
            for learning in issue_learnings[:2]:
                findings.append(
                    ReviewFinding(
                        category="tool_knowledge",
                        description=f"[{tool_discipline.value}] {learning.insight}",
                        suggestion=learning.application,
                        severity="opportunity",
                        supporting_learnings=[learning],
                        discipline=tool_discipline,
                    )
                )

        return findings

    def _review_psychology(
        self, edl: EditDecisionList, prediction: Prediction
    ) -> list[ReviewFinding]:
        """Apply psychology and human behavior knowledge to structure."""
        findings: list[ReviewFinding] = []

        if not edl.shots:
            return findings

        # Peak-end rule: is the ending strong?
        psych_learnings = self._store.by_discipline(Discipline.PSYCHOLOGY)
        peak_end = [
            l
            for l in psych_learnings
            if "peak" in l.technique.lower() or "end" in l.technique.lower()
        ]

        if peak_end and len(edl.shots) >= 3:
            last_shot = edl.shots[-1]
            # A very short final shot may waste the ending
            if last_shot.duration < 0.8:
                findings.append(
                    ReviewFinding(
                        category="psychology",
                        description="Final shot is under 0.8s — may not register as an ending",
                        suggestion=(
                            f"Psychology (peak-end rule): {peak_end[0].technique}. "
                            "The ending needs enough time to land."
                        ),
                        severity="important",
                        supporting_learnings=peak_end[:2],
                        shot_indices=[len(edl.shots) - 1],
                        discipline=Discipline.PSYCHOLOGY,
                    )
                )

        return findings
