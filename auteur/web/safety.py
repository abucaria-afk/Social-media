"""Reporting, blocking, and the record an operator acts on.

This exists because the App Store requires it, and the requirement is a fair
one. Guideline 1.2 says an app carrying content other people wrote must have
four things: a way to filter objectionable material, a way to report it, a way
to block whoever is sending it, and a published way to reach whoever runs the
thing. An app with a feed and a message inbox and none of that is one rejection
away from never shipping, and — more to the point — is one bad afternoon away
from somebody having no way out of a conversation.

It is worth being exact about what "moderation" means for a program shaped like
this one, because the usual answer does not apply. There is no company here and
no moderation team. `auteur serve` runs on somebody's own computer, holding
their own footage, for the handful of people they gave accounts to. So:

* **Blocking is immediate and needs nobody.** It is the one control that has to
  work at three in the morning without anybody being asked, so it is entirely
  in the hands of the person doing it: their films, their messages and their
  name stop reaching the person they blocked, in both directions, at once.
* **Reporting goes to the person who runs the instance**, who is a real named
  human the reporter almost certainly knows — not a queue. It is written here,
  it is printed by `auteur moderate`, and it is the operator's job. Pretending
  there is a review team would be a lie in the App Store listing.
* **Filtering** is the operator's, and the tools are real: any film can be
  removed and any account can be closed from one command. What this does *not*
  do is guess at whether a photograph is objectionable — a program that
  silently deletes somebody's footage because a classifier fired is a worse
  failure than the one it prevents, on an instance holding one family's videos.

A report is kept after it is decided rather than deleted. The question a
reporter actually has is "did anything happen", and a store that forgets cannot
answer it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: What somebody can say is wrong. Short, because a list of twenty categories
#: is a list nobody reads to the end of — and because on an instance this size
#: the note underneath carries the actual information.
REASONS: dict[str, str] = {
    "sexual": "Sexual or nude content",
    "violence": "Violence or threats",
    "harassment": "Harassment or bullying",
    "hate": "Hate speech",
    "illegal": "Something illegal",
    "child-safety": "Something involving a child",
    "spam": "Spam",
    "other": "Something else",
}

#: The ones that mean somebody may be in danger rather than annoyed. They are
#: not treated differently by the store — they are sorted first by
#: `Reports.open_ones`, and `auteur moderate` says so.
URGENT = ("child-safety", "violence", "illegal")

#: What a report can be about.
KINDS = ("film", "message", "person")

#: Longest note. Enough for what happened; not enough to be a document.
LONGEST_NOTE = 600


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


@dataclass
class Report:
    """One thing somebody said was wrong, and what was done about it."""

    id: str
    #: "film", "message" or "person".
    kind: str
    #: The film id, the message id, or the username.
    about: str
    #: Whose content it is. Kept separately from `about` so a report about a
    #: message still names a person without the message having to be found —
    #: which matters, because by the time anybody looks the message may be
    #: gone.
    about_who: str
    by: str
    reason: str
    note: str = ""
    at: float = field(default_factory=time.time)
    #: "open", then "removed" or "kept" once the operator has decided.
    state: str = "open"
    decided_at: float = 0.0
    decided_note: str = ""

    @property
    def urgent(self) -> bool:
        return self.reason in URGENT

    def public(self) -> dict:
        """What the person who filed it is allowed to see.

        Not `by` — they know — and not the operator's note, which is written
        for the operator. What they get is the answer to "did anything
        happen", which is the whole question.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "about": self.about,
            "reason": self.reason,
            "at": round(self.at, 3),
            "state": self.state,
            "decided_at": round(self.decided_at, 3),
        }


class Reports:
    """Everything anybody has reported on this instance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.reports: dict[str, Report] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "reports.json"

    def _load(self) -> None:
        raw = _read(self.path)
        if not isinstance(raw, list):
            return
        known = set(Report.__dataclass_fields__)
        for row in raw:
            if isinstance(row, dict) and "id" in row:
                self.reports[row["id"]] = Report(**{k: v for k, v in row.items() if k in known})

    def _save(self) -> None:
        _write(self.path, [asdict(r) for r in self._newest_first()])

    def _newest_first(self) -> list[Report]:
        return sorted(self.reports.values(), key=lambda r: -r.at)

    # -- writing ---------------------------------------------------------

    def file(
        self,
        *,
        by: str,
        kind: str,
        about: str,
        about_who: str,
        reason: str,
        note: str = "",
    ) -> Report | None:
        """Record a report. None if there is not enough of one to record.

        Reporting the same thing twice returns the report that already exists
        rather than making a second one. Not to be tidy: an operator working
        through a list needs "how many people reported this" to mean
        something, and a person tapping the button again because nothing
        visibly happened would otherwise make it mean nothing.
        """
        if kind not in KINDS or reason not in REASONS or not by or not about:
            return None
        with self.lock:
            for existing in self.reports.values():
                if (
                    existing.by == by
                    and existing.kind == kind
                    and existing.about == about
                    and existing.state == "open"
                ):
                    return existing
            report = Report(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                about=about,
                about_who=about_who,
                by=by,
                reason=reason,
                note=str(note or "").strip()[:LONGEST_NOTE],
            )
            self.reports[report.id] = report
            self._save()
            return report

    def decide(self, report_id: str, state: str, note: str = "") -> Report | None:
        """Mark a report acted on. `state` is "removed" or "kept"."""
        if state not in ("removed", "kept"):
            return None
        with self.lock:
            report = self.reports.get(report_id)
            if report is None:
                return None
            report.state = state
            report.decided_at = time.time()
            report.decided_note = str(note or "").strip()[:LONGEST_NOTE]
            self._save()
            return report

    def forget_everything_about(self, who: str) -> int:
        """Reports by or about somebody whose account has gone.

        Both directions. A report about an account that no longer exists is a
        row an operator cannot act on, and a report *by* one is somebody's
        name kept after they asked for it to be removed.
        """
        with self.lock:
            gone = [r.id for r in self.reports.values() if who in (r.by, r.about_who)]
            for report_id in gone:
                self.reports.pop(report_id, None)
            if gone:
                self._save()
        return len(gone)

    # -- reading ---------------------------------------------------------

    def get(self, report_id: str) -> Report | None:
        with self.lock:
            return self.reports.get(report_id)

    def open_ones(self) -> list[Report]:
        """Undecided, the ones that might mean somebody is in danger first."""
        with self.lock:
            waiting = [r for r in self._newest_first() if r.state == "open"]
        return sorted(waiting, key=lambda r: (not r.urgent, -r.at))

    def by(self, who: str) -> list[Report]:
        """What somebody has reported, so they can see whether anything came
        of it. Anything else would make the button feel like a shrug."""
        with self.lock:
            return [r for r in self._newest_first() if r.by == who]

    def about(self, target: str) -> list[Report]:
        with self.lock:
            return [r for r in self._newest_first() if r.about == target]

    def count_about(self, target: str) -> int:
        return len(self.about(target))

    @property
    def waiting(self) -> int:
        return len(self.open_ones())
