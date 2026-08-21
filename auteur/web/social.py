"""The two things that make this an app people open rather than a tool they run.

A film used to exist only inside the job that made it. Jobs are swept after a
few hours and live in one process's memory, so the only way to see something
you made yesterday was to have saved the file to your camera roll at the time.
That is fine for a renderer and wrong for an app: there was nothing to come
back to, and nothing anyone else could see.

So two small stores, both plain JSON beside the accounts file:

* :class:`Films` — every finished film, kept after its job is gone. This is
  what the feed scrolls.
* :class:`Messages` — conversations between people on the instance, so a film
  can be sent to somebody rather than exported and re-uploaded elsewhere.

Deliberately not a database. The whole point of this program is that it runs
on somebody's own machine from one `pip install`, and a service dependency
would be the first thing in it that cannot.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: How many films the feed hands over at once. A phone showing one film per
#: screen never needs more, and every extra row is a video element the browser
#: will happily start buffering.
PAGE = 24

#: Longest message anybody can send. Not a security boundary — the store is
#: local — but an unbounded text field writes the whole JSON file on every
#: keystroke somebody pastes a novel into.
LONGEST_MESSAGE = 2000


def _write(path: Path, payload: object) -> None:
    """Write JSON so a crash mid-write cannot leave a truncated store.

    The accounts file learned this the hard way: a half-written file is not a
    corrupt account, it is *no* accounts, and the app comes back up claiming
    nobody has ever signed in.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".new")
    scratch.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(scratch, path)


def _read(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Films
# ---------------------------------------------------------------------------


@dataclass
class Film:
    """One finished film, after the job that made it is gone."""

    id: str
    owner: str
    prompt: str
    #: Path to the mp4, as a string because this round-trips through JSON.
    video: str
    #: The same short lines the "your film is ready" screen shows.
    facts: list[str] = field(default_factory=list)
    #: What the editor understood, said back in the person's own words.
    heard: str = ""
    #: Which reference reel it was cut to, if any.
    template: str = ""
    #: Which decade grade it wears, if any.
    era: str = ""
    created: float = field(default_factory=time.time)
    #: Who has liked it. A set of names, kept as a list for JSON.
    liked_by: list[str] = field(default_factory=list)

    def public(self, who: str = "") -> dict:
        """What a browser is allowed to know about this film.

        Never the path on disk. The feed addresses films by id through a route
        that checks the id exists, which is the only reason a filesystem path
        has no business crossing into a page.
        """
        return {
            "id": self.id,
            "owner": self.owner,
            "prompt": self.prompt,
            "facts": list(self.facts),
            "heard": self.heard,
            "template": self.template,
            "era": self.era,
            "created": round(self.created, 3),
            "likes": len(self.liked_by),
            "liked": who in self.liked_by,
            "video": f"/api/films/{self.id}/video",
            "poster": f"/api/films/{self.id}/poster",
            "mine": bool(who) and who == self.owner,
        }


class Films:
    """Every film anybody on this instance has finished, newest first."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.films: dict[str, Film] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "films.json"

    def _load(self) -> None:
        raw = _read(self.path)
        if not isinstance(raw, list):
            return
        for row in raw:
            if not isinstance(row, dict) or "id" not in row:
                continue
            known = set(Film.__dataclass_fields__)
            self.films[row["id"]] = Film(**{k: v for k, v in row.items() if k in known})

    def _save(self) -> None:
        _write(self.path, [asdict(f) for f in self._newest_first()])

    def _newest_first(self) -> list[Film]:
        return sorted(self.films.values(), key=lambda f: -f.created)

    # -- writing ---------------------------------------------------------

    def add(self, **fields) -> Film:
        """Record a finished film. The id is the film's, not the job's.

        A job id is reused as a folder name and swept with the folder; a film
        outlives both, so it gets its own.
        """
        film = Film(id=uuid.uuid4().hex[:12], **fields)
        with self.lock:
            self.films[film.id] = film
            self._save()
        return film

    def like(self, film_id: str, who: str) -> Film | None:
        """Toggle. Returns the film so the caller can send back the new count."""
        with self.lock:
            film = self.films.get(film_id)
            if film is None or not who:
                return None
            if who in film.liked_by:
                film.liked_by.remove(who)
            else:
                film.liked_by.append(who)
            self._save()
            return film

    def forget(self, film_id: str, who: str) -> bool:
        """Remove a film from the feed. Only its own author may."""
        with self.lock:
            film = self.films.get(film_id)
            if film is None or film.owner != who:
                return False
            self.films.pop(film_id)
            self._save()
            return True

    def remove_any(self, film_id: str) -> str | None:
        """Remove a film whoever made it. Returns its path, or None.

        `forget` refuses anything that is not yours, which is right for the
        app and wrong for the person whose computer this is: the App Store
        asks that reported material can actually be taken down, and a
        moderator who can only delete their own films cannot do that. This is
        reachable from `auteur moderate` and from nowhere the network can
        touch — there is no route to it, deliberately.
        """
        with self.lock:
            film = self.films.pop(film_id, None)
            if film is None:
                return None
            self._save()
        return film.video

    def forget_everything_by(self, owner: str) -> list[str]:
        """Every film somebody made, gone. Returns the paths, for the caller.

        Part of deleting an account. The files are handed back rather than
        deleted here because a film points into a job folder this store does
        not own, and a store that unlinks paths out of its own JSON is one
        traversal away from unlinking something else.
        """
        with self.lock:
            mine = [f for f in self.films.values() if f.owner == owner]
            for film in mine:
                self.films.pop(film.id, None)
            if mine:
                self._save()
        return [f.video for f in mine]

    def drop_missing(self) -> int:
        """Forget films whose file is gone, and say how many.

        Jobs are swept with their folders and a film points into one, so a feed
        that never checks fills up with rows that play nothing — the failure
        mode where the app looks busy and every tap is a black screen.
        """
        with self.lock:
            gone = [f.id for f in self.films.values() if not Path(f.video).is_file()]
            for film_id in gone:
                self.films.pop(film_id, None)
            if gone:
                self._save()
        return len(gone)

    # -- reading ---------------------------------------------------------

    def get(self, film_id: str) -> Film | None:
        with self.lock:
            return self.films.get(film_id)

    def feed(self, who: str = "", limit: int = PAGE, before: float | None = None) -> list[Film]:
        """Newest first, optionally everything older than `before`."""
        with self.lock:
            rows = self._newest_first()
        if before is not None:
            rows = [f for f in rows if f.created < before]
        return rows[:limit]

    def by(self, owner: str, limit: int = PAGE) -> list[Film]:
        with self.lock:
            rows = [f for f in self._newest_first() if f.owner == owner]
        return rows[:limit]

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.films)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class Message:
    id: str
    sender: str
    to: str
    text: str = ""
    #: A film id, when somebody sends a film rather than words.
    film: str = ""
    at: float = field(default_factory=time.time)
    read: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "to": self.to,
            "text": self.text,
            "film": self.film,
            "at": round(self.at, 3),
            "read": self.read,
        }


def _pair(a: str, b: str) -> str:
    """One key for a conversation, whichever way round the two names arrive."""
    return "\x00".join(sorted((a, b)))


class Messages:
    """Conversations between people with accounts on this instance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        #: conversation key -> messages, oldest first
        self.threads: dict[str, list[Message]] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "messages.json"

    def _load(self) -> None:
        raw = _read(self.path)
        if not isinstance(raw, dict):
            return
        known = set(Message.__dataclass_fields__)
        for key, rows in raw.items():
            if not isinstance(rows, list):
                continue
            self.threads[key] = [
                Message(**{k: v for k, v in row.items() if k in known})
                for row in rows
                if isinstance(row, dict) and "id" in row
            ]

    def _save(self) -> None:
        _write(self.path, {k: [asdict(m) for m in v] for k, v in self.threads.items()})

    # -- writing ---------------------------------------------------------

    def send(self, sender: str, to: str, text: str = "", film: str = "") -> Message | None:
        """One message. Returns None if there is nothing in it to send."""
        text = (text or "").strip()[:LONGEST_MESSAGE]
        if not sender or not to or sender == to:
            return None
        if not text and not film:
            return None
        note = Message(id=uuid.uuid4().hex[:12], sender=sender, to=to, text=text, film=film)
        with self.lock:
            self.threads.setdefault(_pair(sender, to), []).append(note)
            self._save()
        return note

    def forget_everything_with(self, who: str) -> int:
        """Every conversation somebody is in, gone. Returns how many.

        Both halves of it. A deletion that removed only the messages somebody
        *sent* would leave the other side of every conversation talking to an
        account that no longer exists, which is worse than either keeping it
        all or removing it all.
        """
        with self.lock:
            keys = [k for k in self.threads if who in k.split("\x00")]
            for key in keys:
                self.threads.pop(key, None)
            if keys:
                self._save()
        return len(keys)

    def mark_read(self, who: str, other: str) -> None:
        """Everything `other` sent `who` has now been seen."""
        with self.lock:
            changed = False
            for note in self.threads.get(_pair(who, other), []):
                if note.to == who and not note.read:
                    note.read = True
                    changed = True
            if changed:
                self._save()

    # -- reading ---------------------------------------------------------

    def thread(self, who: str, other: str, limit: int = 200) -> list[Message]:
        with self.lock:
            return list(self.threads.get(_pair(who, other), []))[-limit:]

    def conversations(self, who: str) -> list[dict]:
        """Everyone `who` has talked to, most recent first, with the last line.

        This is the inbox. Each row carries enough to draw itself without a
        second request per person, because a list view that fetches per row is
        how a phone ends up making forty requests to show eight names.
        """
        out = []
        with self.lock:
            for key, notes in self.threads.items():
                names = key.split("\x00")
                if who not in names or not notes:
                    continue
                other = names[0] if names[1] == who else names[1]
                last = notes[-1]
                out.append(
                    {
                        "who": other,
                        "last": last.text or ("sent a film" if last.film else ""),
                        "at": round(last.at, 3),
                        "mine": last.sender == who,
                        "unread": sum(1 for n in notes if n.to == who and not n.read),
                    }
                )
        return sorted(out, key=lambda row: -row["at"])

    def unread(self, who: str) -> int:
        """One number for the badge on the tab bar."""
        return sum(row["unread"] for row in self.conversations(who))
