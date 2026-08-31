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

from . import billing, totp

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

#: The youngest this app is for, and the age it stops treating somebody as a
#: minor. Twelve because that is the App Store rating the app is submitted
#: under — an app rated 12+ that lets an eight-year-old make an account is one
#: whose rating is a claim rather than a fact, and the questionnaire answer and
#: this constant have to agree. `tools/appstore/preflight.py` checks that they
#: do.
MINIMUM_AGE = 12
ADULT_AGE = 18

#: A restriction is lifted with a code, and the code is four digits because it
#: is typed by whoever set it in front of the person it restricts. It is not a
#: password: it stops the person it applies to from turning it off, and it is
#: not standing against somebody with the account file.
LOCK_DIGITS = 4


def age_from(born: int, *, now: float | None = None) -> int:
    """How old somebody is, from a year of birth. -1 if they have not said.

    A year rather than a date, and the reason is worth stating: a year is the
    least that answers both questions this app has — "are they old enough" and
    "are they still a minor" — and it stays right as time passes, which a
    stored yes/no would not. A full date of birth would be more data for no
    more answers.

    It reads one year young for anybody whose birthday has not come round yet,
    which is the direction to be wrong in.
    """
    if not born:
        return -1
    year = time.gmtime(now if now is not None else time.time()).tm_year
    return max(0, year - int(born))


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

    #: Year of birth, or 0 for an account made before this was asked. See
    #: `age_from` for why it is a year and not a date. It never leaves the
    #: machine holding the accounts — there is nowhere for it to go — so the
    #: App Privacy answer stays "data not collected", which is about what the
    #: *developer* receives and not about what a file on your own computer
    #: holds.
    born: int = 0

    #: What this account has paid for: a key from `auteur/pricing.py`, via
    #: `billing.PLANS`. Written only by a Stripe webhook whose signature
    #: verified — there is no route a browser can call to change it, which is
    #: what makes it mean "has paid" rather than "has asked".
    plan: str = billing.DEFAULT_PLAN
    #: When the paid plan lapses, as a unix time. 0 means "does not lapse",
    #: which is the honest answer for the free tier and the wrong one for a
    #: paid tier — `entitled` treats a paid plan with no end date as expired
    #: rather than as permanent, so a half-written grant fails closed.
    plan_until: float = 0.0
    #: The Stripe customer this account is. Empty until somebody buys
    #: something. It is how the subscription events that follow a checkout —
    #: which carry no username — find their way back to a person.
    stripe_customer: str = ""

    #: "none" or "limited". Limited hides films their author or the operator
    #: has marked sensitive, and films with a report nobody has looked at yet.
    restriction: str = "none"
    #: sha256 of the code that lifts it. Empty means it can be lifted without
    #: one, which is right for an adult who turned it on for themselves and
    #: wrong for a restriction somebody else set.
    restriction_lock: str = ""

    def check(self, password: str) -> bool:
        _, candidate = hash_password(password, bytes.fromhex(self.salt))
        return hmac.compare_digest(candidate, self.password_hash)

    @property
    def locked(self) -> bool:
        return time.time() < self.locked_until

    @property
    def age(self) -> int:
        """Years old, or -1 if this account never said."""
        return age_from(self.born)

    @property
    def minor(self) -> bool:
        """Under eighteen, as far as this account has said.

        An account that never said is *not* treated as a minor. That is the
        deliberate direction: accounts made before this was asked belong to
        whoever was already using the instance, and silently restricting them
        would be a change nobody asked for landing on people's own footage.
        """
        age = self.age
        return 0 <= age < ADULT_AGE

    @property
    def restricted(self) -> bool:
        return self.restriction == "limited"

    @property
    def paying(self) -> bool:
        """Whether a paid plan is in force *right now*.

        Two things have to hold, and the second is the one that is easy to
        leave out: the plan is not the free tier, and it has not run out. A
        check that asks only the first keeps somebody on Studio for ever the
        moment a webhook is missed, which is the expensive direction.

        A paid plan with `plan_until == 0` reads as expired. That state should
        not exist — every grant this software writes sets an end date — so if
        it does exist something wrote a plan without one, and the safe reading
        of a half-written grant is that it is not a grant.
        """
        if self.plan == billing.DEFAULT_PLAN:
            return False
        return 0 < self.plan_until and time.time() < self.plan_until

    @property
    def tier(self):
        """The `pricing.Tier` this account is actually on, never None.

        Derived rather than stored: the tier's name, price and feature list
        live in `pricing` and would go stale the moment they were copied here.
        An account whose paid plan has lapsed reports the free tier, because
        that is what it can currently use.
        """
        from .. import pricing

        if not self.paying:
            return pricing.FREE
        for candidate in pricing.TIERS:
            if candidate.key == self.plan:
                return candidate
        return pricing.FREE


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
        #: Whether anybody else may make an account here, and the code they
        #: need. Instance-level rather than per-account, and in the same file
        #: as the accounts because it is the same secret at the same risk —
        #: one lock, one atomic write, one thing to keep at 0600.
        self.invite: dict = {"open": False, "code": ""}
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
        stored = raw.get("invite")
        if isinstance(stored, dict):
            self.invite = {
                "open": bool(stored.get("open")),
                "code": str(stored.get("code") or ""),
            }
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
            "invite": dict(self.invite),
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

    def add(self, username: str, email: str, password: str, born: int = 0) -> Account:
        """Make an account. `born` is a year, or 0 for "did not say".

        An account for somebody under eighteen starts restricted. Not as a
        judgement about them — as the direction to be wrong in, since the
        restriction can be lifted in two taps by whoever should be lifting it
        and cannot be applied retroactively to something already seen.
        """
        salt, digest = hash_password(password)
        account = Account(
            username=username.strip(),
            email=email.strip().lower(),
            salt=salt,
            password_hash=digest,
            born=int(born or 0),
        )
        if account.minor:
            account.restriction = "limited"
        with self.lock:
            self.accounts[account.username.lower()] = account
            self._save()
        return account

    # -- who may join ----------------------------------------------------

    def joining(self) -> dict:
        """Whether anybody else may make an account here, and how.

        Sign-up closed itself permanently the moment the first account
        existed, and the reason was sound: this app serves somebody's own
        camera roll over their own wifi, so an open door is an open door to
        the footage. What that reason did not survive is the app growing a
        feed, an inbox and profiles at shareable addresses — every one of
        which needs a second person, and the only way to get one was
        `auteur account add` typed on the machine's terminal by somebody who
        is not the person joining.

        So it is a decision the owner makes rather than a fact of the build,
        and it is off until they make it. A code by default because a public
        deployment has a public address: "open" with no code is an open
        registration endpoint on the internet, which is not what somebody
        letting a friend in is agreeing to.
        """
        return {"open": bool(self.invite.get("open")), "needs_code": bool(self.invite.get("code"))}

    def open_joining(self, *, with_code: bool = True) -> str:
        """Let others join. Returns the code they need, or "" for none.

        Rolls a fresh code every time it is called, so re-opening after
        closing does not reinstate a code that has been passed around.
        """
        with self.lock:
            self.invite = {
                "open": True,
                "code": secrets.token_urlsafe(9) if with_code else "",
            }
            self._save()
            return self.invite["code"]

    def close_joining(self) -> None:
        with self.lock:
            self.invite = {"open": False, "code": ""}
            self._save()

    def invite_code(self) -> str:
        """The current code, for showing to whoever is allowed to see it."""
        return str(self.invite.get("code") or "")

    def may_join(self, code: str) -> bool:
        """Whether this code opens the door right now.

        Compared in constant time: a code short enough to be read down a
        phone is short enough to be worth guessing, and an early return on
        the first wrong character is a measurable difference.
        """
        if self.empty:
            # The first account has always been allowed to claim an unclaimed
            # copy, and that is unchanged.
            return True
        if not self.invite.get("open"):
            return False
        wanted = str(self.invite.get("code") or "")
        if not wanted:
            return True
        return secrets.compare_digest(wanted, str(code or ""))

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

    # -- what somebody is allowed to see ---------------------------------

    def set_restriction(self, username: str, on: bool) -> bool:
        """Turn the content restriction on or off. True if there was an account.

        Not guarded here. The guard is the *code*, and it belongs to whoever
        is asking — the server checks it before calling this, and the CLI is
        the operator, who does not need to ask themselves for permission.
        """
        with self.lock:
            account = self.accounts.get((username or "").strip().lower())
            if account is None:
                return False
            account.restriction = "limited" if on else "none"
            self._save()
            return True

    def apply_grant(self, grant) -> str:
        """Put a verified entitlement change onto an account.

        Returns the username it landed on, or "" if it landed on nobody. A
        grant for a customer this copy has never seen is not an error — the
        same Stripe account can serve more than one instance, and an event
        about somebody else's customer is correctly a no-op here.

        The two ways an event finds an account are deliberately different.
        A checkout names the person, because the checkout was opened with
        their username in `client_reference_id`; every later event knows only
        the Stripe customer, and is matched by the id the checkout stored. So
        a subscription that was never checked out through this copy cannot
        attach itself to an account by guessing a name.
        """
        with self.lock:
            account = None
            if grant.username:
                account = self.accounts.get(grant.username.strip().lower())
            if account is None and grant.customer:
                account = next(
                    (
                        a
                        for a in self.accounts.values()
                        if a.stripe_customer and a.stripe_customer == grant.customer
                    ),
                    None,
                )
            if account is None:
                return ""
            if grant.plan not in billing.PLANS:
                # A plan this copy does not sell is not a plan. Refusing is
                # the safe direction: the alternative is writing a string
                # nobody can interpret into the field that decides what
                # somebody is allowed to do.
                return ""
            account.plan = grant.plan
            account.plan_until = float(grant.until or 0.0)
            if grant.customer:
                account.stripe_customer = grant.customer
            self._save()
            return account.username

    def set_restriction_lock(self, username: str, code: str) -> str:
        """Set or clear the code that lifts a restriction. "" if it worked.

        Stored hashed, like everything else in this file that lets somebody
        do something. Four digits is not a password and is not pretending to
        be one: it stands between the person the restriction applies to and
        the switch that turns it off, which is exactly as much as a code typed
        in front of them can ever do.
        """
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        with self.lock:
            account = self.accounts.get((username or "").strip().lower())
            if account is None:
                return "no account by that name"
            if not code:
                account.restriction_lock = ""
                self._save()
                return ""
            if len(code) != LOCK_DIGITS:
                return f"{LOCK_DIGITS} digits, please"
            account.restriction_lock = _token_hash(code)
            self._save()
            return ""

    def check_restriction_lock(self, username: str, code: str) -> bool:
        """Does this code lift the restriction? True when there is no code set.

        Compared in constant time. Four digits is a small enough space that a
        timing leak is not the way in, but a hand-rolled `==` on a secret is
        the habit this file does not want to start.
        """
        account = self.get(username)
        if account is None:
            return False
        if not account.restriction_lock:
            return True
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        return bool(code) and hmac.compare_digest(account.restriction_lock, _token_hash(code))

    def remove(self, username: str) -> bool:
        """Delete an account and every session it holds. True if there was one.

        The App Store requires this — an app that can make an account has to be
        able to unmake one from inside itself, and pointing somebody at an
        email address is explicitly not enough (guideline 5.1.1(v)). It is also
        just correct: this program's whole claim is that your footage is yours,
        and "yours" has to include being able to take it back.

        What it does *not* do is the rest of the deletion. Films, messages,
        profile and pictures live in their own stores and are removed by the
        caller, because this class knows nothing about them and an account
        store that reached into three other files would be the wrong shape. The
        server has one place that does all of it — see `_delete_account`.

        Sessions go first in the same lock: an account removed while a token
        for it is still live is an account somebody is still signed in to.
        """
        key = (username or "").strip().lower()
        with self.lock:
            account = self.accounts.pop(key, None)
            if account is None:
                return False
            self.sessions = {k: v for k, v in self.sessions.items() if v[0].lower() != key}
            self._save()
        log.info("account removed")
        return True

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
