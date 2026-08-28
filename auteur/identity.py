"""The handful of things only the person publishing this can fill in.

Every one of these appears in more than one place — the Xcode project, the
App Store listing, the terms page, the app itself — and every one of them is a
value no repository can know. Kept here, once, so that changing a support
address is one edit rather than a search, and so that shipping with a
placeholder still in it is something a check can catch rather than something
App Store review catches for you.

There are two of them, and the split is the point. A **company** publishes;
a **product** is what it publishes. Auteur Studies LLC owns the legal name, the
domain, the support address, the policy documents and the copyright line —
those belong to the umbrella and every product under it inherits them. `auteur`
owns its bundle identifier, its app name and its version. Kept apart, a second
product gets the company's details right by construction rather than by
somebody remembering to copy them, and the copyright line cannot say one thing
while the App Store seller field says another.

`ready()` is the whole point. It is called by `tools/appstore/preflight.py` and
by the test suite, and it fails on anything still holding a placeholder, so the
question "is this ready to submit" has an answer that is run rather than
remembered.

`problems()` and `pending()` answer two different questions and are not
interchangeable. A *problem* is a value nobody has decided — a placeholder, and
entirely within this repository's power to fix. Something *pending* has been
decided and is waiting on the outside world: a domain registered, an entity
filed, a developer account approved. The distinction matters because the second
kind cannot be fixed by editing a file, and treating it as a failure would mean
a red check that stays red no matter what anybody does to the code.

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


#: The year on the copyright line. Not `datetime.now().year`: a copyright
#: year that moves on its own says the work was created in whatever year the
#: reader happens to run the code.
FOUNDED = "2026"


@dataclass(frozen=True)
class Company:
    """The umbrella. One of these; as many products under it as there are.

    Everything here is a fact about the business rather than about any one
    app, which is why it is not on `Identity`: the copyright line, the seller
    name on both stores, the address a person writes to and the domain the
    policy documents live on are the same whatever is being shipped. A second
    product inherits them instead of restating them, and restating is how the
    site ended up describing a command-line tool eighteen months after it
    stopped being one.
    """

    #: The name on the incorporation paperwork, and therefore the name Apple
    #: and Google show as the seller — both display the enrolled entity name
    #: exactly, suffix included, so the suffix belongs in it.
    legal_name: str = _env("AUTEUR_COMPANY", "Auteur Studies LLC")

    #: What it is called in a sentence, without the suffix. Used in prose,
    #: never on a form.
    trading_name: str = _env("AUTEUR_COMPANY_SHORT", "Auteur Studies")

    #: The domain the company controls. Everything below is derived from it,
    #: so there is one place to change and no way for the bundle identifier
    #: to name one domain while the privacy policy is served from another.
    domain: str = _env("AUTEUR_DOMAIN", "auteurstudies.com")

    #: Where somebody reports a problem. Apple guideline 1.2 requires
    #: published contact information for any app carrying other people's
    #: content, and this is it: the terms page, the store listing and the
    #: review notes all read this one value.
    support_email: str = _env("AUTEUR_SUPPORT_EMAIL", "support@auteurstudies.com")

    @property
    def reverse_dns(self) -> str:
        """`auteurstudies.com` -> `com.auteurstudies`, which is what a bundle
        identifier is built from. Apple requires reverse-DNS on a domain the
        publisher controls, so deriving it is also the check: there is no way
        to write an identifier for a domain the company does not claim."""
        return ".".join(reversed(self.domain.split(".")))

    @property
    def copyright_line(self) -> str:
        return f"Copyright (c) {FOUNDED} {self.legal_name}"

    def _page(self, name: str) -> str:
        # `.html` and not a bare path. GitHub Pages serves `privacy.html` at
        # `/privacy.html` and gives `/privacy` a 404 — and a privacy policy
        # URL that 404s is the single most common metadata rejection there
        # is. The filename here is the filename `tools/site/build_site.py`
        # actually writes, and a test holds the two together.
        return f"https://{self.domain}/{name}"

    @property
    def support_url(self) -> str:
        # The site's front page, which is what Apple's "Support URL" field
        # wants: somewhere a person lands and finds a way to ask something.
        return f"https://{self.domain}/"

    @property
    def privacy_url(self) -> str:
        return self._page("privacy.html")

    @property
    def terms_url(self) -> str:
        return self._page("terms.html")


#: The live one.
COMPANY = Company()


@dataclass(frozen=True)
class Identity:
    """Who is publishing this, and where people reach them."""

    #: Reverse-DNS, and it has to be a domain the publisher controls, which is
    #: why it is *derived* from the company's domain rather than typed. Apple
    #: rejects `com.example.*` outright — it is the reserved documentation
    #: domain — and it also refuses to change a bundle identifier after the
    #: first submission, so this is the one value here that is permanent.
    bundle_id: str = _env("AUTEUR_BUNDLE_ID", f"{COMPANY.reverse_dns}.auteur")

    #: The seller name on both stores, and the name in the copyright line.
    #: The company's, because that is whose name it is.
    developer: str = _env("AUTEUR_DEVELOPER", COMPANY.legal_name)

    #: Also the company's: one address for everything it publishes.
    support_email: str = _env("AUTEUR_SUPPORT_EMAIL", COMPANY.support_email)

    #: The three URLs App Store Connect asks for, on the company's own domain.
    #:
    #: These used to default to this repository's GitHub Pages addresses,
    #: which had the great virtue of being live. They are now the company's,
    #: which is where they belong and where they are not yet live — so
    #: `pending()` names the registration as the thing standing between here
    #: and a submission, and `preflight.py --online` fetches all three and
    #: fails on a 404. A privacy policy URL that does not resolve is the
    #: single most common metadata rejection there is.
    support_url: str = _env("AUTEUR_SUPPORT_URL", COMPANY.support_url)
    privacy_url: str = _env("AUTEUR_PRIVACY_URL", COMPANY.privacy_url)
    terms_url: str = _env("AUTEUR_TERMS_URL", COMPANY.terms_url)

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


@dataclass(frozen=True)
class Waiting:
    """Something decided, not yet true, and not fixable from inside the repo."""

    #: What is being waited on, in four or five words.
    what: str

    #: What breaks while it is not true. Not "it would be nice": the concrete
    #: failure, because a checklist item with no consequence is one nobody
    #: does.
    consequence: str

    #: The command that says whether it has become true. Every one of these is
    #: a command this repository actually provides — held to that by a test,
    #: because "run the preflight" is worth nothing if the preflight does not
    #: check the thing.
    confirm: str


def pending(company: Company | None = None) -> list[Waiting]:
    """Decided, and waiting on the world. Not the same list as `problems()`.

    `problems()` is placeholders: values nobody has chosen, fixable with an
    edit, and a legitimate reason for a check to be red. This is the other
    kind — the domain is registered or it is not, and no amount of editing
    makes it so. Keeping them apart is what stops a permanently-red check,
    which is a check people learn to ignore.

    The order is the order to do them in: each one blocks the next.
    """
    who = company or COMPANY
    return [
        # Registering the domain is done — it resolves. What it resolves *to*
        # is the thing that matters and is not done: the records point at the
        # registrar's own hosting, not at GitHub Pages, so every URL in both
        # store listings still answers with a parking page or a 404.
        Waiting(
            what=f"point {who.domain} at GitHub Pages",
            consequence=(
                "all three store URLs answer with the registrar's page "
                "instead of the policy documents — and a privacy policy URL "
                "that does not resolve is the most common metadata rejection "
                "there is. Four A records on the apex: 185.199.108.153, "
                "185.199.109.153, 185.199.110.153, 185.199.111.153; a CNAME "
                "on www to abucaria-afk.github.io. Then Settings -> Pages -> "
                "Custom domain, and tick Enforce HTTPS once the certificate "
                "has been issued"
            ),
            confirm="python3 tools/appstore/preflight.py --online",
        ),
        Waiting(
            what=f"file {who.legal_name}",
            consequence=(
                "both stores show the enrolled entity name as the seller, and "
                "organisation enrolment cannot start without one"
            ),
            confirm="python3 tools/appstore/preflight.py",
        ),
        Waiting(
            what=f"make {who.support_email} receive mail",
            consequence=(
                "Apple guideline 1.2 requires published contact information "
                "for an app carrying other people's content, and review does "
                "write to it"
            ),
            confirm="python3 tools/appstore/preflight.py --online",
        ),
        Waiting(
            what="switch GitHub Pages on",
            consequence=(
                "the documents are built and deployed by "
                ".github/workflows/pages.yml and the CNAME file naming the "
                "custom domain is written by tools/appstore/build_pages.py, "
                "but nothing is served until Settings -> Pages -> Source is "
                "set to GitHub Actions"
            ),
            confirm="python3 tools/appstore/preflight.py --online",
        ),
    ]


def ready(identity: Identity | None = None) -> bool:
    return not problems(identity)
