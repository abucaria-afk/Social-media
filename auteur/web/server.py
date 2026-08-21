"""A small web front end, built for a phone.

The command line is fine at a desk, but the footage is on the phone. This
serves a mobile-first page that takes clips straight from the camera roll,
runs the same agent, and hands back a film you can save or share. It installs
to the iPhone home screen as a web app, so it opens full-screen with no
browser chrome.

Deliberately stdlib-only: no Flask, no FastAPI, nothing extra to install.
"""

from __future__ import annotations

import email.parser
import email.policy
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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from ..config import FORMATS, QUALITIES, Settings
from ..ui import Reporter, describe_count, describe_duration, describe_shape

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
        "/api/sign-in-with",
    }
)

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
    ) -> Job:
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        folder = self.workspace / job_id
        (folder / "clips").mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, prompt=prompt, folder=folder, owner=owner)
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


def _parse_multipart_stream(
    body, content_type: str
) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """The same, from a file object, so the raw post is never copied."""
    parser = email.parser.BytesParser(policy=email.policy.default)
    header = b"Content-Type: " + content_type.encode() + b"\r\n\r\n"
    message = parser.parse(_Prefixed(header, body))

    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    if not message.is_multipart():
        return fields, files

    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((filename, payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files


class _Prefixed(io.RawIOBase):
    """A file that reads `header` first and then everything in `rest`.

    `BytesParser.parse` wants one stream carrying the Content-Type header and
    the body. Concatenating them would rebuild the copy this exists to avoid,
    so they are read in order instead.
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
        return self._rest.readinto(target)


class Handler(BaseHTTPRequestHandler):
    server_version = "auteur"
    #: HTTP/1.1 so connections are kept alive. The page polls every second or
    #: so; a fresh TCP handshake per poll is pure latency.
    protocol_version = "HTTP/1.1"
    studio: Studio
    accounts: Any = None  # an auth.Accounts once serve() has run
    films: Any = None  # a social.Films once serve() has run
    messages: Any = None  # a social.Messages once serve() has run
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
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            bits.append("Secure")
        return "; ".join(bits)

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
        """Films, newest first. `?mine=1` for only your own, `?before=` to page."""
        who = self.current_user() or ""
        query = parse_qs(self.path.partition("?")[2])
        before = None
        try:
            before = float(query["before"][0]) if query.get("before") else None
        except (TypeError, ValueError):
            before = None
        if query.get("mine"):
            films = self.films.by(who)
        else:
            films = self.films.feed(who, before=before)
        self._json({"films": [f.public(who) for f in films], "me": who})

    def _film_media(self, path: str) -> None:
        """The video or the poster for one film, by film id.

        Unlike a job, a film is public to everybody signed in — that is what a
        feed is. The check that matters is that the id names a film this
        instance published, so a path cannot be walked in from the address bar.
        """
        parts = path.strip("/").split("/")
        film = self.films.get(parts[2]) if len(parts) > 2 else None
        if film is None:
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
        names = sorted(n for n in self.accounts.accounts if n != who)
        counts = {n: len(self.films.by(n, limit=999)) for n in names}
        self._json(
            {
                "people": [{"who": n, "films": counts.get(n, 0)} for n in names],
                "me": who,
            }
        )

    def _inbox(self) -> None:
        who = self.current_user() or ""
        rows = self.messages.conversations(who)
        self._json({"conversations": rows, "unread": sum(r["unread"] for r in rows), "me": who})

    def _thread(self, other: str) -> None:
        who = self.current_user() or ""
        other = other.strip()
        if not other:
            self._json({"error": "who with?"}, 400)
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

        if path in ("/login", "/login.html", "/reset"):
            self._static(STATIC / "login.html", "text/html; charset=utf-8")
            return
        if path.startswith("/calendar/") and path.endswith(".ics"):
            self._calendar_feed(path[len("/calendar/") : -len(".ics")])
            return
        if path == "/api/calendar":
            self._calendar_link()
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
        if path == "/api/can-signup":
            self.accounts.refresh()
            self._json({"can": self.accounts.empty})
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

    def _sign_up(self) -> None:
        """Make the first account, and only the first.

        The app serves somebody's own camera roll over their wifi, so an open
        sign-up is an open door. This is closed the moment an account exists —
        after that `auteur account add` is the way, from the machine running
        it, which is the person who owns the footage.
        """
        self.accounts.refresh()
        if not self.accounts.empty:
            self._json(
                {
                    "error": "This one is already claimed. "
                    "Add more with `auteur account add` on the machine running it."
                },
                403,
            )
            return

        payload = self._json_body()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        email = str(payload.get("email", "")).strip()

        if len(username) < 3:
            self._json({"error": "Pick a name of at least three characters."}, 400)
            return
        if len(password) < 10:
            self._json({"error": "Ten characters or more, please."}, 400)
            return

        self.accounts.add(username, email, password)
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
        except Exception:  # noqa: BLE001 - an unknown surface is the check's problem
            out["platform_name"] = plan.platform
            out["platform_spec"] = ""
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

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]

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
        if path == "/api/logout":
            self.accounts.sign_out(self.session_token)
            self._send(
                200,
                b'{"ok":true}',
                "application/json; charset=utf-8",
                extra={"Set-Cookie": self._clear_session_cookie(), "Cache-Control": "no-store"},
            )
            return
        if path == "/api/forgot":
            self._forgot()
            return
        if path == "/api/reset":
            self._reset()
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
        if path == "/api/calendar/roll":
            self._calendar_roll()
            return
        if path == "/api/shared/clear":
            self._clear_shared(self.current_user() or "")
            self._json({"ok": True})
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
    from .social import Films, Messages

    assets.ensure(STATIC)
    root = Path(workspace or Path.cwd() / "auteur-web")
    Handler.studio = Studio(root, quality=quality)
    Handler.accounts = Accounts(Accounts.default_path(root))
    # The feed and the inbox. Both outlive the jobs that fill them, which is
    # the whole difference between a renderer and an app you come back to.
    Handler.films = Films(Films.default_path(root))
    Handler.studio.films = Handler.films
    Handler.messages = Messages(Messages.default_path(root))
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
        print("  auteur  ·  the edit room is open")
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
