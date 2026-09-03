"""Linking an Instagram or TikTok account, and what that does and does not mean.

Two things are deliberately separate here, because collapsing them is how
software ends up posting for people.

*Connected* means the app holds a token and can act. *Posting* is always a
separate, explicit act by a person — the crew may restructure a cut without
being asked and may never publish one, which is a rule the agent layer already
carries and this must not quietly undo.

There are two ways to get a film onto a platform and both are supported:

**Handing off** needs no account, no token and no registered app. The film is
saved to the phone with its caption ready to paste, and the platform's own
composer opens. This is what works today on a phone, it is what most people
actually do, and it is the default.

**Publishing** posts through the platform's API. That needs a registered
developer app, a reviewed set of permissions, and a token the person granted —
none of which this program can conjure. When the credentials are not
configured, that is said plainly rather than shown as a button that fails.

Tokens live in the workspace beside the accounts file, never in the source
tree, written 0600, and are never logged or returned by any endpoint. A token
is a password with a shorter life.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.publish.connections")


def _for_log(text: str, limit: int = 64) -> str:
    """A value from a page, made safe to put in a log line.

    A handle is typed by somebody, and a log file is read as one record per
    line. A handle containing a newline therefore writes a second record that
    looks exactly like a real one — which is a way to hide something in a log
    by burying it under convincing forgeries. Control characters out, length
    capped, quoted so the boundary is visible.
    """
    clean = "".join(ch for ch in str(text) if ch.isprintable())
    return repr(clean[:limit])


#: The platforms this knows how to talk about. Ordered as the tab shows them.
PLATFORMS = ("instagram", "tiktok")

#: Human names, and what a link is actually for.
ABOUT = {
    "instagram": {
        "name": "Instagram",
        "formats": ("reel", "square"),
        "handoff": "Saves the film and opens Instagram with your caption ready to paste.",
        "needs": ("AUTEUR_INSTAGRAM_CLIENT_ID", "AUTEUR_INSTAGRAM_CLIENT_SECRET"),
        "scopes": ("instagram_business_basic", "instagram_business_manage_insights"),
    },
    "tiktok": {
        "name": "TikTok",
        "formats": ("reel",),
        "handoff": "Saves the film and opens TikTok with your caption ready to paste.",
        "needs": ("AUTEUR_TIKTOK_CLIENT_KEY", "AUTEUR_TIKTOK_CLIENT_SECRET"),
        "scopes": ("user.info.basic", "video.list"),
    },
}


#: Substrings that mark a scope as asking for permission to *post*.
#:
#: The app asked for `instagram_business_content_publish` and `video.upload`
#: for as long as it has had a connections tab, and never once used either.
#: Nothing in this program exchanges the code for a token, and nothing holds a
#: token it could post with — `grep -rn "\.token" auteur/` outside this file
#: finds one hit, in the OIDC login, unrelated. A permission asked for and
#: never exercised is the same defect as a number computed and never compared:
#: it exists, so it looks deliberate, and nothing can tell you it is not.
#:
#: It is worse than inert, though, which is why it is a check and not a
#: cleanup. `PRIVACY.md` — served at a public URL, read by a store reviewer —
#: says "there is no code path that publishes to a service". An OAuth consent
#: screen that asks a person for permission to post on their behalf is the
#: platform telling them the opposite, in the platform's own words, at the
#: moment they are deciding whether to trust this. And it is a permission that
#: has to survive App Review at Meta and TikTok, for a capability that would
#: then have to be demonstrated and does not exist.
#:
#: Read scopes are what the product actually does: read back how a post did.
PUBLISHING_MARKERS = ("publish", "upload", "post", "write", "create")


def publishing_scopes() -> list[str]:
    """Every scope requested that asks for permission to post. Should be none.

    Returned rather than asserted so the preflight and the suite can both say
    which one, and so this reads as a question with an answer rather than a
    comment claiming a property.
    """
    return [
        f"{platform}: {scope}"
        for platform, about in ABOUT.items()
        for scope in about["scopes"]
        if any(marker in scope.lower() for marker in PUBLISHING_MARKERS)
    ]


@dataclass
class Connection:
    """One linked account. The token is never part of what a page can see."""

    platform: str
    handle: str = ""
    #: Unix time the token stops working, 0 when the platform did not say.
    expires: float = 0.0
    linked_at: float = field(default_factory=time.time)
    #: Kept out of `public`, out of logs, and out of every response.
    token: str = field(default="", repr=False)

    @property
    def linked(self) -> bool:
        """Whether this account is recorded at all.

        A handoff link carries no token — it is which account the film is for,
        so the caption and the composer are aimed at the right place. Reporting
        *that* as "not connected", which keying this on the token did, tells
        somebody who has just linked their account that nothing happened.
        """
        return bool(self.handle)

    @property
    def live(self) -> bool:
        """Whether a token is held and still good — the posting question."""
        return bool(self.token) and (self.expires == 0 or self.expires > time.time())

    def public(self) -> dict:
        """Everything a page may know. Deliberately not the token.

        A `to_json` that included it would be one careless `self._json(...)`
        away from putting somebody's Instagram token in a browser's network
        log, so the safe shape is the only shape available.
        """
        return {
            "platform": self.platform,
            "name": ABOUT.get(self.platform, {}).get("name", self.platform),
            "handle": self.handle,
            "connected": self.linked,
            # Two different questions, and collapsing them is how an app ends
            # up either lying about a link or claiming it can post.
            "can_publish": self.live,
            "expires": self.expires,
            "linked_at": self.linked_at,
        }


class Connections:
    """Which accounts are linked, per person, kept out of the source tree."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._by_owner: dict[str, dict[str, Connection]] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        """Beside the accounts file, which is already outside the repo."""
        return Path(workspace) / "connections.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a corrupt file is not a crash
            log.warning("could not read connections, starting empty: %s", exc)
            return
        for owner, links in (raw or {}).items():
            self._by_owner[owner] = {
                platform: Connection(
                    platform=platform,
                    handle=str(data.get("handle", "")),
                    expires=float(data.get("expires", 0.0)),
                    linked_at=float(data.get("linked_at", 0.0)),
                    token=str(data.get("token", "")),
                )
                for platform, data in (links or {}).items()
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            owner: {
                platform: {
                    "handle": link.handle,
                    "expires": link.expires,
                    "linked_at": link.linked_at,
                    "token": link.token,
                }
                for platform, link in links.items()
            }
            for owner, links in self._by_owner.items()
        }
        # Written 0600 before anything goes in it, so the tokens are never on
        # disk world-readable even for the moment between create and chmod.
        handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(out, file, indent=2)

    # ------------------------------------------------------------------ use

    def of(self, owner: str) -> list[Connection]:
        """Every platform, linked or not, so a page can show the whole set."""
        held = self._by_owner.get(owner, {})
        return [held.get(p) or Connection(platform=p) for p in PLATFORMS]

    def link(self, owner: str, platform: str, *, handle: str, token: str, expires: float = 0.0):
        if platform not in PLATFORMS:
            raise ValueError(f"no such platform: {platform}")
        link = Connection(platform=platform, handle=handle, token=token, expires=expires)
        self._by_owner.setdefault(owner, {})[platform] = link
        self._save()
        # The handle, never the token — this line ends up in a log file, and
        # both the handle and the owner came from outside this process.
        log.info(
            "linked %s for %s as %s",
            _for_log(platform, 24),
            _for_log(owner),
            _for_log(handle) if handle else "an account",
        )
        return link

    def unlink(self, owner: str, platform: str) -> bool:
        """Forget a link, token and all. Deletes rather than marks."""
        held = self._by_owner.get(owner, {})
        if platform not in held:
            return False
        held.pop(platform)
        self._save()
        log.info("unlinked %s for %s", _for_log(platform, 24), _for_log(owner))
        return True


def configured(platform: str) -> tuple[bool, str]:
    """Whether posting through this platform's API is even possible here.

    Answered from the environment rather than assumed. Everything downstream
    of a wrong answer is a button that cannot work, and the reason a person
    needs is "nobody registered a developer app", not "something went wrong".
    """
    about = ABOUT.get(platform)
    if about is None:
        return False, f"no such platform: {platform}"
    missing = [name for name in about["needs"] if not os.environ.get(name)]
    if missing:
        return False, "not set up for posting — " + " and ".join(missing) + " are not set"
    return True, ""


def authorise_url(platform: str, *, redirect: str, state: str = "") -> tuple[str, str]:
    """Where to send somebody to grant access, and the state to check on return.

    The state is a fresh random value the caller must store and compare when
    the platform redirects back. Without that check, anybody who can make the
    browser follow a link can attach *their* account to somebody else's
    session.
    """
    ok, why = configured(platform)
    if not ok:
        raise RuntimeError(why)
    state = state or secrets.token_urlsafe(24)

    scope = ",".join(ABOUT[platform]["scopes"])

    if platform == "instagram":
        app = os.environ["AUTEUR_INSTAGRAM_CLIENT_ID"]
        # Business Login for Instagram, not Facebook Login. The two are
        # different products with different scope vocabularies, and this URL
        # was sending an `instagram_business_*` scope to
        # `facebook.com/v21.0/dialog/oauth`, which does not know that word —
        # the dialog rejects the request rather than dropping the scope, so
        # the whole flow was dead and nothing said so, because no test can
        # reach a consent screen. Checked against Meta's Business Login
        # documentation, 2026-08.
        return (
            "https://www.instagram.com/oauth/authorize"
            f"?client_id={app}&redirect_uri={redirect}"
            f"&scope={scope}&response_type=code&state={state}"
        ), state

    key = os.environ["AUTEUR_TIKTOK_CLIENT_KEY"]
    return (
        "https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={key}&redirect_uri={redirect}"
        f"&scope={scope}&response_type=code&state={state}"
    ), state


@dataclass
class Handoff:
    """What a person needs to post a film themselves, in one place.

    The path that works with no developer app, no token and no review: the
    film is on the phone, the caption is on the clipboard, and the platform's
    composer is open. Three taps, and nothing about it can post without the
    person doing the posting.
    """

    platform: str
    video: Path
    caption: str = ""
    hashtags: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in self.hashtags)
        return (self.caption + ("\n\n" + tags if tags else "")).strip()

    def steps(self) -> list[str]:
        name = ABOUT.get(self.platform, {}).get("name", self.platform)
        return [
            "Save the film to your phone",
            "Copy the caption",
            f"Open {name} and pick it from your camera roll",
            "Paste the caption",
        ]

    def to_json(self) -> dict:
        return {
            "platform": self.platform,
            "name": ABOUT.get(self.platform, {}).get("name", self.platform),
            "caption": self.text,
            "steps": self.steps(),
        }
