"""When an editing agent is stuck, it asks the Scholar.

The agents are narrow on purpose. The overlay agent knows where a subject is and
where words may go; it does not know why a grade keeps blowing out, or how the
reel it is being measured against got its depth. When one of them runs into
something outside its own objective, the honest options are to give up quietly
or to ask somebody whose whole job is knowing things.

So there is a way to ask. An agent raises a `Question` — what it was trying to
do, what happened, and what it has already measured. The Scholar reads it,
searches what it has already studied, and if that is not enough it goes and
studies the question specifically. It answers with an `Answer`: what it found,
where it came from, and — separately, because these are not the same claim —
how confident it is.

**Three things this deliberately does not do.**

It does not invent an answer. A Scholar that has studied nothing relevant says
so, and says what it would need to watch to be useful. An agent acting on a
confident fabrication is worse off than an agent that stayed stuck.

It does not act. An answer is knowledge handed back to the agent that asked;
the agent still has to turn it into a proposal, and the proposal still goes to
the gate. Nothing here reaches a frame without a person having the chance to
see it.

It does not block. Studying needs a network and a few minutes. An agent in the
middle of a round gets whatever the Scholar already knows, immediately, and the
research happens afterwards so the *next* run is better. A crew that stalled
mid-edit waiting for a tutorial to download would be a worse tool than one that
occasionally says "I do not know yet".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("auteur.scholar.consult")

#: A question with fewer than this many relevant learnings behind it gets sent
#: to research rather than answered from the shelf.
THIN = 2


@dataclass
class Question:
    """Something an agent could not work out on its own."""

    #: Which agent is stuck.
    agent: str
    #: What it was trying to achieve, in its own terms.
    goal: str
    #: What actually happened, including numbers where it has them.
    problem: str
    #: Anything it has already measured, so the Scholar is not told to go and
    #: look at something already on the table.
    evidence: dict = field(default_factory=dict)
    asked_at: float = field(default_factory=time.time)

    @property
    def search_terms(self) -> str:
        """What to look for. The goal and the problem, minus the plumbing."""
        words = f"{self.goal} {self.problem}".lower()
        for noise in ("the ", "a ", "is ", "it ", "and ", "to ", "of ", "in ", "on "):
            words = words.replace(noise, " ")
        return " ".join(words.split()[:8])

    def describe(self) -> str:
        lines = [f"[{self.agent}] {self.goal}", f"    problem: {self.problem}"]
        for key, value in self.evidence.items():
            lines.append(f"    {key}: {value}")
        return "\n".join(lines)


@dataclass
class Answer:
    """What the Scholar could tell the agent, and how sure it is."""

    question: Question
    #: What to actually do, or "" when the Scholar has nothing.
    advice: str = ""
    #: The learnings behind it, so the advice has an address.
    learning_ids: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    #: none | studied | researched. Which shelf this came off.
    basis: str = "none"
    #: What it went and watched because of this question, if anything.
    studied: int = 0

    @property
    def useful(self) -> bool:
        return bool(self.advice)

    @property
    def confidence(self) -> str:
        """Stated separately from the advice, because they are different claims.

        Counted in distinct *channels*, not learnings. Four notes taken from one
        tutorial are one person's opinion written down four times, and calling
        that "several sources agree" is exactly the overclaim this field exists
        to prevent.
        """
        if not self.advice:
            return "none"
        voices = len(self.sources)
        if voices >= 3:
            return f"{voices} independent sources agree"
        if voices == 2:
            return "two independent sources"
        return "one source — treat as a lead, not a fact"

    def describe(self) -> str:
        if not self.useful:
            return (
                f"the Scholar has not studied this yet ({self.question.search_terms}). "
                "It has queued the topic and will know more next run."
            )
        where = f" — {', '.join(self.sources[:3])}" if self.sources else ""
        return f"{self.advice}\n    [{self.confidence}{where}]"


class HelpDesk:
    """Where a stuck agent goes.

    Holds the questions asked during a run so the Scholar can go and study them
    afterwards, when there is time and a network, rather than in the middle of
    somebody's render.
    """

    def __init__(self, scholar=None):
        self._scholar = scholar
        self.asked: list[Question] = []
        self.answered: list[Answer] = []
        #: Questions the Scholar could not answer, kept for the research pass.
        self.homework: list[Question] = []

    @property
    def scholar(self):
        if self._scholar is None:
            from .scholar import Scholar

            self._scholar = Scholar()
        return self._scholar

    def ask(self, question: Question) -> Answer:
        """Answer from what is already known. Never blocks, never invents."""
        self.asked.append(question)
        try:
            found = self.scholar.knowledge.search(question.search_terms)
        except Exception as exc:  # noqa: BLE001 - a broken shelf is not a broken edit
            log.info("could not search the Scholar's knowledge: %s", exc)
            found = []

        if len(found) < THIN:
            # Try the individual words: a search for the whole phrase is an AND
            # across every term, which almost nothing matches.
            loosened: list = []
            for term in question.search_terms.split():
                if len(term) < 4:
                    continue
                for learning in self.scholar.knowledge.search(term):
                    if learning not in loosened:
                        loosened.append(learning)
            found = loosened

        if not found:
            answer = Answer(question=question, basis="none")
            self.homework.append(question)
            self.answered.append(answer)
            log.info("the Scholar has nothing on %r — queued for study", question.search_terms)
            return answer

        # One per channel first, then fill. Picking simply the most substantial
        # notes selects four from whichever creator writes the longest chapter
        # titles, so an answer with three independent voices behind it reports
        # as one source — which is the confidence measure being defeated by the
        # selection that feeds it.
        by_channel: dict[str, object] = {}
        for item in sorted(found, key=lambda item: -len(item.insight)):
            by_channel.setdefault(item.source_channel or "", item)
        best = list(by_channel.values())[:4]
        if len(best) < 4:
            for item in sorted(found, key=lambda item: -len(item.insight)):
                if item not in best:
                    best.append(item)
                if len(best) == 4:
                    break
        answer = Answer(
            question=question,
            advice="; ".join(dict.fromkeys(item.application or item.insight for item in best)),
            learning_ids=[item.learning_id for item in best],
            sources=list(
                dict.fromkeys(item.source_channel for item in best if item.source_channel)
            ),
            basis="studied",
        )
        self.answered.append(answer)
        return answer

    def do_the_homework(self, *, max_videos: int = 3) -> int:
        """Go and study everything nobody could answer. Runs after the edit.

        Deliberately separate from `ask`. Studying needs a network and minutes;
        an agent mid-round needs an answer or a clear no. This is how the *next*
        run is better rather than this one being slower.
        """
        from .youtube import YouTubeUnavailable

        learned = 0
        for question in list(self.homework):
            try:
                session = self.scholar.study(max_videos=max_videos)
            except YouTubeUnavailable as exc:
                log.info("cannot research %r: %s", question.search_terms, exc)
                break
            except Exception as exc:  # noqa: BLE001 - research failing is not fatal
                log.info("research on %r failed: %s", question.search_terms, exc)
                continue
            learned += session.learnings_extracted
            self.homework.remove(question)
        return learned

    def describe(self) -> str:
        if not self.asked:
            return "nobody got stuck"
        lines = [f"{len(self.asked)} question(s) to the Scholar:"]
        for answer in self.answered:
            lines.append(f"  {answer.question.describe()}")
            lines.append(f"    → {answer.describe()}")
        if self.homework:
            lines.append("")
            lines.append(f"{len(self.homework)} queued for study before the next run")
        return "\n".join(lines)
