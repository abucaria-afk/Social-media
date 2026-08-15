"""When each post goes out.

Making five reels in an afternoon is easy. Posting five reels in an afternoon
is the fastest way to be seen by fewer people than posting one, so a queue of
finished work is worth more than a folder of it, and the queue needs to know
two things a folder does not: when each item goes out, and which ones have
gone already.

The rules it enforces are the two that actually matter — a minimum gap between
two posts to the same service, and a ceiling per service per day. Everything
else about posting times is folklore and changes by audience, so the numbers
are defaults you override rather than wisdom baked in.

**This schedules; it does not post.** Nothing here holds a credential or talks
to a network. `due()` tells you what is ready and `export_csv()` hands the
queue to whatever does the posting — a person with a phone, or a tool that has
been given the access this one deliberately has not.

Times are stored as UTC ISO-8601 with a `Z`, because a queue written in one
timezone and read in another is a queue that posts at four in the morning.
Everything on the way in and out goes through `parse_time`/`format_time`.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Iterable, Sequence

from .publish import Deliverable

#: The queue file. Lives in the workspace, not the repository.
QUEUE_NAME = "auteur-schedule.json"

#: Least time between two posts to the same service, in hours.
DEFAULT_GAP_HOURS = 4.0
#: Most posts to one service in a rolling day.
DEFAULT_PER_DAY = 3

STATUSES = ("queued", "posted", "skipped")


def parse_time(text: str | datetime | None) -> datetime:
    """Read a time a person typed, and return it in UTC.

    Accepts "2026-08-20T18:00:00Z", "2026-08-20 18:00", "2026-08-20", or a
    datetime. A value with no timezone is read as **local** time, because that
    is what somebody typing "18:00" means, and then converted — storing it as
    UTC-by-assumption is how a schedule silently slips by however many hours
    you happen to live from Greenwich.
    """
    if isinstance(text, datetime):
        moment = text
    else:
        raw = (text or "").strip()
        if not raw:
            return datetime.now(timezone.utc)
        cleaned = raw.replace("Z", "+00:00").replace("/", "-")
        moment = None
        for form in ("", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M"):
            try:
                if form == "":
                    moment = datetime.fromisoformat(cleaned)
                else:
                    moment = datetime.strptime(cleaned, form)
                break
            except ValueError:
                continue
        if moment is None:
            raise ValueError(f"I cannot read {text!r} as a date and time")
        if moment.year == 1900:  # a bare "18:00" means today at that time
            today = datetime.now()
            moment = moment.replace(year=today.year, month=today.month, day=today.day)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe_time(moment: datetime) -> str:
    """The same instant, in the reader's own timezone, for printing."""
    return moment.astimezone().strftime("%a %d %b %H:%M")


@dataclass
class Post:
    """One queued post."""

    id: str
    platform: str
    service: str
    when: datetime
    video: str
    caption: str
    cover: str = ""
    status: str = "queued"
    note: str = ""
    created: float = field(default_factory=time.time)

    @property
    def is_due(self) -> bool:
        return self.status == "queued" and self.when <= datetime.now(timezone.utc)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "service": self.service,
            "when": format_time(self.when),
            "video": self.video,
            "caption": self.caption,
            "cover": self.cover,
            "status": self.status,
            "note": self.note,
            "created": self.created,
        }

    @staticmethod
    def from_json(raw: dict) -> Post:
        status = str(raw.get("status", "queued"))
        return Post(
            id=str(raw.get("id", "")) or secrets.token_hex(4),
            platform=str(raw.get("platform", "")),
            service=str(raw.get("service", "")),
            when=parse_time(str(raw.get("when", ""))),
            video=str(raw.get("video", "")),
            caption=str(raw.get("caption", "")),
            cover=str(raw.get("cover", "")),
            status=status if status in STATUSES else "queued",
            note=str(raw.get("note", "")),
            created=float(raw.get("created", 0.0)) or time.time(),
        )

    def describe(self) -> str:
        mark = {"queued": "·", "posted": "✓", "skipped": "–"}.get(self.status, "?")
        first_line = (self.caption or "").splitlines()[0] if self.caption else ""
        return (
            f"{mark} {describe_time(self.when):<18} {self.platform:<18} "
            f"{Path(self.video).name:<28} {first_line[:40]}"
        )


class Schedule:
    """The queue, and the rules about how close together things may go."""

    def __init__(
        self,
        path: str | Path,
        *,
        gap_hours: float = DEFAULT_GAP_HOURS,
        per_day: int = DEFAULT_PER_DAY,
    ):
        self.path = Path(path).expanduser().resolve()
        self.gap_hours = max(0.0, gap_hours)
        self.per_day = max(1, per_day)
        self.posts: list[Post] = []
        self._load()

    @staticmethod
    def default_path(root: str | Path) -> Path:
        return Path(root).expanduser().resolve() / QUEUE_NAME

    # -- on disk ----------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        settings = raw.get("settings") or {}
        self.gap_hours = float(settings.get("gap_hours", self.gap_hours))
        self.per_day = int(settings.get("per_day", self.per_day))
        for item in raw.get("posts", []):
            try:
                self.posts.append(Post.from_json(item))
            except ValueError:
                continue
        self.posts.sort(key=lambda post: post.when)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "settings": {"gap_hours": self.gap_hours, "per_day": self.per_day},
            "posts": [post.to_json() for post in sorted(self.posts, key=lambda p: p.when)],
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    # -- adding -----------------------------------------------------------

    def _clashes(self, service: str, when: datetime, ignore: str = "") -> str:
        """Why this slot is a bad idea, or "" if it is fine."""
        same_service = [
            post
            for post in self.posts
            if post.service == service and post.status != "skipped" and post.id != ignore
        ]
        for post in same_service:
            gap = abs((post.when - when).total_seconds()) / 3600.0
            if gap < self.gap_hours:
                return (
                    f"only {gap:.1f}h from the {service} post at "
                    f"{describe_time(post.when)} (minimum {self.gap_hours:.0f}h)"
                )
        day = sum(1 for post in same_service if abs((post.when - when).total_seconds()) < 86400)
        if day >= self.per_day:
            return f"that would be {day + 1} {service} posts inside a day (limit {self.per_day})"
        return ""

    def next_free(self, service: str, after: datetime | None = None) -> datetime:
        """The soonest time this service may post, respecting both rules.

        Walks forward in gap-sized steps. A queue big enough for that to be
        slow would be a queue nobody could keep up with by hand.
        """
        when = after or datetime.now(timezone.utc)
        step = timedelta(hours=self.gap_hours or 1.0)
        for _ in range(500):
            if not self._clashes(service, when):
                return when
            when = when + step
        return when

    def add(
        self,
        deliverable: Deliverable,
        when: str | datetime | None = None,
        *,
        caption: str = "",
        force: bool = False,
    ) -> tuple[Post | None, str]:
        """Queue a finished deliverable. Returns (post, complaint).

        With no time, it takes the next slot the rules allow. With a time, it
        takes that time — and if that breaks a rule it says so and queues
        nothing, unless `force`. Refusing quietly would be worse than either.
        """
        service = deliverable.service
        moment = parse_time(when) if when else self.next_free(service)
        problem = self._clashes(service, moment)
        if problem and not force:
            return None, problem

        post = Post(
            id=secrets.token_hex(4),
            platform=deliverable.platform,
            service=service,
            when=moment,
            video=str(deliverable.video),
            caption=caption or deliverable.caption.body,
            cover=str(deliverable.cover) if deliverable.cover else "",
        )
        self.posts.append(post)
        self.posts.sort(key=lambda item: item.when)
        return post, problem if force else ""

    def plan(
        self,
        deliverables: Sequence[Deliverable],
        *,
        start: str | datetime | None = None,
        caption_of: dict[str, str] | None = None,
    ) -> list[Post]:
        """Lay a batch out across the coming days, spaced by the rules."""
        when = parse_time(start) if start else datetime.now(timezone.utc)
        made: list[Post] = []
        for deliverable in deliverables:
            slot = self.next_free(deliverable.service, when)
            post, _ = self.add(
                deliverable,
                slot,
                caption=(caption_of or {}).get(deliverable.platform, ""),
                force=True,
            )
            if post is not None:
                made.append(post)
        return made

    # -- reading and changing ---------------------------------------------

    def get(self, post_id: str) -> Post | None:
        return next((post for post in self.posts if post.id == post_id), None)

    def due(self, now: datetime | None = None) -> list[Post]:
        moment = now or datetime.now(timezone.utc)
        return [post for post in self.posts if post.status == "queued" and post.when <= moment]

    def upcoming(self, limit: int = 0) -> list[Post]:
        queued = [post for post in self.posts if post.status == "queued"]
        queued.sort(key=lambda post: post.when)
        return queued[:limit] if limit else queued

    def mark(self, post_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r} (choose from {', '.join(STATUSES)})")
        post = self.get(post_id)
        if post is None:
            return False
        post.status = status
        return True

    def remove(self, post_id: str) -> bool:
        before = len(self.posts)
        self.posts = [post for post in self.posts if post.id != post_id]
        return len(self.posts) < before

    def forget_missing(self) -> list[Post]:
        """Drop queued posts whose video is no longer on disk."""
        gone = [
            post for post in self.posts if post.status == "queued" and not Path(post.video).exists()
        ]
        for post in gone:
            self.posts.remove(post)
        return gone

    def export_csv(self, posts: Iterable[Post] | None = None) -> str:
        """The queue as CSV, for whatever actually does the posting."""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["when_utc", "platform", "service", "video", "cover", "status", "caption"])
        for post in posts if posts is not None else sorted(self.posts, key=lambda p: p.when):
            writer.writerow(
                [
                    format_time(post.when),
                    post.platform,
                    post.service,
                    post.video,
                    post.cover,
                    post.status,
                    post.caption.replace("\n", " / "),
                ]
            )
        return buffer.getvalue()

    def describe(self) -> str:
        counts = dict.fromkeys(STATUSES, 0)
        for post in self.posts:
            counts[post.status] = counts.get(post.status, 0) + 1
        return (
            f"{counts['queued']} queued, {counts['posted']} posted, "
            f"{counts['skipped']} skipped · "
            f"one every {self.gap_hours:.0f}h, at most {self.per_day} a day per service"
        )
