"""Accounts on the platforms a film goes out to, and what they report back.

`auteur/web/oidc.py` already signs somebody *in* with Google or Apple. This is
a different job that looks similar enough to be worth separating out loud: that
one answers "who is this person", and this one answers "which TikTok account
may this instance post to and read numbers from". Same protocol, different
consent, different tokens, and a token that can publish on somebody's behalf is
not a token that should live in the same store as a sign-in.

**What this changes about the app, said plainly.** Until now the honest claim
was that nothing left the device: no third-party code, no analytics, works in
aeroplane mode. Connecting an account here is the first thing in this project
that genuinely sends data somewhere the publisher does not run, and it makes
that claim false wherever it is still written down. `PRIVACY.md`, the Play Data
safety declaration and `brand.py` are all updated in the same change, because a
privacy claim that is true of the version you tested and false of the version
you shipped is the exact failure a Data safety form is a policy strike for.

Nothing here connects on its own. An account is connected when a person taps
Connect and completes the platform's own consent screen, and disconnecting
deletes the tokens.

**On numbers that are not there.** Every platform below needs an approved
developer application before it will return a single figure — Instagram wants a
Business or Creator account, a Meta app and App Review for
`instagram_manage_insights`; TikTok wants Login Kit and an audited app. Until a
publisher supplies those, this returns nothing, and the screen says there is
nothing rather than showing a plausible chart. That is not squeamishness: the
insight layer in this repository was fitted to a simulation for months and
every number it produced was invented, which is the single worst thing a tool
like this can do to somebody making decisions with it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

#: When the endpoints and scope names below were last read off the platforms'
#: own documentation. Same discipline as `workflows/platforms.py` and
#: `brand.py`: a number nobody dates is a number nobody re-checks. Both
#: platforms have renamed scopes inside the last two years.
AS_OF = "2026-08"


@dataclass(frozen=True)
class Platform:
    """One place films go, and what it takes to talk to it.

    The URLs and scope names are the platforms' own and are worth keeping in
    code rather than in a document, because the failure they prevent — a scope
    that was renamed, an endpoint that moved — shows up as an authorisation
    screen that refuses rather than as an error anybody can read.
    """

    key: str
    label: str
    #: Where the person is sent to approve.
    authorize: str
    #: Where the code is exchanged for tokens.
    token: str
    #: What is asked for. Deliberately the least that does the job: reading
    #: numbers and publishing are separate scopes on both platforms, and an app
    #: that asks for publishing rights in order to draw a chart is one people
    #: are right to refuse.
    read_scopes: str
    #: Where the numbers come from once an account is connected.
    insights: str
    #: What has to be true before the platform will approve the application at
    #: all. Written down because it is the part that takes weeks, and finding
    #: out about it after building is how this kind of feature dies.
    gate: str
    #: The page these values were read off, and when. Every one of these was
    #: first written from memory and then checked against the platform's own
    #: documentation, which is the only reason they can be trusted — a scope
    #: name recalled rather than read is a consent screen that refuses with no
    #: explanation, and this project has a long history of numbers that were
    #: never checked against their source.
    source: str
    #: The environment variables a publisher sets. Nothing is hard-coded and no
    #: default is plausible-looking: an unset client id has to fail, not work
    #: badly.
    id_var: str
    secret_var: str
    # Last, because a field with a default cannot precede one without. Third
    # time this exact ordering has bitten in this repository.
    checked: str = AS_OF


PLATFORMS: dict[str, Platform] = {
    "tiktok": Platform(
        key="tiktok",
        label="TikTok",
        authorize="https://www.tiktok.com/v2/auth/authorize/",
        token="https://open.tiktokapis.com/v2/oauth/token/",
        # `user.info.stats` is the follower and likes count; `video.list` is the
        # per-video figures. Publishing is `video.publish` and is not asked for
        # here — see the note on scopes above.
        read_scopes="user.info.basic,user.info.stats,video.list",
        insights="https://open.tiktokapis.com/v2/video/query/",
        source="https://developers.tiktok.com/doc/login-kit-manage-user-access-tokens/",
        gate=(
            "A TikTok developer application, and an audit before it may leave "
            "sandbox. In sandbox only accounts explicitly added as testers can "
            "authorise it, so this works for the publisher's own account "
            "immediately and for anybody else's only after the audit."
        ),
        id_var="AUTEUR_TIKTOK_CLIENT_KEY",
        secret_var="AUTEUR_TIKTOK_CLIENT_SECRET",
    ),
    "instagram": Platform(
        key="instagram",
        label="Instagram",
        authorize="https://www.instagram.com/oauth/authorize",
        token="https://api.instagram.com/oauth/access_token",
        read_scopes="instagram_business_basic,instagram_business_manage_insights",
        # `GET /{ig-user-id}/insights?metric=...&period=day`. Note that
        # `impressions` and `profile_views` are being retired in favour of
        # `views`, so a metric list is not a constant to hard-code once and
        # forget — which is why this carries a date and a source like
        # everything else that describes somebody else's product.
        insights="https://graph.facebook.com/{account}/insights",
        source="https://developers.facebook.com/documentation/instagram-platform/insights",
        gate=(
            "A Meta app, and the account has to be a Business or Creator "
            "account rather than a personal one — Instagram returns no "
            "insights at all for a personal account, which is the single most "
            "common reason this appears to be broken when it is working. "
            "Reading anybody else's numbers needs App Review."
        ),
        id_var="AUTEUR_INSTAGRAM_CLIENT_ID",
        secret_var="AUTEUR_INSTAGRAM_CLIENT_SECRET",
    ),
}


def configured(key: str) -> bool:
    """Whether a publisher has supplied credentials for this platform."""
    platform = PLATFORMS[key]
    return bool(os.environ.get(platform.id_var) and os.environ.get(platform.secret_var))


def what_is_missing() -> list[str]:
    """Every platform that cannot be connected yet, and what it needs.

    Shown on the screen rather than logged. Somebody looking at an empty
    Schedule tab deserves to know it is empty because nothing is configured,
    not because their account has no views.
    """
    gaps = []
    for platform in PLATFORMS.values():
        if not configured(platform.key):
            gaps.append(
                f"{platform.label} is not configured: set {platform.id_var} and "
                f"{platform.secret_var}. {platform.gate}"
            )
    return gaps


@dataclass
class Connection:
    """One platform account, connected by one person on this instance."""

    who: str
    platform: str
    #: The account's own name on that platform, for showing which one is
    #: connected when somebody has three.
    handle: str = ""
    #: Seconds since the epoch. Tokens expire and a screen that says "connected"
    #: about an expired token is worse than one that says nothing.
    expires_at: float = 0.0
    connected_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at <= time.time()

    def to_json(self) -> dict:
        out = asdict(self)
        out["expired"] = self.expired
        return out


class Connections:
    """Which platform accounts each person has connected.

    **Tokens are not in here.** This file is the list of connections and is read
    to draw a screen; the tokens live beside it with the permissions of a
    secret, because the two have completely different blast radii if the file
    is ever mishandled. A leaked list of handles is embarrassing. A leaked
    access token posts to somebody's TikTok.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.rows: list[Connection] = []
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "connections.json"

    @property
    def _secrets(self) -> Path:
        return self.path.with_name(self.path.stem + "-tokens.json")

    def _load(self) -> None:
        if not self.path.is_file():
            self.rows = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt file is not a crash: the app still works with nothing
            # connected, which is the state it ships in.
            self.rows = []
            return
        self.rows = [
            Connection(
                who=str(row.get("who", "")),
                platform=str(row.get("platform", "")),
                handle=str(row.get("handle", "")),
                expires_at=float(row.get("expires_at") or 0.0),
                connected_at=float(row.get("connected_at") or 0.0),
            )
            for row in raw
            if row.get("who") and row.get("platform") in PLATFORMS
        ]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(row) for row in self.rows], indent=2), encoding="utf-8"
        )

    def _write_token(self, who: str, platform: str, token: str, refresh: str) -> None:
        store: dict = {}
        if self._secrets.is_file():
            try:
                store = json.loads(self._secrets.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                store = {}
        store[f"{who}:{platform}"] = {"access": token, "refresh": refresh}
        self._secrets.parent.mkdir(parents=True, exist_ok=True)
        self._secrets.write_text(json.dumps(store, indent=2), encoding="utf-8")
        # Owner only. The file sits in somebody's workspace beside their films,
        # and the default for a new file is usually world-readable.
        try:
            self._secrets.chmod(0o600)
        except OSError:  # pragma: no cover - filesystem without modes
            pass

    def _drop_token(self, who: str, platform: str) -> None:
        if not self._secrets.is_file():
            return
        try:
            store = json.loads(self._secrets.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        store.pop(f"{who}:{platform}", None)
        self._secrets.write_text(json.dumps(store, indent=2), encoding="utf-8")

    def token(self, who: str, platform: str) -> str:
        """The access token, or "" — never raises, so a caller cannot leak it
        into a traceback by forgetting a connection is gone."""
        if not self._secrets.is_file():
            return ""
        try:
            store = json.loads(self._secrets.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(store.get(f"{who}:{platform}", {}).get("access", ""))

    def connect(
        self,
        who: str,
        platform: str,
        *,
        handle: str = "",
        token: str = "",
        refresh: str = "",
        expires_in: float = 0.0,
    ) -> Connection:
        if platform not in PLATFORMS:
            raise ValueError(f"no such platform: {platform}")
        self.disconnect(who, platform)
        row = Connection(
            who=who,
            platform=platform,
            handle=handle,
            expires_at=time.time() + expires_in if expires_in else 0.0,
        )
        self.rows.append(row)
        self._save()
        if token:
            self._write_token(who, platform, token, refresh)
        return row

    def disconnect(self, who: str, platform: str) -> bool:
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r.who == who and r.platform == platform)]
        if len(self.rows) != before:
            self._save()
        self._drop_token(who, platform)
        return len(self.rows) != before

    def of(self, who: str) -> list[Connection]:
        return [row for row in self.rows if row.who == who]

    def forget_everything_about(self, who: str) -> int:
        """Deleting an account disconnects its platforms and drops its tokens.

        Guideline 5.1.1(v) is about the account being erasable from inside the
        app, and a live token that can post to somebody's TikTok surviving the
        deletion of the account that authorised it is the worst possible
        remnant.
        """
        mine = self.of(who)
        for row in mine:
            self.disconnect(who, row.platform)
        return len(mine)
