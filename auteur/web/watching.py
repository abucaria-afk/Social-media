"""What was watched, for how long, and by whom.

The app had a feed with nothing to rank it by and an insight layer scoring
`three_second_watch_rate` and `share_to_view_ratio` against a corpus it had
*simulated*, because nothing anywhere measured a single view. A recommender
with no observations is a shuffle with extra steps, and "we collect nothing"
was not a privacy feature so much as a missing one — it made the algorithm
impossible and called that a virtue.

So this measures. Two things, deliberately separated, because they carry very
different weight:

**Per film.** Views, three-second watches, completion, average seconds held,
loops, share taps. Nobody is in these numbers — they are facts about a film,
the same kind of fact as its runtime. They feed the ranking, and they are what
`auteur.insight` has been waiting for since it was written.

**Per viewer.** What one account watched and how much of it. This is personal
data and it is named as such rather than hidden inside an aggregate: it is what
makes the feed learn *your* taste instead of the average taste, and it is the
part that has to disappear when somebody deletes their account.

**Where it lives.** On the instance — the copy of the server the person runs on
their own machine, in the folder they chose. It is not sent anywhere. The
developer operates no service and receives none of this, which is why the store
listings can still answer "data shared with the developer" with No, while
answering the collection questions honestly rather than by a technicality.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.web.watching")

#: How long a view has to last to count as somebody having actually seen it.
#: Three seconds is the industry's number and it is also the one the insight
#: layer scores against, so it is defined once and read by both.
SEEN_SECONDS = 3.0

#: Watching this fraction of a film counts as having finished it. Not 1.0:
#: players stop reporting a little short, and a film that ends on a held frame
#: gets abandoned a beat early by people who have plainly seen the whole thing.
FINISHED = 0.9

#: How many of a person's recent views shape what they are shown next. Long
#: enough to have a taste in it, short enough that a taste can change.
TASTE_WINDOW = 60


@dataclass
class Seen:
    """One person's history with one film. The personal half of this file."""

    who: str
    film: str
    #: How many times they started it.
    plays: int = 0
    #: Total seconds watched across every play.
    seconds: float = 0.0
    #: Whether they ever got to the end.
    finished: bool = False
    #: The last time they watched, so taste can decay.
    last: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "who": self.who,
            "film": self.film,
            "plays": self.plays,
            "seconds": round(self.seconds, 2),
            "finished": self.finished,
            "last": self.last,
        }

    @classmethod
    def from_json(cls, data: dict) -> Seen:
        return cls(
            who=str(data["who"]),
            film=str(data["film"]),
            plays=int(data.get("plays", 0)),
            seconds=float(data.get("seconds", 0.0)),
            finished=bool(data.get("finished", False)),
            last=float(data.get("last", 0.0)),
        )


@dataclass
class Reception:
    """How one film has been received. Nobody is in here."""

    film: str
    plays: int = 0
    #: Plays that lasted at least `SEEN_SECONDS`.
    seen: int = 0
    finishes: int = 0
    seconds: float = 0.0
    loops: int = 0
    shares: int = 0
    #: How many distinct accounts have played it. A count, not a list — the
    #: difference between "eleven people watched this" and a guest list.
    watchers: int = 0

    @property
    def three_second_watch_rate(self) -> float:
        return self.seen / self.plays if self.plays else 0.0

    @property
    def completion_rate(self) -> float:
        return self.finishes / self.plays if self.plays else 0.0

    @property
    def avg_seconds(self) -> float:
        return self.seconds / self.plays if self.plays else 0.0

    @property
    def share_to_view_ratio(self) -> float:
        return self.shares / self.plays if self.plays else 0.0

    @property
    def loop_count(self) -> float:
        return self.loops / self.plays if self.plays else 0.0

    def to_json(self) -> dict:
        return {
            "film": self.film,
            "plays": self.plays,
            "seen": self.seen,
            "finishes": self.finishes,
            "seconds": round(self.seconds, 2),
            "loops": self.loops,
            "shares": self.shares,
            "watchers": self.watchers,
        }

    @classmethod
    def from_json(cls, data: dict) -> Reception:
        return cls(
            film=str(data["film"]),
            plays=int(data.get("plays", 0)),
            seen=int(data.get("seen", 0)),
            finishes=int(data.get("finishes", 0)),
            seconds=float(data.get("seconds", 0.0)),
            loops=int(data.get("loops", 0)),
            shares=int(data.get("shares", 0)),
            watchers=int(data.get("watchers", 0)),
        )


class Watching:
    """The instance's record of what has been watched.

    Two files rather than one, because they have different lifetimes: the
    per-film numbers survive an account being deleted (they are facts about a
    film, and a film outlives a viewer), and the per-viewer history does not.
    Keeping them in one file would mean deletion had to rewrite the aggregates
    too, which is how a deletion routine ends up subtly wrong.
    """

    def __init__(self, folder: Path):
        self._folder = Path(folder)
        self._folder.mkdir(parents=True, exist_ok=True)
        self._films_path = self._folder / "reception.json"
        self._people_path = self._folder / "watched.json"
        self._films: dict[str, Reception] = {}
        self._people: dict[tuple[str, str], Seen] = {}
        self._load()

    # -- storage ---------------------------------------------------------

    def _load(self) -> None:
        if self._films_path.is_file():
            try:
                rows = json.loads(self._films_path.read_text(encoding="utf-8"))
                self._films = {r["film"]: Reception.from_json(r) for r in rows}
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("reception file unreadable, starting empty: %s", exc)
        if self._people_path.is_file():
            try:
                rows = json.loads(self._people_path.read_text(encoding="utf-8"))
                self._people = {(r["who"], r["film"]): Seen.from_json(r) for r in rows}
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("watch history unreadable, starting empty: %s", exc)

    def _save(self) -> None:
        self._films_path.write_text(
            json.dumps([r.to_json() for r in self._films.values()], indent=1),
            encoding="utf-8",
        )
        self._people_path.write_text(
            json.dumps([s.to_json() for s in self._people.values()], indent=1),
            encoding="utf-8",
        )

    # -- recording -------------------------------------------------------

    def played(
        self,
        who: str,
        film: str,
        *,
        seconds: float,
        runtime: float,
        looped: int = 0,
    ) -> Reception:
        """Record one viewing.

        `seconds` is time actually watched, which can exceed `runtime` when
        somebody let it loop — that is what `looped` counts, and it is why the
        two are reported separately rather than inferred from each other.
        """
        film = str(film or "").strip()
        if not film:
            raise ValueError("a view has to be of something")

        seconds = max(0.0, float(seconds))
        runtime = max(0.0, float(runtime))
        # A single view cannot claim more time than the film could have played
        # in a sitting. Without this a stuck timer or a hostile client writes
        # any number it likes into the ranking.
        ceiling = max(runtime, 1.0) * (max(0, int(looped)) + 1) + SEEN_SECONDS
        seconds = min(seconds, ceiling)

        record = self._films.setdefault(film, Reception(film=film))
        record.plays += 1
        record.seconds += seconds
        record.loops += max(0, int(looped))
        if seconds >= SEEN_SECONDS:
            record.seen += 1
        if runtime > 0 and seconds >= runtime * FINISHED:
            record.finishes += 1

        who = str(who or "").strip()
        if who:
            key = (who, film)
            first = key not in self._people
            seen = self._people.setdefault(key, Seen(who=who, film=film))
            seen.plays += 1
            seen.seconds += seconds
            seen.last = time.time()
            if runtime > 0 and seconds >= runtime * FINISHED:
                seen.finished = True
            if first:
                record.watchers += 1

        self._save()
        return record

    def shared(self, film: str) -> Reception:
        """Somebody tapped share. The strongest signal in the set."""
        record = self._films.setdefault(str(film), Reception(film=str(film)))
        record.shares += 1
        self._save()
        return record

    # -- reading ---------------------------------------------------------

    def reception(self, film: str) -> Reception:
        return self._films.get(str(film)) or Reception(film=str(film))

    def history(self, who: str) -> list[Seen]:
        """Everything one person has watched, most recent first.

        This is theirs. It is what the profile screen shows them about
        themselves, and what account deletion removes.
        """
        mine = [s for s in self._people.values() if s.who == who]
        return sorted(mine, key=lambda s: s.last, reverse=True)

    def already_seen(self, who: str) -> set[str]:
        """Films this person has finished, so the feed can stop repeating."""
        return {s.film for s in self._people.values() if s.who == who and s.finished}

    # -- ranking ---------------------------------------------------------

    def merit(self, film: str) -> float:
        """How well a film has done, independent of who is asking.

        Completion carries the most weight because it is the hardest to fake
        and the least confounded: a share can be a favour, a play can be a
        mis-tap, but watching to the end is the audience agreeing with the cut.
        Confidence rises with the number of plays, so one lucky view does not
        outrank a film that has been watched fifty times — a film with a single
        play sits near the middle rather than at either end.
        """
        record = self.reception(film)
        if not record.plays:
            return 0.0
        raw = (
            record.completion_rate * 0.5
            + record.three_second_watch_rate * 0.2
            + min(record.share_to_view_ratio * 4.0, 1.0) * 0.2
            + min(record.loop_count, 1.0) * 0.1
        )
        # Shrink towards 0.5 until there is enough evidence to have an opinion.
        confidence = 1.0 - math.exp(-record.plays / 8.0)
        return 0.5 + (raw - 0.5) * confidence

    def taste(self, who: str) -> dict[str, float]:
        """How much this person likes each *maker*, from what they watched.

        Deliberately about who made a film rather than about tags. A person's
        history on a small instance is a handful of films, which is far too
        little to fit a topic model to and plenty to notice whose work they sit
        through. Recent views count for more, so a taste can change.
        """
        now = time.time()
        weights: dict[str, float] = {}
        recent = sorted(
            (s for s in self._people.values() if s.who == who),
            key=lambda s: s.last,
            reverse=True,
        )[:TASTE_WINDOW]
        for seen in recent:
            # A fortnight halves it.
            age = max(0.0, now - seen.last)
            decay = 0.5 ** (age / (14 * 86400))
            weights[seen.film] = (1.0 if seen.finished else 0.35) * decay
        return weights

    def for_you(self, who: str, films, *, made_by=None) -> list:
        """Order films for one person: merit, their taste, and novelty.

        `films` is whatever the feed was going to show. `made_by` maps a film
        id to its maker, so taste learned about a maker can transfer to work of
        theirs this person has not seen.

        Nothing here removes a film. A ranking that hides things is a ranking
        somebody has to fight; this one reorders and lets anybody scroll.
        """
        made_by = made_by or {}
        liked = self.taste(who)
        by_maker: dict[str, float] = {}
        for film_id, weight in liked.items():
            maker = made_by.get(film_id)
            if maker:
                by_maker[maker] = by_maker.get(maker, 0.0) + weight

        finished = self.already_seen(who)
        best = max(by_maker.values()) if by_maker else 0.0

        def score(film) -> float:
            film_id = getattr(film, "id", None) or str(film)
            maker = made_by.get(film_id) or getattr(film, "owner", "")
            value = self.merit(film_id)
            if best > 0 and maker in by_maker:
                value += (by_maker[maker] / best) * 0.35
            # Seen it through already: still there, further down.
            if film_id in finished:
                value -= 0.4
            return value

        return sorted(films, key=score, reverse=True)

    # -- what the insight layer reads ------------------------------------

    def signals(self) -> list[dict]:
        """The per-film numbers in the shape `auteur.insight` expects.

        This is the point of the whole file. `insight.dataset` has always been
        able to read a real export and never had one, so the model behind every
        virality score was fitted to a simulation of itself. These are rows a
        person's own instance measured.
        """
        return [
            {
                "post_id": r.film,
                "form": "reel",
                "three_second_watch_rate": round(r.three_second_watch_rate, 4),
                "completion_rate": round(r.completion_rate, 4),
                "avg_time_spent_sec": round(r.avg_seconds, 3),
                "share_to_view_ratio": round(r.share_to_view_ratio, 4),
                "loop_count": round(r.loop_count, 3),
                "views_10m": r.plays,
            }
            for r in self._films.values()
            if r.plays
        ]

    # -- forgetting ------------------------------------------------------

    def forget_everything_about(self, who: str) -> int:
        """Erase one person's history. Called when an account is deleted.

        The per-film aggregates are left alone on purpose, and the reasoning
        matters. They contain no reference to anybody: subtracting a departed
        viewer's seconds from them would not make anybody more private, and it
        would silently rewrite the performance history of films belonging to
        people who are still here. `watchers` is not decremented for the same
        reason — it is a count of how many accounts once played a film, not a
        roster, and there is nobody to identify in it.
        """
        gone = [key for key in self._people if key[0] == who]
        for key in gone:
            del self._people[key]
        if gone:
            self._save()
        return len(gone)

    def forget_film(self, film: str) -> None:
        """Erase a film's record, when the film itself is deleted."""
        self._films.pop(str(film), None)
        for key in [k for k in self._people if k[1] == str(film)]:
            del self._people[key]
        self._save()
