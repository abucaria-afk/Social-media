"""A project: one piece of work, with an album and a map.

The app could already plan a post and cut a film, and had nowhere to put the
thing both of those belong to. A trip is not one post. It is a fortnight, a
place, forty clips, three reels cut from them, and a dozen half-ideas that
occurred to you before you went and would otherwise live in a notes app.

So one object with two faces, because they are two views of the same work and
splitting them into two features would mean maintaining the join by hand:

* **The album** — what actually came back. The films cut under this project,
  and the plans on the board that belong to it. Nothing is copied into it; a
  film carries the project it was made for, and the album is the question
  asked the other way round.
* **The map** — what you were thinking. A canvas of nodes: ideas, shots you
  want, a reel whose timing you liked, a decade you want it to look like, a
  film you already made. Links between them, because "this leads to that" is
  most of what planning actually is and a list cannot hold it.

The map is deliberately not free-form drawing. Every node has a *kind*, and
the kinds are the things this app already understands — so a shot node can
become a real capture on a plan, a look node can set the grade, and a reel
node is a template the editor can cut to. A canvas of arbitrary shapes would
be prettier and would connect to nothing.

Coordinates are stored in world units, unrounded, and the page decides the
zoom. Storing screen positions would mean a map laid out on a phone opening
scrambled on an iPad.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: What a node can be. Each one is a thing the rest of the app has an opinion
#: about, which is the whole reason the map is typed rather than free-form.
KINDS: dict[str, str] = {
    "idea": "a thought, in your own words",
    "shot": "something to go and film",
    "look": "a decade or a grade to wear",
    "reel": "a reference whose timing you liked",
    "film": "something already cut",
    "place": "somewhere to be",
    "person": "somebody who should be in it",
}

#: How long a node's text may be. A node is a label on a map, not a document —
#: past this it stops being readable at a glance, which is the only thing a
#: map is for.
LONGEST_TEXT = 240
LONGEST_NAME = 80
LONGEST_NOTE = 600

#: How far apart new nodes are dropped when nothing says where they go, so a
#: handful added in a row do not land on top of each other.
STEP = 190


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".new")
    scratch.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(scratch, path)


def _read(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _one_line(text: str, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()[:limit]


def _tidy(text: str, limit: int) -> str:
    """Keep the line breaks somebody typed, lose the runs of blank lines."""
    text = re.sub(r"\r\n?", "\n", str(text or ""))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def _when(value: str) -> str:
    """A date as `YYYY-MM-DD`, or "". Not parsed into a datetime.

    A trip's dates are written down, compared as strings and shown back; none
    of that needs a timezone, and giving them one would invent a time of day
    nobody typed.
    """
    raw = _one_line(value, 10)
    return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else ""


@dataclass
class Node:
    """One thing on the map."""

    id: str
    kind: str
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    #: What it points at, when the kind has something to point at: a film id,
    #: a template name, an era key. Empty for an idea.
    ref: str = ""
    #: Set when a shot node has been turned into a real capture on a plan, so
    #: it cannot be turned into a second one.
    done: bool = False

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Link:
    """ "This leads to that." The only relationship the map has, on purpose —
    a map with six kinds of arrow is a map with a legend."""

    id: str
    a: str
    b: str
    note: str = ""

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Project:
    """One piece of work: a trip, a season, a wedding, a campaign."""

    id: str
    owner: str
    name: str
    note: str = ""
    #: The time it covers. A trip has dates; a running project may not, and
    #: both are fine.
    starts: str = ""
    ends: str = ""
    place: str = ""
    #: A film id used as the album's cover. Empty means the newest film in it.
    cover: str = ""
    nodes: list = field(default_factory=list)
    links: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    @property
    def dated(self) -> str:
        """The dates as one readable string, or ""."""
        if self.starts and self.ends and self.starts != self.ends:
            return f"{self.starts} to {self.ends}"
        return self.starts or self.ends or ""

    def public(self, *, films: int = 0, plans: int = 0) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "note": self.note,
            "starts": self.starts,
            "ends": self.ends,
            "dated": self.dated,
            "place": self.place,
            "cover": self.cover,
            "films": films,
            "plans": plans,
            "nodes": len(self.nodes),
            "created": round(self.created, 3),
            "updated": round(self.updated, 3),
        }

    def whole(self, *, films: list | None = None, plans: list | None = None) -> dict:
        """Everything one project page needs, in one answer.

        One request rather than four. A page that fetches the project, then
        its nodes, then its links, then its films is a page that renders four
        times and lays out differently each time.
        """
        out = self.public(films=len(films or []), plans=len(plans or []))
        out["map"] = {
            "nodes": [Node(**n).public() for n in self.nodes],
            "links": [Link(**link).public() for link in self.links],
        }
        out["album"] = {"films": films or [], "plans": plans or []}
        out["kinds"] = KINDS
        return out


class Projects:
    """Everybody's projects, on this instance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.projects: dict[str, Project] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "projects.json"

    def _load(self) -> None:
        raw = _read(self.path)
        if not isinstance(raw, list):
            return
        known = set(Project.__dataclass_fields__)
        for row in raw:
            if isinstance(row, dict) and "id" in row:
                self.projects[row["id"]] = Project(**{k: v for k, v in row.items() if k in known})

    def _save(self) -> None:
        _write(self.path, [asdict(p) for p in self._newest_first()])

    def _newest_first(self) -> list[Project]:
        return sorted(self.projects.values(), key=lambda p: -p.updated)

    # -- projects --------------------------------------------------------

    def make(self, owner: str, name: str, **fields) -> Project | None:
        name = _one_line(name, LONGEST_NAME)
        if not owner or not name:
            return None
        project = Project(id=uuid.uuid4().hex[:12], owner=owner, name=name)
        with self.lock:
            self.projects[project.id] = project
        self.edit(project.id, owner, **fields)
        return self.get(project.id, owner)

    def get(self, project_id: str, owner: str = "") -> Project | None:
        """One project, and only if it is theirs.

        The owner check is here rather than in the route because every route
        needs it and one of them will forget. A project is somebody's own
        planning — it is not shared, and there is no route that shares it.
        """
        with self.lock:
            found = self.projects.get(project_id)
            if found is None or (owner and found.owner != owner):
                return None
            return found

    def by(self, owner: str) -> list[Project]:
        with self.lock:
            return [p for p in self._newest_first() if p.owner == owner]

    def edit(self, project_id: str, owner: str, **fields) -> Project | None:
        """Change a project. Only the fields given; `None` means "leave it"."""
        project = self.get(project_id, owner)
        if project is None:
            return None
        with self.lock:
            if fields.get("name") is not None:
                project.name = _one_line(fields["name"], LONGEST_NAME) or project.name
            if fields.get("note") is not None:
                project.note = _tidy(fields["note"], LONGEST_NOTE)
            if fields.get("place") is not None:
                project.place = _one_line(fields["place"], LONGEST_NAME)
            if fields.get("starts") is not None:
                project.starts = _when(fields["starts"])
            if fields.get("ends") is not None:
                project.ends = _when(fields["ends"])
            if fields.get("cover") is not None:
                project.cover = _one_line(fields["cover"], 64)
            # A trip typed backwards is a trip somebody typed backwards, not
            # an error worth refusing — but the album sorts by these, so they
            # are put the right way round rather than left to sort wrongly.
            if project.starts and project.ends and project.ends < project.starts:
                project.starts, project.ends = project.ends, project.starts
            project.updated = time.time()
            self._save()
            return project

    def drop(self, project_id: str, owner: str) -> bool:
        """Forget a project and its map. The films it held are not touched.

        Deliberately: a project is a way of looking at footage, and deleting
        the way of looking should never delete the footage. Films that
        belonged to it lose the label and stay in the feed.
        """
        project = self.get(project_id, owner)
        if project is None:
            return False
        with self.lock:
            self.projects.pop(project_id, None)
            self._save()
        return True

    def forget_everything_by(self, owner: str) -> int:
        """Part of deleting an account."""
        with self.lock:
            mine = [p.id for p in self.projects.values() if p.owner == owner]
            for project_id in mine:
                self.projects.pop(project_id, None)
            if mine:
                self._save()
        return len(mine)

    # -- the map ---------------------------------------------------------

    def add_node(self, project_id: str, owner: str, **fields) -> Node | None:
        project = self.get(project_id, owner)
        if project is None or fields.get("kind") not in KINDS:
            return None
        with self.lock:
            # Where it lands when nothing says. Down and to the right of the
            # last one, so adding five in a row gives five you can see rather
            # than one with four underneath it.
            count = len(project.nodes)
            node = Node(
                id=uuid.uuid4().hex[:8],
                kind=fields["kind"],
                text=_one_line(fields.get("text", ""), LONGEST_TEXT),
                ref=_one_line(fields.get("ref", ""), 120),
                x=float(fields.get("x", (count % 4) * STEP)),
                y=float(fields.get("y", (count // 4) * STEP)),
            )
            project.nodes.append(asdict(node))
            project.updated = time.time()
            self._save()
            return node

    def move_nodes(self, project_id: str, owner: str, moves: dict) -> int:
        """Put nodes where a drag left them. `moves` is id -> (x, y).

        Plural, and applied in one write. A drag emits a position on every
        frame; saving each one would be sixty writes a second to a JSON file,
        so the page sends where things ended up and this is what receives it.
        """
        project = self.get(project_id, owner)
        if project is None or not moves:
            return 0
        moved = 0
        with self.lock:
            for node in project.nodes:
                where = moves.get(node["id"])
                if not where:
                    continue
                try:
                    node["x"], node["y"] = float(where[0]), float(where[1])
                except (TypeError, ValueError, IndexError):
                    continue
                moved += 1
            if moved:
                project.updated = time.time()
                self._save()
        return moved

    def edit_node(self, project_id: str, owner: str, node_id: str, **fields) -> Node | None:
        project = self.get(project_id, owner)
        if project is None:
            return None
        with self.lock:
            for node in project.nodes:
                if node["id"] != node_id:
                    continue
                if fields.get("text") is not None:
                    node["text"] = _one_line(fields["text"], LONGEST_TEXT)
                if fields.get("ref") is not None:
                    node["ref"] = _one_line(fields["ref"], 120)
                if fields.get("kind") in KINDS:
                    node["kind"] = fields["kind"]
                if fields.get("done") is not None:
                    node["done"] = bool(fields["done"])
                project.updated = time.time()
                self._save()
                return Node(**node)
        return None

    def drop_node(self, project_id: str, owner: str, node_id: str) -> bool:
        """Remove a node, and any link that touched it.

        The second half is the point: a link to a node that is gone is an
        arrow to nowhere, and it draws as one.
        """
        project = self.get(project_id, owner)
        if project is None:
            return False
        with self.lock:
            before = len(project.nodes)
            project.nodes = [n for n in project.nodes if n["id"] != node_id]
            if len(project.nodes) == before:
                return False
            project.links = [
                link for link in project.links if node_id not in (link["a"], link["b"])
            ]
            project.updated = time.time()
            self._save()
            return True

    def link(self, project_id: str, owner: str, a: str, b: str, note: str = "") -> Link | None:
        """Join two nodes. Returns the existing link if there already is one."""
        project = self.get(project_id, owner)
        if project is None or not a or not b or a == b:
            return None
        with self.lock:
            have = {n["id"] for n in project.nodes}
            if a not in have or b not in have:
                return None
            for link in project.links:
                # Either way round: two nodes are joined or they are not, and
                # a second arrow back the other way is a duplicate, not a
                # different fact.
                if {link["a"], link["b"]} == {a, b}:
                    return Link(**link)
            made = Link(id=uuid.uuid4().hex[:8], a=a, b=b, note=_one_line(note, 120))
            project.links.append(asdict(made))
            project.updated = time.time()
            self._save()
            return made

    def unlink(self, project_id: str, owner: str, link_id: str) -> bool:
        project = self.get(project_id, owner)
        if project is None:
            return False
        with self.lock:
            before = len(project.links)
            project.links = [link for link in project.links if link["id"] != link_id]
            if len(project.links) == before:
                return False
            project.updated = time.time()
            self._save()
            return True
