"""Scrolling, rather than reading about scrolling.

Everything else the Scholar does is study: it reads a page about the Instagram
ranking and keeps a sentence. A sentence about an algorithm is somebody's
account of it, and accounts go stale the week the ranking changes.

This is the other half. The Scholar asks a feed for reels and watches what it
is handed, **in the order it is handed them**, measuring each one frame by
frame with the same code that reads a reference. The order is the data. Nobody
tells it what the ranking favours; it looks at what arrived first and what
arrived tenth and reports the difference.

That difference is a claim about one session on one account, and it is written
down that way. Ten sessions agreeing is worth something; one is an anecdote,
and the store's confidence ladder already knows how to say so.

A feed is any source that serves a *sequence* on request — YouTube through
yt-dlp, or a folder of reels somebody already has. The sequence is the whole
point, so a source that returns an unordered set is not a feed and is not
accepted as one.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.scholar.feed")

#: How many reels one scroll takes before it stops. A feed will keep serving
#: forever; the point is a session, not a crawl.
SESSION = 12

#: Nothing shorter is a reel. Guards a feed that starts handing over thumbnails
#: or six-frame previews.
LEAST_SECONDS = 1.0

#: Relative difference below which the top and the bottom of a feed are the
#: same number and the session has no direction to contribute.
TIE = 0.02


@dataclass
class Serving:
    """One reel, and where in the feed it turned up."""

    #: 0 is the first thing served. This is the measurement that matters.
    position: int
    name: str
    source: str
    seconds: float = 0.0
    cuts_per_10s: float = 0.0
    shot_seconds: float = 0.0
    hook: float = 0.0
    luma: float = 0.0
    motion: float = 0.0
    #: What the platform said about it, when it said anything. Kept separate
    #: from the measurements because one is a claim and the other is a reading.
    claimed: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Scroll:
    """One session of being served reels, kept whole."""

    feed: str
    query: str
    at: float = field(default_factory=time.time)
    servings: list[Serving] = field(default_factory=list)
    #: Feeds that could not be reached, said plainly rather than left as an
    #: empty list that reads like "there was nothing there".
    unreachable: str = ""

    @property
    def watched(self) -> int:
        return len(self.servings)

    def to_json(self) -> dict:
        return {
            "feed": self.feed,
            "query": self.query,
            "at": self.at,
            "unreachable": self.unreachable,
            "servings": [s.to_json() for s in self.servings],
        }

    @classmethod
    def from_json(cls, data: dict) -> Scroll:
        return cls(
            feed=str(data.get("feed", "")),
            query=str(data.get("query", "")),
            at=float(data.get("at", 0.0)),
            unreachable=str(data.get("unreachable", "")),
            servings=[Serving(**s) for s in data.get("servings", [])],
        )

    # -------------------------------------------------------------- reading

    def what_it_served(self) -> list[str]:
        """What this session actually put in front of it, in plain sentences.

        Top half against bottom half rather than a correlation coefficient: a
        dozen items is not enough for a coefficient to mean anything, and
        "the first six cut twice as fast as the last six" is both true and
        legible. Only differences big enough to survive a session this short
        are reported at all.
        """
        if len(self.servings) < 4:
            return []

        half = len(self.servings) // 2
        top = self.servings[:half]
        rest = self.servings[half:]
        said: list[str] = []

        for label, get, unit, floor in (
            ("cut", lambda s: s.cuts_per_10s, " cuts per ten seconds", 0.2),
            ("hold", lambda s: s.shot_seconds, "s a shot", 0.2),
            ("run", lambda s: s.seconds, "s long", 0.2),
            ("move", lambda s: s.motion, " inter-frame motion", 0.3),
        ):
            first = statistics.median([get(s) for s in top])
            last = statistics.median([get(s) for s in rest])
            if last <= 0 or first <= 0:
                continue
            change = (first - last) / last
            if abs(change) < floor:
                continue
            said.append(
                f"The first {len(top)} it was served {label} at {first:.2f}{unit}; "
                f"the next {len(rest)}, {last:.2f}. "
                f"{'Higher' if change > 0 else 'Lower'} up the feed by "
                f"{abs(change) * 100:.0f}%."
            )

        if not said:
            said.append(
                f"Across {len(self.servings)} reels this feed served, nothing measured "
                "differed enough between the top and the bottom to be worth reporting."
            )
        return said

    def describe(self) -> str:
        if self.unreachable:
            return f"{self.feed}: could not be reached — {self.unreachable}"
        return f"{self.feed} on {self.query!r}: watched {self.watched}"


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


class Feed:
    """Something that serves an ordered sequence of films on request."""

    name = "feed"

    def reachable(self) -> tuple[bool, str]:
        """Whether this feed can be used from here, and why not if it cannot."""
        return True, ""

    def serve(self, query: str, *, count: int) -> Iterable[tuple[Path, dict]]:
        """Yield (file, what the platform claimed) in the order served."""
        raise NotImplementedError


class LocalFeed(Feed):
    """A folder of reels, served in a fixed order.

    Not a stand-in for a platform and not described as one: there is no
    ranking in a folder, so a scroll of it measures the Scholar's own library
    rather than anybody's algorithm. It is here because it exercises every
    other part of this — watching, measuring, keeping the order — without a
    network, and because "what is in my own library" is a fair question.
    """

    name = "library"

    def __init__(self, folder: str | Path):
        self._folder = Path(folder)

    def reachable(self) -> tuple[bool, str]:
        if not self._folder.is_dir():
            return False, f"no folder at {self._folder}"
        return True, ""

    def serve(self, query: str, *, count: int) -> Iterable[tuple[Path, dict]]:
        wanted = (query or "").strip().lower()
        files = sorted(
            p
            for p in self._folder.iterdir()
            if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
        )
        for path in files:
            if wanted and wanted not in path.name.lower():
                continue
            yield path, {"from": "library"}
            count -= 1
            if count <= 0:
                return


class YouTubeFeed(Feed):
    """What YouTube hands back, in the order it hands it back.

    Search order *is* a ranking — it is the one this program can actually be
    served by — so the sequence is kept exactly as returned rather than sorted
    into something tidier. Sorting it would throw away the only signal here.
    """

    name = "youtube"

    def __init__(self, *, workdir: Path | None = None, shorts: bool = True):
        self._workdir = Path(workdir or Path.home() / ".auteur" / "scholar" / "scrolled")
        self._shorts = shorts

    def reachable(self) -> tuple[bool, str]:
        """Asks YouTube, rather than asking whether yt-dlp is installed.

        `youtube.reachable` answers the second question and used to be
        documented as answering the first — which is how a scroll behind a
        proxy set off downloading twelve reels it was never going to get.
        """
        from .youtube import can_reach

        ok, why = can_reach()
        return bool(ok), "" if ok else str(why)

    def serve(self, query: str, *, count: int) -> Iterable[tuple[Path, dict]]:
        import subprocess

        self._workdir.mkdir(parents=True, exist_ok=True)
        wanted = query or "reels"
        # `ytsearchN:` returns them ranked. Kept in that order on purpose.
        target = f"ytsearch{count}:{wanted}"

        listing = subprocess.run(
            ["yt-dlp", "--flat-playlist", "-J", "--no-warnings", target],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if listing.returncode != 0:
            raise RuntimeError((listing.stderr or "yt-dlp failed").strip().splitlines()[-1][:300])

        entries = (json.loads(listing.stdout or "{}") or {}).get("entries") or []
        for entry in entries[:count]:
            video = entry.get("id")
            if not video:
                continue
            out = self._workdir / f"{video}.mp4"
            if not out.exists():
                got = subprocess.run(
                    [
                        "yt-dlp",
                        "--no-warnings",
                        "-f",
                        "mp4[height<=720]/best[height<=720]",
                        "-o",
                        str(out),
                        f"https://www.youtube.com/watch?v={video}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if got.returncode != 0 or not out.exists():
                    log.info("could not fetch %s, skipping it", video)
                    continue
            yield out, {
                "title": entry.get("title") or "",
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "views": entry.get("view_count"),
                "seconds_claimed": entry.get("duration"),
            }


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------


def scroll(feed: Feed, query: str = "", *, count: int = SESSION) -> Scroll:
    """Be served `count` reels, watch each one, and keep the order.

    Every reel is read with the same frame-by-frame pass a reference gets, so
    what comes back is measurement rather than metadata — a feed's own claim
    about a video's length is not a reading of it.
    """
    from ..insight.template import read

    session = Scroll(feed=feed.name, query=query)
    ok, why = feed.reachable()
    if not ok:
        session.unreachable = why
        log.info("%s is not reachable from here: %s", feed.name, why)
        return session

    position = 0
    try:
        served = feed.serve(query, count=count)
        for path, claimed in served:
            template = read(path)
            if template is None or template.seconds < LEAST_SECONDS:
                log.info("served something that would not open as a reel: %s", Path(path).name)
                continue
            session.servings.append(
                Serving(
                    position=position,
                    name=template.name,
                    source=str(path),
                    seconds=round(template.seconds, 3),
                    cuts_per_10s=round(template.cuts_per_10s, 3),
                    shot_seconds=round(template.shot_seconds, 4),
                    hook=round(template.hook, 4),
                    luma=round(
                        (
                            statistics.median([b.luma for b in template.beats])
                            if template.beats
                            else 0.0
                        ),
                        4,
                    ),
                    motion=round(
                        (
                            statistics.median([b.motion for b in template.beats])
                            if template.beats
                            else 0.0
                        ),
                        4,
                    ),
                    claimed=dict(claimed or {}),
                )
            )
            position += 1
    except Exception as exc:  # noqa: BLE001 - a feed that breaks mid-scroll is not a crash
        session.unreachable = str(exc)[:300]
        log.info("the scroll stopped early: %s", exc)

    return session


def learnings_from(session: Scroll) -> list:
    """What a session is worth keeping, as learnings the crew can be held to.

    One session is one voice, so every learning starts TENTATIVE and is keyed
    on the session rather than on the feed — ten scrolls of YouTube agreeing is
    ten sources, and the store's corroboration should be allowed to see that.
    Keying them all on "youtube" would make ten sessions one voice forever,
    which is the mistake the film library already made once.
    """
    from .knowledge import Discipline, Learning

    if not session.servings:
        return []

    channel = f"scroll:{session.feed}:{int(session.at)}"
    out = []
    for index, sentence in enumerate(session.what_it_served()):
        out.append(
            Learning(
                learning_id=f"{channel}:{index}",
                disciplines=[Discipline.PLATFORM_ALGORITHM],
                insight=sentence,
                technique="what the feed served",
                application=(
                    "measured by being served it, not read about — one session, "
                    "so it is worth what one session is worth"
                ),
                source_video_id=channel,
                source_channel=channel,
                source_title=f"{session.feed} scroll, {session.watched} reels",
                measurements={
                    "watched": float(session.watched),
                    "median_cuts_per_10s": float(
                        statistics.median([s.cuts_per_10s for s in session.servings])
                    ),
                    "median_shot_seconds": float(
                        statistics.median([s.shot_seconds for s in session.servings])
                    ),
                },
            )
        )
    return out


class ScrollHistory:
    """Every session, kept, because one is an anecdote and ten are a pattern."""

    def __init__(self, folder: str | Path | None = None):
        self._folder = Path(folder or Path.home() / ".auteur" / "scholar" / "scrolls")
        self._folder.mkdir(parents=True, exist_ok=True)

    @property
    def folder(self) -> Path:
        return self._folder

    def keep(self, session: Scroll) -> Path:
        path = self._folder / f"{session.feed}-{int(session.at)}.json"
        path.write_text(json.dumps(session.to_json(), indent=2), encoding="utf-8")
        return path

    def all(self) -> list[Scroll]:
        out = []
        for file in sorted(self._folder.glob("*.json")):
            try:
                out.append(Scroll.from_json(json.loads(file.read_text(encoding="utf-8"))))
            except Exception as exc:  # noqa: BLE001 - one bad file is not fatal
                log.info("skipping unreadable scroll %s: %s", file.name, exc)
        return out

    def across_sessions(self, sessions: Sequence[Scroll] | None = None) -> list[str]:
        """What holds up across sessions, which is the only thing that should.

        A single scroll can say anything — one session of twelve is a sample of
        twelve. This only reports a direction that the majority of sessions
        agree on, and says how many that was.
        """
        held = list(sessions if sessions is not None else self.all())
        useful = [s for s in held if len(s.servings) >= 4]
        if len(useful) < 2:
            return []

        out: list[str] = []
        for label, get, unit in (
            ("cutting rate", lambda s: s.cuts_per_10s, " cuts per ten seconds"),
            ("shot length", lambda s: s.shot_seconds, "s"),
            ("runtime", lambda s: s.seconds, "s"),
        ):
            directions = []
            for session in useful:
                half = len(session.servings) // 2
                top = statistics.median([get(s) for s in session.servings[:half]])
                rest = statistics.median([get(s) for s in session.servings[half:]])
                if not (top and rest):
                    continue
                # A tie is no signal, not agreement. `1 if top > rest else -1`
                # calls an exact draw "lower", so two sessions that measured
                # the same number at both ends of the feed were reported as
                # agreeing on a direction that neither of them saw.
                if abs(top - rest) / rest < TIE:
                    continue
                directions.append(1 if top > rest else -1)
            if len(directions) < 2:
                continue
            up = sum(1 for d in directions if d > 0)
            agree = max(up, len(directions) - up)
            if agree < len(directions) * 0.7:
                continue
            tops = [
                statistics.median([get(s) for s in session.servings[: len(session.servings) // 2]])
                for session in useful
            ]
            out.append(
                f"Across {len(useful)} sessions, {agree} of them served a higher {label} "
                f"at the top of the feed than the bottom "
                f"(median at the top {statistics.median(tops):.2f}{unit})."
                if up >= agree
                else (
                    f"Across {len(useful)} sessions, {agree} of them served a lower {label} "
                    f"at the top of the feed than the bottom "
                    f"(median at the top {statistics.median(tops):.2f}{unit})."
                )
            )
        return out
