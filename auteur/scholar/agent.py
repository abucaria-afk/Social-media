"""The Scholar as a member of the crew.

The Scholar studies on a long cycle and the crew argues on a short one, so they
are separate things. But the Scholar's review — the pass where it watches the
finished cut with everything it has read — produces exactly what the crew
already consumes: proposals. `OutputReview.review_as_proposals` even has the
`inspect` signature. All that was missing was somebody putting it in the room.

Without this the Scholar could learn indefinitely and never change a frame. It
had a teaching interface, a review interface, and no path from either to an
edit; `Scholar` was constructed nowhere in the program at all.

It stays quiet until it has something to say. A Scholar with an empty knowledge
store proposes nothing, because a review backed by no study is just the Gaze
agent's opinion arriving twice.
"""

from __future__ import annotations

import logging

from ..edl import EditDecisionList
from ..insight import FitReport, Prediction
from ..agents.base import Proposal

log = logging.getLogger("auteur.scholar.agent")

#: Below this the Scholar has not studied enough for its review to be worth a
#: round. One learning is an anecdote, not a discipline.
ENOUGH_TO_SPEAK = 3


class ScholarAgent:
    """Reviews the cut with what the Scholar has actually studied."""

    name = "scholar"
    objective = "craft_knowledge"

    def __init__(self, scholar=None, *, readings: dict | None = None):
        from .scholar import Scholar

        self.scholar = scholar if scholar is not None else Scholar()
        self.readings = readings or {}
        self._said: set[str] = set()
        self._pending: list[Proposal] = []

    @property
    def studied(self) -> int:
        return self.scholar.knowledge.total_learnings

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if self.studied < ENOUGH_TO_SPEAK:
            return []
        try:
            proposals = self.scholar.review_as_proposals(edl, prediction, model)
        except Exception as exc:  # noqa: BLE001 - a broken review must not lose the film
            log.warning("the Scholar's review failed: %s", exc)
            return []

        # The crew runs several rounds and these findings change nothing, so
        # every round would produce the same list again. Said once is a note;
        # said three times is a complaint.
        fresh = [p for p in proposals if p.title not in self._said]
        self._said.update(p.title for p in fresh)
        self._pending.extend(fresh)
        return fresh

    def learn_from(self, proposals: list[Proposal]) -> int:
        """Promote the learnings whose advice measurably helped.

        This is the other half of the loop, and the half that makes the Scholar
        different from a bookmark folder. Corroboration gets a technique to
        SUPPORTED because unrelated people agree on it; only a real edit, where
        the proposal was applied and the prediction went up, gets it to
        VALIDATED. `record_validation` existed for exactly this and had no
        caller anywhere in the program.

        A proposal that was applied and *lost* prediction is recorded too, with
        its negative gain — a learning that has been tried and did not help is
        worth more than one that has never been tried at all.
        """
        promoted = 0
        for proposal in proposals or self._pending:
            if not proposal.applied:
                continue
            for learning_id in getattr(proposal, "learning_ids", ()):
                self.scholar.knowledge.record_validation(learning_id, proposal.predicted_gain)
                promoted += 1
        return promoted
