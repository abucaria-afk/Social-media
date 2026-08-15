"""The media manager: one index of everything you have shot.

Pointing the editor at a folder works, and stops working the moment there is
more than a folder. Footage arrives from a phone, a camera and three AirDrops,
half of it is the same clip twice under different names, and probing a hundred
files takes long enough that doing it again for every edit is the slow part of
the day.

So this keeps an index. Scan once, and afterwards a scan only looks at what
changed — size and modification time decide that, the way every backup tool
decides it. What it holds is what the rest of the agent needs to choose
footage: kind, runtime, frame size, whether there is sound. What it deliberately
does not hold is the footage itself; the index is a JSON file beside your media,
and deleting it costs one rescan and nothing else.

Duplicates are found by content rather than by name, because the same clip
saved twice is the single most common thing in a footage folder and the name is
never the same. See `digest_of` for how, and for what that guarantee is worth.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable, Sequence

from ..config import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES
from ..ingest import classify, discover, probe_asset

log = logging.getLogger("auteur.workflows.library")

#: Bytes read from each end of a file when fingerprinting it. Four mebibytes
#: covers a video's header and its final packets, which is where two different
#: recordings differ even when they are the same length.
_CHUNK = 4 * 1024 * 1024

#: The index file, kept beside the media it describes.
INDEX_NAME = "auteur-library.json"


def digest_of(path: Path) -> str:
    """A fingerprint for "is this the same file I already have?".

    Size, then the first and last few megabytes. Not the whole file: media is
    measured in gigabytes and hashing all of it would make a rescan cost as
    much as the original scan, which is the thing this module exists to avoid.

    Two different files *can* collide here — same size, same head, same tail,
    different middle. It is not a cryptographic claim and nothing security-
    related may rest on it. `Library.scan` therefore treats a matching digest
    as a *candidate* and confirms it with a full byte comparison before calling
    anything a duplicate, which is cheap because near-misses are vanishingly
    rare.
    """
    size = path.stat().st_size
    hasher = hashlib.sha256(str(size).encode())
    with path.open("rb") as handle:
        hasher.update(handle.read(_CHUNK))
        if size > _CHUNK * 2:
            handle.seek(-_CHUNK, os.SEEK_END)
            hasher.update(handle.read(_CHUNK))
    return hasher.hexdigest()


@dataclass
class LibraryEntry:
    """One piece of material, as the index remembers it."""

    path: str
    digest: str
    kind: str
    size: int
    modified: float
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    readable: bool = True
    why_unreadable: str = ""
    added: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    @property
    def file(self) -> Path:
        return Path(self.path)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def is_vertical(self) -> bool:
        return bool(self.height) and self.height > self.width

    @property
    def is_visual(self) -> bool:
        return self.kind in ("video", "image")

    def exists(self) -> bool:
        return Path(self.path).exists()

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "digest": self.digest,
            "kind": self.kind,
            "size": self.size,
            "modified": self.modified,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "has_audio": self.has_audio,
            "readable": self.readable,
            "why_unreadable": self.why_unreadable,
            "added": self.added,
            "tags": list(self.tags),
        }

    @staticmethod
    def from_json(raw: dict) -> LibraryEntry:
        return LibraryEntry(
            path=str(raw.get("path", "")),
            digest=str(raw.get("digest", "")),
            kind=str(raw.get("kind", "video")),
            size=int(raw.get("size", 0)),
            modified=float(raw.get("modified", 0.0)),
            duration=float(raw.get("duration", 0.0)),
            width=int(raw.get("width", 0)),
            height=int(raw.get("height", 0)),
            fps=float(raw.get("fps", 0.0)),
            has_audio=bool(raw.get("has_audio", False)),
            readable=bool(raw.get("readable", True)),
            why_unreadable=str(raw.get("why_unreadable", "")),
            added=float(raw.get("added", 0.0)) or time.time(),
            tags=[str(tag) for tag in raw.get("tags", [])],
        )

    def summary(self) -> str:
        bits: list[str] = [self.kind]
        if self.duration:
            bits.append(f"{self.duration:.1f}s")
        if self.width and self.height:
            bits.append(f"{self.width}x{self.height}")
        if self.has_audio:
            bits.append("sound")
        if not self.readable:
            bits.append(f"unreadable: {self.why_unreadable}")
        return f"{self.name} ({', '.join(bits)})"


def describe_bytes(count: int) -> str:
    """Sizes a person reads. GB for footage, MB for the rest."""
    if count >= 1_000_000_000:
        return f"{count / 1e9:.2f} GB"
    if count >= 1_000_000:
        return f"{count / 1e6:.0f} MB"
    return f"{count / 1e3:.0f} kB"


@dataclass
class ScanReport:
    """What one scan changed. Printed, so it is written to be read aloud."""

    added: list[LibraryEntry] = field(default_factory=list)
    updated: list[LibraryEntry] = field(default_factory=list)
    unchanged: int = 0
    #: (the copy, the original it duplicates)
    duplicates: list[tuple[LibraryEntry, LibraryEntry]] = field(default_factory=list)
    unreadable: list[LibraryEntry] = field(default_factory=list)
    #: Entries whose file is no longer where the index left it.
    missing: list[LibraryEntry] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.missing)

    def describe(self) -> str:
        lines = [
            f"{len(self.added)} new, {len(self.updated)} changed, {self.unchanged} already known"
        ]
        if self.duplicates:
            wasted = sum(copy.size for copy, _ in self.duplicates)
            lines.append(
                f"{len(self.duplicates)} duplicate(s), "
                f"{describe_bytes(wasted)} of the same footage twice"
            )
        if self.unreadable:
            lines.append(f"{len(self.unreadable)} file(s) nothing could open")
        if self.missing:
            lines.append(f"{len(self.missing)} file(s) in the index have gone")
        return "\n".join(lines)


class Library:
    """An index of every piece of material, and what is in it."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.entries: dict[str, LibraryEntry] = {}
        self.roots: list[str] = []
        self.scanned: float = 0.0
        self._load()

    # -- on disk ----------------------------------------------------------

    @staticmethod
    def default_path(root: str | Path) -> Path:
        return Path(root).expanduser().resolve() / INDEX_NAME

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written index is not worth a crash; it is worth a rescan.
            log.warning(
                "could not read the media index at %s — starting a new one", self.path
            )
            return
        self.roots = [str(item) for item in raw.get("roots", [])]
        self.scanned = float(raw.get("scanned", 0.0))
        for item in raw.get("entries", []):
            entry = LibraryEntry.from_json(item)
            if entry.path:
                self.entries[entry.path] = entry

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "roots": self.roots,
            "scanned": self.scanned,
            "entries": [entry.to_json() for entry in self.sorted_entries()],
        }
        # Write beside and rename, so an interrupted save cannot leave the
        # index truncated — the failure mode that made it unreadable above.
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    # -- scanning ---------------------------------------------------------

    def scan(self, roots: Sequence[str | Path], *, prune: bool = True) -> ScanReport:
        """Bring the index up to date with what is on disk.

        Only files that are new, or whose size or timestamp moved, are probed.
        Everything else is left exactly as it was, which is what makes the
        second scan of a big folder take a second rather than a minute.
        """
        started = time.perf_counter()
        report = ScanReport()

        found = discover(roots)
        for root in roots:
            text = str(Path(root).expanduser().resolve())
            if text not in self.roots:
                self.roots.append(text)

        seen: set[str] = set()
        by_digest: dict[str, LibraryEntry] = {}
        # Existing entries seed the duplicate table, so a clip that duplicates
        # something scanned last week is still caught this week.
        for entry in self.sorted_entries():
            by_digest.setdefault(entry.digest, entry)

        for path in found:
            key = str(path)
            seen.add(key)
            try:
                stat = path.stat()
            except OSError:
                continue

            known = self.entries.get(key)
            if known and known.size == stat.st_size and known.modified == stat.st_mtime:
                report.unchanged += 1
                if not known.readable:
                    report.unreadable.append(known)
                continue

            entry = self._probe(path, stat.st_size, stat.st_mtime)
            if known is not None:
                entry.added = known.added
                entry.tags = list(known.tags)
                report.updated.append(entry)
            else:
                report.added.append(entry)
            self.entries[key] = entry

            if not entry.readable:
                report.unreadable.append(entry)
                continue

            original = by_digest.get(entry.digest)
            if original is None or original.path == entry.path:
                by_digest.setdefault(entry.digest, entry)
            elif self._really_identical(entry, original):
                # Whichever was written first is the original. Without this the
                # answer is alphabetical: scanning a folder holding one.mp4 and
                # copy.mp4 reported *one.mp4* as the copy, because "c" sorts
                # before "o". Nobody should be told to delete the original.
                copy, kept = entry, original
                if entry.modified < original.modified:
                    copy, kept = original, entry
                    by_digest[entry.digest] = entry
                report.duplicates.append((copy, kept))

        if prune:
            for key in [key for key in self.entries if key not in seen]:
                entry = self.entries[key]
                if not entry.exists():
                    report.missing.append(entry)
                    del self.entries[key]

        self.scanned = time.time()
        report.seconds = time.perf_counter() - started
        return report

    def _probe(self, path: Path, size: int, modified: float) -> LibraryEntry:
        kind = classify(path) or "video"
        entry = LibraryEntry(
            path=str(path),
            digest=digest_of(path),
            kind=kind,
            size=size,
            modified=modified,
        )
        asset = probe_asset(path)
        if asset is None:
            entry.readable = False
            entry.why_unreadable = "ffprobe could not read it"
            return entry
        width, height = asset.display_size
        entry.kind = asset.kind
        entry.duration = asset.duration
        entry.width, entry.height = width, height
        entry.fps = asset.fps
        entry.has_audio = asset.has_audio
        return entry

    @staticmethod
    def _really_identical(one: LibraryEntry, other: LibraryEntry) -> bool:
        """Confirm a digest match byte for byte before crying duplicate.

        The digest only reads each end of the file. Telling someone to delete
        footage on the strength of that would be careless, and the full compare
        only ever runs for the handful of files that already look identical.
        """
        try:
            return filecmp.cmp(one.path, other.path, shallow=False)
        except OSError:
            return False

    # -- reading it -------------------------------------------------------

    def sorted_entries(self) -> list[LibraryEntry]:
        return sorted(self.entries.values(), key=lambda entry: entry.path)

    @property
    def total_footage(self) -> float:
        return sum(
            e.duration
            for e in self.entries.values()
            if e.kind == "video" and e.readable
        )

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries.values())

    def pick(
        self,
        *,
        kind: str | None = None,
        vertical: bool | None = None,
        min_duration: float = 0.0,
        max_duration: float = 0.0,
        tag: str | None = None,
        readable_only: bool = True,
        limit: int = 0,
    ) -> list[LibraryEntry]:
        """The material matching a description, longest first.

        Longest first because a workflow asking for footage wants the clips
        with room to cut inside them, not the three-second fragments.
        """
        chosen: list[LibraryEntry] = []
        for entry in self.entries.values():
            if readable_only and not entry.readable:
                continue
            if kind and entry.kind != kind:
                continue
            if (
                vertical is not None
                and entry.kind == "video"
                and entry.is_vertical != vertical
            ):
                continue
            if min_duration and entry.duration < min_duration:
                continue
            if max_duration and entry.duration > max_duration:
                continue
            if tag and tag not in entry.tags:
                continue
            chosen.append(entry)
        chosen.sort(key=lambda entry: (-entry.duration, entry.path))
        return chosen[:limit] if limit else chosen

    def duplicate_groups(self) -> list[list[LibraryEntry]]:
        """Files the index believes are the same footage, confirmed byte for byte.

        Grouped rather than paired, because the same clip arriving four times
        is one decision to make, not three.
        """
        by_digest: dict[str, list[LibraryEntry]] = {}
        for entry in self.sorted_entries():
            if entry.readable:
                by_digest.setdefault(entry.digest, []).append(entry)

        groups: list[list[LibraryEntry]] = []
        for candidates in by_digest.values():
            if len(candidates) < 2:
                continue
            first = candidates[0]
            confirmed = [first] + [
                other
                for other in candidates[1:]
                if self._really_identical(first, other)
            ]
            if len(confirmed) > 1:
                # Oldest first, so the head of each group is the one to keep.
                confirmed.sort(key=lambda entry: (entry.modified, entry.path))
                groups.append(confirmed)
        groups.sort(key=lambda group: -sum(entry.size for entry in group))
        return groups

    def tag(self, paths: Iterable[str | Path], *label: str) -> int:
        """Label material so a workflow can ask for it by name later."""
        touched = 0
        wanted = {str(Path(path).expanduser().resolve()) for path in paths}
        for key, entry in self.entries.items():
            if key not in wanted:
                continue
            for one in label:
                if one and one not in entry.tags:
                    entry.tags.append(one)
                    touched += 1
        return touched

    def describe(self) -> str:
        kinds = {"video": 0, "image": 0, "audio": 0}
        for entry in self.entries.values():
            kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        return (
            f"{len(self.entries)} file(s): "
            f"{kinds.get('video', 0)} video, {kinds.get('image', 0)} image, "
            f"{kinds.get('audio', 0)} audio · "
            f"{self.total_footage / 60:.1f} minutes of footage · "
            f"{describe_bytes(self.total_bytes)}"
        )


#: Exposed so the CLI can say what it will look at without importing config.
KNOWN_SUFFIXES = sorted(VIDEO_SUFFIXES | IMAGE_SUFFIXES | AUDIO_SUFFIXES)
