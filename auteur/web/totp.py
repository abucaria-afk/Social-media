"""Two-step verification, to the standard every authenticator app implements.

RFC 6238 time-based one-time passwords, and RFC 4648 base32 to carry the
secret. Both are small enough to write against the specification rather than
take a dependency for, which matters here: this project has two dependencies on
purpose, and an authentication library is the last place to accept a fourth
party you have not read.

What it is protecting against is specific. A password on this app is checked by
a server on somebody's own machine, with a lockout after five wrong tries — so
the risk is not a remote attacker guessing it. It is a password that has been
reused somewhere else and leaked from there, which is how nearly every account
anywhere is actually lost. A second factor makes a leaked password insufficient
on its own, and that is the whole of what it does.

Three decisions worth stating:

* **A window of one step either side.** Phone clocks drift and people finish
  typing a code as it rolls over. Zero tolerance means a correct code is
  refused often enough that people turn the feature off; a wide window is a
  longer replay opportunity. One step each way is 90 seconds of validity, which
  is what the RFC suggests and what every implementation does.
* **Codes are single use.** Without that, a code is valid for its whole window
  no matter how many times it is presented, so anyone who sees one over a
  shoulder has 30 seconds to use it too.
* **Recovery codes are stored hashed**, like passwords, because that is what
  they are: they let somebody in. Storing them in the clear would mean the file
  that survives a lost phone is also the file that replaces the second factor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

#: How long one code lasts, in seconds. Thirty is the value every authenticator
#: app assumes and none of them let you change.
STEP = 30

#: Digits in a code. Six, for the same reason.
DIGITS = 6

#: How many steps either side of now are accepted. One: 90 seconds of validity
#: in total, which absorbs a drifting phone clock and a slow typist without
#: leaving a code usable long enough to be worth stealing.
DRIFT = 1

#: How many recovery codes are issued, and how long each is. Ten is enough to
#: survive losing a phone more than once; twelve characters of a 32-symbol
#: alphabet is 60 bits, which is not guessable.
RECOVERY_CODES = 10
RECOVERY_LENGTH = 12

#: Crockford-ish: no I, L, O, U — the characters people mistype when reading a
#: code off a screen and typing it on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def new_secret() -> str:
    """A fresh shared secret, base32 as every authenticator app expects it."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def code_at(secret: str, moment: float | None = None, *, step: int = 0) -> str:
    """The code for a point in time. `step` moves whole windows."""
    counter = int((moment if moment is not None else time.time()) // STEP) + step
    padded = secret + "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception:  # noqa: BLE001 - a malformed secret verifies nothing
        return ""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    chunk = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(chunk % (10**DIGITS)).zfill(DIGITS)


def check(secret: str, given: str, *, moment: float | None = None) -> int | None:
    """Which step a code belongs to, or None.

    Returns the *counter* rather than True so the caller can remember it and
    refuse the same code twice — a code that stays valid for its whole window
    however many times it is presented is one that can be reused by anybody who
    read it over a shoulder.

    Compared in constant time. A digit-by-digit comparison on a six digit code
    is a small leak and an entirely avoidable one.
    """
    given = "".join(ch for ch in (given or "") if ch.isdigit())
    if len(given) != DIGITS or not secret:
        return None
    now = moment if moment is not None else time.time()
    for step in range(-DRIFT, DRIFT + 1):
        if hmac.compare_digest(code_at(secret, now, step=step), given):
            return int(now // STEP) + step
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str = "Auteur") -> str:
    """The `otpauth://` URI an authenticator app registers itself for.

    On a phone this is a tappable link that opens the authenticator directly,
    which is the whole of what a QR code does — and a QR code would mean either
    a dependency or four hundred lines of encoder for something the device can
    already do.
    """
    label = urllib.parse.quote(f"{issuer}:{account}", safe="")
    query = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def readable(secret: str) -> str:
    """The secret in groups of four, for typing in by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def new_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """Codes for getting back in after losing the phone."""
    return [
        "".join(secrets.choice(_ALPHABET) for _ in range(RECOVERY_LENGTH)) for _ in range(count)
    ]


def hash_recovery(code: str) -> str:
    """Stored hashed, because a recovery code is a password by another name.

    Plain sha256 rather than the slow hash used for passwords, and that is a
    deliberate difference rather than an oversight: these are twelve random
    characters from a thirty-symbol alphabet, so there is no dictionary to run
    and nothing for a work factor to slow down. What matters is that the file
    which survives a lost phone is not also the file that replaces the second
    factor.
    """
    return hashlib.sha256(normalise(code).encode("utf-8")).hexdigest()


def normalise(code: str) -> str:
    """Upper case, no spaces or dashes — however somebody typed it."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def spend_recovery(stored: list[str], given: str) -> str | None:
    """The hash this code matches, or None. The caller removes it.

    Removing it is not optional: a recovery code that works twice is a password
    that never changes.
    """
    wanted = hash_recovery(given)
    for known in stored:
        if hmac.compare_digest(known, wanted):
            return known
    return None
