"""The account this instance starts with.

The first time `auteur serve` runs against an empty workspace it creates one
account, so there is something to sign in as. After that this module is never
consulted again — the real accounts live in `<workspace>/accounts.json`, which
is outside the repository and gitignored.

**No credential material lives here.** Not a password, and not a hash of one
either. An earlier version shipped a salt and a scrypt hash so that a known
password would work out of the box; that was a defensible trade while the
repository was private and a bad one the moment it was not. scrypt makes
guessing expensive, but "expensive" is a budget, and a short password behind a
public hash is a matter of time rather than of possibility.

So the first run mints a password nobody has ever seen and prints it once.
Whoever can read that console is the person who owns the machine the renders
happen on, which is the same trust boundary the password-reset link already
relies on.

Set your own instead, and nothing is ever generated:

    AUTEUR_USERNAME=... AUTEUR_EMAIL=... AUTEUR_PASSWORD=... python -m auteur serve
"""

from __future__ import annotations

import os
import secrets

#: Who the first account belongs to. A username and an email are not secrets;
#: they are here so the instance has an identity out of the box.
SEED_USERNAME = "streetlightseason"
SEED_EMAIL = "streetlightseason@gmail.com"

#: Words for a generated password. Short, unambiguous, distinct, and easy to
#: read off a screen and type on a phone once — after which it should be
#: changed to something memorable with `auteur account password`.
#:
#: Exactly 64 of them, which is not decoration: 64 is 2**6, so every word drawn
#: is worth precisely six bits and the arithmetic below is exact rather than
#: approximate. A test asserts the count and that none repeats — a duplicate
#: would quietly bias the draw and cost real entropy.
_WORDS = (
    "amber ember cinder lantern beacon harbour meadow thicket bramble willow "
    "cobalt indigo saffron russet umber slate quartz basalt pebble driftwood "
    "kestrel heron marten otter badger ferret raven swallow plover curlew "
    "compass anchor rudder tiller mizzen halyard spindle bellows anvil furnace "
    "cedar birch rowan alder hazel juniper bracken heather cavern granite "
    "lichen thistle marram estuary shingle tideline sextant vellum kelp dune "
    "aurora meridian solstice zenith"
).split()

#: Words per generated password. Five at six bits each is 30 bits, plus just
#: over 13 from the four digits: ~43 bits. Online that is unreachable — five
#: wrong answers locks the account for fifteen minutes. Offline, against the
#: scrypt cost this project uses (~0.1s a guess), it is thousands of years of
#: single-machine work. It is also five words, which is still typable once.
_WORD_COUNT = 5


def generate_password(words: int = _WORD_COUNT) -> str:
    """A password worth having: five random words and a number.

    `secrets.choice` does the work — a CSPRNG, drawn fresh, never reused, and
    never written down anywhere a stranger can read it. Anyone who wants a
    different one sets AUTEUR_PASSWORD, which is held to the same rules.
    """
    picked = "-".join(secrets.choice(_WORDS) for _ in range(words))
    return f"{picked}-{secrets.randbelow(9000) + 1000}"


def bootstrap(accounts) -> tuple[str, str | None] | None:
    """Create the first account if there are none.

    Returns (username, password) where the password is None if it came from the
    environment — there is nothing to tell anyone in that case — or the freshly
    generated one, which the caller must show once and never store.
    """
    if not accounts.empty:
        return None

    username = os.environ.get("AUTEUR_USERNAME", SEED_USERNAME).strip() or SEED_USERNAME
    email = os.environ.get("AUTEUR_EMAIL", SEED_EMAIL).strip() or SEED_EMAIL

    chosen = os.environ.get("AUTEUR_PASSWORD", "")
    if chosen:
        # The same rules the reset form and the CLI apply. An environment
        # variable is a convenience, not a way around them: refusing here is
        # noisy and recoverable, whereas a one-character password on a machine
        # reachable from the wifi is neither.
        from .auth import password_problem

        problem = password_problem(chosen, username=username, email=email)
        if problem:
            raise ValueError(f"AUTEUR_PASSWORD is not good enough. {problem}")
        accounts.add(username, email, chosen)
        return username, None

    password = generate_password()
    accounts.add(username, email, password)
    return username, password
