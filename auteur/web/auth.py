"""Accounts, sessions and password resets.

Small on purpose. This guards one person's footage on one home network, so it
needs to be correct rather than elaborate: passwords hashed with scrypt,
sessions as opaque random tokens in an HttpOnly cookie, a lockout that makes
guessing pointless, and a reset flow that does not tell strangers which
accounts exist.

Nothing here stores a password. The account file holds a salt and a scrypt
hash, and it is written into the *workspace*, not the repository — see
`Accounts.default_path`. A repository is copied, forked and cloned; a
credential that lives in one eventually lives everywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import totp

log = logging.getLogger("auteur.web.auth")

#: scrypt parameters. n=2^15 costs roughly 100ms and ~32MB per attempt, which
#: is invisible on a login and ruinous on a dictionary.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
#: 128 * n * r bytes, which for these parameters is a shade over 32MB — exactly
#: OpenSSL's default ceiling, so it refuses without being told otherwise.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

#: How long a signed-in phone stays signed in. Long, because re-typing a
#: password on a phone is the main reason people stop using a thing.
SESSION_LIFETIME = 30 * 24 * 3600
#: Reset links are short-lived; a link in an inbox is a key lying on a table.
RESET_LIFETIME = 30 * 60

#: After this many wrong passwords the account stops answering for a while.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 900


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for a password."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        maxmem=SCRYPT_MAXMEM,
    )
    return salt.hex(), digest.hex()


def _token_hash(token: str) -> str:
    """Session and reset tokens are stored hashed.

    They are bearer credentials: whoever holds one is signed in. Keeping only
    a hash means a leaked account file cannot be replayed.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Account:
    username: str
    email: str
    salt: str
    password_hash: str
    created: float = field(default_factory=time.time)
    #: Wrong-password attempts since the last success, and when to stop refusing.
    failures: int = 0
    locked_until: float = 0.0
    #: sha256 of the outstanding reset token, and when it expires.
    reset_hash: str = ""
    reset_expires: float = 0.0
    #: Two-step verification. `totp_secret` exists as soon as somebody starts
    #: setting it up; `totp_on` only once they have proved they can produce a
    #: code from it, so a half-finished setup can never lock anybody out.
    totp_secret: str = ""
    totp_on: bool = False
    #: sha256 of each unspent recovery code.
    recovery: list = field(default_factory=list)
    #: The last time-step spent, so one code cannot be used twice inside its
    #: own window by whoever read it over a shoulder.
    totp_last_step: int = 0

    #: The secret in this person's calendar subscription URL. A calendar app is
    #: not a browser and will not sign in, so the link *is* the credential —
    #: which makes it a capability: long, unguessable, per person, and
    #: rollable. Empty until somebody asks for their calendar.
    calendar_token: str = ""

    def check(self, password: str) -> bool:
        _, candidate = hash_password(password, bytes.fromhex(self.salt))
        return hmac.compare_digest(candidate, self.password_hash)

    @property
    def locked(self) -> bool:
        return time.time() < self.locked_until


class Accounts:
    """The account file, and the live sessions it protects."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.accounts: dict[str, Account] = {}
        #: token hash -> (username, expiry)
        self.sessions: dict[str, tuple[str, float]] = {}
        #: Half-finished sign-ins: the password was right and the second step
        #: is owed. In memory only — a sign-in interrupted by a restart should
        #: start again rather than resume.
        self._tickets: dict[str, tuple[str, float]] = {}
        #: mtime of the file as we last read or wrote it, for `refresh()`.
        self._stamp = 0.0
        self._load()

    # -- storage ---------------------------------------------------------

    @staticmethod
    def default_path(workspace: Path) -> Path:
        """Beside the uploads, never inside the source tree."""
        return Path(workspace) / "accounts.json"

    def refresh(self) -> None:
        """Re-read the file if something else has written to it.

        `auteur account add` and `auteur account password` edit the same file
        while the server is running. Without this the server keeps serving the
        set it read at start-up, and a password change appears to do nothing —
        the confusing kind of nothing, where the old password still works.
        """
        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            return
        if stamp == self._stamp:
            return
        with self.lock:
            live = dict(self.sessions)
            self.accounts.clear()
            self._load()
            # Keep the sessions we already hold. The CLI writes back whatever
            # session list it happened to read, so taking the file's copy here
            # would sign out a phone that signed in a moment ago.
            live.update(self.sessions)
            self.sessions = live

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            self._stamp = self.path.stat().st_mtime
        except OSError:
            self._stamp = 0.0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); starting with no accounts", self.path, exc)
            return
        for record in raw.get("accounts", []):
            try:
                account = Account(**record)
            except TypeError:
                log.warning("skipping malformed account record")
                continue
            self.accounts[account.username.lower()] = account
        now = time.time()
        self.sessions = {
            key: (name, expiry)
            for key, (name, expiry) in (raw.get("sessions") or {}).items()
            if expiry > now
        }

    def _save(self) -> None:
        """Write atomically, and keep the file to the owner."""
        payload = {
            "accounts": [asdict(account) for account in self.accounts.values()],
            "sessions": {key: list(value) for key, value in self.sessions.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass  # Windows, or a filesystem without permissions
        temporary.replace(self.path)
        # Remember our own write, so refresh() does not immediately undo it.
        try:
            self._stamp = self.path.stat().st_mtime
        except OSError:
            self._stamp = 0.0

    # -- accounts --------------------------------------------------------

    def get(self, who: str) -> Account | None:
        """By username or by email — people remember whichever they used."""
        key = (who or "").strip().lower()
        if not key:
            return None
        account = self.accounts.get(key)
        if account is not None:
            return account
        return next((a for a in self.accounts.values() if a.email.lower() == key), None)

    def add(self, username: str, email: str, password: str) -> Account:
        salt, digest = hash_password(password)
        account = Account(
            username=username.strip(), email=email.strip().lower(), salt=salt, password_hash=digest
        )
        with self.lock:
            self.accounts[account.username.lower()] = account
            self._save()
        return account

    def calendar_token(self, username: str, *, roll: bool = False) -> str:
        """This person's calendar secret, made on first ask.

        Not derived from the username or the password — a derived token cannot
        be rolled without changing the thing it is derived from, and the reason
        to roll one is that it has been shared with somebody it should not have
        been.
        """
        with self.lock:
            account = self.accounts.get(username.lower())
            if account is None:
                return ""
            if roll or not account.calendar_token:
                account.calendar_token = secrets.token_urlsafe(24)
                self._save()
            return account.calendar_token

    def by_calendar_token(self, token: str) -> Account | None:
        """Whose calendar this is. Compared in constant time.

        A short-circuiting comparison over a set of secrets leaks how much of a
        guess was right, one character at a time.
        """
        if not token:
            return None
        with self.lock:
            for account in self.accounts.values():
                if account.calendar_token and hmac.compare_digest(account.calendar_token, token):
                    return account
        return None

    def set_password(self, account: Account, password: str) -> None:
        salt, digest = hash_password(password)
        with self.lock:
            account.salt, account.password_hash = salt, digest
            account.failures, account.locked_until = 0, 0.0
            account.reset_hash, account.reset_expires = "", 0.0
            # Every other device is signed out. A password change is usually a
            # response to worrying that somebody else has it.
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] != account.username}
            self._save()

    @property
    def empty(self) -> bool:
        return not self.accounts

    # -- signing in ------------------------------------------------------

    def sign_in(self, who: str, password: str) -> tuple[str | None, str]:
        """Return (session token, message). The token is None on failure."""
        account = self.get(who)
        if account is None:
            # Spend the time anyway. Answering instantly for unknown names and
            # slowly for real ones tells an attacker which is which.
            hash_password(password)
            return None, "That username and password do not match."

        with self.lock:
            locked_until = account.locked_until if account.locked else 0.0

        # Check the password even while locked. Not to let anyone in — a locked
        # account never signs in below, whatever it was given — but because
        # "Too many tries" is otherwise an answer to a question nobody asked.
        # Say it only to someone who already knows the password, which is the
        # owner wondering why they are stuck; anyone spraying names gets the
        # same sentence they would get for a name that does not exist.
        correct = account.check(password)

        if locked_until:
            if correct:
                wait = int((locked_until - time.time()) / 60) + 1
                return None, f"Too many tries. Try again in {wait} minute(s)."
            return None, "That username and password do not match."

        if not correct:
            with self.lock:
                account.failures += 1
                if account.failures >= MAX_ATTEMPTS:
                    account.locked_until = time.time() + LOCKOUT_SECONDS
                    account.failures = 0
                self._save()
            return None, "That username and password do not match."

        with self.lock:
            account.failures, account.locked_until = 0, 0.0
            self._save()
            owed = account.totp_on

        if owed:
            # The password was right and that is not enough. What comes back is
            # a ticket, not a session: it names the account, expires in a few
            # minutes, is spent by being used, and can do nothing else — so a
            # stolen one is worth nothing without the code it is waiting for.
            return None, "code:" + self.open_ticket(account.username)
        return self.open_session(account.username), "Signed in."

    #: How long somebody has to fetch a code from their phone.
    TICKET_LIFETIME = 300

    def open_ticket(self, username: str) -> str:
        token = secrets.token_urlsafe(24)
        with self.lock:
            self._tickets[_token_hash(token)] = (username, time.time() + self.TICKET_LIFETIME)
            self._sweep_tickets()
        return token

    def spend_ticket(self, token: str) -> str | None:
        """Whose half-finished sign-in this is. Once."""
        if not token:
            return None
        with self.lock:
            self._sweep_tickets()
            found = self._tickets.pop(_token_hash(token), None)
        if found is None:
            return None
        username, expiry = found
        return username if expiry > time.time() else None

    def _sweep_tickets(self) -> None:
        now = time.time()
        for key in [k for k, (_, expiry) in self._tickets.items() if expiry < now]:
            self._tickets.pop(key, None)

    # -- two-step verification -------------------------------------------

    def begin_totp(self, username: str) -> str:
        """A secret to set up with, not yet switched on."""
        with self.lock:
            account = self.accounts.get(username.lower())
            if account is None:
                return ""
            if not account.totp_secret or account.totp_on:
                account.totp_secret = totp.new_secret()
                self._save()
            return account.totp_secret

    def confirm_totp(self, username: str, code: str) -> list[str] | None:
        """Switch it on, once they have proved they can produce a code.

        Returns the recovery codes, once, in the clear — the only time they
        exist in readable form. Returns None if the code was wrong, which
        leaves the feature off rather than half on.
        """
        with self.lock:
            account = self.accounts.get(username.lower())
            if account is None or not account.totp_secret:
                return None
            step = totp.check(account.totp_secret, code)
            if step is None:
                return None
            plain = totp.new_recovery_codes()
            account.recovery = [totp.hash_recovery(c) for c in plain]
            account.totp_last_step = step
            account.totp_on = True
            self._save()
            return plain

    def disable_totp(self, username: str, password: str) -> bool:
        """Turn it off, and only with the password.

        Asking for the password again is the point: a borrowed unlocked phone
        with a live session should not be able to remove the factor that
        protects the account it is signed in to.
        """
        with self.lock:
            account = self.accounts.get(username.lower())
        if account is None or not account.check(password):
            return False
        with self.lock:
            account.totp_on = False
            account.totp_secret = ""
            account.recovery = []
            account.totp_last_step = 0
            self._save()
        return True

    def second_step(self, username: str, given: str) -> bool:
        """Check a code, or a recovery code. Either is spent by using it."""
        with self.lock:
            account = self.accounts.get(username.lower())
            if account is None or not account.totp_on:
                return False

            step = totp.check(account.totp_secret, given)
            if step is not None:
                # Single use. Without this a code is good for its whole window
                # however many times it is presented.
                if step <= account.totp_last_step:
                    return False
                account.totp_last_step = step
                self._save()
                return True

            spent = totp.spend_recovery(account.recovery, given)
            if spent is None:
                return False
            account.recovery = [h for h in account.recovery if h != spent]
            self._save()
            return True

    def open_session(self, username: str) -> str:
        """A session for somebody whose identity has already been established.

        Extracted from `sign_in` rather than copied so there is one place a
        session comes into existence. **This does not authenticate anybody** —
        it is the step *after* authentication, and every caller is responsible
        for having done that first: `sign_in` checks a password, and the
        identity-provider route checks a verified email against an account that
        already exists. A third caller that skips that is an open door.
        """
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[_token_hash(token)] = (username, time.time() + SESSION_LIFETIME)
            self._save()
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        with self.lock:
            found = self.sessions.get(_token_hash(token))
            if found is None:
                return None
            username, expiry = found
            if expiry < time.time():
                self.sessions.pop(_token_hash(token), None)
                self._save()
                return None
            return username

    def sign_out(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            if self.sessions.pop(_token_hash(token), None) is not None:
                self._save()

    # -- forgotten passwords ---------------------------------------------

    def begin_reset(self, who: str) -> tuple[Account, str] | None:
        """Start a reset. Returns (account, token), or None if there is no such
        account — which the caller must not reveal."""
        account = self.get(who)
        if account is None:
            return None
        token = secrets.token_urlsafe(32)
        with self.lock:
            account.reset_hash = _token_hash(token)
            account.reset_expires = time.time() + RESET_LIFETIME
            self._save()
        return account, token

    def account_for_reset(self, token: str) -> Account | None:
        if not token:
            return None
        digest = _token_hash(token)
        with self.lock:
            for account in self.accounts.values():
                if (
                    account.reset_hash
                    and hmac.compare_digest(account.reset_hash, digest)
                    and account.reset_expires > time.time()
                ):
                    return account
        return None

    def finish_reset(self, token: str, password: str) -> bool:
        account = self.account_for_reset(token)
        if account is None:
            return False
        self.set_password(account, password)
        return True


# ---------------------------------------------------------------------------
# Password rules
# ---------------------------------------------------------------------------

#: Length is the only property that reliably buys anything. Modern guidance
#: (NIST 800-63B) is to require length and screen against known-bad choices,
#: and to drop composition rules — forcing a symbol mostly produces
#: "Password1!", which is on every list there is.
MIN_PASSWORD = 12

#: The shapes people actually reach for. Checked as substrings, because
#: "mypassword2024" is no better than "password".
_WEAK_PATTERNS = (
    "password",
    "qwerty",
    "letmein",
    "welcome",
    "admin",
    "iloveyou",
    "monkey",
    "dragon",
    "abc123",
    "123456",
    "111111",
    "changeme",
    "auteur",
)


def password_problem(password: str, *, username: str = "", email: str = "") -> str:
    """Why this password will not do, in words a person can act on.

    Returns "" when it is fine. The wording matters as much as the rule: a
    refusal that does not say what would work sends people to `Password1!`.
    """
    password = password or ""
    if len(password) < MIN_PASSWORD:
        return (
            f"Use at least {MIN_PASSWORD} characters. "
            "Several ordinary words in a row beat one clever word."
        )
    if password.strip() != password:
        return "Do not start or end with a space — it is too easy to mistype."

    lowered = password.lower()
    if any(pattern in lowered for pattern in _WEAK_PATTERNS):
        return "That contains something on every guessing list. Try unrelated words."
    if len(set(lowered)) < 5:
        return "That repeats too few different characters to be worth much."

    # A password built from the account's own name is the second thing tried.
    for own in (username or "", (email or "").split("@")[0]):
        if len(own) >= 4 and own.lower() in lowered:
            return "Do not build it out of your own username or email."
    return ""
    return ""


# ---------------------------------------------------------------------------
# Delivering the reset link
# ---------------------------------------------------------------------------


def send_reset(email: str, link: str) -> str:
    """Get the reset link to its owner. Returns how it was delivered.

    Email when SMTP is configured, and the server's own console otherwise —
    which is not a fallback so much as the honest default for a tool you run on
    your own machine: the person who can read that console is the person who
    owns the account.
    """
    host = os.environ.get("AUTEUR_SMTP_HOST", "").strip()
    if not host:
        print()
        print("  ┌─ password reset ──────────────────────────────────────────")
        print(f"  │  for: {email}")
        print("  │  open this within 30 minutes:")
        print(f"  │  {link}")
        print("  └───────────────────────────────────────────────────────────")
        print(flush=True)
        return "console"

    import smtplib
    from email.message import EmailMessage

    port = int(os.environ.get("AUTEUR_SMTP_PORT", "587"))
    user = os.environ.get("AUTEUR_SMTP_USER", "")
    password = os.environ.get("AUTEUR_SMTP_PASSWORD", "")
    sender = os.environ.get("AUTEUR_SMTP_FROM", user or f"auteur@{host}")

    message = EmailMessage()
    message["Subject"] = "Reset your Auteur password"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "Someone asked to reset the password on your Auteur account.\n\n"
        f"Open this link within 30 minutes:\n\n{link}\n\n"
        "If it was not you, you can ignore this — nothing has changed.\n"
    )

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if port != 465:
                smtp.starttls()
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - never fail a reset over the mailer
        log.error("could not send the reset email (%s); printing it instead", exc)
        return send_reset_to_console(email, link)
    return "email"


def send_reset_to_console(email: str, link: str) -> str:
    saved = os.environ.pop("AUTEUR_SMTP_HOST", None)
    try:
        return send_reset(email, link)
    finally:
        if saved is not None:
            os.environ["AUTEUR_SMTP_HOST"] = saved
