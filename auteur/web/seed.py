"""The account this instance starts with.

The first time `auteur serve` runs against an empty workspace it creates one
account, so there is something to sign in as. After that this module is never
consulted again — the real accounts live in `<workspace>/accounts.json`, which
is outside the repository and holds whatever the password has since become.

**No password is written down here, and none should be.** What is stored is a
salt and a scrypt hash at n=2^15: enough to check a password, and about 0.1s
and 32MB per guess for anyone trying to work backwards from it. That is a
serious obstacle but not a wall, and this repository is private for a reason.
Two ways to avoid relying on that at all:

    # set your own at first run, so nothing is committed anywhere
    AUTEUR_USERNAME=... AUTEUR_EMAIL=... AUTEUR_PASSWORD=... python -m auteur serve

    # or change it afterwards, which replaces the hash below for good
    python -m auteur account password
"""

from __future__ import annotations

import os

#: Username, email, salt, scrypt hash. Never a password.
SEED_USERNAME = "streetlightseason"
SEED_EMAIL = "streetlightseason@gmail.com"
SEED_SALT = "a843dbda6c9de9ee05eb49999a7c6547"
SEED_HASH = "18b4e2a606e7e70df2ceb8f1c712518257d400843fae4820fe5cec1b1a8bd653"


def bootstrap(accounts) -> str | None:
    """Create the first account if there are none. Returns its username.

    Environment beats the seed, so an install that would rather not inherit a
    hash from version control simply sets three variables and never touches it.
    """
    if not accounts.empty:
        return None

    password = os.environ.get("AUTEUR_PASSWORD", "")
    username = os.environ.get("AUTEUR_USERNAME", SEED_USERNAME).strip() or SEED_USERNAME
    email = os.environ.get("AUTEUR_EMAIL", SEED_EMAIL).strip() or SEED_EMAIL

    if password:
        accounts.add(username, email, password)
        return username

    from .auth import Account

    account = Account(username=SEED_USERNAME, email=SEED_EMAIL,
                      salt=SEED_SALT, password_hash=SEED_HASH)
    with accounts.lock:
        accounts.accounts[account.username.lower()] = account
        accounts._save()
    return account.username
