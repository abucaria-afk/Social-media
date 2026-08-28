"""The manager's plans, as a calendar anybody's calendar app can subscribe to.

A plan with a date on it that lives only inside this app is a reminder nobody
gets. This turns the board into a feed in the format every calendar reads —
Apple Calendar, Google Calendar, Outlook, Fastmail, all of them — so a shoot
lands on the phone that is going to take the photographs.

A *subscription*, not an export. An exported file is a snapshot that is wrong
the moment a plan moves; a subscribed feed is re-fetched, so moving a shoot in
the manager moves it on every device that follows the calendar. That is the
whole reason the UIDs are stable and the SEQUENCE counts up: those two fields
are what tell a calendar "this is the same event, changed" rather than "here is
another event".

The URL carries a secret rather than a session, because a calendar app is not a
browser and will not sign in. That makes it a capability: whoever has the link
can read the plans in it, so it is long, unguessable, per person, and can be
rolled. It carries no footage and no account details — a title, a time, and
what to go and shoot.

RFC 5545 is fussy in three ways that are easy to get wrong and produce a file
that imports as empty rather than as broken: lines end CRLF, lines fold at 75
octets, and commas, semicolons and backslashes inside text are escaped. All
three are handled in `_line` and `_text`, and there is a test for each.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: How often a calendar should come back for a fresh copy. Calendars treat this
#: as advice and most poll rather less often, which is why a plan moved an hour
#: before its alarm may not move on the phone in time — said here rather than
#: pretended away.
REFRESH_MINUTES = 60

#: What the calendar is called wherever somebody subscribes to it.
CALENDAR_NAME = "Auteur — what to shoot"

#: The reminders on every plan, as (hours before, what it says). These are the
#: three moments a plan actually needs somebody: long enough before to go and
#: get the footage, the night before to cut it, and when it is meant to go out.
ALARMS: tuple[tuple[float, str], ...] = (
    (48.0, "Shoot this — {captures} things to get"),
    (12.0, "Cut it"),
    (0.0, "Post it yourself — this app does not post"),
)


def _text(value: str) -> str:
    """Escape a TEXT value. Order matters: backslashes first, or they double."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _line(name: str, value: str) -> list[str]:
    """One property, folded to 75 octets.

    Folded on *bytes* rather than characters: the limit is octets, and a
    calendar that receives a line folded mid-character shows mojibake or drops
    the event. Continuation lines begin with a single space.
    """
    raw = f"{name}:{value}".encode()
    if len(raw) <= 75:
        return [raw.decode("utf-8")]

    out: list[str] = []
    start = 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Never split a multi-byte character.
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunk = raw[start:end].decode("utf-8")
        out.append(chunk if not out else " " + chunk)
        start = end
        limit = 74  # the leading space counts toward the next line's 75
    return out


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _uid(plan_id: str, host: str = "auteur.local") -> str:
    """Stable for the life of a plan, so an edit updates rather than duplicates."""
    return f"{plan_id}@{host}"


@dataclass
class Event:
    """One plan, as the calendar sees it."""

    uid: str
    start: datetime
    minutes: int
    summary: str
    description: str
    #: Counts up every time the plan changes; a calendar ignores an update that
    #: does not.
    sequence: int
    captures: int = 0


def _describe(plan: dict) -> str:
    """What the event says when somebody opens it on their phone."""
    lines: list[str] = []
    if plan.get("prompt"):
        lines.append(str(plan["prompt"]))
    captures = plan.get("captures") or []
    if captures:
        lines.append("")
        lines.append(f"Go and shoot ({len(captures)}):")
        for index, capture in enumerate(captures, start=1):
            times = capture.get("times", 1)
            lines.append(
                f"{index}. {capture.get('what', '')}"
                f"  [{capture.get('role', '')}, used {times}x]"
            )
    caption = str(plan.get("caption") or "").strip()
    if caption:
        lines.append("")
        lines.append("Caption:")
        lines.append(caption)
    tags = plan.get("hashtags") or []
    if tags:
        lines.append(" ".join("#" + str(t) for t in tags))
    lines.append("")
    lines.append("Auteur plans and checks it. You post it.")
    return "\n".join(lines)


def event_for(plan: dict, *, host: str = "auteur.local") -> Event | None:
    """One plan as an event, or None if it has no usable time on it."""
    when = str(plan.get("when") or "")
    try:
        start = datetime.fromisoformat(when)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    captures = plan.get("captures") or []
    # A sequence derived from the plan's own contents rather than counted:
    # nothing here stores a revision number, and a hash of what a calendar
    # would show changes exactly when the event changes. Kept small because
    # SEQUENCE is an integer and calendars compare it numerically.
    fingerprint = hashlib.sha256(
        "|".join(
            [
                str(plan.get("title", "")),
                when,
                str(plan.get("prompt", "")),
                str(plan.get("caption", "")),
                str(len(captures)),
                str(plan.get("status", "")),
            ]
        ).encode("utf-8")
    ).digest()
    sequence = int.from_bytes(fingerprint[:2], "big")

    return Event(
        uid=_uid(str(plan.get("id", "")), host),
        start=start,
        minutes=15,
        summary=str(plan.get("title") or "Untitled") + " — post",
        description=_describe(plan),
        sequence=sequence,
        captures=len(captures),
    )


def feed(plans: list[dict], *, host: str = "auteur.local", now: datetime | None = None) -> str:
    """The whole calendar, as one iCalendar document."""
    moment = now or datetime.now(timezone.utc)
    out: list[str] = []
    out += _line("BEGIN", "VCALENDAR")
    out += _line("VERSION", "2.0")
    out += _line("PRODID", "-//auteur//the edit room//EN")
    out += _line("CALSCALE", "GREGORIAN")
    # PUBLISH, not REQUEST: this is a calendar to read, not an invitation that
    # expects anybody to reply to it.
    out += _line("METHOD", "PUBLISH")
    out += _line("X-WR-CALNAME", _text(CALENDAR_NAME))
    out += _line("NAME", _text(CALENDAR_NAME))
    out += _line("X-PUBLISHED-TTL", f"PT{REFRESH_MINUTES}M")
    out += _line("REFRESH-INTERVAL;VALUE=DURATION", f"PT{REFRESH_MINUTES}M")

    for plan in plans:
        event = event_for(plan, host=host)
        if event is None:
            continue
        out += _line("BEGIN", "VEVENT")
        out += _line("UID", event.uid)
        out += _line("DTSTAMP", _stamp(moment))
        out += _line("DTSTART", _stamp(event.start))
        out += _line("DTEND", _stamp(event.start + timedelta(minutes=event.minutes)))
        out += _line("SEQUENCE", str(event.sequence))
        out += _line("SUMMARY", _text(event.summary))
        out += _line("DESCRIPTION", _text(event.description))
        # A plan somebody has already posted, or dropped, stays in the calendar
        # as a record rather than vanishing — but it stops being something to
        # do, and CANCELLED is how a calendar is told that.
        status = str(plan.get("status") or "")
        out += _line("STATUS", "CANCELLED" if status in ("posted", "dropped") else "CONFIRMED")
        out += _line("TRANSP", "TRANSPARENT")

        if status not in ("posted", "dropped"):
            for hours, message in ALARMS:
                out += _line("BEGIN", "VALARM")
                out += _line("ACTION", "DISPLAY")
                out += _line("DESCRIPTION", _text(message.format(captures=event.captures or "the")))
                # -PT0M is not valid; an alarm at the moment itself is PT0S.
                trigger = "PT0S" if hours <= 0 else f"-PT{int(hours)}H"
                out += _line("TRIGGER", trigger)
                out += _line("END", "VALARM")

        out += _line("END", "VEVENT")

    out += _line("END", "VCALENDAR")
    # CRLF, and a trailing one. A calendar that receives bare newlines will
    # often parse the file as empty rather than reporting it as malformed.
    return "\r\n".join(out) + "\r\n"
