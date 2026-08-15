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
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES, maxmem=SCRYPT_MAXMEM,
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
        self._load()

    # -- storage ---------------------------------------------------------

    @staticmethod
    def default_path(workspace: Path) -> Path:
        """Beside the uploads, never inside the source tree."""
        return Path(workspace) / "accounts.json"

    def _load(self) -> None:
        if not self.path.is_file():
            return
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
        account = Account(username=username.strip(), email=email.strip().lower(),
                          salt=salt, password_hash=digest)
        with self.lock:
            self.accounts[account.username.lower()] = account
            self._save()
        return account

    def set_password(self, account: Account, password: str) -> None:
        salt, digest = hash_password(password)
        with self.lock:
            account.salt, account.password_hash = salt, digest
            account.failures, account.locked_until = 0, 0.0
            account.reset_hash, account.reset_expires = "", 0.0
            # Every other device is signed out. A password change is usually a
            # response to worrying that somebody else has it.
            self.sessions = {k: v for k, v in self.sessions.items()
                             if v[0] != account.username}
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
            if account.locked:
                wait = int((account.locked_until - time.time()) / 60) + 1
                return None, f"Too many tries. Try again in {wait} minute(s)."

        if not account.check(password):
            with self.lock:
                account.failures += 1
                if account.failures >= MAX_ATTEMPTS:
                    account.locked_until = time.time() + LOCKOUT_SECONDS
                    account.failures = 0
                self._save()
            return None, "That username and password do not match."

        token = secrets.token_urlsafe(32)
        with self.lock:
            account.failures, account.locked_until = 0, 0.0
            self.sessions[_token_hash(token)] = (account.username,
                                                 time.time() + SESSION_LIFETIME)
            self._save()
        return token, "Signed in."

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
                if (account.reset_hash
                        and hmac.compare_digest(account.reset_hash, digest)
                        and account.reset_expires > time.time()):
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

MIN_PASSWORD = 8


def password_problem(password: str) -> str:
    """Why this password will not do, in words a person can act on."""
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password.strip() != password:
        return "Do not start or end with a space — it is too easy to mistype."
    if password.lower() in ("password", "12345678", "qwertyui", "letmein1"):
        return "That is one of the first passwords anybody tries."
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
