"""A small web front end, built for a phone.

The command line is fine at a desk, but the footage is on the phone. This
serves a mobile-first page that takes clips straight from the camera roll,
runs the same agent, and hands back a film you can save or share. It installs
to the iPhone home screen as a web app, so it opens full-screen with no
browser chrome.

Deliberately stdlib-only: no Flask, no FastAPI, nothing extra to install.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import socket
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote

from ..config import FORMATS, QUALITIES, Settings
from ..ui import Reporter, describe_count, describe_duration, describe_shape
from .. import brand, pricing, projects
from . import auth, billing, profiles, safety
from .social import PAGE

log = logging.getLogger("auteur.web")

STATIC = Path(__file__).resolve().parent / "static"

#: Where performance exports are read from, if there are any. Outside the
#: repository and gitignored — it is your account's data, not the project's.
#: With nothing here the studio fits on simulated rows and says so.
EXPORTS = (
    Path(os.environ.get("AUTEUR_EXPORTS", ""))
    if os.environ.get("AUTEUR_EXPORTS")
    else (Path.cwd() / "auteur-exports")
)
#: Anything bigger than this is refused rather than swallowing the machine.
#: The most a single post may carry.
#:
#: 512 MB, not the 2 GB this was. The number was aspirational: the multipart
#: parser materialises the whole body *and* the parsed parts, so a 2 GB upload
#: peaked at several gigabytes of resident memory and the process was killed
#: rather than answering — a denial of service anybody could trigger by
#: accident with a long 4K clip. Still a minute of 4K, and now bounded by
#: something a small machine has.
MAX_UPLOAD = int(os.environ.get("AUTEUR_MAX_UPLOAD") or 512 * 1024 * 1024)

#: Kept in memory up to here; past it the body spools to a temporary file. So
#: an ordinary post costs nothing extra and a large one costs disk rather than
#: RAM.
SPOOL_TO_DISK = 16 * 1024 * 1024

#: Whether this instance is reached over HTTPS, and whether there is a proxy in
#: front of it whose forwarding headers can be believed. Both off by default,
#: because the ordinary way to run this is on a LAN over plain HTTP — where a
#: cookie marked Secure simply never comes back.
PUBLIC_HTTPS = (os.environ.get("AUTEUR_PUBLIC_HTTPS") or "").lower() in ("1", "true", "yes")
TRUST_PROXY = (os.environ.get("AUTEUR_TRUST_PROXY") or "").lower() in ("1", "true", "yes")

#: Sent on every response.
#:
#: `Referrer-Policy` is the one that is load-bearing rather than tidy: the
#: calendar subscription URL carries its credential in the path, so any
#: outbound navigation from a page would put somebody's calendar secret in
#: another site's logs. `no-referrer` is the only value that closes it.
#:
#: The rest are the ordinary set. `nosniff` matters here because the app serves
#: video somebody uploaded and text somebody typed; `frame-ancestors 'none'`
#: because nothing about this app should ever be inside somebody else's frame.
SAFETY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), interest-cohort=()",
    # Everything the pages need is served from this origin, and `blob:` is how
    # a finished film reaches the video element. No remote script, style, font
    # or frame is ever wanted, so none is allowed.
    # No Strict-Transport-Security here: it is added below only when this
    # instance is actually served over HTTPS. Sending it from a plain-HTTP LAN
    # server would tell every browser on the network to refuse to reach it.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
}
#: How much of a file to put on the wire at a time.
CHUNK = 512 * 1024

#: The most a Stripe webhook body may be. Stripe's own limit on the events it
#: sends is well under this; the number is here so that a body large enough to
#: be a denial-of-service is refused before it is hashed rather than after.
WEBHOOK_LIMIT = 64 * 1024
#: Types worth gzipping. PNG and MP4 are already compressed; running them
#: through gzip spends CPU to make them very slightly bigger.
COMPRESSIBLE = ("text/", "javascript", "json", "manifest", "xml", "svg")


def _compressible(content_type: str) -> bool:
    return any(token in content_type for token in COMPRESSIBLE)


#: The session cookie's name, and how long the browser should keep it.
COOKIE = "auteur_session"
SESSION_LIFETIME = 30 * 24 * 3600

#: Reachable without signing in. Everything else — including finished films and
#: production notes, which are the user's own footage — needs a session.
PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/login.html",
        "/reset",
        "/manifest.webmanifest",
        "/sw.js",
        # The browser asks for this before anybody has signed in — it is on the
        # login page too. Behind the gate it 303s to /login, which is a
        # redirect the browser cannot use as an icon.
        "/favicon.ico",
        "/api/session",
        "/api/login",
        "/api/signup",
        "/api/can-signup",
        "/api/forgot",
        "/api/reset",
        # The second step of signing in happens while signed out, by
        # definition — a ticket is not a session and reaches nothing else.
        "/api/login/step2",
        # A fault on the sign-in page is exactly the one nobody could report
        # if reporting needed an account.
        "/api/trouble",
        "/api/sign-in-with",
        # Stripe has no session and never will. This path is reachable
        # without one and refuses everything whose signature does not verify,
        # which is a stronger gate than a cookie, not a weaker one.
        "/api/stripe/webhook",
        # The App Store requires a privacy policy at a URL anybody can open,
        # and "anybody" includes a reviewer who has not been given an account.
        # The terms go with it: the sign-up screen links to them, which happens
        # before anybody has an account by definition.
        "/privacy",
        "/privacy.html",
        "/terms",
        "/terms.html",
        # Deleting an account from outside the app. Apple asks only that it be
        # possible from inside (guideline 5.1.1(v)) and the profile screen does
        # that; Google Play's data-deletion policy asks for a page anybody can
        # open without the app, which a screen behind the sign-in gate cannot
        # be. It still needs the password — it signs in and calls the same
        # endpoint the profile's button calls — so being public here is a page
        # anybody can read, not an account anybody can delete.
        "/delete-account",
        "/delete-account.html",
        # A crawler asks for this before anything else and is entitled to an
        # answer rather than a redirect to a sign-in page.
        "/robots.txt",
    }
)


class Throttle:
    """How often one caller may ask for the same thing, in memory.

    The endpoints reachable without a session are the ones nobody has to get
    past anything to reach, and three of them do real work on being asked:
    sign-up writes an account, forgotten-password sends mail to an address the
    caller names, and the trouble report writes to disk. None of them was
    counted, so each was an unbounded loop somebody else could run — a mailbox
    filled by asking for the same reset a thousand times, a log grown until
    the disk is full.

    Deliberately not the sign-in endpoint. That has an account lockout, which
    is the better tool there: it counts failures against the account being
    attacked rather than against wherever the attempt came from, so it cannot
    be escaped by moving and cannot lock a household out of its own app for
    sharing an address. This counts requests rather than failures, which is
    the right shape for work that succeeds and still costs something.

    In memory and per-process, like the sign-in attempts next door. An
    instance is one machine and one process; a restart forgetting who has
    asked recently is a few free requests, not a hole. The caller is the
    socket's own address — behind a reverse proxy that is the proxy, so every
    caller shares one bucket and the limit becomes a limit on the instance.
    That is a weaker guarantee than it looks and it is written down here
    rather than discovered.
    """

    def __init__(self, allowed: int, per_seconds: float) -> None:
        self.allowed = allowed
        self.per_seconds = per_seconds
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def ask(self, key: str) -> float:
        """Nought if this one may proceed, else the seconds until it may."""
        now = time.time()
        with self._lock:
            recent = [at for at in self._seen.get(key, ()) if now - at < self.per_seconds]
            if len(recent) >= self.allowed:
                self._seen[key] = recent
                return max(0.0, self.per_seconds - (now - recent[0]))
            recent.append(now)
            self._seen[key] = recent
            # Swept here rather than on a timer: the only thing that grows
            # this dict is being asked, so the only time it needs pruning is
            # while being asked.
            if len(self._seen) > 4096:
                self._seen = {
                    k: v for k, v in self._seen.items() if v and now - v[-1] < self.per_seconds
                }
            return 0.0

    def forget(self) -> None:
        """Drop every bucket. For a test that needs to start from nothing."""
        with self._lock:
            self._seen.clear()


#: What each unauthenticated endpoint costs. Generous enough that nobody using
#: the app ever meets one — five password resets in a quarter of an hour is
#: already somebody who has forgotten twice and mistyped their address — and
#: small enough that none of them is worth automating.
THROTTLES = {
    "/api/signup": Throttle(10, 3600),
    "/api/forgot": Throttle(5, 900),
    "/api/trouble": Throttle(20, 900),
}


#: Prefixes reachable before signing in. `/auth/` is the round trip to an
#: identity provider and back, which by definition happens while signed out.
PUBLIC_AUTH_PREFIX = "/auth/"
#: The calendar feed carries its own credential in the path, because a
#: calendar app has no cookie to send. See `_calendar_feed`.
PUBLIC_PREFIXES = ("/static/", PUBLIC_AUTH_PREFIX, "/calendar/")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """One film being made, and everything the page needs to show about it."""

    id: str
    prompt: str
    folder: Path
    #: The project this was made for, or "". Carried on the job rather than
    #: passed to `_run`, because it is not something the render uses — it is
    #: only where the finished film gets filed.
    project: str = ""
    #: Who asked for it. A job id is not a secret — it sits in the address bar
    #: and in history — so being signed in is not by itself permission to read
    #: somebody else's footage.
    owner: str = ""
    status: str = "queued"  # queued | running | done | error
    stage: str = "Getting ready"
    lines: list[dict[str, str]] = field(default_factory=list)
    percent: float = 0.0
    detail: str = ""
    error: str = ""
    video: Path | None = None
    facts: list[str] = field(default_factory=list)
    #: What it understood from the words, said back. The page has had a
    #: panel for this the whole time and the server never filled it, so a
    #: prompt whose effect you cannot see was indistinguishable from one
    #: that was ignored — which is exactly what people reported.
    heard: str = ""
    notes: Path | None = None
    created: float = field(default_factory=time.time)
    thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "percent": round(self.percent, 1),
            # How long this has been going. `created` has been on this record
            # since the first version and was never sent, so the page could
            # show a percentage and a spinner and no sense of how long any of
            # it takes — and a real film is about two minutes. A bar with no
            # clock beside it is indistinguishable from a bar that has stopped,
            # which is the wrong thing to be unsure about on your first film.
            "elapsed": round(max(0.0, time.time() - self.created), 1),
            "detail": self.detail,
            "lines": self.lines[-40:],
            "error": self.error,
            "facts": self.facts,
            "heard": self.heard,
            "video": f"/api/jobs/{self.id}/video" if self.video else None,
            "notes": f"/api/jobs/{self.id}/notes" if self.notes else None,
        }


class WebReporter(Reporter):
    """Feeds the agent's own progress reporting into a job the page can poll."""

    def __init__(self, job: Job, lock: threading.Lock):
        super().__init__(enabled=False)
        self.job = job
        self.lock = lock

    def banner(self, prompt: str) -> None:  # the page already shows the prompt
        pass

    def step(self, title: str) -> None:
        with self.lock:
            self.job.stage = title
            self.job.detail = ""
            self.job.lines.append({"kind": "step", "text": title})

    def detail(self, text: str) -> None:
        with self.lock:
            self.job.detail = text
            self.job.lines.append({"kind": "detail", "text": text})

    def found(self, label: str, text: str) -> None:
        with self.lock:
            self.job.lines.append({"kind": label.strip() or "note", "text": text})

    def warn(self, text: str) -> None:
        with self.lock:
            self.job.lines.append({"kind": "warn", "text": text})

    def progress(self, done: int, total: int, label: str = "") -> None:
        with self.lock:
            self.job.percent = 100.0 * done / total if total else 0.0
            self.job.detail = label

    def progress_done(self, label: str = "done") -> None:
        with self.lock:
            self.job.percent = 100.0

    def result(self, headline: str = "", **_: Any) -> None:  # the page renders its own ending
        pass

    def failure(self, headline: str, hint: str = "") -> None:
        with self.lock:
            self.job.lines.append({"kind": "warn", "text": headline})


class Studio:
    """Holds the jobs and runs them one at a time."""

    def __init__(self, workspace: Path, *, quality: str = "draft", stickers: Path | None = None):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Somewhere to drop your own transparent PNGs. Made under the workspace
        # by default so there is always a folder to point a phone's share sheet
        # at, rather than a setting nobody finds.
        self.sticker_dir = Path(stickers) if stickers else self.workspace / "stickers"
        self.sticker_dir.mkdir(parents=True, exist_ok=True)
        self.quality = quality
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # Rendering is CPU-bound; running two at once just makes both slower.
        self.queue_lock = threading.Lock()
        #: Where finished films are published so they outlive their job.
        #: Set by serve(); left None by the tests that only want a render.
        self.films: Any = None
        #: owner -> the last edit they planned. The studio page works on this:
        #: the agents argue about a cut that already exists, not about a prompt.
        self.recent_edls: dict[str, Any] = {}

    def create(
        self,
        prompt: str,
        shape: str,
        seconds: float | None,
        owner: str = "",
        template: str = "",
        era: str = "",
        project: str = "",
    ) -> Job:
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        folder = self.workspace / job_id
        (folder / "clips").mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, prompt=prompt, folder=folder, owner=owner, project=project)
        job.thread = threading.Thread(
            target=self._run, args=(job, shape, seconds, template, era), daemon=True
        )
        with self.lock:
            self.jobs[job_id] = job
        return job

    def start(self, job: Job) -> None:
        """Split from create() so the clips are on disk before the agent looks."""
        if job.thread is not None:
            job.thread.start()

    def get(self, job_id: str, owner: str | None = None) -> Job | None:
        """The job, if it exists and belongs to `owner`.

        Passing owner=None skips the check and is for internal callers only.
        """
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            return None
        if owner is not None and job.owner and job.owner != owner:
            return None
        return job

    def last_edl(self, owner: str | None):
        """The most recent cut this person made, for the agents to work on.

        Returns a *copy*: the studio hands it to agents that mutate it, and the
        finished job's own timeline must not change under the report that
        describes it.
        """
        import copy

        with self.lock:
            edl = self.recent_edls.get(owner or "")
        return copy.deepcopy(edl) if edl is not None else None

    def _run(
        self,
        job: Job,
        shape: str,
        seconds: float | None,
        template: str = "",
        era: str = "",
    ) -> None:
        from ..agent import direct

        with self.queue_lock:
            try:
                with self.lock:
                    job.status = "running"

                fmt = FORMATS.get(shape, FORMATS["reel"])
                settings = Settings(
                    quality=QUALITIES.get(self.quality, QUALITIES["draft"]),
                    primary_format=fmt,
                    target_duration=seconds or 20.0,
                    revision_rounds=1,
                )
                reporter = WebReporter(job, self.lock)

                # A chosen reel's timeline, imposed after the edit is planned
                # and before a frame is rendered. `on_plan` is documented as
                # the last honest place to intervene and this is exactly that
                # kind of intervention: the director still decides which
                # picture goes where, the reference decides when to cut.
                beats: list = []
                if template:
                    for entry in _templates():
                        if entry.get("id") == template:
                            beats = entry.get("beats") or []
                            break

                wanted_look = ERA_LOOKS.get(era, "")

                def on_plan(edl, _beats=beats, _seconds=seconds, _look=wanted_look):
                    if _beats:
                        _fit_to_template(edl, _beats, _seconds)
                    if _look:
                        # The whole film, not a shot here and there: a decade
                        # is the film's stock, and half a reel shot on Kodak
                        # is a continuity error rather than a style.
                        for shot in edl.shots:
                            shot.look.preset = _look
                            shot.look.strength = 1.0

                production = direct(
                    [job.folder / "clips"],
                    job.prompt,
                    settings=settings,
                    workspace=job.folder / "work",
                    formats=(fmt,),
                    duration=seconds,
                    reporter=reporter,
                    on_plan=on_plan,
                )

                critique = production.final_critique
                facts = [
                    f"{describe_duration(production.edl.duration)}",
                    describe_count(len(production.edl.shots), "shot"),
                    describe_shape(fmt.width, fmt.height),
                ]
                if critique is not None:
                    facts.append(f"it rates itself {critique.score:.0%}")

                # Said back in the person's own terms. Built from the edit that
                # was actually made rather than from the prompt, so it reports
                # what happened rather than what was asked for — the two differ
                # whenever a word was not understood, and that difference is
                # the only thing worth showing.
                heard = _said_back(
                    production.edl,
                    prompt=job.prompt,
                    template=template,
                    shape=fmt,
                )

                with self.lock:
                    self.recent_edls[job.owner] = production.edl
                    job.video = production.primary
                    job.notes = production.workspace.root / "production-notes.md"
                    job.facts = facts
                    job.heard = heard
                    job.status = "done"
                    job.stage = "Your film is ready"
                    job.percent = 100.0
                    job.detail = ""

                # Into the feed. Outside the lock because publishing writes a
                # file, and holding the studio's lock across a disk write makes
                # every other phone polling this server wait on it.
                if self.films is not None and job.owner:
                    try:
                        self.films.add(
                            owner=job.owner,
                            prompt=job.prompt,
                            video=str(production.primary),
                            facts=facts,
                            heard=heard,
                            template=template,
                            era=era,
                            project=job.project,
                        )
                    except Exception as exc:  # noqa: BLE001 - the film is made
                        log.warning("could not publish %s to the feed: %s", job.id, exc)
            except FileNotFoundError as exc:
                self._fail(job, "I could not find any footage in what you sent.", exc)
            except Exception as exc:  # noqa: BLE001 - the page must always hear back
                log.exception("job %s failed", job.id)
                self._fail(job, "Something went wrong making the film.", exc)

    def _fail(self, job: Job, headline: str, exc: BaseException) -> None:
        with self.lock:
            job.status = "error"
            job.stage = "It did not work"
            job.error = f"{headline} {_plain_cause(exc)}".strip()
            log.error("job %s: %s", job.id, exc)

    def sweep(self, max_age_hours: float = 6.0) -> None:
        """Delete finished jobs older than a few hours, so a phone-sized box copes."""
        cutoff = time.time() - max_age_hours * 3600
        with self.lock:
            stale = [
                job
                for job in self.jobs.values()
                if job.created < cutoff and job.status in ("done", "error")
            ]
            for job in stale:
                self.jobs.pop(job.id, None)
        for job in stale:
            shutil.rmtree(job.folder, ignore_errors=True)

        # Whatever was just deleted, a film may have been pointing at. Checked
        # here rather than only at start-up: a film outlives its job on
        # purpose, but it cannot outlive its file, and an instance left running
        # for a day filled its feed with rows that play nothing.
        if stale and self.films is not None:
            gone = self.films.drop_missing()
            if gone:
                log.info("dropped %d film(s) whose footage was swept", gone)


def _plain_cause(exc: BaseException) -> str:
    """One short, readable line about what went wrong.

    A failed render carries the whole filter graph in its message — thousands of
    characters of `[12:v]settb=AVTB...`. Putting that on a phone screen tells
    nobody anything; the full text goes to the log instead.
    """
    text = " ".join(str(exc).split())
    if not text:
        return ""
    if "matches no streams" in text or "filtergraph" in text:
        return "One of the clips could not be used."
    if "Invalid data" in text or "moov atom" in text:
        return "One of the files was not readable video."
    if "No space left" in text:
        return "The computer running this ran out of disk space."
    return text if len(text) <= 160 else text[:157] + "..."


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """Pull fields and files out of a browser form post, using only stdlib.

    Kept for callers that already hold the body — the share target reads a
    small form, and the tests build one. Anything that reads from the socket
    should use `_parse_multipart_stream`, which does not need the whole post to
    exist as a single bytes object first.
    """
    return _parse_multipart_stream(io.BytesIO(body), content_type)


def _boundary_of(content_type: str) -> bytes:
    """The delimiter, out of the Content-Type header. Empty if there is none."""
    _, _, rest = content_type.partition(";")
    for parameter in rest.split(";"):
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "boundary":
            return value.strip().strip('"').encode("ascii", "ignore")
    return b""


def _parse_multipart_stream(
    body, content_type: str
) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """The same, from a file object, so the raw post is never copied.

    Split by hand rather than handed to `email.parser`.

    That is not a preference. This used `BytesParser`, and the email package
    parses a *text* format: it normalises line endings as it goes, turning
    every CRLF into LF and every lone CR into LF. On prose that is invisible.
    On an mp4 it removes one byte for every CRLF that happens to occur in the
    video stream and rewrites every lone CR — so a 700KB clip arrived 16 bytes
    short and a 10MB one 168 bytes short, ffprobe refused them, and the app
    told the person their file was damaged. It was: the app had damaged it.
    Every video and every photo ever uploaded through the web app.

    Nothing caught it because no test compared what came out against what went
    in — the same shape of hole as every other one this file guards against.

    The rules being implemented, from RFC 2046 §5.1.1 and RFC 7578: parts are
    separated by CRLF + "--" + boundary; the first delimiter may open the body
    with no CRLF before it; a delimiter followed by "--" ends the body; and
    within a part, headers end at the first CRLF CRLF and the body runs to the
    CRLF that begins the next delimiter — that CRLF belongs to the delimiter,
    not to the file.
    """
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []

    boundary = _boundary_of(content_type)
    if not boundary:
        return fields, files

    raw = body.read()
    delimiter = b"--" + boundary

    # The opening delimiter, which may or may not be preceded by a CRLF.
    if raw.startswith(delimiter):
        rest = raw[len(delimiter) :]
    else:
        opening = raw.find(b"\r\n" + delimiter)
        if opening < 0:
            return fields, files
        rest = raw[opening + 2 + len(delimiter) :]

    while True:
        if rest.startswith(b"--"):  # the closing delimiter
            break
        if not rest.startswith(b"\r\n"):
            break
        rest = rest[2:]

        blank = rest.find(b"\r\n\r\n")
        if blank < 0:
            break
        raw_headers = rest[:blank]
        rest = rest[blank + 4 :]

        ending = rest.find(b"\r\n" + delimiter)
        if ending < 0:
            break
        payload = rest[:ending]
        rest = rest[ending + 2 + len(delimiter) :]

        name, filename = _disposition_of(raw_headers)
        if filename:
            files.append((filename, payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace").strip()

    return fields, files


def _disposition_of(raw_headers: bytes) -> tuple[str, str]:
    """`name` and `filename` out of one part's headers.

    The header block is ASCII by the time it gets here — a browser percent- or
    RFC 2231-encodes anything else — so decoding it is safe in a way decoding
    the body never is.
    """
    text = raw_headers.decode("utf-8", "replace")
    disposition = ""
    for line in text.split("\r\n"):
        field, _, value = line.partition(":")
        if field.strip().lower() == "content-disposition":
            disposition = value
            break
    if not disposition:
        return "", ""

    found = {"name": "", "filename": ""}
    for parameter in disposition.split(";")[1:]:
        key, _, value = parameter.partition("=")
        key = key.strip().lower()
        if key in found and not found[key]:
            found[key] = value.strip().strip('"')
    # A browser sends the basename, but a crafted post need not: anything with
    # a separator in it is somebody else's directory.
    return found["name"], PurePosixPath(found["filename"]).name if found["filename"] else ""


class _Prefixed(io.RawIOBase):
    """A file that reads `header` first and then everything in `rest`.

    `BytesParser.parse` wants one stream carrying the Content-Type header and
    the body. Concatenating them would rebuild the copy this exists to avoid,
    so they are read in order instead.

    `rest` is a `SpooledTemporaryFile`, which only grew a `readinto` in Python
    3.11 — it did not fully implement `IOBase` before that. Calling it directly
    raised `AttributeError` on 3.10, and every caller turns any exception into
    "I could not read that upload", so on 3.10 *every* multipart post failed:
    no film could be made, no reel added, no profile picture set. The whole app
    was unusable on a version its own CI claims to support, and the only thing
    that ever said so was one test asserting the wrong message. So the read is
    done through whichever of the two the object actually has.
    """

    def __init__(self, header: bytes, rest) -> None:
        self._header = io.BytesIO(header)
        self._rest = rest

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        got = self._header.readinto(target)
        if got:
            return got
        if hasattr(self._rest, "readinto"):
            return self._rest.readinto(target)
        # Python 3.10's SpooledTemporaryFile. `read` into a memoryview of the
        # target keeps this a copy of one block rather than of the whole body.
        block = self._rest.read(len(target))
        if not block:
            return 0
        target[: len(block)] = block
        return len(block)


#: Platform connections part way through the platform's consent screen, by
#: `state`. In memory rather than on disk for the same reason `oidc.Attempts`
#: is: an interrupted connection should not survive a restart.
_CONNECTING: dict[str, tuple[str, str, float]] = {}
_CONNECTING_LOCK = threading.Lock()
#: How long somebody has to finish a consent screen. Ten minutes is generous
#: for a form and short enough that an abandoned state is not lying around.
CONNECTING_TTL = 600.0


def _sweep_connecting() -> None:
    """Drop states nobody came back with. Callers hold the lock."""
    cutoff = time.time() - CONNECTING_TTL
    for state in [key for key, row in _CONNECTING.items() if row[2] < cutoff]:
        _CONNECTING.pop(state, None)


def _shape_of(fmt) -> str:
    """The edit room's name for a delivery format.

    The room offers three — tall, square, wide — and the platform table has
    five, so `portrait` (1080x1350) has no chip of its own and is carried as
    the tall one it is nearest to. Returning "" for anything unrecognised
    leaves the room on its own default rather than guessing.
    """
    width, height = getattr(fmt, "width", 0), getattr(fmt, "height", 0)
    if not width or not height:
        return ""
    if height > width:
        return "reel"
    if width > height:
        return "wide"
    return "square"


class Handler(BaseHTTPRequestHandler):
    server_version = "auteur"
    #: HTTP/1.1 so connections are kept alive. The page polls every second or
    #: so; a fresh TCP handshake per poll is pure latency.
    protocol_version = "HTTP/1.1"
    studio: Studio
    accounts: Any = None  # an auth.Accounts once serve() has run
    films: Any = None  # a social.Films once serve() has run
    messages: Any = None  # a social.Messages once serve() has run
    profiles: Any = None  # a profiles.Profiles once serve() has run
    reports: Any = None  # a safety.Reports once serve() has run
    projects: Any = None  # a projects.Projects once serve() has run
    watching: Any = None  # a watching.Watching once serve() has run
    connections: Any = None  # a social.accounts.Connections once serve() has run
    board: Any = None  # a manager.Board once serve() has run
    sign_in_with: Any = None  # provider settings once serve() has run
    attempts: Any = None  # an oidc.Attempts once serve() has run

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        # The request line carries the path, and one path has a credential in
        # it: a calendar app cannot send a cookie, so the subscription URL *is*
        # the secret. Logging it would put it in whatever collects these logs,
        # which is exactly the place a rollable secret should never reach.
        log.debug("%s - %s", self.address_string(), _redact(fmt % args))

    # -- who is asking ---------------------------------------------------

    @property
    def session_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value or None
        return None

    def current_user(self) -> str | None:
        """Who this request is, or None.

        None when no account store is configured, which refuses everything.
        The other direction — treating "auth is not set up" as "everybody is
        allowed" — turns a missing line in start-up into a silently open server
        handing out the user's footage, with nothing in the log to say so.
        """
        if self.accounts is None:
            return None
        # Cheap: one stat() per request, and it is what lets `auteur account`
        # take effect against a server that is already running.
        self.accounts.refresh()
        return self.accounts.session_user(self.session_token)

    def _set_session_cookie(self, token: str) -> str:
        # HttpOnly so a script cannot read it; SameSite=Strict so another site
        # cannot make the browser spend it. Secure only over HTTPS — setting it
        # on a plain LAN address would make the cookie silently not stick.
        bits = [
            f"{COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={SESSION_LIFETIME}",
        ]
        if self._is_https:
            bits.append("Secure")
        return "; ".join(bits)

    @property
    def _is_https(self) -> bool:
        """Whether this connection is really encrypted.

        `X-Forwarded-Proto` is a header, which means anybody can send it, and
        trusting it unconditionally is how a cookie ends up marked Secure on a
        plain connection — or, worse in the other direction, how a deployment
        that *is* behind TLS never gets the flag because the proxy spells it
        differently. So the header is believed only when the operator has said
        there is a proxy in front, and `--https` forces it on for anyone
        terminating TLS some other way.

        The default is the honest one for how this is usually run: on a LAN
        over plain HTTP, where marking the cookie Secure would stop it sticking
        at all.
        """
        if PUBLIC_HTTPS:
            return True
        if TRUST_PROXY:
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return False

    def _clear_session_cookie(self) -> str:
        return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    # -- helpers ---------------------------------------------------------

    def _send(
        self, code: int, body: bytes, content_type: str, *, extra: dict | None = None
    ) -> None:
        headers = dict(extra or {})
        # Text compresses by ~4x, and the shell is fetched over wifi from a
        # phone. Skip it for anything already compressed (png, mp4).
        if (
            len(body) > 512
            and "gzip" in self.headers.get("Accept-Encoding", "")
            and _compressible(content_type)
        ):
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SAFETY_HEADERS.items():
            if key not in headers:
                self.send_header(key, value)
        if self._is_https and "Strict-Transport-Security" not in headers:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(payload).encode(),
            "application/json; charset=utf-8",
            extra={"Cache-Control": "no-store"},
        )

    def _static(self, path: Path, content_type: str | None = None) -> None:
        """Serve a shell file, revalidated by ETag.

        The page's own assets never change while the server is up, so answering
        the reload with a 304 costs one small round trip instead of resending
        the stylesheet and the script every time.
        """
        # Callers pass STATIC / Path(request_path).name, which cannot contain a
        # separator — but `.name` of "/static/.." is ".." and that resolves to
        # the parent. Nothing readable lives there and a directory fails the
        # is_file() check below, so this was never a way to read anything; the
        # invariant is worth holding anyway rather than resting on two accidents.
        try:
            inside = path.resolve().is_relative_to(STATIC.resolve())
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            inside = False
        if not inside or not path.is_file():
            self._json({"error": "not found"}, 404)
            return

        stat = path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        guessed = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(
            200,
            path.read_bytes(),
            guessed,
            extra={
                "ETag": etag,
                # Revalidate rather than trust: a restarted server may have a new
                # palette or a new script, and a stale shell is a confusing bug.
                "Cache-Control": "no-cache",
            },
        )

    def _file(self, path: Path, content_type: str | None = None) -> None:
        """Serve a file, honouring Range.

        Range is not an optimisation here. iOS Safari opens a video with
        `Range: bytes=0-1` and refuses to play anything that answers with a
        plain 200 and the whole file, so without this the finished film shows
        as a black box on the one device this page was written for.
        """
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return

        size = path.stat().st_size
        guessed = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200

        header = self.headers.get("Range", "")
        if header.startswith("bytes=") and size:
            first, _, last = header[6:].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:  # a suffix range: the last N bytes
                    start = max(0, size - int(last))
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", guessed)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(CHUNK, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the phone locked, or scrubbed elsewhere in the file
                remaining -= len(block)

    # -- routes ----------------------------------------------------------

    # -- the studio ------------------------------------------------------

    #: The fitted model, kept on the class so it is computed once per server
    #: rather than once per request. Fitting is cheap, but the page polls.
    _model = None
    #: The last plan the studio produced, with its outstanding proposals. One
    #: per server, deliberately: this is a single-operator edit room, and a
    #: second concurrent planner would be two people arguing through one UI.
    _pending: dict = {}

    @staticmethod
    def _platforms() -> list[dict]:
        from ..workflows.platforms import PLATFORMS

        return [
            {
                "name": spec.name,
                "service": spec.service,
                "surface": spec.surface,
                "width": spec.format.width,
                "height": spec.format.height,
                "min_seconds": spec.min_seconds,
                "max_seconds": spec.max_seconds,
                "ideal_seconds": spec.ideal_seconds,
                "note": spec.note,
            }
            for spec in PLATFORMS.values()
        ]

    @classmethod
    def _fitted(cls):
        if cls._model is None:
            from ..insight import corpus, fit

            exports = sorted(Path(EXPORTS).glob("*.csv")) if EXPORTS.exists() else []
            cls._model = fit(corpus(exports, simulate_rows=2000 if not exports else 0))
        return cls._model

    def _insight(self) -> dict:
        model = self._fitted()
        return {
            "provenance": model.provenance,
            "caveat": model.caveat,
            "elite_three_second": round(model.elite_three_second, 4),
            "elite_share": round(model.elite_share, 4),
            "elite_loop": round(model.elite_loop, 4),
            "best_hook_duration": round(model.best_hook_duration, 2),
            "drivers": [[column, label, round(r, 3)] for column, label, r in model.drivers],
            "style_ranking": [[name, round(v, 4)] for name, v in model.style_ranking],
            "conflicts": list(model.conflicts),
            "generated_forms": list(model.generated_forms),
            "measured_rows": model.measured_rows,
            "simulated_rows": model.simulated_rows,
        }

    def _crew_memory(self) -> dict:
        """What the crew has found worth doing, across every film so far."""
        from ..agents.ledger import Ledger

        ledger = Ledger()

        def row(track) -> dict:
            return {
                "agent": track.agent,
                "title": track.title,
                "mean_gain": round(track.mean_gain, 4),
                "tries": track.tries,
                "take_rate": round(track.take_rate, 3),
            }

        return {
            "kinds": len(ledger.tracks),
            "scored": sum(t.tries for t in ledger.tracks.values()),
            "proven": [row(t) for t in ledger.proven()],
            "wasted": [row(t) for t in ledger.wasted()],
            # Said here as well as in the CLI, because it is the one thing about
            # these numbers that is easy to get wrong.
            "note": (
                "the scoring model's own verdicts, not view counts — "
                "load real exports to check them against reality"
            ),
        }

    def _scholar_state(self) -> dict:
        """What the study agent knows, and whether it can study at all."""
        from ..scholar import Scholar
        from ..scholar.youtube import reachable

        can_study, how = reachable()
        try:
            scholar = Scholar()
            status = scholar.status()
        except Exception as exc:  # noqa: BLE001 - the app serves without a Scholar
            return {"available": False, "reason": str(exc), "can_study": can_study}

        try:
            wants, why = scholar.should_study()
        except Exception:  # noqa: BLE001 - no network is not an error here
            wants, why = bool(scholar.knowledge.gaps()), "knowledge gaps"

        # What it has learned about this app. Nothing in the crew can act on a
        # rule about tap targets, so it is surfaced to the person instead —
        # here, on the page it is about.
        try:
            brief = scholar.teach_product()
            product = {
                "summary": brief.summary,
                "learnings": [
                    {
                        "technique": item.technique,
                        "insight": item.insight,
                        "confidence": item.confidence.value,
                    }
                    for item in brief.learnings[:6]
                ],
            }
        except Exception as exc:  # noqa: BLE001 - a missing brief is not an outage
            log.debug("no product brief: %s", exc)
            product = {"summary": "", "learnings": []}

        # What the studied films agree on. Separate from the product notes
        # because it is about the *films*, not the app, and because it is the
        # only thing in the store an editing agent can be held to: a median, a
        # spread, and how many independent films it rests on.
        try:
            agreed = scholar.teach_all().consensus[:5]
        except Exception as exc:  # noqa: BLE001 - a brief is not an outage
            log.debug("no consensus: %s", exc)
            agreed = []

        return {
            "available": True,
            "can_study": can_study,
            "how": how,
            "learnings": status["total_learnings"],
            "sessions": status["sessions_completed"],
            "disciplines_studied": status["disciplines_studied"],
            "gaps": status["knowledge_gaps"][:6],
            "subscriptions": status["subscriptions"],
            "wants_to_study": wants,
            "why": why,
            "product": product,
            "consensus": agreed,
        }

    def _overlay_rules(self) -> dict:
        """The graphics vocabulary and the numbers the OverlayAgent places by.

        Read out of the agent and the EDL rather than written out again here.
        A studio panel that restates a constant is a second copy of it, and it
        goes stale the first time somebody tunes the original — which is how a
        page ends up confidently describing behaviour the program stopped
        having.
        """
        from ..agents import overlay as agent
        from ..edl import GRAPHIC_KINDS, GRAPHIC_MOVES

        return {
            "kinds": sorted(GRAPHIC_KINDS),
            "moves": sorted(GRAPHIC_MOVES),
            "rules": [
                f"Up to {agent.MAX_LAYERS} on screen at once — "
                "one is a watermark, four is a mood board.",
                f"On every {agent.BEAT_STRIDE}{'nd' if agent.BEAT_STRIDE == 2 else 'th'} "
                "beat off the downbeat, so it reads as rhythm rather than noise.",
                f"No more than {agent.MOST_STICKERS} in a whole film.",
                f"Never within {agent.TOO_CLOSE:.2f} of the frame of each other.",
                f"{len(agent.LANES)} lanes to sit in, none of them the middle "
                "or the caption band.",
            ],
        }

    # -- the feed and the inbox ------------------------------------------

    def _poster_for(self, film) -> Path | None:
        """A still to show before the video loads, made once and kept.

        A feed of eight video elements with no poster is eight black rectangles
        until each one has buffered, which on a phone is most of a second per
        film. One frame pulled at a tenth of the way in — far enough past the
        first cut that it is not the title card, early enough to be the hook.
        """
        source = Path(film.video)
        if not source.is_file():
            return None
        poster = source.with_suffix(".poster.jpg")
        if poster.is_file():
            return poster
        try:
            from ..ffmpeg import probe, run

            seconds = float((probe(source).get("format") or {}).get("duration") or 0.0)
            at = max(0.1, seconds * 0.1)
            run(
                [
                    "-ss",
                    f"{at:.2f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "4",
                    "-y",
                    str(poster),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - a missing poster is not an outage
            log.debug("no poster for %s: %s", film.id, exc)
            return None
        return poster if poster.is_file() else None

    def _feed(self) -> None:
        """Films, newest first. `?scope=` picks whose, `?before=` pages back.

        Three scopes, because "everyone" is the wrong default to be stuck with
        on an instance where a household shares one server: `all` is everybody,
        `following` is the people this person chose, and `mine` is their own.
        `?mine=1` still works — it is what the first version of the feed sent,
        and a phone with the old page cached is a real client.
        """
        who = self.current_user() or ""
        query = parse_qs(self.path.partition("?")[2])
        before = None
        try:
            before = float(query["before"][0]) if query.get("before") else None
        except (TypeError, ValueError):
            before = None

        # Nobody on either side of a block appears in anybody's feed. Applied
        # here rather than in each scope, because a filter that has to be
        # remembered three times is one that will be remembered twice.
        apart = self.profiles.apart(who)
        held = self._held_back(who)

        scope = (query.get("scope") or [""])[0]
        if query.get("mine"):
            scope = "mine"
        if scope == "mine":
            films = self.films.by(who)
        elif scope == "following":
            # Only the people they actually follow. Slipping their own films in
            # was tempting — it stops the tab being empty on the day somebody
            # signs up — but a tab labelled Following that shows something you
            # do not follow is a label that lies, and the empty state says what
            # to do about it instead.
            wanted = set(self.profiles.following_of(who))
            films = [f for f in self.films.feed(who, limit=999, before=before) if f.owner in wanted]
        else:
            films = self.films.feed(who, limit=999, before=before)

        films = [f for f in films if f.owner not in apart and f.id not in held]

        # Rank it, now that there is something to rank by.
        #
        # Only the "everybody" scope. `mine` and `following` are answers to a
        # question the person asked in their own words — show me my films, show
        # me the people I chose — and reordering those by merit answers a
        # different question than the tab is labelled with. Newest-first stays
        # the rule where the person did the choosing.
        if scope not in ("mine", "following") and self.watching is not None and who:
            films = self.watching.for_you(who, films, made_by={f.id: f.owner for f in films})

        films = films[:PAGE]
        rows = [f.public(who) for f in films]
        self._json(
            {
                "films": rows,
                "me": who,
                "scope": scope or "all",
                # Enough to draw each author's disc without a request per film.
                "people": self.profiles.cards(sorted({f["owner"] for f in rows})),
                "following": len(self.profiles.following_of(who)),
            }
        )

    # ------------------------------------------------------------- profiles

    def _mine(self, who: str) -> dict:
        """Your own profile, with the film count on it.

        Every handler that answers with a profile goes through here, because
        the count is the part that is easy to leave out: `public_of` defaults
        it to zero, so saving a bio or a picture used to hand back a profile
        that said "0 Films" and the header would change to match. The page was
        right, the answer was incomplete, and it looked like the films had been
        deleted.
        """
        return self.profiles.public_of(who, viewer=who, films=len(self.films.by(who, limit=999)))

    def _record_watch(self) -> None:
        """One view, reported by the player when it stops.

        Sent as a beacon, so it has to be cheap and it has to be safe to
        receive twice — a phone locking mid-scroll can fire the same one again.
        Nothing here trusts the numbers: `played` clamps the seconds to what
        the film could physically have played, because a client is a thing on
        somebody else's machine and a ranking that believes it is a ranking
        anybody can buy.
        """
        if self.watching is None:
            self._json({"ok": True})
            return
        payload = self._json_body()
        film = str(payload.get("film") or "").strip()
        if not film:
            self._json({"error": "which film?"}, 400)
            return
        try:
            seconds = float(payload.get("seconds") or 0.0)
            runtime = float(payload.get("runtime") or 0.0)
            looped = int(payload.get("looped") or 0)
        except (TypeError, ValueError):
            self._json({"error": "that is not a number of seconds"}, 400)
            return

        if payload.get("shared"):
            self.watching.shared(film)
        record = self.watching.played(
            self.current_user() or "",
            film,
            seconds=seconds,
            runtime=runtime,
            looped=looped,
        )
        self._json({"ok": True, "plays": record.plays})

    def _my_watching(self) -> None:
        """What this instance has recorded about the person asking.

        Theirs to see, because a history somebody cannot look at is a history
        they cannot judge. The per-film totals for their *own* films come with
        it — that is the useful half for somebody who makes things.
        """
        who = self.current_user() or ""
        if self.watching is None or not who:
            self._json({"watched": [], "films": []})
            return
        mine = {f.id: f for f in self.films.by(who, limit=999)}
        self._json(
            {
                "watched": [
                    {
                        "film": s.film,
                        # The prompt, not the id. A history that reads
                        # "f3a91c2e — 41s" tells somebody nothing about what
                        # they watched, which defeats the point of showing it
                        # to them. A film since deleted has no prompt to give,
                        # and says so rather than showing a bare identifier.
                        "prompt": (seen.prompt if (seen := self.films.get(s.film)) else ""),
                        "plays": s.plays,
                        "seconds": round(s.seconds, 1),
                        "finished": s.finished,
                        "last": s.last,
                    }
                    for s in self.watching.history(who)[:100]
                ],
                "films": [
                    {
                        "film": film_id,
                        "prompt": film.prompt,
                        **self.watching.reception(film_id).to_json(),
                        "merit": round(self.watching.merit(film_id), 3),
                    }
                    for film_id, film in mine.items()
                ],
            }
        )

    def _start_connecting(self, platform_key: str) -> None:
        """Send somebody to the platform's own consent screen.

        Only ever reached from a Connect control, which only appears when the
        publisher has supplied credentials — but it is checked again here,
        because a route that trusts the page to have checked is a route anybody
        can call directly.

        The redirect URI has to match the one registered with the platform
        exactly, down to the trailing slash, and a mismatch is refused by them
        with a message that names the wrong thing. So it is built from the
        app's own public URL rather than from the request's Host header, which
        a proxy can rewrite.
        """
        import secrets
        import urllib.parse

        from ..social import accounts as social_accounts

        who = self.current_user() or ""
        if not who:
            self._redirect("/login")
            return
        platform = social_accounts.PLATFORMS.get(platform_key)
        if platform is None:
            self._json({"error": "no such platform"}, 404)
            return
        if not social_accounts.configured(platform_key):
            # Not a 500. Nothing is broken: a publisher has not supplied
            # credentials, and the person looking at this can do nothing about
            # it, so the message is for whoever runs the instance.
            self._json(
                {
                    "error": f"{platform.label} is not set up on this instance",
                    "needs": [platform.id_var, platform.secret_var],
                    "why": platform.gate,
                },
                503,
            )
            return

        # `state` is the CSRF defence the specification requires: it comes
        # back unchanged, and a callback carrying one this instance never
        # issued is somebody else's authorisation being replayed at this user.
        # In memory, like the sign-in attempts next door, because a state that
        # outlives the process that issued it is a replay waiting to happen.
        state = secrets.token_urlsafe(24)
        with _CONNECTING_LOCK:
            _sweep_connecting()
            _CONNECTING[state] = (who, platform_key, time.time())
        query = urllib.parse.urlencode(
            {
                "client_id": os.environ.get(platform.id_var, ""),
                "scope": platform.read_scopes,
                "response_type": "code",
                "redirect_uri": f"{self._site_root()}/connect/{platform_key}/done",
                "state": state,
            }
        )
        self._redirect(f"{platform.authorize}?{query}")

    def _my_connections(self) -> None:
        """Which platforms this person has connected, and why not the rest.

        The second half is the part that matters. A Schedule screen showing an
        empty chart is indistinguishable from an account with no views, and
        this project has already shipped an insight layer fitted to a
        simulation — so when there are no numbers the screen says which
        credentials are missing and what each platform requires, rather than
        drawing something plausible.
        """
        from ..social import accounts as social_accounts

        who = self.current_user() or ""
        if not who:
            self._json({"connected": [], "available": [], "missing": []})
            return

        mine = {row.platform: row for row in self.connections.of(who)} if self.connections else {}
        self._json(
            {
                "connected": [row.to_json() for row in mine.values()],
                "available": [
                    {
                        "key": platform.key,
                        "label": platform.label,
                        "configured": social_accounts.configured(platform.key),
                        "connected": platform.key in mine,
                        # What is asked for, in the platform's own words, so
                        # somebody can see it is reading and not publishing
                        # before they tap Connect.
                        "asks_for": platform.read_scopes,
                        "gate": platform.gate,
                    }
                    for platform in social_accounts.PLATFORMS.values()
                ],
                "missing": social_accounts.what_is_missing(),
                "checked": social_accounts.AS_OF,
            }
        )

    def _disconnect_platform(self) -> None:
        """Forget a platform account and drop its tokens."""
        who = self.current_user() or ""
        if not who or self.connections is None:
            self._json({"error": "not signed in"}, 403)
            return
        body = self._json_body()
        platform = str(body.get("platform", ""))
        from ..social import accounts as social_accounts

        if platform not in social_accounts.PLATFORMS:
            self._json({"error": "no such platform"}, 400)
            return
        self.connections.disconnect(who, platform)
        self._my_connections()

    def _my_profile(self) -> None:
        """Everything the profile page needs about the person looking at it."""
        who = self.current_user() or ""
        self.accounts.refresh()
        account = self.accounts.accounts.get(who)
        payload = self._mine(who)
        payload["email"] = getattr(account, "email", "") if account else ""
        payload["two_step"] = bool(getattr(account, "totp_on", False)) if account else False
        self._json({"profile": payload})

    def _profile(self, who: str) -> None:
        """Somebody's profile, and the films of theirs the feed would show."""
        me = self.current_user() or ""
        who = unquote(who).strip()
        self.accounts.refresh()
        if who not in self.accounts.accounts:
            self._json({"error": "no one here by that name"}, 404)
            return
        films = self.films.by(who, limit=60)
        self._json(
            {
                "profile": self.profiles.public_of(who, viewer=me, films=len(films)),
                "films": [f.public(me) for f in films],
            }
        )

    def _profile_picture(self, who: str) -> None:
        """The picture itself, or a 404 if they have not set one.

        Cached hard and addressed with a `?v=` that moves when the picture
        does, which is the same bargain the film posters make: a disc fetched
        on every screen should come off the cache, and a *replaced* picture
        should still appear immediately.
        """
        found = self.profiles.picture_path(unquote(who).strip())
        if found is None:
            self._json({"error": "not found"}, 404)
            return
        self._file(found, "image/jpeg")

    def _profile_people(self, who: str, which: str) -> None:
        """Who somebody follows, or who follows them."""
        me = self.current_user() or ""
        who = unquote(who).strip()
        names = (
            self.profiles.following_of(who)
            if which == "following"
            else self.profiles.followers_of(who)
        )
        cards = self.profiles.cards(names)
        mine = set(self.profiles.following_of(me))
        rows = [dict(cards[n], you_follow=n in mine, me=n == me) for n in names]
        self._json({"who": who, "which": which, "people": rows})

    def _edit_profile(self) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        # `None` for anything not sent, so a form that posts one field does not
        # blank the other two — see `Profiles.edit`.
        profile = self.profiles.edit(
            who,
            name=payload["name"] if "name" in payload else None,
            bio=payload["bio"] if "bio" in payload else None,
            link=payload["link"] if "link" in payload else None,
        )
        if "link" in payload and str(payload["link"] or "").strip() and not profile.link:
            self._json({"error": "That link needs to start with http:// or https://"}, 400)
            return
        self._json({"profile": self._mine(who)})

    def _set_picture(self) -> None:
        """A new profile picture, posted as the image's own bytes.

        Raw rather than multipart because the page has already decoded the
        photograph into a canvas and handed back a blob — which is also how a
        four-megabyte HEIC off a phone becomes a sixty-kilobyte JPEG before it
        touches the network. The server re-encodes it regardless: the page is
        a convenience and never the check.
        """
        who = self.current_user() or ""
        # Checked before reading, and separately from the empty case: they are
        # different mistakes and "there was nothing in that upload" is a
        # baffling thing to be told about an eleven-megabyte photograph.
        if int(self.headers.get("Content-Length") or 0) > profiles.LARGEST_UPLOAD:
            self._json({"error": "That picture is too large. Try one under 8MB."}, 413)
            return
        raw = self._read_body(profiles.LARGEST_UPLOAD)
        if not raw:
            self._json({"error": "There was nothing in that upload."}, 400)
            return
        try:
            filename = profiles.store_picture(raw, self.profiles.pictures, who)
        except profiles.BadPicture as bad:
            self._json({"error": str(bad)}, 400)
            return
        self.profiles.set_picture(who, filename)
        log.info("%s set a profile picture", _for_log(who))
        self._json({"profile": self._mine(who)})

    def _clear_picture(self) -> None:
        who = self.current_user() or ""
        self.profiles.forget_picture(who)
        self._json({"profile": self._mine(who)})

    def _may_see(self, film, who: str) -> bool:
        """Whether this account may have this film at all — block, then restriction.

        The same rule as `_held_back`, asked about one film instead of all of
        them, because this runs on every request for a video file and that one
        walks the whole feed.

        It exists because the restriction was enforced in the list and not in
        the item. `_held_back` had exactly one caller — the feed — so a
        restricted account was correctly shown a feed with the sensitive films
        missing, and `GET /api/films/<id>` handed over the video to anybody
        signed in. Run against a real server, a restricted fourteen-year-old
        got 200 and four kilobytes of it. An id is not a secret: it travels in
        a share, a message, a link somebody sends.

        Both store listings say an account for somebody under 18 starts with
        sensitive films hidden, and the age rating rests on it. Hidden from the
        feed and served from the address bar is not hidden.
        """
        if film is None:
            return False
        if film.owner == who:
            # A restriction is about what you are shown, not about hiding your
            # own work from you. The same carve-out `_held_back` makes.
            return True

        # A block, in both directions. `Profiles.apart` puts everybody on
        # either side of one into a single set precisely so this cannot be got
        # half right, and its docstring says what half-right looks like:
        # "leaves somebody able to watch the films of the person who blocked
        # them ... which is not a block, it is a mute."
        #
        # It was applied in the feed and nowhere else, so that is exactly what
        # a block was. Blocked, the feed hid the film and this served it — 200
        # and three kilobytes, measured. The wall had two sides and a door.
        if film.owner in self.profiles.apart(who):
            return False

        self.accounts.refresh()
        account = self.accounts.get(who)
        if account is None or not account.restricted:
            return True
        if film.sensitive:
            return False
        # "This might be bad, we do not know yet" — held while the operator
        # decides, exactly as it is in the feed.
        return not any(r.kind == "film" and r.about == film.id for r in self.reports.open_ones())

    def _held_back(self, who: str) -> set[str]:
        """Film ids this account's content restriction keeps off the screen.

        Two kinds, and the second is the one worth stating. A film marked
        sensitive is held back because somebody said so. A film with a report
        nobody has looked at yet is held back *while* the operator decides —
        which is the honest thing to do with "this might be bad, we do not
        know yet" on an account somebody has restricted, and it costs nothing
        if the report turns out to be nonsense.

        Never applied to your own films. A restriction is about what you are
        shown, not about hiding your own work from you.
        """
        self.accounts.refresh()
        account = self.accounts.get(who)
        if account is None or not account.restricted:
            return set()
        out = {f.id for f in self.films.feed(who, limit=9999) if f.sensitive and f.owner != who}
        out |= {
            r.about for r in self.reports.open_ones() if r.kind == "film" and r.about_who != who
        }
        return out

    # -------------------------------------------------------------- projects

    def _own_project(self, project_id: str) -> str:
        """A project id, but only if it is this person's. "" otherwise."""
        who = self.current_user() or ""
        if not project_id or self.projects is None:
            return ""
        return project_id if self.projects.get(project_id, who) is not None else ""

    def _album_of(self, project) -> tuple[list, list]:
        """What belongs to a project: its films, and its plans.

        Asked the other way round rather than stored. A film carries the
        project it was made for; the album is "which films say this", which
        means there is one place the association lives and no second list to
        fall out of step with it.
        """
        who = project.owner
        films = [f for f in self.films.by(who, limit=999) if f.project == project.id]
        plans = []
        if self.board is not None:
            plans = [p for p in self.board.by(who) if getattr(p, "project", "") == project.id]
        return films, plans

    def _projects(self) -> None:
        who = self.current_user() or ""
        rows = []
        for project in self.projects.by(who):
            films, plans = self._album_of(project)
            row = project.public(films=len(films), plans=len(plans))
            # A cover to draw the album with: the one they chose, or the
            # newest film in it. An album of forty clips drawn as a grey
            # rectangle is an album nobody opens.
            cover = next((f for f in films if f.id == project.cover), None) or (
                films[0] if films else None
            )
            row["poster"] = f"/api/films/{cover.id}/poster" if cover else ""
            rows.append(row)
        self._json({"projects": rows, "kinds": projects.KINDS})

    def _project(self, project_id: str) -> None:
        who = self.current_user() or ""
        project = self.projects.get(project_id, who)
        if project is None:
            self._json({"error": "not found"}, 404)
            return
        films, plans = self._album_of(project)
        self._json(
            {
                "project": project.whole(
                    films=[f.public(who) for f in films],
                    plans=[self._plan_json(p) for p in plans],
                )
            }
        )

    def _make_project(self) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        project = self.projects.make(
            who,
            str(payload.get("name") or ""),
            note=payload.get("note"),
            place=payload.get("place"),
            starts=payload.get("starts"),
            ends=payload.get("ends"),
        )
        if project is None:
            self._json({"error": "Give it a name."}, 400)
            return
        log.info("%s started a project", _for_log(who))
        self._json({"project": project.public()}, 201)

    def _edit_project(self, project_id: str) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        project = self.projects.edit(
            project_id,
            who,
            **{
                key: payload[key]
                for key in ("name", "note", "place", "starts", "ends", "cover")
                if key in payload
            },
        )
        if project is None:
            self._json({"error": "not found"}, 404)
            return
        self._json({"project": project.public()})

    def _drop_project(self, project_id: str) -> None:
        who = self.current_user() or ""
        if not self.projects.drop(project_id, who):
            self._json({"error": "not found"}, 404)
            return
        # The films it held are untouched, and the answer says so — deleting a
        # way of looking at footage should never read as deleting the footage.
        self._json({"ok": True, "films_kept": True})

    def _project_node(self, project_id: str) -> None:
        """Add a node, or move a handful of them after a drag."""
        who = self.current_user() or ""
        payload = self._json_body()

        moves = payload.get("moves")
        if isinstance(moves, dict):
            moved = self.projects.move_nodes(project_id, who, moves)
            self._json({"moved": moved})
            return

        node = self.projects.add_node(
            project_id,
            who,
            kind=str(payload.get("kind") or ""),
            text=payload.get("text", ""),
            ref=payload.get("ref", ""),
            **{k: payload[k] for k in ("x", "y") if k in payload},
        )
        if node is None:
            self._json({"error": "That is not a kind of node."}, 400)
            return
        self._json({"node": node.public()}, 201)

    def _project_node_one(self, project_id: str, node_id: str, what: str) -> None:
        who = self.current_user() or ""
        if what == "drop":
            if not self.projects.drop_node(project_id, who, node_id):
                self._json({"error": "not found"}, 404)
                return
            self._json({"ok": True})
            return
        payload = self._json_body()
        node = self.projects.edit_node(
            project_id,
            who,
            node_id,
            **{k: payload[k] for k in ("text", "ref", "kind", "done") if k in payload},
        )
        if node is None:
            self._json({"error": "not found"}, 404)
            return
        self._json({"node": node.public()})

    def _project_link(self, project_id: str) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        made = self.projects.link(
            project_id,
            who,
            str(payload.get("a") or ""),
            str(payload.get("b") or ""),
            str(payload.get("note") or ""),
        )
        if made is None:
            self._json({"error": "Those two cannot be joined."}, 400)
            return
        self._json({"link": made.public()}, 201)

    def _project_unlink(self, project_id: str, link_id: str) -> None:
        who = self.current_user() or ""
        if not self.projects.unlink(project_id, who, link_id):
            self._json({"error": "not found"}, 404)
            return
        self._json({"ok": True})

    def _gather(self, project_id: str) -> None:
        """Put a film into a project's album, or take it out."""
        who = self.current_user() or ""
        payload = self._json_body()
        wanted = str(payload.get("project") if "project" in payload else project_id or "")
        if wanted and self.projects.get(wanted, who) is None:
            self._json({"error": "not found"}, 404)
            return
        film = self.films.belongs(str(payload.get("film") or ""), wanted, who)
        if film is None:
            self._json({"error": "that is not yours to move"}, 403)
            return
        self._json({"film": film.public(who)})

    # ---------------------------------------------------------------- safety

    def _restriction_state(self) -> None:
        """What this account is allowed to see, and whether it can change it."""
        who = self.current_user() or ""
        self.accounts.refresh()
        account = self.accounts.get(who)
        if account is None:
            self._json({"error": "Please sign in."}, 401)
            return
        self._json(
            {
                "on": account.restricted,
                # Whether lifting it needs a code — not the code, and not a
                # hash of it. A page that is told "there is no lock" can hide
                # the field; a page told anything more could check the code
                # itself, which is not where a check belongs.
                "locked": bool(account.restriction_lock),
                "age": account.age,
                "minor": account.minor,
                "digits": auth.LOCK_DIGITS,
            }
        )

    def _set_restriction(self) -> None:
        """Turn the restriction on or off, and set or clear its code.

        Turning it *on* never needs the code — anybody may restrict
        themselves, and asking for permission to see less would be a strange
        thing to require. Turning it off needs it, which is the entire point:
        the switch has to be out of reach of the person it applies to.
        """
        who = self.current_user() or ""
        payload = self._json_body()
        self.accounts.refresh()
        account = self.accounts.get(who)
        if account is None:
            self._json({"error": "Please sign in."}, 401)
            return

        wanted = bool(payload.get("on"))
        if account.restricted and not wanted:
            if not self.accounts.check_restriction_lock(who, str(payload.get("code") or "")):
                self._json({"error": "That code does not lift it."}, 403)
                return

        self.accounts.set_restriction(who, wanted)

        # A code can be set while turning it on, and is cleared when it comes
        # off — a lock left behind on a restriction that is no longer there is
        # a surprise waiting for whoever turns it back on.
        if "lock" in payload:
            if not wanted:
                self.accounts.set_restriction_lock(who, "")
            else:
                problem = self.accounts.set_restriction_lock(who, str(payload.get("lock") or ""))
                if problem:
                    self._json({"error": problem[0].upper() + problem[1:] + "."}, 400)
                    return
        elif not wanted:
            self.accounts.set_restriction_lock(who, "")

        log.info("%s set their content restriction to %s", _for_log(who), wanted)
        self._restriction_state()

    def _mark_film(self, film_id: str) -> None:
        """Mark your own film sensitive, or unmark it."""
        who = self.current_user() or ""
        wanted = bool(self._json_body().get("sensitive", True))
        film = self.films.mark(film_id, wanted, who=who)
        if film is None:
            self._json({"error": "that is not yours to mark"}, 403)
            return
        self._json({"film": film.public(who)})

    def _block(self, who: str) -> None:
        """Block somebody, or stop. Takes effect at once and asks nobody.

        This is the control that has to work at three in the morning without
        anybody being available, which is why it is entirely in the hands of
        the person doing it and why it needs no confirmation beyond the tap.
        """
        me = self.current_user() or ""
        who = unquote(who).strip()
        self.accounts.refresh()
        if who not in self.accounts.accounts:
            self._json({"error": "no one here by that name"}, 404)
            return
        if who == me:
            self._json({"error": "You cannot block yourself."}, 400)
            return
        if bool(self._json_body().get("block", True)):
            self.profiles.block(me, who)
            log.info("%s blocked somebody", _for_log(me))
        else:
            self.profiles.unblock(me, who)
        self._json({"profile": self.profiles.public_of(who, viewer=me)})

    def _report(self) -> None:
        """Report a film, a message or a person to whoever runs this instance.

        It goes to a real named human — the person whose computer this is —
        rather than to a queue, and the answer says so. Claiming a review team
        would be a lie, and a reporting flow that lies about where the report
        went is worse than one that is honest about being small.
        """
        me = self.current_user() or ""
        payload = self._json_body()
        kind = str(payload.get("kind") or "").strip()
        about = str(payload.get("about") or "").strip()
        reason = str(payload.get("reason") or "").strip()

        if kind not in safety.KINDS:
            self._json({"error": "What is being reported?"}, 400)
            return
        if reason not in safety.REASONS:
            self._json({"error": "Pick what is wrong with it."}, 400)
            return

        # Whose it is, worked out here rather than trusted from the page: a
        # report that names whoever the browser said it names is a report
        # somebody can file against anybody.
        about_who = ""
        if kind == "film":
            film = self.films.get(about)
            if film is None:
                self._json({"error": "That film is not here any more."}, 404)
                return
            about_who = film.owner
        elif kind == "person":
            self.accounts.refresh()
            if about not in self.accounts.accounts:
                self._json({"error": "no one here by that name"}, 404)
                return
            about_who = about
        else:  # a message
            about_who = str(payload.get("about_who") or "").strip()
            self.accounts.refresh()
            if about_who not in self.accounts.accounts:
                self._json({"error": "no one here by that name"}, 404)
                return

        if about_who == me:
            self._json({"error": "That is yours. Delete it instead."}, 400)
            return

        report = self.reports.file(
            by=me,
            kind=kind,
            about=about,
            about_who=about_who,
            reason=reason,
            note=str(payload.get("note") or ""),
        )
        if report is None:
            self._json({"error": "That could not be reported."}, 400)
            return
        # Loud in the log, because on this instance the log is a person's own
        # terminal and that is the fastest route to their attention.
        log.warning(
            "REPORT %s: a %s was reported as %s — run `auteur moderate` to see it",
            "urgent" if report.urgent else "filed",
            report.kind,
            report.reason,
        )
        # Blocking at the same time, if they asked. Almost everybody who
        # reports somebody also wants to stop hearing from them, and making
        # that a second journey through a different screen is how people end
        # up doing neither.
        blocked = False
        if bool(payload.get("block")) and about_who and about_who != me:
            blocked = self.profiles.block(me, about_who)
        self._json({"report": report.public(), "blocked": blocked}, 201)

    def _my_reports(self) -> None:
        """What this person has reported, and what came of it."""
        who = self.current_user() or ""
        self._json(
            {
                "reports": [r.public() for r in self.reports.by(who)],
                "reasons": safety.REASONS,
                "blocked": self.profiles.blocked_of(who),
            }
        )

    def _delete_account(self) -> None:
        """Delete this account and everything it made. Password required.

        The App Store requires an app that can create an account to be able to
        delete one from inside itself — an email address to write to is
        explicitly not enough (guideline 5.1.1(v)). It is also the right
        behaviour for a program whose whole claim is that the footage is yours.

        Three things about how it is done:

        **The password, again.** A live session is not proof that the person
        holding the phone is the person who owns the account. Every other
        destructive step in this app asks — turning two-step off does — and
        this is the most destructive one there is.

        **Everything, in one place.** The account store, the films, the
        messages, the profile, the pictures, the plans and the templates. Each
        of those is its own file and none of them knows about the others, so
        the only way this stays complete is for one function to do all of it
        and one test to check that nothing survives.

        **The files, not only the rows.** A film row removed while its mp4 is
        still on disk is footage somebody asked to have deleted, still there.
        """
        who = self.current_user() or ""
        payload = self._json_body()
        self.accounts.refresh()
        account = self.accounts.get(who)
        if account is None:
            self._json({"error": "Please sign in."}, 401)
            return
        if not account.check(str(payload.get("password") or "")):
            self._json({"error": "That password does not match."}, 403)
            return
        # Typed out in full, so a tap and a mis-tap are different things.
        if str(payload.get("confirm") or "").strip().lower() != "delete":
            self._json({"error": 'Type the word "delete" to confirm.'}, 400)
            return

        gone = self.films.forget_everything_by(who)
        for path in gone:
            for candidate in (Path(path), Path(path).with_suffix(".poster.jpg")):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as exc:  # noqa: PERF203 - one bad path is not the rest
                    log.warning("could not remove %s: %s", candidate.name, exc)
        self.messages.forget_everything_with(who)
        self.profiles.forget(who)
        self.reports.forget_everything_about(who)
        self.projects.forget_everything_by(who)
        # The personal half of the watch record. The per-film totals stay:
        # they name nobody, and unpicking a departed viewer's seconds from
        # them would rewrite the performance history of films belonging to
        # people who are still here without making anybody more private.
        if self.watching is not None:
            self.watching.forget_everything_about(who)
        if self.connections is not None:
            # Guideline 5.1.1(v) is about the account being erasable from
            # inside the app, and a live token that can still post to
            # somebody's TikTok is the worst possible remnant of one.
            self.connections.forget_everything_about(who)
        if self.board is not None:
            self.board.forget_everyones(who)
        # The reels somebody added as templates, which live under their name.
        templates = self.studio.workspace / "templates" / f"{_safe_name(who)}.json"
        try:
            templates.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - reported, not fatal
            log.warning("could not remove templates: %s", exc)
        self.accounts.remove(who)
        log.info("account and everything in it removed")

        self._send(
            200,
            json.dumps({"ok": True, "films": len(gone)}).encode(),
            "application/json; charset=utf-8",
            extra={"Set-Cookie": self._clear_session_cookie(), "Cache-Control": "no-store"},
        )

    def _follow(self, who: str) -> None:
        me = self.current_user() or ""
        who = unquote(who).strip()
        self.accounts.refresh()
        if who not in self.accounts.accounts:
            self._json({"error": "no one here by that name"}, 404)
            return
        if who == me:
            self._json({"error": "You already know what you make."}, 400)
            return
        wanted = bool(self._json_body().get("follow", True))
        if wanted:
            self.profiles.follow(me, who)
        else:
            self.profiles.unfollow(me, who)
        self._json({"profile": self.profiles.public_of(who, viewer=me)})

    def _film_media(self, path: str) -> None:
        """The video or the poster for one film, by film id.

        Unlike a job, a film is public to everybody signed in — that is what a
        feed is, and the check that matters is that the id names a film this
        instance published, so a path cannot be walked in from the address bar.

        *Except* where a content restriction says otherwise. That sentence used
        to end at "signed in", and the restriction was enforced only in the
        feed, so the list hid a sensitive film and this handed over the file.
        `_may_see` is the same rule asked about one film.
        """
        parts = path.strip("/").split("/")
        film = self.films.get(parts[2]) if len(parts) > 2 else None
        if film is None:
            self._json({"error": "no such film"}, 404)
            return
        if not self._may_see(film, self.current_user() or ""):
            # The same 404 as "no such film", deliberately. A 403 would confirm
            # that the id names a real film and that it is the restricted kind,
            # which is the one thing worth learning from the outside.
            self._json({"error": "no such film"}, 404)
            return
        want = parts[3] if len(parts) > 3 else "video"
        if want == "poster":
            poster = self._poster_for(film)
            if poster is None:
                self._json({"error": "no poster"}, 404)
                return
            self._file(poster, "image/jpeg")
            return
        source = Path(film.video)
        if not source.is_file():
            self._json({"error": "that film's file has been swept"}, 410)
            return
        self._file(source, "video/mp4")

    def _like(self, film_id: str) -> None:
        who = self.current_user() or ""
        film = self.films.like(film_id, who)
        if film is None:
            self._json({"error": "no such film"}, 404)
            return
        self._json({"likes": len(film.liked_by), "liked": who in film.liked_by})

    def _unpublish(self, film_id: str) -> None:
        """Take your own film out of the feed. The file on disk is left alone."""
        who = self.current_user() or ""
        if not self.films.forget(film_id, who):
            self._json({"error": "that is not yours to remove"}, 403)
            return
        self._json({"ok": True})

    def _people(self) -> None:
        """Everyone else with an account here, so there is somebody to write to.

        Names only. An account list is not a secret on an instance where you
        have to be signed in to ask, and without it the message screen is a
        text field with nowhere to send anything.
        """
        who = self.current_user() or ""
        self.accounts.refresh()
        apart = self.profiles.apart(who)
        names = sorted(n for n in self.accounts.accounts if n != who and n not in apart)
        counts = {n: len(self.films.by(n, limit=999)) for n in names}
        cards = self.profiles.cards(names)
        mine = set(self.profiles.following_of(who))
        self._json(
            {
                "people": [
                    dict(cards[n], films=counts.get(n, 0), you_follow=n in mine) for n in names
                ],
                "me": who,
            }
        )

    def _inbox(self) -> None:
        who = self.current_user() or ""
        apart = self.profiles.apart(who)
        rows = [r for r in self.messages.conversations(who) if r["who"] not in apart]
        self._json({"conversations": rows, "unread": sum(r["unread"] for r in rows), "me": who})

    def _thread(self, other: str) -> None:
        who = self.current_user() or ""
        other = other.strip()
        if not other:
            self._json({"error": "who with?"}, 400)
            return
        if other in self.profiles.apart(who):
            # Deliberately the same answer either way round. Telling somebody
            # "you have been blocked" turns a wall into a notification, which
            # is the thing people blocking somebody are usually trying to
            # avoid; and the person who did the blocking does not need to be
            # told either, they know.
            self._json({"who": other, "messages": [], "films": {}, "closed": True})
            return
        notes = self.messages.thread(who, other)
        # Reading a conversation is what marks it read. Doing it on a separate
        # "seen" call means a badge that clears only when the page remembers to
        # say so, which it eventually will not.
        self.messages.mark_read(who, other)
        films = {}
        for note in notes:
            if note.film and note.film not in films:
                found = self.films.get(note.film)
                if found is not None:
                    films[note.film] = found.public(who)
        self._json({"who": other, "messages": [n.public() for n in notes], "films": films})

    def _send_message(self) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        to = str(payload.get("to") or "").strip()
        self.accounts.refresh()
        if to not in self.accounts.accounts:
            self._json({"error": "no one here by that name"}, 404)
            return
        if to in self.profiles.apart(who):
            self._json({"error": "That conversation is closed."}, 403)
            return
        note = self.messages.send(
            who,
            to,
            text=str(payload.get("text") or ""),
            film=str(payload.get("film") or ""),
        )
        if note is None:
            self._json({"error": "there was nothing in that to send"}, 400)
            return
        log.info("message from %s to %s", _for_log(who), _for_log(to))
        self._json({"message": note.public()}, 201)

    def _scholar_ask(self) -> None:
        """Put a question to the Scholar and hand back what it says.

        It answers out of what it has actually studied, and when its language
        model is unreachable it says so in a sentence rather than returning
        something shaped like an answer — which is what `speech` already does,
        so this only has to not paper over it.
        """
        payload = self._json_body()
        question = str(payload.get("text") or "").strip()[:2000]
        if not question:
            self._json({"error": "Ask it something."}, 400)
            return

        try:
            from ..scholar import Scholar

            scholar = Scholar()
        except Exception as exc:  # noqa: BLE001 - the app serves without a Scholar
            self._json({"reply": f"The Scholar is not available: {exc}", "reachable": False})
            return

        # The conversation is keyed per signed-in person, so two people using
        # the same instance do not read each other's questions back.
        conversation = f"web:{self.current_user() or 'guest'}"
        try:
            answer = scholar.chat(question, conversation_id=conversation)
        except Exception as exc:  # noqa: BLE001 - a failed reply is not an outage
            log.info("the Scholar could not answer: %s", exc)
            self._json({"reply": f"It could not answer that: {exc}", "reachable": False})
            return

        text = getattr(answer, "text", "") or ""
        self._json(
            {
                "reply": text,
                # Carried on the response rather than sniffed out of the
                # wording. Matching "not reachable from here" meant the page
                # would call a real answer an outage the day somebody
                # reworded a sentence, or quoted it back.
                "reachable": bool(getattr(answer, "reachable", True)),
                # Read out of the knowledge store instead of written. The
                # page labels it, because notes and an answer are not the
                # same thing and only one of them was thought about.
                "from_study": bool(getattr(answer, "from_study", False)),
                "learnings": scholar.knowledge.total_learnings,
            }
        )

    def _robots(self) -> None:
        """Nothing here is for a search engine.

        An instance is somebody's own machine and everything on it that is
        worth reading is behind the sign-in gate, which answers a crawler with
        a redirect rather than a page — so most of this is already unindexable
        by accident. The handful of paths that are public are not: the sign-in
        screen, the two policy documents and the page that deletes an account.
        The documents are published properly at their own address, where they
        are meant to be found; the copies served here are the same text on
        somebody's home network, and a search result pointing at one of those
        is a search result pointing at a stranger's computer.

        `login.html` has carried a noindex tag for a while, which said this
        about exactly one page. This says it about the instance.
        """
        said = (
            b"# An instance of this app is somebody's own machine. The\n"
            b"# documents are published at their own address; what is here is\n"
            b"# a copy of them on a private network, and everything else needs\n"
            b"# an account.\n"
            b"User-agent: *\n"
            b"Disallow: /\n"
        )
        self._send(200, said, "text/plain; charset=utf-8")

    def _allowed(self, path: str) -> bool:
        """Whether this request may proceed without signing in."""
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return True
        name = Path(path).name
        if name.startswith("icon") and name.endswith(".png") and path == "/" + name:
            return True
        return self.current_user() is not None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]

        if not self._allowed(path):
            # A page navigation gets sent to the sign-in screen; a fetch gets a
            # 401 it can act on, because redirecting XHR to HTML is a riddle.
            if path.startswith("/api/"):
                self._json({"error": "Please sign in."}, 401)
            else:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if path in ("/terms", "/terms.html"):
            self._static(STATIC / "terms.html", "text/html; charset=utf-8")
            return
        if path in ("/privacy", "/privacy.html"):
            self._static(STATIC / "privacy.html", "text/html; charset=utf-8")
            return
        if path in ("/delete-account", "/delete-account.html"):
            self._static(STATIC / "delete-account.html", "text/html; charset=utf-8")
            return
        if path == "/robots.txt":
            self._robots()
            return
        if path in ("/login", "/login.html", "/reset"):
            self._static(STATIC / "login.html", "text/html; charset=utf-8")
            return
        if path.startswith("/calendar/") and path.endswith(".ics"):
            self._calendar_feed(path[len("/calendar/") : -len(".ics")])
            return
        if path == "/api/calendar":
            self._calendar_link()
            return
        if path == "/api/two-step":
            self._two_step_state()
            return
        if path == "/api/trouble":
            self._trouble_log()
            return
        if path == "/api/sign-in-with":
            self._sign_in_options()
            return
        if path.startswith("/auth/"):
            parts = path.strip("/").split("/")
            provider_key = parts[1] if len(parts) > 1 else ""
            what = parts[2] if len(parts) > 2 else ""
            if what == "start":
                self._oidc_start(provider_key)
            elif what == "return":
                query = parse_qs(self.path.partition("?")[2])
                self._oidc_return(provider_key, {k: v[0] for k, v in query.items() if v})
            else:
                self._json({"error": "not found"}, 404)
            return
        if path == "/api/joining":
            self._who_may_join()
            return
        if path == "/api/plan":
            self._plan_state()
            return
        if path == "/api/can-signup":
            self.accounts.refresh()
            joining = self.accounts.joining()
            first = self.accounts.empty
            self._json(
                {
                    # Three states, not two. "Can I sign up" used to answer
                    # yes only for the very first account and no forever
                    # after, and the button was simply hidden — which reads as
                    # an app without sign-up rather than a copy not taking any.
                    "can": first or joining["open"],
                    "first": first,
                    "needs_code": (not first) and joining["needs_code"],
                    "why": (
                        ""
                        if first or joining["open"]
                        else "This copy is not taking new accounts. Whoever "
                        "runs it can open it in Settings."
                    ),
                }
            )
            return
        if path == "/api/session":
            self._json({"user": self.current_user()})
            return

        if path in ("/", "/index.html"):
            self._static(STATIC / "index.html", "text/html; charset=utf-8")
            return
        if path in ("/ask", "/ask.html"):
            self._static(STATIC / "ask.html", "text/html; charset=utf-8")
            return
        if path in ("/overlays", "/overlays.html", "/animation"):
            self._static(STATIC / "overlays.html", "text/html; charset=utf-8")
            return
        # `/connect/<platform>` starts a connection; `/connect` alone is the
        # "where it goes" screen. Checked before that one, because
        # startswith would otherwise swallow it.
        if path.startswith("/connect/"):
            rest = path[len("/connect/") :].strip("/")
            if rest and "/" not in rest:
                self._start_connecting(rest)
                return
            self._json({"error": "not found"}, 404)
            return
        if path in ("/connect", "/connect.html", "/connections"):
            self._static(STATIC / "connect.html", "text/html; charset=utf-8")
            return
        if path in ("/studio", "/studio.html"):
            self._static(STATIC / "studio.html", "text/html; charset=utf-8")
            return
        if path in ("/templates", "/templates.html"):
            self._static(STATIC / "templates.html", "text/html; charset=utf-8")
            return
        if path in ("/feed", "/feed.html"):
            self._static(STATIC / "feed.html", "text/html; charset=utf-8")
            return
        if path in ("/inbox", "/inbox.html", "/messages"):
            self._static(STATIC / "inbox.html", "text/html; charset=utf-8")
            return
        if path in ("/profile", "/profile.html", "/me", "/you") or path.startswith("/u/"):
            # `/u/<name>` so a profile is a link somebody can send. The page
            # reads the name out of the address itself; the server serves the
            # same document either way, which is what makes back and forward
            # work between two people's profiles.
            self._static(STATIC / "profile.html", "text/html; charset=utf-8")
            return
        if path in ("/projects", "/projects.html") or path.startswith("/project/"):
            # `/project/<id>` so one project is a place you can be, with a
            # back button that goes to the list rather than to wherever you
            # happened to come from.
            self._static(
                STATIC / ("project.html" if path.startswith("/project/") else "projects.html"),
                "text/html; charset=utf-8",
            )
            return
        if path in ("/manager", "/manager.html", "/plan"):
            self._static(STATIC / "manager.html", "text/html; charset=utf-8")
            return
        if path == "/api/platforms":
            self._json({"platforms": self._platforms()})
            return
        if path == "/api/insight":
            self._json(self._insight())
            return
        # The phone should be able to see everything the terminal can. These two
        # existed only as CLI commands, so `auteur agents` and `auteur scholar`
        # told you things the app you carry could not — which is the same seam
        # that had the studio running a weaker crew than the CLI.
        if path == "/api/crew":
            self._json(self._crew_memory())
            return
        if path == "/api/overlays":
            self._json(self._overlay_rules())
            return
        if path == "/api/templates":
            self._json({"templates": _templates() + self._my_templates()})
            return
        if path == "/api/connections":
            self._json(self._connection_state())
            return
        if path == "/api/scholar":
            self._json(self._scholar_state())
            return
        if path == "/api/feed":
            self._feed()
            return
        # Not "/api/connections": that is already the *destinations* a film can
        # be handed off to — Instagram and TikTok as places to paste a caption
        # — and claiming the path twice meant the first branch answered and
        # this one was dead code. The page fetched it, got a payload with the
        # wrong shape, read `undefined` and drew nothing, with no error
        # anywhere. These are linked accounts, which is a different noun.
        if path == "/api/linked-accounts":
            self._my_connections()
            return
        if path == "/api/watching":
            self._my_watching()
            return
        if path == "/api/profile":
            self._my_profile()
            return
        if path == "/api/reports":
            self._my_reports()
            return
        if path == "/api/projects":
            self._projects()
            return
        if path.startswith("/api/projects/"):
            self._project(path.strip("/").split("/")[2])
            return
        if path == "/api/restriction":
            self._restriction_state()
            return
        if path.startswith("/api/profiles/"):
            parts = path.strip("/").split("/")
            name = parts[2] if len(parts) > 2 else ""
            what = parts[3] if len(parts) > 3 else ""
            if what == "picture":
                self._profile_picture(name)
            elif what == "block":
                self._json({"error": "not found"}, 404)
            elif what in ("following", "followers"):
                self._profile_people(name, what)
            elif what:
                self._json({"error": "not found"}, 404)
            else:
                self._profile(name)
            return
        if path == "/api/people":
            self._people()
            return
        if path == "/api/messages":
            self._inbox()
            return
        if path == "/api/shared":
            self._shared_state()
            return
        if path == "/api/plans":
            self._plans()
            return
        if path.startswith("/api/plans/"):
            self._plan_one(path.strip("/").split("/")[2])
            return
        if path.startswith("/api/messages/"):
            self._thread(unquote(path[len("/api/messages/") :]))
            return
        if path.startswith("/api/films/"):
            self._film_media(path)
            return
        if path.startswith("/static/"):
            self._static(STATIC / Path(path).name)
            return
        # The manifest and the <link> tags ask for these from the root, not from
        # /static/. Match the icons by shape rather than listing them: the
        # manifest names sizes this route must not have to be kept in step with.
        name = Path(path).name
        if path in ("/manifest.webmanifest", "/sw.js") or (
            name.startswith("icon") and name.endswith(".png") and path == "/" + name
        ):
            self._static(STATIC / name)
            return
        # Every browser asks for this on every visit whether or not anything
        # links to it, so not answering meant a 404 in the console and in the
        # log on every page load. The 192 is the smallest square already built.
        if path == "/favicon.ico":
            self._static(STATIC / "icon-192.png")
            return

        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = self.studio.get(parts[2], owner=self.current_user()) if len(parts) > 2 else None
            if job is None:
                self._json({"error": "no such job"}, 404)
                return
            if len(parts) == 3:
                self._json(job.snapshot())
                return
            if parts[3] == "video" and job.video:
                self._file(job.video, "video/mp4")
                return
            if parts[3] == "notes" and job.notes:
                self._file(job.notes, "text/markdown; charset=utf-8")
                return
            self._json({"error": "not ready"}, 404)
            return

        self._json({"error": "not found"}, 404)

    do_HEAD = do_GET  # noqa: N815 - stdlib naming

    # -- signing in ------------------------------------------------------

    def _connections(self):
        """The link store, made on first use and kept beside the accounts."""
        from ..publish import Connections

        held = getattr(type(self), "_links", None)
        if held is None:
            held = Connections(Connections.default_path(self.studio.workspace))
            type(self)._links = held
        return held

    def _connection_state(self) -> dict:
        """Which platforms are linked, and what each one can actually do here.

        Never includes a token. `Connection.public` is the only shape that
        leaves this process, so there is no path from a careless response to
        somebody's Instagram credentials in a browser's network log.
        """
        from ..publish import ABOUT, configured

        who = self.current_user() or ""
        out = []
        for link in self._connections().of(who):
            can_post, why = configured(link.platform)
            about = ABOUT[link.platform]
            out.append(
                {
                    **link.public(),
                    "handoff": about["handoff"],
                    "formats": list(about["formats"]),
                    "can_post": can_post,
                    # Said plainly. A button that cannot work is worse than a
                    # sentence saying nobody registered a developer app.
                    "why_not": why,
                }
            )
        return {"platforms": out}

    def _link_account(self) -> None:
        """Record a link the person made.

        Posting through an API needs a registered developer app, which this
        cannot conjure; where that is not configured a link still means
        something — it is which account the handoff is for, so the caption and
        the composer are aimed at the right place.
        """
        payload = self._json_body()
        platform = str(payload.get("platform", "")).strip().lower()
        handle = str(payload.get("handle", "")).strip()[:64]
        from ..publish import PLATFORMS

        if platform not in PLATFORMS:
            self._json({"error": "No such platform."}, 400)
            return
        if not handle:
            self._json({"error": "Which account? Put your handle in."}, 400)
            return
        who = self.current_user() or ""
        # No token: this is the handoff link, and saying so is the point.
        self._connections().link(who, platform, handle=handle, token="")
        self._json(self._connection_state())

    def _unlink_account(self) -> None:
        payload = self._json_body()
        platform = str(payload.get("platform", "")).strip().lower()
        self._connections().unlink(self.current_user() or "", platform)
        self._json(self._connection_state())

    def _who_may_join(self) -> None:
        """Whether this copy is taking accounts, and the code if it is.

        Signed-in only, and the code is in the answer — the person asking is
        the person who runs this copy, and the whole use of a code is to be
        read out and passed on.
        """
        if not self.current_user():
            self._json({"error": "sign in first"}, 403)
            return
        self.accounts.refresh()
        state = self.accounts.joining()
        self._json(
            {
                "open": state["open"],
                "code": self.accounts.invite_code(),
                # Said in the payload as well as on the screen, the same way
                # the calendar link says it: this is a credential.
                "warning": "Anybody with this code can make an account here.",
            }
        )

    def _set_who_may_join(self) -> None:
        """Open or close this copy to other people.

        A fresh code every time it opens. Re-opening after closing must not
        reinstate a code that has already been passed around — closing is what
        somebody does *because* a code got further than they meant.
        """
        if not self.current_user():
            self._json({"error": "sign in first"}, 403)
            return
        payload = self._json_body()
        self.accounts.refresh()
        if payload.get("open"):
            # Opening is the part the top tier is sold as. Closing, below, is
            # not gated on anything and must never be: closing is what
            # somebody does *because* a code got further than they meant, and
            # a copy that cannot be shut because a card expired is a copy
            # whose billing state has become a security problem.
            problem = billing.may_open_instance(self.accounts.get(self.current_user() or ""))
            if problem:
                self._json({"error": problem, "needs": pricing.TOP_TIER.key}, 402)
                return
            code = self.accounts.open_joining(with_code=bool(payload.get("code", True)))
            self._json({"open": True, "code": code})
            return
        self.accounts.close_joining()
        self._json({"open": False, "code": ""})

    def _plan_state(self) -> None:
        """What this person is on, and what it would take to be on more.

        The app had no way to ask. `Account.plan` was written by the webhook
        and read by nothing, which is the same shape of defect as a gate that
        is never called: a fact the program holds and never acts on.

        Everything here is derived from `auteur/pricing.py` so the screen and
        the checkout cannot advertise two different numbers.
        """
        account = self.accounts.get(self.current_user() or "")
        if account is None:
            self._json({"error": "sign in first"}, 403)
            return
        tier = account.tier
        # A checkout that is not usable is not offered. `checkout_for` raises
        # on a test link rather than returning one, which is the behaviour
        # that keeps a fake card form off a real page.
        checkout = ""
        try:
            # With the buyer's name on it. Stripe hands `client_reference_id`
            # back on the completed session and it is the only thing there
            # that names a person, so a link without it is a charge this copy
            # can never match to an account.
            checkout = pricing.checkout_for_person(pricing.TOP_TIER, account.username)
        except ValueError:
            checkout = ""
        self._json(
            {
                "plan": account.plan,
                "name": tier.name,
                "monthly": tier.monthly,
                "paying": account.paying,
                "until": account.plan_until,
                # Whether the entitlement means anything here at all. On
                # somebody's own machine it does not, and the screen should
                # say so rather than dangling an upgrade at them.
                "hosted": billing.hosted(),
                "top": {
                    "key": pricing.TOP_TIER.key,
                    "name": pricing.TOP_TIER.name,
                    "monthly": pricing.TOP_TIER.monthly,
                    "includes": list(pricing.TOP_TIER.includes),
                    "checkout": checkout,
                    "promo": pricing.PROMO_CODE if checkout else "",
                    "trial_days": pricing.TRIAL_DAYS,
                },
            }
        )

    def _sign_up(self) -> None:
        """Make the first account, and only the first.

        The app serves somebody's own camera roll over their wifi, so an open
        sign-up is an open door. This is closed the moment an account exists —
        after that `auteur account add` is the way, from the machine running
        it, which is the person who owns the footage.
        """
        self.accounts.refresh()
        payload = self._json_body()
        if not self.accounts.may_join(str(payload.get("code", ""))):
            # One message for "closed" and for "wrong code", deliberately.
            # Two would say which of the two it was, and telling an unknown
            # caller that a code exists and theirs is wrong is telling them a
            # code is worth guessing.
            self._json(
                {
                    "error": "This copy is not taking new accounts, or that "
                    "invite code is not right. Whoever runs it can open it "
                    "in Settings."
                },
                403,
            )
            return
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        email = str(payload.get("email", "")).strip()

        if len(username) < 3:
            self._json({"error": "Pick a name of at least three characters."}, 400)
            return

        # The same policy every other path uses, rather than a second, looser
        # one written here.
        #
        # This said `len(password) < 10` and nothing else, while
        # `auth.password_problem` — twelve characters, no leading or trailing
        # space, nothing off a guessing list, at least five distinct
        # characters, and not built from the account's own name — was what
        # `auteur account add`, the first-run seeder and the password *reset*
        # all enforced. So the one path a person who is not a programmer
        # actually takes was the weakest, and these all sailed through it:
        #
        #     0123456789   two characters short of the floor
        #     password12   on every guessing list
        #     aaaaaaaaaa   four distinct characters
        #     tester1234   the account's own username with digits after it
        #
        # And the person who chose one would then be told "use at least 12
        # characters" the first time they tried to reset it. Two rules for one
        # thing, with the weaker one on the front door.
        problem = auth.password_problem(password, username=username, email=email)
        if problem:
            self._json({"error": problem}, 400)
            return

        # The age gate. This app is submitted to the App Store at the rating
        # `MINIMUM_AGE` names, and a rating an app does not itself hold to is a
        # claim rather than a fact.
        try:
            born = int(str(payload.get("born") or "").strip())
        except ValueError:
            born = 0
        this_year = time.gmtime().tm_year
        if not (1900 <= born <= this_year):
            self._json({"error": "What year were you born?"}, 400)
            return
        if auth.age_from(born) < auth.MINIMUM_AGE:
            # Said plainly, and not as "you are too young" — the person
            # reading it may have mistyped a year, and either way there is
            # nothing to do about it here.
            self._json(
                {
                    "error": f"{brand.NAME} is for people "
                    f"{auth.MINIMUM_AGE} and over. "
                    "If that year is wrong, put it right and try again."
                },
                403,
            )
            return

        self.accounts.add(username, email, password, born=born)
        token, message = self.accounts.sign_in(username, password)
        if token is None:
            self._json({"error": message}, 400)
            return
        self._send(
            200,
            json.dumps({"user": username}).encode(),
            "application/json; charset=utf-8",
            extra={"Set-Cookie": self._set_session_cookie(token), "Cache-Control": "no-store"},
        )

    def _sign_in(self) -> None:
        self.accounts.refresh()
        payload = self._json_body()
        token, message = self.accounts.sign_in(
            str(payload.get("username", "")), str(payload.get("password", ""))
        )
        if token is None and message.startswith("code:"):
            # The password was right and it is not enough. No cookie yet: what
            # goes back is a ticket, which names the account and can do nothing
            # but be exchanged for a session by somebody holding a code.
            self._json({"needs": "code", "ticket": message[5:]}, 200)
            return
        if token is None:
            # 401 with the same wording whatever went wrong, so the response
            # never distinguishes "no such user" from "wrong password".
            self._json({"error": message}, 401)
            return
        user = self.accounts.session_user(token)
        self._send(
            200,
            json.dumps({"user": user}).encode(),
            "application/json; charset=utf-8",
            extra={"Set-Cookie": self._set_session_cookie(token), "Cache-Control": "no-store"},
        )

    #: Most faults kept on disk. A log that grows without bound is a log that
    #: eventually fills the machine the app is running on.
    TROUBLE_KEEP = 200

    def _record_trouble(self) -> None:
        """A script error from a page, written down where somebody can find it.

        Not telemetry, and the difference is not a matter of intent: this goes
        to a file on the machine already running the app and nowhere else.
        There is no endpoint anywhere in this program that sends anything off
        the machine, which is what makes "nothing leaves your phone" a fact
        rather than a policy.

        Open to anybody who can reach the server, signed in or not: a fault on
        the sign-in page is exactly the one nobody can report otherwise.
        """
        payload = self._json_body()
        entry = {
            "at": str(payload.get("at") or "")[:40],
            "what": str(payload.get("what") or "")[:300],
            "where": str(payload.get("where") or "")[:120],
            "line": int(payload.get("line") or 0),
            "page": str(payload.get("page") or "")[:120],
            "stack": str(payload.get("stack") or "")[:900],
            "screen": str(payload.get("screen") or "")[:20],
            "agent": str(payload.get("agent") or "")[:160],
            "who": _for_log(self.current_user() or ""),
        }
        if not entry["what"]:
            self._json({"error": "nothing to record"}, 400)
            return

        path = self.studio.workspace / "trouble.json"
        try:
            with self.studio.lock:
                try:
                    known = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    known = []
                if not isinstance(known, list):
                    known = []
                known.append(entry)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(known[-self.TROUBLE_KEEP :], indent=1), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - failing to log is not a crash
            log.warning("could not write the trouble log: %s", exc)

        log.warning(
            "a page reported: %s (%s:%s)", _for_log(entry["what"]), entry["where"], entry["line"]
        )
        self._json({"recorded": True})

    def _trouble_log(self) -> None:
        """What has gone wrong, newest first, for the studio to show."""
        path = self.studio.workspace / "trouble.json"
        try:
            known = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = []
        if not isinstance(known, list):
            known = []
        self._json({"trouble": list(reversed(known))[:50], "kept": len(known)})

    def _second_step(self) -> None:
        """The code, exchanged for a session.

        Rate limited by the same lockout the password is: a six digit code is
        guessable in a million tries and a server that answers a million tries
        is the whole of the weakness.
        """
        self.accounts.refresh()
        payload = self._json_body()
        who = self.accounts.spend_ticket(str(payload.get("ticket") or ""))
        if who is None:
            self._json({"error": "That sign-in expired. Start again."}, 401)
            return
        if not self.accounts.second_step(who, str(payload.get("code") or "")):
            log.info("a wrong second step for %s", _for_log(who))
            self._json({"error": "That code did not match."}, 401)
            return
        token = self.accounts.open_session(who)
        log.info("%s signed in with a second step", _for_log(who))
        self._send(
            200,
            json.dumps({"user": who}).encode(),
            "application/json; charset=utf-8",
            extra={"Set-Cookie": self._set_session_cookie(token), "Cache-Control": "no-store"},
        )

    def _two_step_state(self) -> None:
        who = self.current_user() or ""
        self.accounts.refresh()
        account = self.accounts.get(who) if who else None
        self._json(
            {
                "on": bool(account and account.totp_on),
                "recovery_left": len(account.recovery) if account else 0,
            }
        )

    def _two_step_start(self) -> None:
        from . import totp

        who = self.current_user() or ""
        secret = self.accounts.begin_totp(who)
        if not secret:
            self._json({"error": "no such account"}, 404)
            return
        self._json(
            {
                # The secret goes to the person setting it up and nowhere else.
                "secret": totp.readable(secret),
                "uri": totp.provisioning_uri(secret, account=who),
            }
        )

    def _two_step_confirm(self) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        codes = self.accounts.confirm_totp(who, str(payload.get("code") or ""))
        if codes is None:
            self._json({"error": "That code did not match. It is still off."}, 400)
            return
        log.info("%s turned on two-step verification", _for_log(who))
        # Shown once. They are hashed the moment they are made, so this is the
        # only time they exist in a form anybody can read.
        self._json({"on": True, "recovery": codes})

    def _two_step_off(self) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        if not self.accounts.disable_totp(who, str(payload.get("password") or "")):
            self._json({"error": "That password did not match."}, 401)
            return
        log.info("%s turned off two-step verification", _for_log(who))
        self._json({"on": False})

    def _forgot(self) -> None:
        from .auth import send_reset

        payload = self._json_body()
        who = str(payload.get("username", "")).strip()

        # Byte-for-byte the same answer whether or not the account exists —
        # including `via`, which is a property of how this server is configured
        # and not of the account. Returning it only for real accounts turned
        # this endpoint into a way of asking which addresses have one.
        reply = {
            "ok": True,
            "message": "If that account exists, a reset link is on its way.",
            "via": "email" if os.environ.get("AUTEUR_SMTP_HOST", "").strip() else "console",
        }

        started = self.accounts.begin_reset(who) if who else None
        if started is not None:
            account, token = started
            link = f"{self._site_root()}/reset?token={token}"
            try:
                send_reset(account.email, link)
            except Exception:  # noqa: BLE001 - a mailer must not break the endpoint
                log.exception("reset delivery failed")
        self._json(reply)

    def _reset(self) -> None:
        from .auth import password_problem

        payload = self._json_body()
        token = str(payload.get("token", ""))
        password = str(payload.get("password", ""))

        account = self.accounts.account_for_reset(token)
        problem = password_problem(
            password,
            username=account.username if account else "",
            email=account.email if account else "",
        )
        if problem:
            self._json({"error": problem}, 400)
            return
        if not self.accounts.finish_reset(token, password):
            self._json({"error": "That link has expired. Ask for a new one."}, 400)
            return
        self._json({"ok": True, "message": "Password changed. You can sign in now."})

    def _site_root(self) -> str:
        """The address to put in a password-reset link.

        Deliberately *not* the request's Host header. That header belongs to
        whoever sent the request, and this URL is emailed to the account's
        owner: anyone able to reach the port could ask for a reset with
        `Host: attacker.example.com`, and the owner would receive a real,
        valid token pointing at the attacker's server. Own address only, or an
        explicit one the operator set.
        """
        configured = os.environ.get("AUTEUR_PUBLIC_URL", "").strip().rstrip("/")
        if configured:
            return configured
        host, port = self.server.server_address[:2]
        # 0.0.0.0 means "every interface", which is not an address anyone can
        # open; name the one the phone would actually dial.
        if host in ("", "0.0.0.0", "::", "::0"):
            host = local_address()
        return f"http://{host}:{port}"

    def _read_body(self, limit: int) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            block = self.rfile.read(min(CHUNK, remaining))
            if not block:
                return None
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def _json_body(self) -> dict:
        body = self._read_body(64 * 1024) or b"{}"
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _stripe_webhook(self) -> None:
        """Stripe saying somebody paid, or stopped paying.

        This is the only thing in the program that can put an account onto a
        paid plan. It is unauthenticated because Stripe has no session here —
        and it is therefore signature-checked before the body is looked at,
        which is the whole of its security. `_read_body` is used directly
        rather than `_json_body` because the signature covers the exact bytes
        that were sent, and `_json_body` decodes with `errors="replace"`: a
        single byte replaced is a signature that can never match.

        Every refusal answers with the same sentence. Stripe does not read it,
        and anybody else asking is being told only that it did not work.
        """
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        body = self._read_body(WEBHOOK_LIMIT) or b""
        problem = billing.signature_problem(
            body, self.headers.get("Stripe-Signature", "") or "", secret
        )
        if problem:
            # Logged in full, answered in one word. The log is the operator's,
            # who needs to know an unconfigured secret from a bad signature;
            # the response is the caller's, who does not.
            log.warning("stripe webhook refused: %s", problem)
            self._json({"error": "not accepted"}, 400)
            return

        event = billing.read_event(body)
        if event is None:
            self._json({"error": "not accepted"}, 400)
            return

        grant = billing.grant_from(event)
        if grant is None:
            # A verified event this copy has no opinion about. 200, because
            # anything else asks Stripe to retry it for three days and then
            # switch the endpoint off.
            self._json({"ok": True, "changed": ""})
            return

        self.accounts.refresh()
        who = self.accounts.apply_grant(grant)
        if who:
            log.info("stripe: %s is now on %s", who, grant.plan)
        self._json({"ok": True, "changed": who})

    def _agents_plan(self) -> None:
        """Plan a cut and collect what the agents want to change.

        Nothing renders here. The point of the studio is that a person sees the
        proposals *before* the machine spends three minutes acting on them.
        """
        from ..agents import Gate, Mode
        from ..agents.assemble import build_crew, readings_for
        from ..craft.graphics import find_stickers
        from ..workflows import resolve

        payload = self._json_body()
        try:
            spec = resolve(str(payload.get("platform", "tiktok")))
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        mode_name = str(payload.get("mode", "supervised"))
        if mode_name == "off":
            self._json({"proposals": [], "prediction": None})
            return
        try:
            mode = Mode(mode_name)
        except ValueError:
            self._json({"error": f"unknown mode {mode_name!r}"}, 400)
            return

        edl = self.studio.last_edl(self.current_user())
        if edl is None:
            self._json(
                {"error": "Make a film first — the agents work on a cut, not on a prompt."}, 409
            )
            return

        held: list = []

        def hold(proposal) -> tuple[str, str]:
            # Every proposal needing a person is parked, not answered. The page
            # renders them and the human decides; a server that answered on
            # their behalf would make the gate decorative.
            held.append(proposal)
            return "reject", "waiting for you"

        # The same crew the CLI builds, from the same function. The studio's
        # whole job is to show what the agents want *before* three minutes are
        # spent acting on it, so a shorter list here than the CLI would act on
        # is the one thing it must never show. Readings come off the cut's own
        # shots, which is how the web path gets measured subjects without a
        # folder of rushes to point at.
        crew = build_crew(
            self._fitted(),
            gate=Gate(mode, on_ask=hold),
            readings=readings_for(edl),
            spec=spec,
            stickers=find_stickers(self.studio.sticker_dir),
        )
        result = crew.run(edl)

        type(self)._pending = {
            "user": self.current_user(),
            "platform": spec.name,
            "edl": result.edl,
            "proposals": [p for round_ in result.rounds for p in round_.proposals],
        }
        self._json(
            {
                "prediction": result.final.to_json(),
                "baseline": result.baseline.to_json(),
                "proposals": [p.to_json() for p in type(self)._pending["proposals"]],
                "waiting": len(held),
            }
        )

    def _agents_decide(self) -> None:
        """Apply or discard one held proposal."""
        from ..insight import predict

        payload = self._json_body()
        pending = type(self)._pending
        if not pending or pending.get("user") != self.current_user():
            self._json({"error": "nothing waiting for a decision"}, 409)
            return

        proposals = pending["proposals"]
        try:
            index = int(payload.get("index", -1))
            proposal = proposals[index]
        except (TypeError, ValueError, IndexError):
            self._json({"error": "no such proposal"}, 404)
            return

        answer = str(payload.get("answer", "reject"))
        proposal.decided_by = "human"
        if answer == "approve":
            try:
                proposal.change(pending["edl"])
            except Exception as exc:  # noqa: BLE001 - report, do not crash the room
                proposal.decision_note = f"could not apply: {exc}"
                self._json({"error": proposal.decision_note}, 400)
                return
            proposal.applied = True
            proposal.decision_note = "you applied it"
        else:
            proposal.decision_note = "you left it"

        self._json(
            {
                "proposals": [p.to_json() for p in proposals],
                "prediction": predict(pending["edl"], self._fitted()).to_json(),
            }
        )

    # ------------------------------------------------------------- calendar

    def _calendar_feed(self, token: str) -> None:
        """The subscribable calendar for whoever holds this token.

        Deliberately outside the session gate: a calendar app is not a browser,
        has no cookie, and will not sign in. The URL is the credential, which
        is why it is long, unguessable, per person and rollable — and why this
        answers exactly the same way for a wrong token as for a right one
        belonging to somebody with no plans.
        """
        from .. import calendar as ics

        self.accounts.refresh()
        account = self.accounts.by_calendar_token(token)
        plans = [p.public() for p in self.board.by(account.username)] if account else []
        body = ics.feed(plans).encode("utf-8")
        self._send(
            200,
            body,
            "text/calendar; charset=utf-8",
            extra={
                # Named so it lands in the calendar rather than in downloads.
                "Content-Disposition": 'inline; filename="auteur.ics"',
                # A calendar that caches this would show a stale shoot, which
                # is the one failure that makes a reminder worse than none.
                "Cache-Control": "no-store, max-age=0",
            },
        )

    def _calendar_link(self) -> None:
        """The URL to subscribe to, and the words to explain it."""
        who = self.current_user() or ""
        token = self.accounts.calendar_token(who) if who else ""
        self._json(
            {
                "path": f"/calendar/{token}.ics" if token else "",
                "refresh_minutes": __import__(
                    "auteur.calendar", fromlist=["REFRESH_MINUTES"]
                ).REFRESH_MINUTES,
                # Said in the payload as well as on screen: the link is a
                # secret, and anybody who has it can read the board.
                "secret": True,
            }
        )

    def _calendar_roll(self) -> None:
        who = self.current_user() or ""
        token = self.accounts.calendar_token(who, roll=True) if who else ""
        log.info("%s rolled their calendar link", _for_log(who))
        self._json({"path": f"/calendar/{token}.ics" if token else ""})

    # ------------------------------------------------------- signing in with

    def _sign_in_options(self) -> None:
        """Which providers this copy offers, and why any of them is missing."""
        from . import oidc

        self._json({"providers": oidc.offered(self.sign_in_with)})

    def _oidc_start(self, provider_key: str) -> None:
        from . import oidc

        settings = self.sign_in_with.get(provider_key)
        if provider_key not in oidc.PROVIDERS or settings is None or not settings.usable:
            self._redirect("/login?trouble=unconfigured")
            return
        attempt = self.attempts.begin(provider_key)
        try:
            where = oidc.begin(provider_key, settings, attempt)
        except Exception as exc:  # noqa: BLE001 - a misconfiguration is not a crash
            log.warning("could not start %s sign-in: %s", provider_key, exc)
            self._redirect("/login?trouble=unconfigured")
            return
        self._redirect(where)

    def _oidc_return(self, provider_key: str, fields: dict[str, str]) -> None:
        """Back from the provider. Signs in an account that already exists.

        Never creates one. Sign-up on this app closes after the first account
        because it is serving somebody's own footage over their own wifi, and
        an identity provider is a way to prove who you are rather than a way in
        — so an unrecognised address is told plainly that it is unrecognised.
        """
        from . import oidc

        if fields.get("error"):
            self._redirect("/login?trouble=refused")
            return
        attempt = self.attempts.claim(fields.get("state", ""))
        if attempt is None or attempt.provider != provider_key:
            self._redirect("/login?trouble=stale")
            return
        settings = self.sign_in_with.get(provider_key)
        code = fields.get("code", "")
        if settings is None or not settings.usable or not code:
            self._redirect("/login?trouble=unconfigured")
            return

        try:
            claims = oidc.finish(provider_key, settings, attempt, code)
        except Exception as exc:  # noqa: BLE001 - a failed exchange is a message
            log.info("%s sign-in did not complete: %s", provider_key, exc)
            self._redirect("/login?trouble=failed")
            return

        email = oidc.email_of(claims)
        if not email:
            self._redirect("/login?trouble=unverified")
            return

        self.accounts.refresh()
        account = self.accounts.get(email)
        if account is None:
            log.info("%s sign-in for an address with no account here", provider_key)
            self._redirect("/login?trouble=nomatch")
            return

        token = self.accounts.open_session(account.username)
        log.info("%s signed in with %s", _for_log(account.username), provider_key)
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", self._set_session_cookie(token))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # -------------------------------------------------------------- manager

    def _hold_and_open(self) -> tuple[float, float]:
        """The two numbers the manager judges a plan by, from the Scholar.

        Fetched here rather than inside `check` so that what a plan is being
        held against is visible at the call site. Falls back to zero, which the
        check reads as "not measured" and reports rather than papering over.
        """
        try:
            from ..scholar import Scholar

            store = Scholar().knowledge
        except Exception:  # noqa: BLE001 - no Scholar is a fallback, not a failure
            return 0.0, 0.0
        hold = opening = 0.0
        for question, key in (
            ("hypercut how fast a fast cut is", "shot_seconds"),
            ("how long before the first cut, the opening hold", "first_cut"),
        ):
            for hit in store.recall(question, limit=3):
                value = (getattr(hit, "measurements", None) or {}).get(key)
                if value:
                    if key == "shot_seconds":
                        hold = float(value)
                    else:
                        opening = float(value)
                    break
        return hold, opening

    def _plan_json(self, plan, *, checked: bool = False) -> dict:
        from .. import manager

        out = plan.public()
        # The surface's own name and spec, resolved here rather than looked up
        # in the page: the page fills its platform table from a second request,
        # and a plan drawn before that arrives showed the raw key.
        try:
            from ..workflows.platforms import resolve

            spec = resolve(plan.platform)
            out["platform_name"] = spec.title
            out["platform_spec"] = spec.describe()
            # Which of the edit room's shapes this surface is, resolved here
            # because the geometry is here. "Make this film now" carries it
            # over so the plan's surface is not chosen twice, once in the
            # planner and again in a control that opened on its default.
            out["shape"] = _shape_of(spec.format)
        except Exception:  # noqa: BLE001 - an unknown surface is the check's problem
            out["platform_name"] = plan.platform
            out["platform_spec"] = ""
            out["shape"] = ""
        if checked:
            hold, opening = self._hold_and_open()
            report = manager.check(
                plan, hold=hold, first_cut=opening, others=self.board.by(plan.owner)
            )
            score, why = manager.predict_for(plan, self._film_file(plan.film))
            report.predicted = score
            report.provenance = why
            out["check"] = report.to_json()
        return out

    def _runtime_of(self, video: str) -> float:
        """How long a finished film actually runs, or 0.0 if it cannot be read.

        Zero rather than a plausible default: a plan showing a runtime nobody
        measured is a number somebody will plan around.
        """
        from .. import ffmpeg as ff

        try:
            info = ff.probe(str(video))
            return round(float(info.get("format", {}).get("duration") or 0.0), 2)
        except (ff.FFmpegError, ValueError, TypeError, OSError):
            return 0.0

    def _film_file(self, film_id: str):
        if not film_id or self.films is None:
            return None
        film = self.films.get(film_id)
        return film.video if film is not None else None

    def _plans(self) -> None:
        who = self.current_user() or ""
        from .. import manager
        from ..workflows.platforms import PLATFORMS

        plans = self.board.by(who)
        self._json(
            {
                "plans": [self._plan_json(p) for p in plans],
                "platforms": [
                    {
                        "id": key,
                        "name": spec.title,
                        "service": spec.service,
                        "spec": spec.describe(),
                        "ideal": spec.ideal_seconds,
                    }
                    for key, spec in PLATFORMS.items()
                ],
                "statuses": list(manager.STATUSES),
                # Said in the payload as well as in the page, so anything that
                # reads this API rather than the screen still knows.
                "posts": False,
            }
        )

    def _plan_one(self, plan_id: str) -> None:
        plan = self.board.get(plan_id)
        if plan is None or plan.owner != (self.current_user() or ""):
            self._json({"error": "no such plan"}, 404)
            return
        self._json({"plan": self._plan_json(plan, checked=True)})

    def _schedule_a_film(self) -> None:
        """Send a finished film to the Schedule.

        `Plan.film` has existed since the board was written, is read by the
        calendar and by the prediction, and nothing could ever set it. The two
        halves of the app — the one that makes a film and the one that decides
        when it goes out — had no door between them, so a person finished
        something and then started again from a blank plan.

        A film goes to a *new* plan rather than being offered a list of
        existing ones. The plan carries the film's own prompt as its title, so
        the row on the board says what the film is rather than "Untitled", and
        the caption starts empty because a caption is a thing somebody writes.
        """
        who = self.current_user() or ""
        if not who:
            self._json({"error": "not signed in"}, 403)
            return
        payload = self._json_body()
        film_id = str(payload.get("film") or "").strip()
        film = self.films.get(film_id) if self.films else None
        if film is None:
            self._json({"error": "no such film"}, 404)
            return
        if film.owner != who:
            # Somebody else's film is not yours to schedule, and the check is
            # here rather than on the page because a page can be edited.
            self._json({"error": "that is not your film"}, 403)
            return

        when = (
            str(payload.get("when") or "").strip()
            or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        )
        plan = self.board.add(
            owner=who,
            title=(film.prompt or "A film")[:120],
            platform=str(payload.get("platform") or "instagram-reel"),
            when=when,
            prompt=film.prompt or "",
            # The film's real runtime, measured. `Film` has no `seconds`
            # field, so the first draft of this read `getattr(film,
            # "seconds", 20.0)` — which is a fabricated 20 every single time,
            # dressed as a fallback. A plan is a thing somebody makes
            # decisions from and a made-up runtime on it is worse than none.
            seconds=self._runtime_of(film.video),
            # No shot list. The shots exist already — they are in the film —
            # and generating a list of things to go and photograph for footage
            # that is finished is the kind of busywork that makes somebody stop
            # trusting a tool.
            shots=[],
            captures=[],
            film=film.id,
        )
        self._json({"plan": plan.public()})

    def _make_plan(self) -> None:
        """A new plan, with its shot list already worked out."""
        from .. import manager

        who = self.current_user() or ""
        payload = self._json_body()
        prompt = str(payload.get("prompt") or "").strip()[:500]
        title = str(payload.get("title") or "").strip()[:120] or (prompt[:60] or "Untitled")
        if not prompt:
            self._json({"error": "Say what the film is."}, 400)
            return
        try:
            seconds = float(payload.get("seconds") or 20.0)
        except (TypeError, ValueError):
            seconds = 20.0
        seconds = max(3.0, min(600.0, seconds))

        hold, _ = self._hold_and_open()
        shots = manager.shot_list(prompt, seconds=seconds, hold=hold)
        plan = self.board.add(
            owner=who,
            title=title,
            platform=str(payload.get("platform") or "instagram-reel"),
            when=str(payload.get("when") or "").strip()
            or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            prompt=prompt,
            seconds=seconds,
            shots=[asdict(shot) | {"why": shot.why} for shot in shots],
            captures=[asdict(c) for c in manager.capture_list(shots)],
            caption=str(payload.get("caption") or "")[:2500],
            hashtags=[t.strip().lstrip("#") for t in (payload.get("hashtags") or []) if t.strip()][
                :30
            ],
            alt_text=str(payload.get("alt_text") or "")[:500],
            project=str(payload.get("project") or "")[:64],
        )
        log.info("%s planned %s", _for_log(who), _for_log(plan.title))
        self._json({"plan": self._plan_json(plan, checked=True)}, 201)

    def _edit_plan(self, plan_id: str) -> None:
        who = self.current_user() or ""
        payload = self._json_body()
        fields = {}
        for key in ("title", "platform", "when", "prompt", "caption", "alt_text", "status", "note"):
            if key in payload:
                fields[key] = str(payload[key])[:2500]
        if "seconds" in payload:
            try:
                fields["seconds"] = max(3.0, min(600.0, float(payload["seconds"])))
            except (TypeError, ValueError):
                pass
        if "hashtags" in payload:
            fields["hashtags"] = [
                str(t).strip().lstrip("#") for t in (payload["hashtags"] or []) if str(t).strip()
            ][:30]
        if "film" in payload:
            fields["film"] = str(payload["film"])[:64]
        plan = self.board.update(plan_id, who, **fields)
        if plan is None:
            self._json({"error": "no such plan"}, 404)
            return
        self._json({"plan": self._plan_json(plan, checked=True)})

    def _reshoot_plan(self, plan_id: str) -> None:
        """Work the shot list out again, after the brief or the runtime changed."""
        from .. import manager

        who = self.current_user() or ""
        plan = self.board.get(plan_id)
        if plan is None or plan.owner != who:
            self._json({"error": "no such plan"}, 404)
            return
        hold, _ = self._hold_and_open()
        shots = manager.shot_list(plan.prompt, seconds=plan.seconds, hold=hold)
        plan = self.board.update(
            plan_id,
            who,
            shots=[asdict(shot) | {"why": shot.why} for shot in shots],
            captures=[asdict(c) for c in manager.capture_list(shots)],
        )
        self._json({"plan": self._plan_json(plan, checked=True)})

    def _drop_plan(self, plan_id: str) -> None:
        if not self.board.drop(plan_id, self.current_user() or ""):
            self._json({"error": "that is not yours to remove"}, 403)
            return
        self._json({"ok": True})

    def _mark_posted(self, plan_id: str) -> None:
        """Record that a *person* posted it. This program does not post.

        Kept as its own route with its own name so that nothing about it can be
        mistaken for publishing: there is no credential, no request, and no
        service on the other end of this. It moves a row on a board.
        """
        plan = self.board.mark_posted(plan_id, self.current_user() or "")
        if plan is None:
            self._json({"error": "no such plan"}, 404)
            return
        self._json({"plan": self._plan_json(plan), "posted_by": "you"})

    # ---------------------------------------------------------------- share

    #: Footage handed to this app by the phone's own share sheet, waiting for
    #: somebody to say what film to make of it. Cleared when it is used, and
    #: swept with the workspace like everything else.
    SHARED_KEEP = 12

    def _shared_dir(self, who: str) -> Path:
        return self.studio.workspace / "shared" / _safe_name(who)

    def _receive_share(self) -> None:
        """Footage shared *into* the app from Photos, Gallery, or anywhere else.

        This is the route the product was missing. Everything here starts with
        footage that is already on the phone, and the way a phone hands footage
        to an app is the system share sheet — so without a share target, "share
        to Auteur" simply is not an option where people already are, and the
        only way in is to open the app first and find the file picker. The
        manifest advertises it; this receives it.

        The share sheet posts a plain form and expects a page back, not JSON:
        it is a navigation, so the answer is a redirect to the make screen with
        the footage already waiting on it.
        """
        who = self.current_user() or ""
        if not who:
            # Signing in first, then coming back, would lose the files. Better
            # to say so than to drop somebody's video silently.
            self._redirect("/login?from=share")
            return
        try:
            files, fields = self._read_upload()
        except MemoryError:
            self._redirect("/?shared=toobig")
            return
        except Exception as exc:  # noqa: BLE001 - a bad share is not a crash
            log.info("could not read a share from %s: %s", _for_log(who), exc)
            self._redirect("/?shared=unreadable")
            return

        folder = self._shared_dir(who)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        kept = 0
        for name, blob in files[: self.SHARED_KEEP]:
            if not blob:
                continue
            (folder / _safe_name(Path(name).name or f"clip-{kept}")).write_bytes(blob)
            kept += 1

        # Some share sheets send the caption as `text` and some as `title`; the
        # manifest maps text to `prompt`, and title is a reasonable fallback.
        said = (fields.get("prompt") or fields.get("title") or "").strip()[:500]
        if said:
            (folder / "said.txt").write_text(said, encoding="utf-8")
        log.info("%s shared %d file(s) into the app", _for_log(who), kept)
        self._redirect("/?shared=1" if kept else "/?shared=empty")

    def _shared_state(self) -> None:
        """What the share sheet left waiting, so the make screen can say so."""
        who = self.current_user() or ""
        folder = self._shared_dir(who) if who else None
        if folder is None or not folder.is_dir():
            self._json({"waiting": 0, "said": "", "names": []})
            return
        names = sorted(f.name for f in folder.iterdir() if f.is_file() and f.name != "said.txt")
        said = ""
        note = folder / "said.txt"
        if note.is_file():
            said = note.read_text(encoding="utf-8")[:500]
        self._json({"waiting": len(names), "said": said, "names": names[:12]})

    def _take_shared(self, who: str) -> list[Path]:
        """Claim the shared footage for a job, and stop it being claimed twice."""
        folder = self._shared_dir(who) if who else None
        if folder is None or not folder.is_dir():
            return []
        return [f for f in sorted(folder.iterdir()) if f.is_file() and f.name != "said.txt"]

    def _clear_shared(self, who: str) -> None:
        if who:
            shutil.rmtree(self._shared_dir(who), ignore_errors=True)

    def _redirect(self, where: str) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_upload(self) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
        """The posted multipart body, as (files, fields).

        Read in blocks: a phone posting a minute of 4K over wifi is a long,
        interruptible transfer, and one giant read() hides that it stalled.
        Raises on anything malformed; every caller turns that into a 400.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("nothing was sent")
        if length > MAX_UPLOAD:
            raise MemoryError("larger than MAX_UPLOAD")

        # Spooled rather than accumulated in a list. The list held one copy and
        # `b"".join` made a second, so a large post cost twice its own size in
        # memory before the parser had even seen it. A SpooledTemporaryFile
        # keeps an ordinary post in memory and lets a big one go to disk.
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_TO_DISK) as spool:
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("the upload stopped part way")
                spool.write(block)
                remaining -= len(block)
            spool.seek(0)
            fields, files = _parse_multipart_stream(spool, self.headers.get("Content-Type", ""))
        return files, fields

    # ------------------------------------------------------------ templates

    def _my_templates(self) -> list[dict]:
        """The reels this person has added, newest first."""
        who = self.current_user() or ""
        if not who:
            return []
        store = self.studio.workspace / "templates" / f"{_safe_name(who)}.json"
        if not store.exists():
            return []
        try:
            mine = json.loads(store.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a corrupt file is not a crash
            log.warning("could not read %s's templates: %s", _for_log(who), exc)
            return []
        for entry in mine:
            entry["mine"] = True
        return list(reversed(mine))

    def _read_a_reel(self) -> None:
        """Watch an uploaded reel, write down its timing, and throw the reel away.

        The footage is measured and deleted. What is kept is where the cuts
        fall, which is the only part anybody cuts to — and it means the app
        never becomes a place other people's video is stored, which is a
        promise worth being able to make.
        """
        who = self.current_user()
        if not who:
            self._json({"error": "Please sign in."}, 401)
            return
        try:
            files, _fields = self._read_upload()
        except Exception:  # noqa: BLE001 - a malformed post is the client's problem
            self._json({"error": "I could not read that upload."}, 400)
            return
        if not files:
            self._json({"error": "Pick a reel first."}, 400)
            return

        from ..insight.template import read as read_template

        name, payload = files[0]
        safe = "".join(ch for ch in Path(name).name if ch.isalnum() or ch in "._- ")[-80:]
        holding = Path(tempfile.mkdtemp(prefix="auteur-reel-"))
        reel = holding / (safe or "reel.mp4")
        try:
            reel.write_bytes(payload)
            measured = read_template(reel, name=Path(safe).stem[:24] or "your reel")
            if measured is None:
                self._json({"error": "That file did not open as video."}, 400)
                return
            if measured.shots < LEAST_TEMPLATE_SHOTS or measured.seconds < LEAST_TEMPLATE_SECONDS:
                self._json(
                    {
                        "error": (
                            f"That reel is {measured.seconds:.0f}s with "
                            f"{measured.shots} cut(s) in it. A template needs at least "
                            f"{LEAST_TEMPLATE_SHOTS} cuts to be worth copying."
                        )
                    },
                    400,
                )
                return
            entry = _template_json(measured)
        finally:
            # Always, including when the measurement raised.
            shutil.rmtree(holding, ignore_errors=True)

        store = self.studio.workspace / "templates" / f"{_safe_name(who)}.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        try:
            mine = json.loads(store.read_text(encoding="utf-8")) if store.exists() else []
        except Exception:  # noqa: BLE001
            mine = []
        # One template per reel, however many times it is uploaded.
        mine = [e for e in mine if e.get("id") != entry["id"]]
        mine.append(entry)
        store.write_text(json.dumps(mine[-40:], indent=1), encoding="utf-8")
        log.info("read a reel for %s: %s", _for_log(who), _for_log(entry["label"]))
        entry["mine"] = True
        self._json({"template": entry}, 201)

    def _within_limits(self, path: str) -> bool:
        """Whether this caller may ask for this path again yet.

        Answered before the handler runs, because the point is not to do the
        work. A 429 with `Retry-After` is what the header is for, and it says
        the same thing to a person who has genuinely asked five times as it
        does to a script.
        """
        throttle = THROTTLES.get(path)
        if throttle is None:
            return True
        wait = throttle.ask(f"{self.client_address[0]}:{path}")
        if not wait:
            return True

        # Read the body even though nothing will look at it. This connection
        # is kept alive, and bytes left in it are not discarded — they are
        # read as the start of the next request. Refusing without draining
        # answered the *following* request with `Unsupported method
        # ('{"username":"..."}GET')`, which is how a browser found this and a
        # test opening a fresh connection per call never could.
        self._read_body(64 * 1024)

        seconds = int(wait) + 1
        # Said in whichever unit a person would use. "Try again in 898s" is a
        # number to convert rather than an answer.
        if seconds >= 120:
            when = f"{round(seconds / 60)} minutes"
        elif seconds >= 60:
            when = "a minute"
        else:
            when = f"{seconds} seconds"
        log.info("throttled %s", path)
        self._send(
            429,
            json.dumps({"error": f"That has been asked for a lot. Try again in {when}."}).encode(),
            "application/json; charset=utf-8",
            extra={"Retry-After": str(seconds), "Cache-Control": "no-store"},
        )
        return False

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]

        if not self._within_limits(path):
            return

        if path.startswith("/auth/") and path.endswith("/return"):
            # Apple replies with a POST form rather than a redirect, because it
            # may carry the person's name the first time they authorise.
            provider_key = path.strip("/").split("/")[1]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 16384)).decode("utf-8", "replace")
            posted = {k: v[0] for k, v in parse_qs(raw).items() if v}
            self._oidc_return(provider_key, posted)
            return
        if path == "/share":
            # Before the sign-in gate reads it as an API call: this is a
            # navigation from the operating system, so it answers with a
            # redirect either way rather than a 401 nobody sees.
            self._receive_share()
            return
        if path == "/api/joining":
            self._set_who_may_join()
            return
        if path == "/api/signup":
            self._sign_up()
            return
        if path == "/api/templates":
            self._read_a_reel()
            return
        if path == "/api/connections/link":
            self._link_account()
            return
        if path == "/api/connections/unlink":
            self._unlink_account()
            return
        if path == "/api/login":
            self._sign_in()
            return
        if path == "/api/login/step2":
            self._second_step()
            return
        if path == "/api/logout":
            self.accounts.sign_out(self.session_token)
            self._send(
                200,
                b'{"ok":true}',
                "application/json; charset=utf-8",
                extra={"Set-Cookie": self._clear_session_cookie(), "Cache-Control": "no-store"},
            )
            return
        if path == "/api/trouble":
            self._record_trouble()
            return
        if path == "/api/forgot":
            self._forgot()
            return
        if path == "/api/reset":
            self._reset()
            return

        if path == "/api/stripe/webhook":
            self._stripe_webhook()
            return

        if not self._allowed(path):
            self._json({"error": "Please sign in."}, 401)
            return
        if path == "/api/scholar/ask":
            self._scholar_ask()
            return
        if path == "/api/agents/plan":
            self._agents_plan()
            return
        if path == "/api/agents/decide":
            self._agents_decide()
            return
        if path == "/api/messages/send":
            self._send_message()
            return
        if path == "/api/profile":
            self._edit_profile()
            return
        if path == "/api/profile/picture":
            self._set_picture()
            return
        if path == "/api/profile/picture/remove":
            self._clear_picture()
            return
        if path == "/api/profile/delete":
            self._delete_account()
            return
        if path == "/api/report":
            self._report()
            return
        if path == "/api/disconnect":
            self._disconnect_platform()
            return
        if path == "/api/watched":
            self._record_watch()
            return
        if path == "/api/restriction":
            self._set_restriction()
            return
        if path == "/api/projects":
            self._make_project()
            return
        if path.startswith("/api/projects/"):
            parts = path.strip("/").split("/")
            project_id = parts[2] if len(parts) > 2 else ""
            what = parts[3] if len(parts) > 3 else ""
            if what == "drop":
                self._drop_project(project_id)
            elif what == "node" and len(parts) > 4:
                self._project_node_one(project_id, parts[4], parts[5] if len(parts) > 5 else "")
            elif what == "node":
                self._project_node(project_id)
            elif what == "link" and len(parts) > 4:
                self._project_unlink(project_id, parts[4])
            elif what == "link":
                self._project_link(project_id)
            elif what == "gather":
                self._gather(project_id)
            else:
                self._edit_project(project_id)
            return
        if path.startswith("/api/profiles/") and path.endswith("/block"):
            self._block(path.split("/")[3])
            return
        if path.startswith("/api/profiles/") and path.endswith("/follow"):
            self._follow(path.split("/")[3])
            return
        if path == "/api/calendar/roll":
            self._calendar_roll()
            return
        if path == "/api/two-step/start":
            self._two_step_start()
            return
        if path == "/api/two-step/confirm":
            self._two_step_confirm()
            return
        if path == "/api/two-step/off":
            self._two_step_off()
            return
        if path == "/api/shared/clear":
            self._clear_shared(self.current_user() or "")
            self._json({"ok": True})
            return
        if path == "/api/schedule-film":
            self._schedule_a_film()
            return
        if path == "/api/plans":
            self._make_plan()
            return
        if path.startswith("/api/plans/"):
            parts = path.strip("/").split("/")
            plan_id = parts[2] if len(parts) > 2 else ""
            what = parts[3] if len(parts) > 3 else ""
            if what == "reshoot":
                self._reshoot_plan(plan_id)
            elif what == "drop":
                self._drop_plan(plan_id)
            elif what == "posted":
                self._mark_posted(plan_id)
            else:
                self._edit_plan(plan_id)
            return
        if path.startswith("/api/films/") and path.endswith("/like"):
            self._like(path.split("/")[3])
            return
        if path.startswith("/api/films/") and path.endswith("/delete"):
            self._unpublish(path.split("/")[3])
            return
        if path.startswith("/api/films/") and path.endswith("/sensitive"):
            self._mark_film(path.split("/")[3])
            return
        if path != "/api/jobs":
            self._json({"error": "not found"}, 404)
            return

        try:
            files, fields = self._read_upload()
        except MemoryError:
            self._json({"error": "That is more footage than this can take at once."}, 413)
            return
        except Exception:  # noqa: BLE001 - a malformed post is the client's problem
            self._json({"error": "I could not read that upload."}, 400)
            return

        # Footage the phone's share sheet already handed over counts as
        # picking clips. Without this the share target delivers the files and
        # the make screen then insists nothing was chosen.
        shared = [] if files else self._take_shared(self.current_user() or "")
        if not files and not shared:
            self._json({"error": "Pick at least one clip first."}, 400)
            return
        prompt = fields.get("prompt", "").strip()
        if not prompt:
            self._json({"error": "Say what kind of film you want."}, 400)
            return

        seconds: float | None = None
        try:
            if fields.get("seconds"):
                seconds = max(3.0, min(180.0, float(fields["seconds"])))
        except ValueError:
            seconds = None

        job = self.studio.create(
            prompt,
            fields.get("shape", "reel"),
            seconds,
            owner=self.current_user() or "",
            template=fields.get("template", "").strip(),
            era=fields.get("era", "").strip(),
            # Checked, not trusted: a project id from a form is a project id
            # somebody could have typed, and filing a film under somebody
            # else's project would be filing their footage under your name.
            project=self._own_project(fields.get("project", "").strip()),
        )
        clips = job.folder / "clips"
        for index, (filename, payload) in enumerate(files):
            safe = Path(filename).name or f"clip{index}"
            safe = "".join(char for char in safe if char.isalnum() or char in "._- ")[-80:]
            (clips / f"{index:02d}-{safe or 'clip.mp4'}").write_bytes(payload)
        for index, source in enumerate(shared, start=len(files)):
            shutil.copy2(source, clips / f"{index:02d}-{source.name}")
        if shared:
            # Claimed. Leaving them would put the same footage in the next film
            # somebody made, which is the kind of bug people assume is a ghost.
            self._clear_shared(self.current_user() or "")

        self.studio.start(job)
        self._json(job.snapshot(), 202)


#: What each decade is called in words, for folding a picked era back into the
#: prompt. Keys match the values the front end sends.
ERA_WORDS = {
    "seventies": "1970s super 8",
    "eighties": "1980s VHS",
    "nineties": "1990s film",
    "y2k": "2000s digital",
    "tens": "2010s faded",
    "now": "2020s clean digital",
}


class Server(ThreadingHTTPServer):
    """The stdlib server, minus the shouting.

    Keep-alive plus a phone means dropped connections all day: the screen
    locks, the app is backgrounded, a video is scrubbed elsewhere in the file.
    The default handler prints a full traceback for each one. That console is
    where password-reset links are printed, so burying it under stack traces
    for a normal event is worse than useless.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        kind = sys.exc_info()[0]
        if kind is not None and issubclass(kind, (ConnectionError, TimeoutError, BrokenPipeError)):
            log.debug("client %s went away", client_address)
            return
        super().handle_error(request, client_address)


def local_address() -> str:
    """This machine's address on the wifi, which is what the phone must dial.

    `localhost` is useless here — the whole point is to open the page on a
    different device. There is no reliable way to ask for "my LAN address", so
    use the oldest trick: open a UDP socket toward the internet and read back
    the interface the routing table chose. Nothing is sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


#: The measured timelines of the reference reels, read once and kept.
#:
#: The markup for the template card has shipped in `index.html` for a while and
#: nothing ever filled it: `window.auteurTemplates` is injected by the artifact
#: builder, so the published page had templates and the app people actually run
#: had a card that stayed hidden forever. Dead markup is worse than no markup —
#: it looks like a feature in the source and is not one on the screen.
_TEMPLATES: list[dict] | None = None


def _templates() -> list[dict]:
    global _TEMPLATES
    if _TEMPLATES is None:
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "tools"
            / "artifact"
            / ("templates.json")
        )
        try:
            _TEMPLATES = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a missing file is not a crash
            log.warning("no reel templates to offer: %s", exc)
            _TEMPLATES = []
    return _TEMPLATES


#: The least a reel can be and still be worth copying. Under this there is not
#: enough timeline to cut anything else to — it is a cadence, not a shape.
LEAST_TEMPLATE_SHOTS = 8
LEAST_TEMPLATE_SECONDS = 4.0


def _for_log(text: str, limit: int = 64) -> str:
    """A value from a page, made safe to put in a log line.

    Same containment as `auteur.publish.connections`: a username or a filename
    carrying a newline writes a second record that looks exactly like a real
    one, which is a way to hide something in a log under convincing forgeries.
    """
    clean = "".join(ch for ch in str(text) if ch.isprintable())
    return repr(clean[:limit])


def _redact(line: str) -> str:
    """Blank out anything in a log line that is a credential.

    Only one thing qualifies today — the calendar token in `/calendar/<x>.ics`
    — and it is handled here rather than at the call site so the next secret
    that ends up in a path has somewhere obvious to be added.
    """
    return _CALENDAR_PATH.sub("/calendar/[redacted].ics", line)


_CALENDAR_PATH = re.compile(r"/calendar/[A-Za-z0-9_-]+\.ics")


def _safe_name(who: str) -> str:
    """A filename from a username. Never a path, never empty."""
    kept = "".join(ch for ch in who if ch.isalnum() or ch in "-_")[:64]
    return kept or "someone"


def _template_json(template) -> dict:
    """A measured reel, in the shape the page reads.

    Beats are flat arrays rather than objects: fifty shots times six named
    fields is most of the file size and none of the information, and the page
    reads them back by position. Same shape `make_templates.py` writes, because
    a template a person uploaded and one that shipped have to be the same kind
    of thing.
    """
    hold = template.shot_seconds
    if hold <= 0.14:
        name = "Razor"
    elif hold <= 0.20:
        name = "Hypercut"
    elif hold <= 0.40:
        name = "Quick"
    else:
        name = "Held"
    # A camera roll hands over names like `7b74c759-766cb232d76f4810`, which is
    # not a name. When the stem is mostly hex and has no words in it, the
    # character and the shot count say more than the filename does.
    given = (template.name or "").strip()
    hexish = sum(ch in "0123456789abcdefABCDEF-" for ch in given)
    if not given or (len(given) > 8 and hexish / len(given) > 0.85):
        title = f"{name} · your reel"
    else:
        title = f"{name} · {given}"

    return {
        "id": template.fingerprint[:10],
        "label": title[:40],
        "note": f"{template.shots} shots · {hold:.2f}s each",
        "seconds": round(template.seconds, 3),
        "shots": template.shots,
        "hold": round(hold, 4),
        "beats": [
            [
                round(b.duration, 4),
                round(b.luma, 3),
                round(b.contrast, 3),
                round(b.saturation, 3),
                round(b.warmth, 3),
                round(b.motion, 3),
            ]
            for b in template.beats
        ],
    }


def _fit_to_template(edl, beats: list, seconds: float | None) -> None:
    """Give a planned edit the rhythm of a reference reel.

    A template is where a reel's cuts actually fall, so imposing one means
    replacing the planned shot *lengths* with the reference's, and repeating
    the planned shots until the runtime is full. Keeping the director's shot
    count and only stretching each one would keep the film's length and lose
    the thing being copied; a reel cut at 0.125s has five times the shots of
    one cut at 0.6s, and that difference is the template.

    Which pictures go where stays the director's decision. This changes when
    the cuts land, not what is on either side of them.
    """
    import copy

    from ..edl import Transition

    holds = [float(b[0]) for b in beats if b and float(b[0]) > 0.02]
    if not holds or not edl.shots:
        return
    target = seconds or edl.duration or sum(holds)

    wanted: list[float] = []
    at = 0.0
    while at < target and len(wanted) < 600:
        hold = holds[len(wanted) % len(holds)]
        wanted.append(hold)
        at += hold
    if not wanted:
        return

    out = []
    for index, hold in enumerate(wanted):
        base = edl.shots[index % len(edl.shots)]
        shot = copy.deepcopy(base)
        # A still can be held for any length. A clip cannot be extended past
        # the footage the director chose without running into frames nobody
        # looked at, so it is only ever shortened.
        span = base.end - base.start
        shot.end = shot.start + (hold if base.is_still else min(hold, span))
        # Every shot after the first inherits its neighbour's join; the first
        # one opens the film and cannot dissolve from anything.
        if index == 0:
            shot.transition_in = Transition()
        out.append(shot)
    edl.shots = out


#: What the decade chooser sends, and the look preset it means.
#:
#: The chooser has been on the first screen sending a value nobody read: the
#: server never looked at the field and the director has no notion of a decade,
#: so picking 90s changed nothing about the film. A control that sets a
#: variable nobody transmits does nothing, which is worse than not offering
#: one — the same rule the length control is already held to.
ERA_LOOKS = {
    "seventies": "1970s",
    "eighties": "1980s",
    "nineties": "1990s",
    "y2k": "2000s",
    "tens": "2010s",
    "now": "2020s",
}


def _said_back(edl, *, prompt: str, template: str, shape) -> str:  # noqa: ARG001
    """One sentence describing the edit that was made.

    A prompt whose effect you cannot see is indistinguishable from a prompt
    that was ignored, which is what people reported about this app. The page
    has had a panel for this the whole time; the server simply never sent
    anything to put in it.
    """
    import re

    shots = len(edl.shots)
    if not shots:
        return ""
    holds = sorted(shot.duration for shot in edl.shots)
    median = holds[len(holds) // 2]
    joins = {}
    for shot in edl.shots[1:]:
        kind = "cut" if shot.transition_in.is_cut else shot.transition_in.kind
        joins[kind] = joins.get(kind, 0) + 1
    looks = {shot.look.preset for shot in edl.shots if shot.look.preset}

    parts = [
        f"{shots} shots over {edl.duration:.0f} seconds, a median {median:.2f}s each",
    ]
    if looks:
        parts.append("graded " + ", ".join(sorted(looks)))
    if template:
        parts.append("cut to a reference reel's timeline")
    if joins:
        best = sorted(joins.items(), key=lambda kv: -kv[1])[:3]
        parts.append("joins: " + ", ".join(f"{n} {kind}" for kind, n in best))
    quoted = re.findall(
        r'["\u201c\u2018\']([^"\u201c\u201d\u2018\u2019\']{1,48})["\u201d\u2019\']', prompt
    )
    if quoted:
        parts.append("on screen: " + ", ".join(f"\u201c{q}\u201d" for q in quoted[:4]))
    return "It made " + "; ".join(parts) + "."


def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    *,
    workspace: Path | None = None,
    quality: str = "draft",
    announce: bool = True,
    claimable: bool = False,
) -> ThreadingHTTPServer:
    """Run the web app until interrupted."""
    from . import assets, seed
    from .auth import Accounts
    from ..manager import Board
    from . import oidc
    from .profiles import Profiles
    from ..projects import Projects
    from .safety import Reports
    from .social import Films, Messages
    from .watching import Watching
    from ..social.accounts import Connections

    assets.ensure(STATIC)
    root = Path(workspace or Path.cwd() / "auteur-web")
    Handler.studio = Studio(root, quality=quality)
    Handler.accounts = Accounts(Accounts.default_path(root))
    # The feed and the inbox. Both outlive the jobs that fill them, which is
    # the whole difference between a renderer and an app you come back to.
    Handler.films = Films(Films.default_path(root))
    Handler.studio.films = Handler.films
    Handler.messages = Messages(Messages.default_path(root))
    # Who everybody is: the picture, the name they chose, and who they follow.
    Handler.profiles = Profiles(Profiles.default_path(root), root / "pictures")
    # What anybody has reported, and what was done about it.
    Handler.reports = Reports(Reports.default_path(root))
    # What has been watched, and by whom. The feed had nothing to rank by and
    # the insight layer was fitted to a simulation of itself; this is where the
    # real numbers come from. It lives here, on this machine, and goes nowhere.
    Handler.watching = Watching(root / "watching")
    # Accounts on TikTok and Instagram. Separate from `accounts`, which is who
    # somebody is here — a token that can publish on a person's behalf does not
    # belong in the same store as a sign-in.
    Handler.connections = Connections(Connections.default_path(root))
    # A project: the album of what came back, and the map of what you were
    # thinking before you went.
    Handler.projects = Projects(Projects.default_path(root))
    Handler.board = Board(Board.default_path(root))
    # Credentials from the workspace or the environment, never the repo.
    Handler.sign_in_with = oidc.load(root)
    Handler.attempts = oidc.Attempts()
    ready = [row["key"] for row in oidc.offered(Handler.sign_in_with) if row["ready"]]
    if ready:
        log.info("signing in with %s is available", ", ".join(ready))
    # A film points at a file inside a job folder, and job folders are swept
    # after a few hours. Checked once at start-up rather than on every feed
    # request, so a long-running instance does not stat the whole library to
    # answer a scroll.
    gone = Handler.films.drop_missing()
    if gone:
        log.info("dropped %d film(s) whose file had been swept", gone)
    # And the same check one level up: a deleted account otherwise stays in
    # everybody's following list as a name with no profile behind it, so the
    # count on the profile page is larger than the list underneath it.
    Handler.accounts.refresh()
    stale = Handler.profiles.drop_unknown(set(Handler.accounts.accounts))
    if stale:
        log.info("dropped %d follow(s) of accounts that no longer exist", stale)
    # Unless the first person is to claim it from their phone. `bootstrap`
    # generates an account and prints its password to the terminal, which is
    # fine at a desk and is the reason the first step of the whole product
    # needed a terminal — on the device the footage is actually on, there
    # isn't one.
    first = None if claimable else seed.bootstrap(Handler.accounts)

    server = Server((host, port), Handler)

    if announce:
        url = f"http://{local_address()}:{port}"
        print()
        print(f"  {brand.NAME}  ·  the edit room is open")
        print()
        print(f"     on this computer   http://localhost:{port}")
        print(f"     on your phone      {url}")
        print()
        print("     Open that on your iPhone, then Share -> Add to Home Screen")
        print("     to keep it as an app. Both devices need the same wifi.")
        if first:
            username, password = first
            print()
            print(f"     Sign in as        {username}")
            if password:
                # Shown once, on the console of the machine doing the renders.
                # It is not stored anywhere in readable form and cannot be
                # printed again — `auteur account password` sets a new one.
                print(f"     Password          {password}")
                print()
                print("     That password was generated just now and is shown once.")
                print("     Change it when you are in:  python -m auteur account password")
        print()
        print("     Press Ctrl-C to close it.")
        print()
    elif first and first[1]:
        # Quiet mode still has to say this once. The generated password exists
        # nowhere but here and in a hash; swallowing it locks the owner out of
        # their own instance.
        print(f"auteur: created account {first[0]} with password {first[1]} (shown once)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if announce:
            print("\n  closed.\n")
    finally:
        server.server_close()
    return server
