"""The Scholar's knowledge store — what it has learned, organised by discipline.

Knowledge is structured as a growing corpus of `Learning` objects, each one a
distilled insight from a watched video. Learnings are tagged by discipline,
technique, and source so the Scholar can:

- Find relevant knowledge when teaching another agent.
- Detect gaps in its understanding (disciplines with few learnings).
- Avoid watching the same tutorial twice.
- Track which NLE tool a technique applies to.
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.scholar.knowledge")


class Discipline(enum.Enum):
    """The sixteen disciplines the Scholar studies, plus NLE tools."""

    # Creative theory
    COLOR_THEORY = "color_theory"
    MUSIC_THEORY = "music_theory"
    ART_HISTORY = "art_history"
    ART_BASICS = "art_basics"
    ART_THEORY = "art_theory"
    PHOTOGRAPHY = "photography"
    CINEMATOGRAPHY = "cinematography"

    # Human understanding
    HUMAN_BEHAVIOR = "human_behavior"
    HUMAN_CONDITION = "human_condition"
    PSYCHOLOGY = "psychology"
    PHILOSOPHY = "philosophy"
    PSYCHOLOGICAL_PHILOSOPHY = "psychological_philosophy"
    PATTERN_RECOGNITION = "pattern_recognition"

    # Production craft
    CONTENT_CREATION = "content_creation"
    MOVIE_MAKING = "movie_making"
    DIRECTING = "directing"

    # Getting it seen. Distinct from making it well: a film can be cut
    # perfectly and posted into a dead hour on an account the ranking has
    # stopped showing to anybody.
    PLATFORM_ALGORITHM = "platform_algorithm"
    ANALYTICS = "analytics"
    SCHEDULING = "scheduling"

    # The product itself. A film nobody can get to is a film nobody watches,
    # so the thing that delivers the work is part of the work. These are the
    # disciplines behind the app, the site and the shop.
    WEB_DESIGN = "web_design"
    WEB_DEVELOPMENT = "web_development"
    APP_DEVELOPMENT = "app_development"
    ACCESSIBILITY = "accessibility"
    CONVERSION = "conversion"
    ECOMMERCE = "ecommerce"

    # NLE tools — learned through tutorials
    ANIMATION = "animation"
    COMPUTER_SFX = "computer_sfx"
    PREMIERE_PRO = "premiere_pro"
    DAVINCI_RESOLVE = "davinci_resolve"
    CAPCUT = "capcut"
    IMOVIE = "imovie"
    AFTER_EFFECTS = "after_effects"
    FINAL_CUT = "final_cut"


#: Disciplines that are NLE/tool-specific — the Scholar watches tutorials for these.
TOOL_DISCIPLINES = {
    Discipline.ANIMATION,
    Discipline.COMPUTER_SFX,
    Discipline.PREMIERE_PRO,
    Discipline.DAVINCI_RESOLVE,
    Discipline.CAPCUT,
    Discipline.IMOVIE,
    Discipline.AFTER_EFFECTS,
    Discipline.FINAL_CUT,
    # Building is tutorial-shaped in the same way an NLE is: somebody shows you
    # the steps. Designing is not, which is why web *design* is not in here.
    Discipline.WEB_DEVELOPMENT,
    Discipline.APP_DEVELOPMENT,
    Discipline.ECOMMERCE,
}

#: The disciplines behind the thing that delivers the work rather than the work
#: itself. Kept as its own set because their learnings do not belong to any
#: editing agent — no proposal changes an EDL because of a tap-target rule —
#: and they have to land somewhere that is not the cut.
PRODUCT_DISCIPLINES = {
    Discipline.WEB_DESIGN,
    Discipline.WEB_DEVELOPMENT,
    Discipline.APP_DEVELOPMENT,
    Discipline.ACCESSIBILITY,
    Discipline.CONVERSION,
    Discipline.ECOMMERCE,
}

#: Disciplines that are theory/knowledge — the Scholar learns principles.
THEORY_DISCIPLINES = {d for d in Discipline if d not in TOOL_DISCIPLINES}


class Confidence(enum.Enum):
    """How confident the Scholar is in a learning."""

    #: Extracted from a single source, not yet validated.
    TENTATIVE = "tentative"
    #: Seen across multiple sources — likely a real principle.
    SUPPORTED = "supported"
    #: Tested against the editing crew's scoring — confirmed to help.
    VALIDATED = "validated"


@dataclass
class Learning:
    """One thing the Scholar learned from watching a video."""

    #: A unique identifier for this learning.
    learning_id: str
    #: Which discipline(s) this belongs to.
    disciplines: list[Discipline]
    #: A concise statement of what was learned.
    insight: str
    #: The underlying principle or technique.
    technique: str
    #: Why this matters for content creation — the actionable part.
    application: str
    #: Where it came from.
    source_video_id: str
    source_channel: str
    source_title: str
    #: Timestamp range in the source where this was taught.
    source_start_sec: float = 0.0
    source_end_sec: float = 0.0
    #: Which NLE tool this applies to, if tool-specific.
    tool: str = ""
    #: How certain the Scholar is.
    confidence: Confidence = Confidence.TENTATIVE
    #: When it was learned (Unix timestamp).
    learned_at: float = field(default_factory=time.time)
    #: How many times this has been used in teaching.
    times_taught: int = 0
    #: Whether this learning has been validated against actual output scoring.
    validated_gain: float | None = None
    #: The numbers behind the sentence, when there were any.
    #:
    #: A learning taken from a measured film knows that the film cut 17.6 times
    #: per ten seconds. Written only into `insight` that fact is a string, and
    #: holding the crew to it would mean parsing English back into a float —
    #: which works until somebody rewords the sentence. Carried here it stays a
    #: number. Empty for everything learned from prose, which is most of them.
    measurements: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "learning_id": self.learning_id,
            "disciplines": [d.value for d in self.disciplines],
            "insight": self.insight,
            "technique": self.technique,
            "application": self.application,
            "source_video_id": self.source_video_id,
            "source_channel": self.source_channel,
            "source_title": self.source_title,
            "source_start_sec": self.source_start_sec,
            "source_end_sec": self.source_end_sec,
            "tool": self.tool,
            "confidence": self.confidence.value,
            "learned_at": self.learned_at,
            "times_taught": self.times_taught,
            "validated_gain": self.validated_gain,
            "measurements": dict(self.measurements),
        }

    @classmethod
    def from_json(cls, data: dict) -> Learning:
        return cls(
            learning_id=data["learning_id"],
            disciplines=[Discipline(d) for d in data["disciplines"]],
            insight=data["insight"],
            technique=data["technique"],
            application=data["application"],
            source_video_id=data["source_video_id"],
            source_channel=data["source_channel"],
            source_title=data["source_title"],
            source_start_sec=data.get("source_start_sec", 0.0),
            source_end_sec=data.get("source_end_sec", 0.0),
            tool=data.get("tool", ""),
            confidence=Confidence(data.get("confidence", "tentative")),
            learned_at=data.get("learned_at", 0.0),
            times_taught=data.get("times_taught", 0),
            validated_gain=data.get("validated_gain"),
            measurements=data.get("measurements") or {},
        )


class KnowledgeStore:
    """Persistent storage for the Scholar's accumulated learnings.

    Backed by a JSONL file that grows over time. Each line is one Learning.
    The store supports querying by discipline, by confidence level, and by
    keyword — so the teaching layer can find relevant knowledge efficiently.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or Path.home() / ".auteur" / "scholar" / "knowledge.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._learnings: list[Learning] = []
        self._watched_videos: set[str] = set()
        if self._path.exists():
            self._load()

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                learning = Learning.from_json(data)
                self._learnings.append(learning)
                self._watched_videos.add(learning.source_video_id)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("skipping corrupt learning record: %s", exc)

    def save(self) -> None:
        """Persist all learnings to disk."""
        self._path.write_text(
            "\n".join(json.dumps(learning.to_json()) for learning in self._learnings) + "\n",
            encoding="utf-8",
        )

    def add(self, learning: Learning) -> bool:
        """Record a new learning, persist it, and re-check its corroboration.

        Returns whether it was actually stored, so a caller can report what it
        kept rather than what it produced. Without that the study command said
        "46 learnings kept" on a second run over the same folder, when it had
        kept none of them.

        Ignores one it already holds. Learning ids are *derived* from the
        source and the claim rather than generated, precisely so that studying
        the same material twice is a no-op — and without this check it was not:
        reading one document three times left three copies of every sentence,
        which double-counts in every median and every gap count. That matters
        more than it sounds, because "read the folder again" is the normal way
        to use this.
        """
        if any(known.learning_id == learning.learning_id for known in self._learnings):
            return False
        self._learnings.append(learning)
        self._watched_videos.add(learning.source_video_id)
        # Append rather than rewrite — cheaper for large stores.
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(learning.to_json()) + "\n")
        self._corroborate(learning)
        return True

    #: How many *different* channels have to say the same thing before it stops
    #: being one person's opinion. Two is the smallest number that can rule out
    #: a single creator's house style, which is the failure this guards against.
    CORROBORATION = 2

    def _corroborate(self, learning: Learning) -> None:
        """Raise a technique to SUPPORTED once unrelated creators agree on it.

        Everything arrives TENTATIVE, because one person saying something on the
        internet is exactly one person saying something. But every consumer of
        this store — the teaching briefs, the review pass — asks for SUPPORTED
        or better, and nothing in the program promoted anything. The result was
        a learning loop with no exit: the Scholar could study indefinitely,
        accumulate thousands of learnings, and teach precisely none of them.

        Independent agreement is the cheapest honest promotion available here.
        Same technique, different channels, means it is a convention rather than
        one editor's habit. It is not proof the technique works — that is what
        VALIDATED is for, and that takes a measured gain on a real edit — but it
        is a real reason to raise confidence, and it is checked rather than
        assumed.
        """
        technique = (learning.technique or "").strip().lower()
        if not technique or learning.confidence != Confidence.TENTATIVE:
            return

        agreeing = {
            other.source_channel
            for other in self._learnings
            if (other.technique or "").strip().lower() == technique and other.source_channel
        }
        if len(agreeing) < self.CORROBORATION:
            return

        # Through `promote`, so there is one place a confidence level moves.
        # Written inline this did `promote`'s exact job two lines from it while
        # leaving `promote` itself with no caller anywhere in the program.
        raised = [
            other.learning_id
            for other in self._learnings
            if (other.technique or "").strip().lower() == technique
            and other.confidence == Confidence.TENTATIVE
        ]
        for learning_id in raised:
            self.promote(learning_id, Confidence.SUPPORTED, save=False)
        if raised:
            log.info(
                "%r corroborated by %d channels — now supported", learning.technique, len(agreeing)
            )
            self.save()

    def already_watched(self, video_id: str) -> bool:
        """Has the Scholar already extracted learnings from this video?"""
        return video_id in self._watched_videos

    @property
    def total_learnings(self) -> int:
        return len(self._learnings)

    def all(self) -> list[Learning]:
        """Everything known, newest first.

        A copy, so a caller iterating cannot be surprised by a study session
        appending underneath it.
        """
        return sorted(self._learnings, key=lambda learning: learning.learned_at, reverse=True)

    def by_discipline(self, discipline: Discipline) -> list[Learning]:
        """All learnings for a given discipline, newest first."""
        return sorted(
            [learning for learning in self._learnings if discipline in learning.disciplines],
            key=lambda learning: learning.learned_at,
            reverse=True,
        )

    def by_tool(self, tool: str) -> list[Learning]:
        """All learnings for a specific NLE tool."""
        tool_lower = tool.lower()
        return [learning for learning in self._learnings if learning.tool.lower() == tool_lower]

    def by_confidence(self, minimum: Confidence = Confidence.SUPPORTED) -> list[Learning]:
        """Learnings at or above a confidence level."""
        levels = list(Confidence)
        min_index = levels.index(minimum)
        return [
            learning
            for learning in self._learnings
            if levels.index(learning.confidence) >= min_index
        ]

    def search(self, keywords: str) -> list[Learning]:
        """Keyword search across insights and techniques."""
        terms = keywords.lower().split()
        results: list[Learning] = []
        for learning in self._learnings:
            text = f"{learning.insight} {learning.technique} {learning.application}".lower()
            if all(term in text for term in terms):
                results.append(learning)
        return results

    def gaps(self) -> list[Discipline]:
        """Disciplines with fewer than 5 learnings — knowledge gaps to fill."""
        counts: dict[Discipline, int] = dict.fromkeys(Discipline, 0)
        for learning in self._learnings:
            for d in learning.disciplines:
                counts[d] = counts.get(d, 0) + 1
        return [d for d, count in counts.items() if count < 5]

    def promote(self, learning_id: str, to: Confidence, *, save: bool = True) -> None:
        """Move one learning's confidence.

        `save=False` lets a caller promote a batch and write once, which is the
        difference between one file write and one per learning when a
        corroborated technique raises twenty of them together.
        """
        for learning in self._learnings:
            if learning.learning_id == learning_id:
                learning.confidence = to
                if save:
                    self.save()
                return

    def record_validation(self, learning_id: str, gain: float) -> None:
        """Record that a learning was tested and produced a measurable gain."""
        for learning in self._learnings:
            if learning.learning_id == learning_id:
                learning.validated_gain = gain
                if gain > 0:
                    learning.confidence = Confidence.VALIDATED
                self.save()
                return
