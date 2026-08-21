"""The handful of things only the person publishing this can fill in.

Every one of these appears in more than one place — the Xcode project, the
App Store listing, the terms page, the app itself — and every one of them is a
value no repository can know. Kept here, once, so that changing a support
address is one edit rather than a search, and so that shipping with a
placeholder still in it is something a check can catch rather than something
App Store review catches for you.

`ready()` is the whole point. It is called by `tools/appstore/preflight.py` and
by the test suite, and it fails on anything still holding a placeholder, so the
question "is this ready to submit" has an answer that is run rather than
remembered.

None of this is a secret. A bundle identifier and a support address are on the
App Store listing where everybody can read them; the credentials that actually
matter — the signing identity and the App Store Connect key — live on the
machine doing the build and are never in a repository, which is the same rule
the password store follows.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: What an unfilled value looks like. Anything containing this is a value
#: somebody has to replace before the app can be submitted, and `ready()` says
#: so by name rather than letting it through. Matched case-insensitively
#: through `_unfilled` — "Example Developer" is as much a placeholder as
#: "com.example.auteur", and the first version of this check let it through
#: because it compared against a lowercase string.
PLACEHOLDER = "example"


def _unfilled(value: str) -> bool:
    return PLACEHOLDER in (value or "").lower()


def _env(name: str, fallback: str) -> str:
    """Overridable from the environment, so a fork does not have to edit code.

    `AUTEUR_BUNDLE_ID=com.yours.auteur python3 tools/appstore/preflight.py` is
    enough to check somebody else's settings without a commit.
    """
    return os.environ.get(name, "").strip() or fallback


@dataclass(frozen=True)
class Identity:
    """Who is publishing this, and where people reach them."""

    #: Reverse-DNS, and it has to be a domain the publisher controls. Apple
    #: rejects `com.example.*` outright — it is the reserved documentation
    #: domain — so the default here is deliberately one that fails `ready()`
    #: rather than one that looks plausible enough to ship by accident.
    bundle_id: str = _env("AUTEUR_BUNDLE_ID", "com.example.auteur")

    #: The name on the App Store listing, and in the copyright line.
    developer: str = _env("AUTEUR_DEVELOPER", "Example Developer")

    #: Where somebody reports a problem. Apple requires published contact
    #: information for any app carrying other people's content (guideline
    #: 1.2), and this is it — it goes in the terms page, the App Store listing
    #: and the review notes.
    support_email: str = _env("AUTEUR_SUPPORT_EMAIL", "support@example.com")

    #: The three URLs App Store Connect asks for. The defaults are the GitHub
    #: Pages addresses this repository actually publishes to — see
    #: `.github/workflows/pages.yml` — so they are real as soon as Pages is
    #: turned on, rather than being somewhere to fill in later.
    support_url: str = _env("AUTEUR_SUPPORT_URL", "https://abucaria-afk.github.io/Social-media/")
    privacy_url: str = _env(
        "AUTEUR_PRIVACY_URL", "https://abucaria-afk.github.io/Social-media/privacy.html"
    )
    terms_url: str = _env(
        "AUTEUR_TERMS_URL", "https://abucaria-afk.github.io/Social-media/terms.html"
    )

    #: What the app is called on the home screen and in the store. Checked
    #: against Apple's 30-character limit, which is not advice.
    app_name: str = _env("AUTEUR_APP_NAME", "Auteur")

    #: The version people see, and the build number that has to go up on every
    #: upload. App Store Connect refuses a build number it has seen before,
    #: after the upload, by email.
    marketing_version: str = _env("AUTEUR_VERSION", "1.0")
    build_number: str = _env("AUTEUR_BUILD", "1")


#: The live one. Read this rather than constructing another.
IDENTITY = Identity()

#: Apple's own limits, so a listing cannot be written past them and discovered
#: at upload. From App Store Connect's field validation.
NAME_LIMIT = 30
SUBTITLE_LIMIT = 30
KEYWORDS_LIMIT = 100
PROMOTIONAL_LIMIT = 170
DESCRIPTION_LIMIT = 4000


def problems(identity: Identity | None = None) -> list[str]:
    """Everything still standing between this and a submission. Empty is ready.

    Each line names the value, what is wrong with it, and the environment
    variable that sets it — because "not ready" with no next step is the same
    as no check at all.
    """
    who = identity or IDENTITY
    out: list[str] = []

    if _unfilled(who.bundle_id):
        out.append(
            f"bundle identifier is still {who.bundle_id!r} — Apple rejects the "
            "reserved com.example domain. Set AUTEUR_BUNDLE_ID to reverse-DNS "
            "on a domain you own, e.g. com.yourname.auteur."
        )
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", who.bundle_id) or who.bundle_id.count(".") < 2:
        out.append(
            f"bundle identifier {who.bundle_id!r} is not reverse-DNS — letters, "
            "digits, hyphens and at least two dots."
        )

    if _unfilled(who.developer):
        out.append("developer name is a placeholder. Set AUTEUR_DEVELOPER.")

    if _unfilled(who.support_email) or "@" not in who.support_email:
        out.append(
            f"support address is still {who.support_email!r}. Guideline 1.2 "
            "requires published contact information for an app carrying other "
            "people's content. Set AUTEUR_SUPPORT_EMAIL."
        )

    for label, url, variable in (
        ("support", who.support_url, "AUTEUR_SUPPORT_URL"),
        ("privacy policy", who.privacy_url, "AUTEUR_PRIVACY_URL"),
        ("terms", who.terms_url, "AUTEUR_TERMS_URL"),
    ):
        if not url.startswith("https://"):
            out.append(f"{label} URL must be https, got {url!r}. Set {variable}.")
        elif _unfilled(url):
            out.append(f"{label} URL is a placeholder. Set {variable}.")

    if len(who.app_name) > NAME_LIMIT:
        out.append(
            f"app name is {len(who.app_name)} characters; App Store Connect "
            f"allows {NAME_LIMIT}."
        )

    if not re.fullmatch(r"\d+(\.\d+){0,2}", who.marketing_version):
        out.append(f"version {who.marketing_version!r} is not one to three numbers.")
    if not who.build_number.isdigit():
        out.append(f"build number {who.build_number!r} is not a whole number.")

    return out


def ready(identity: Identity | None = None) -> bool:
    return not problems(identity)
