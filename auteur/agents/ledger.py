"""What the crew has tried before, and what it was worth.

Every run started from nothing. The crew would propose the same twenty changes
on the hundredth film as on the first, in the same order, having learned
precisely nothing from the ninety-nine before it — which is a strange property
for something called an agent crew.

This is the memory. One line per proposal per run: who suggested it, what it
was, whether it was applied, and what the model thought it was worth. Over a
few dozen films that turns into a real ranking of which changes earn their
place in *your* work, which is not the same as which changes earn their place
in general.

**What these numbers are, precisely.** `predicted_gain` is the scoring model's
opinion, measured by applying the change to a copy and re-scoring. It is not a
view count. A change with a long history of positive predicted gain is one this
program has consistently believed in, which is worth knowing and is not the
same as one that has been shown to work. When real performance exports arrive,
`auteur insight` is where they meet the truth; this file never pretends to be
that.

The ledger is advisory. It reorders proposals so the historically valuable ones
are tried first — which genuinely matters, because each applied change alters
the timeline the next agent inspects — and it never blocks anything. An agent
whose idea has never worked before still gets to make its case.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.agents.ledger")

#: Below this many attempts a mean gain is one film's luck, not a track record.
ENOUGH_TRIES = 3

#: Numbers inside a proposal's title, which name the same change on a different
#: film rather than a different change.
_COUNTS = re.compile(r"\b\d+(?:\.\d+)?s?\b")


def kind_of(title: str) -> str:
    """The kind of change, with the specifics of one film stripped out.

    "Reframe 1 shot(s) onto the subject" and "Reframe 2 shot(s) onto the
    subject" are the same idea meeting different footage, and keeping them as
    separate tracks meant a change could be made on forty films and never reach
    three tries under any one name — so nothing ever became established and the
    ledger stayed permanently uncertain about everything.
    """
    return _COUNTS.sub("N", title).strip()


@dataclass
class Track:
    """One kind of proposal, and how it has gone."""

    agent: str
    title: str
    objective: str = ""
    tries: int = 0
    applied: int = 0
    total_gain: float = 0.0
    last_seen: float = 0.0

    @property
    def mean_gain(self) -> float:
        """Average predicted gain across every time it was scored."""
        return self.total_gain / self.tries if self.tries else 0.0

    @property
    def take_rate(self) -> float:
        return self.applied / self.tries if self.tries else 0.0

    @property
    def established(self) -> bool:
        return self.tries >= ENOUGH_TRIES

    def describe(self) -> str:
        confidence = "" if self.established else "  (too few tries to say)"
        return (
            f"{self.mean_gain:+.1%} over {self.tries} run(s), "
            f"taken {self.take_rate:.0%}{confidence}  [{self.agent}] {self.title}"
        )


class Ledger:
    """What worked, remembered across runs."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else self.default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tracks: dict[tuple[str, str], Track] = {}
        if self.path.exists():
            self._load()

    @staticmethod
    def default_path() -> Path:
        return Path.home() / ".auteur" / "crew-ledger.jsonl"

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                track = Track(**data)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # A corrupt line loses one track, not the whole history.
                log.debug("skipping unreadable ledger line: %s", exc)
                continue
            self.tracks[(track.agent, track.title)] = track

    def save(self) -> None:
        self.path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "agent": t.agent,
                        "title": t.title,
                        "objective": t.objective,
                        "tries": t.tries,
                        "applied": t.applied,
                        "total_gain": round(t.total_gain, 6),
                        "last_seen": round(t.last_seen, 1),
                    }
                )
                for t in self.tracks.values()
            )
            + "\n",
            encoding="utf-8",
        )

    def record(self, proposals) -> int:
        """Fold one run's proposals into the history.

        Proposals the crew skipped without scoring — a repeat of something
        already declined this run — carry no verdict and are not counted, or a
        change would look worse the more often it was suppressed.
        """
        kept = 0
        now = time.time()
        for proposal in proposals:
            if proposal.decision_note.startswith("change failed"):
                continue
            key = (proposal.agent, kind_of(proposal.title))
            track = self.tracks.get(key)
            if track is None:
                track = Track(agent=proposal.agent, title=kind_of(proposal.title))
                self.tracks[key] = track
            track.objective = proposal.objective or track.objective
            track.tries += 1
            track.applied += 1 if proposal.applied else 0
            track.total_gain += float(proposal.predicted_gain)
            track.last_seen = now
            kept += 1
        if kept:
            self.save()
        return kept

    def value_of(self, agent: str, title: str) -> float:
        """What this proposal has been worth before, or 0 when it is new.

        New proposals score zero rather than something pessimistic: an idea
        nobody has tried should be tried, not buried under everything with a
        track record.
        """
        track = self.tracks.get((agent, kind_of(title)))
        return track.mean_gain if track is not None and track.established else 0.0

    def order(self, proposals: list) -> list:
        """Most historically valuable first, stable for everything untested.

        Order matters more than it looks: every applied change alters the
        timeline the next proposal is scored against, so trying the reliably
        good ones first means the marginal ones are judged against a better cut
        rather than a worse one.
        """
        return sorted(
            proposals,
            key=lambda p: -self.value_of(p.agent, p.title),
        )

    def proven(self, limit: int = 12) -> list[Track]:
        """The changes that have actually earned their place, best first.

        Being taken is part of earning it. A proposal with a faintly positive
        mean gain that the crew has never once accepted has not proved
        anything, and listing it under both "earned its place" and "keeps being
        turned down" — which is what happened before `applied` was checked —
        tells the reader nothing except that the report contradicts itself.
        """
        return sorted(
            (t for t in self.tracks.values() if t.established and t.mean_gain > 0 and t.applied),
            key=lambda t: -t.mean_gain,
        )[:limit]

    def wasted(self, limit: int = 12) -> list[Track]:
        """Proposals that keep being made and keep being turned down."""
        return sorted(
            (t for t in self.tracks.values() if t.established and t.take_rate < 0.2),
            key=lambda t: -t.tries,
        )[:limit]

    def describe(self) -> str:
        if not self.tracks:
            return "no runs recorded yet — the crew has nothing to go on"
        lines = [
            f"{len(self.tracks)} kind(s) of change seen across "
            f"{sum(t.tries for t in self.tracks.values())} scored proposal(s)"
        ]
        proven = self.proven()
        if proven:
            lines += ["", "what has earned its place:"]
            lines += [f"    {t.describe()}" for t in proven]
        wasted = self.wasted()
        if wasted:
            lines += ["", "keeps being suggested, keeps being turned down:"]
            lines += [f"    {t.describe()}" for t in wasted]
        if not proven and not wasted:
            lines += ["", f"nothing has been tried {ENOUGH_TRIES} times yet"]
        return "\n".join(lines)


@dataclass
class NullLedger:
    """A ledger that remembers nothing, for callers that do not want a file."""

    tracks: dict = field(default_factory=dict)

    def record(self, proposals) -> int:
        return 0

    def value_of(self, agent: str, title: str) -> float:
        return 0.0

    def order(self, proposals: list) -> list:
        return list(proposals)

    def describe(self) -> str:
        return "not remembering anything this run"
