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
import json
import logging
import mimetypes
import os
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import FORMATS, QUALITIES, Settings
from ..ui import Reporter, describe_count, describe_duration, describe_shape

log = logging.getLogger("auteur.web")

STATIC = Path(__file__).resolve().parent / "static"
#: Anything bigger than this is refused rather than swallowing the machine.
MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GB
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
PUBLIC_PATHS = frozenset({
    "/login", "/login.html", "/reset", "/manifest.webmanifest", "/sw.js",
    "/api/session", "/api/login", "/api/forgot", "/api/reset",
})
PUBLIC_PREFIXES = ("/static/",)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """One film being made, and everything the page needs to show about it."""

    id: str
    prompt: str
    folder: Path
    status: str = "queued"  # queued | running | done | error
    stage: str = "Getting ready"
    lines: list[dict[str, str]] = field(default_factory=list)
    percent: float = 0.0
    detail: str = ""
    error: str = ""
    video: Path | None = None
    facts: list[str] = field(default_factory=list)
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

    def result(self, **_: Any) -> None:  # the page renders its own ending
        pass

    def failure(self, headline: str, hint: str = "") -> None:
        with self.lock:
            self.job.lines.append({"kind": "warn", "text": headline})


class Studio:
    """Holds the jobs and runs them one at a time."""

    def __init__(self, workspace: Path, *, quality: str = "draft"):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.quality = quality
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # Rendering is CPU-bound; running two at once just makes both slower.
        self.queue_lock = threading.Lock()

    def create(self, prompt: str, shape: str, seconds: float | None) -> Job:
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        folder = self.workspace / job_id
        (folder / "clips").mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, prompt=prompt, folder=folder)
        job.thread = threading.Thread(target=self._run, args=(job, shape, seconds), daemon=True)
        with self.lock:
            self.jobs[job_id] = job
        return job

    def start(self, job: Job) -> None:
        """Split from create() so the clips are on disk before the agent looks."""
        if job.thread is not None:
            job.thread.start()

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _run(self, job: Job, shape: str, seconds: float | None) -> None:
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
                production = direct(
                    [job.folder / "clips"], job.prompt,
                    settings=settings, workspace=job.folder / "work",
                    formats=(fmt,), duration=seconds, reporter=reporter,
                )

                critique = production.final_critique
                facts = [
                    f"{describe_duration(production.edl.duration)}",
                    describe_count(len(production.edl.shots), "shot"),
                    describe_shape(fmt.width, fmt.height),
                ]
                if critique is not None:
                    facts.append(f"it rates itself {critique.score:.0%}")

                with self.lock:
                    job.video = production.primary
                    job.notes = production.workspace.root / "production-notes.md"
                    job.facts = facts
                    job.status = "done"
                    job.stage = "Your film is ready"
                    job.percent = 100.0
                    job.detail = ""
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
            stale = [job for job in self.jobs.values()
                     if job.created < cutoff and job.status in ("done", "error")]
            for job in stale:
                self.jobs.pop(job.id, None)
        for job in stale:
            shutil.rmtree(job.folder, ignore_errors=True)


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

def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """Pull fields and files out of a browser form post, using only stdlib."""
    parser = email.parser.BytesParser(policy=email.policy.default)
    message = parser.parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)

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


class Handler(BaseHTTPRequestHandler):
    server_version = "auteur"
    #: HTTP/1.1 so connections are kept alive. The page polls every second or
    #: so; a fresh TCP handshake per poll is pure latency.
    protocol_version = "HTTP/1.1"
    studio: Studio
    accounts: Any = None  # an auth.Accounts once serve() has run

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        log.debug("%s - %s", self.address_string(), fmt % args)

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
        if self.accounts is None:      # auth not configured: the library case
            return "anonymous"
        return self.accounts.session_user(self.session_token)

    def _set_session_cookie(self, token: str) -> str:
        # HttpOnly so a script cannot read it; SameSite=Strict so another site
        # cannot make the browser spend it. Secure only over HTTPS — setting it
        # on a plain LAN address would make the cookie silently not stick.
        bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
                f"Max-Age={SESSION_LIFETIME}"]
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            bits.append("Secure")
        return "; ".join(bits)

    def _clear_session_cookie(self) -> str:
        return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str, *, extra: dict | None = None) -> None:
        headers = dict(extra or {})
        # Text compresses by ~4x, and the shell is fetched over wifi from a
        # phone. Skip it for anything already compressed (png, mp4).
        if (len(body) > 512 and "gzip" in self.headers.get("Accept-Encoding", "")
                and _compressible(content_type)):
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8",
                   extra={"Cache-Control": "no-store"})

    def _static(self, path: Path, content_type: str | None = None) -> None:
        """Serve a shell file, revalidated by ETag.

        The page's own assets never change while the server is up, so answering
        the reload with a 304 costs one small round trip instead of resending
        the stylesheet and the script every time.
        """
        if not path.is_file():
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
        self._send(200, path.read_bytes(), guessed, extra={
            "ETag": etag,
            # Revalidate rather than trust: a restarted server may have a new
            # palette or a new script, and a stale shell is a confusing bug.
            "Cache-Control": "no-cache",
        })

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
        if path == "/api/session":
            self._json({"user": self.current_user()})
            return

        if path in ("/", "/index.html"):
            self._static(STATIC / "index.html", "text/html; charset=utf-8")
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

        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = self.studio.get(parts[2]) if len(parts) > 2 else None
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

    def _sign_in(self) -> None:
        payload = self._json_body()
        token, message = self.accounts.sign_in(
            str(payload.get("username", "")), str(payload.get("password", "")))
        if token is None:
            # 401 with the same wording whatever went wrong, so the response
            # never distinguishes "no such user" from "wrong password".
            self._json({"error": message}, 401)
            return
        user = self.accounts.session_user(token)
        self._send(200, json.dumps({"user": user}).encode(),
                   "application/json; charset=utf-8",
                   extra={"Set-Cookie": self._set_session_cookie(token),
                          "Cache-Control": "no-store"})

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

        problem = password_problem(password)
        if problem:
            self._json({"error": problem}, 400)
            return
        if not self.accounts.finish_reset(token, password):
            self._json({"error": "That link has expired. Ask for a new one."}, 400)
            return
        self._json({"ok": True, "message": "Password changed. You can sign in now."})

    def _site_root(self) -> str:
        host = self.headers.get("Host") or f"localhost:{self.server.server_address[1]}"
        scheme = self.headers.get("X-Forwarded-Proto", "http").lower()
        return f"{scheme}://{host}"

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

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]

        if path == "/api/login":
            self._sign_in()
            return
        if path == "/api/logout":
            self.accounts.sign_out(self.session_token)
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                       extra={"Set-Cookie": self._clear_session_cookie(),
                              "Cache-Control": "no-store"})
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
        if path != "/api/jobs":
            self._json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json({"error": "Nothing was sent."}, 400)
            return
        if length > MAX_UPLOAD:
            self._json({"error": "That is more footage than this can take at once."}, 413)
            return

        # Read in blocks: a phone posting a minute of 4K over wifi is a long,
        # interruptible transfer, and one giant read() hides that it stalled.
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            block = self.rfile.read(min(CHUNK, remaining))
            if not block:
                self._json({"error": "The upload stopped part way."}, 400)
                return
            chunks.append(block)
            remaining -= len(block)
        body = b"".join(chunks)

        try:
            fields, files = _parse_multipart(body, self.headers.get("Content-Type", ""))
        except Exception:  # noqa: BLE001 - a malformed post is the client's problem
            self._json({"error": "I could not read that upload."}, 400)
            return

        if not files:
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

        job = self.studio.create(prompt, fields.get("shape", "reel"), seconds)
        clips = job.folder / "clips"
        for index, (filename, payload) in enumerate(files):
            safe = Path(filename).name or f"clip{index}"
            safe = "".join(char for char in safe if char.isalnum() or char in "._- ")[-80:]
            (clips / f"{index:02d}-{safe or 'clip.mp4'}").write_bytes(payload)

        self.studio.start(job)
        self._json(job.snapshot(), 202)


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


def serve(host: str = "0.0.0.0", port: int = 8000, *, workspace: Path | None = None,
          quality: str = "draft", announce: bool = True) -> ThreadingHTTPServer:
    """Run the web app until interrupted."""
    from . import assets, seed
    from .auth import Accounts

    assets.ensure(STATIC)
    root = Path(workspace or Path.cwd() / "auteur-web")
    Handler.studio = Studio(root, quality=quality)
    Handler.accounts = Accounts(Accounts.default_path(root))
    created = seed.bootstrap(Handler.accounts)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

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
        if created:
            print()
            print(f"     Sign in as        {created}")
        print()
        print("     Press Ctrl-C to close it.")
        print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if announce:
            print("\n  closed.\n")
    finally:
        server.server_close()
    return server
