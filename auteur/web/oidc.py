"""Signing in with a Google or Apple account.

Both are OpenID Connect, so this is one implementation with two sets of
endpoints rather than two integrations. What it does *not* do is create
accounts: this app serves somebody's own camera roll over their own wifi and
sign-up closes the moment the first account exists. Signing in with Google
therefore signs you into an account that is already here, matched on a
**verified** email address, and says so plainly when there is no match. An
identity provider is a way to prove who you are, not a way in.

Three decisions worth stating, because each of them is a place this could be
wrong in a way nobody would notice.

**The authorization code flow, never the implicit one.** The `id_token` is
fetched by *this server* from the provider's token endpoint over TLS, and is
never accepted from a browser. That is what makes the next point safe.

**No local signature verification, deliberately.** Verifying an RS256 or ES256
JWT needs a real crypto library, and this project has two dependencies on
purpose. It does not need one here: a token collected by a direct server-to-
server TLS call to a pinned endpoint is authenticated by that channel, which is
what Google's own guidance says about this exact case. The safety of it rests
entirely on the token never arriving any other way — so `_claims_from` is only
ever called on a token-endpoint response, and if anybody ever wires a token in
from the browser, signature verification stops being optional and this comment
becomes a bug report.

**The redirect URI comes from configuration, never from the Host header.** It
is the one value an attacker would most like to influence, it has to match what
is registered with the provider anyway, and deriving it from the request would
let a forged Host send somebody's authorization code somewhere else.

Credentials live in the workspace or the environment and never in the
repository, alongside the accounts file and under the same reasoning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.web.oidc")

#: How long somebody has to finish signing in before the attempt is forgotten.
#: Long enough to read a consent screen, short enough that a stolen `state` is
#: worthless by the time anybody could use it.
PENDING_SECONDS = 600

#: Never wait forever on a provider that has stopped answering.
NETWORK_TIMEOUT = 20


@dataclass(frozen=True)
class Provider:
    """One identity provider, and everything needed to talk to it."""

    key: str
    label: str
    authorize: str
    token: str
    scopes: str
    #: Apple answers the callback as a POST form rather than a redirect with a
    #: query string, because it may include the person's name the first time.
    form_post: bool = False
    #: Apple's client secret is not a string you paste, it is a short-lived
    #: ES256 JWT you sign with a key they issue. That needs a crypto library
    #: this project does not depend on, which is a real limitation and is
    #: reported as one rather than hidden behind a button that fails.
    signed_secret: bool = False
    note: str = ""


#: The two the app offers. Endpoints are the providers' published ones; nothing
#: here is discovered at runtime, so an outage in a discovery document cannot
#: silently change where credentials get sent.
PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        key="google",
        label="Continue with Google",
        authorize="https://accounts.google.com/o/oauth2/v2/auth",
        token="https://oauth2.googleapis.com/token",
        scopes="openid email profile",
        note=(
            "Google accepts http://localhost and http://127.0.0.1 as redirect "
            "URIs, so this works on the machine running the app without a "
            "domain or a certificate. Reaching it from a phone over wifi needs "
            "the app published at a real https address."
        ),
    ),
    "apple": Provider(
        key="apple",
        label="Continue with Apple",
        authorize="https://appleid.apple.com/auth/authorize",
        token="https://appleid.apple.com/auth/token",
        scopes="openid email name",
        form_post=True,
        signed_secret=True,
        note=(
            "Apple requires an https redirect on a domain you have registered "
            "with them — it accepts no localhost exception — and a client "
            "secret signed with a key they issue. Both are properties of how "
            "the app is published, not of this code."
        ),
    ),
}


@dataclass
class Settings:
    """What an operator has configured, per provider."""

    client_id: str = ""
    client_secret: str = ""
    #: Exactly what is registered with the provider. Compared byte for byte.
    redirect_uri: str = ""
    #: Apple only.
    team_id: str = ""
    key_id: str = ""
    private_key: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.client_id and self.redirect_uri)


def _from_environment(key: str) -> Settings:
    prefix = f"AUTEUR_{key.upper()}_"
    return Settings(
        client_id=os.environ.get(prefix + "CLIENT_ID", ""),
        client_secret=os.environ.get(prefix + "CLIENT_SECRET", ""),
        redirect_uri=os.environ.get(prefix + "REDIRECT_URI", ""),
        team_id=os.environ.get(prefix + "TEAM_ID", ""),
        key_id=os.environ.get(prefix + "KEY_ID", ""),
        private_key=os.environ.get(prefix + "PRIVATE_KEY", ""),
    )


def load(workspace: Path | str | None) -> dict[str, Settings]:
    """Read what is configured, from the workspace file then the environment.

    Never from the repository. A client secret in version control is the same
    mistake as a password in version control, and this project has already made
    that one once.
    """
    found: dict[str, Settings] = {}
    raw: dict = {}
    if workspace:
        path = Path(workspace) / "sign-in.json"
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}
    for key in PROVIDERS:
        settings = _from_environment(key)
        block = raw.get(key)
        if isinstance(block, dict):
            for field_name in Settings.__dataclass_fields__:
                value = block.get(field_name)
                if value and not getattr(settings, field_name):
                    setattr(settings, field_name, str(value))
        found[key] = settings
    return found


def offered(configured: dict[str, Settings]) -> list[dict]:
    """What the sign-in page should show, and why anything missing is missing.

    Every provider is listed whether or not it is set up. A button that is
    simply absent reads as a feature this app does not have; a row that says
    what it needs reads as a feature waiting on one piece of configuration,
    which is the truth.
    """
    # `None` rather than an empty mapping is what a server that has not called
    # `load()` yet has, and asking a None for `.get` is a 500 on the sign-in
    # page — the one page somebody reaches before anything else. Nothing
    # configured is a fine answer here; crashing is not.
    configured = configured or {}
    out = []
    for key, provider in PROVIDERS.items():
        settings = configured.get(key, Settings())
        ready = settings.usable
        why = ""
        if not ready:
            why = "not configured on this copy"
        elif provider.signed_secret and not _can_sign():
            ready = False
            why = "needs a signing library this install does not have"
        out.append(
            {
                "key": key,
                "label": provider.label,
                "ready": ready,
                "why": why,
                "note": provider.note,
            }
        )
    return out


def _can_sign() -> bool:
    """Whether Apple's ES256 client secret can be produced here."""
    try:  # pragma: no cover - depends on what is installed
        from cryptography.hazmat.primitives import hashes  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means no
        return False
    return True


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """One sign-in in progress."""

    provider: str
    state: str
    nonce: str
    verifier: str
    started: float = field(default_factory=time.time)


class Attempts:
    """The sign-ins currently in flight.

    In memory rather than on disk: an interrupted sign-in should not survive a
    restart, and a `state` that outlives the process it was issued by is a
    replay waiting to happen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[str, Attempt] = {}

    def begin(self, provider: str) -> Attempt:
        attempt = Attempt(
            provider=provider,
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(24),
            verifier=secrets.token_urlsafe(48),
        )
        with self._lock:
            self._sweep()
            self._live[attempt.state] = attempt
        return attempt

    def claim(self, state: str) -> Attempt | None:
        """Take the attempt for this `state`, once. A second use gets nothing."""
        with self._lock:
            self._sweep()
            attempt = self._live.pop(state, None)
        if attempt is None:
            return None
        if time.time() - attempt.started > PENDING_SECONDS:
            return None
        return attempt

    def _sweep(self) -> None:
        cutoff = time.time() - PENDING_SECONDS
        for state in [s for s, a in self._live.items() if a.started < cutoff]:
            self._live.pop(state, None)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def begin(provider_key: str, settings: Settings, attempt: Attempt) -> str:
    """The URL to send somebody to."""
    provider = PROVIDERS[provider_key]
    query = {
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": provider.scopes,
        "state": attempt.state,
        "nonce": attempt.nonce,
        # PKCE. Not strictly required for a confidential client, and included
        # anyway: it costs one hash and it removes a whole class of
        # authorization-code interception outright.
        "code_challenge": _challenge(attempt.verifier),
        "code_challenge_method": "S256",
    }
    if provider.form_post:
        query["response_mode"] = "form_post"
    return provider.authorize + "?" + urllib.parse.urlencode(query)


def _post(url: str, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as answer:
        return json.loads(answer.read().decode("utf-8"))


def _claims_from(id_token: str) -> dict:
    """The claims inside an id_token collected from a token endpoint.

    Read, not verified — see the module docstring. Called on nothing else, ever.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("that is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    if not isinstance(claims, dict):
        raise ValueError("the token carried no claims")
    return claims


def finish(provider_key: str, settings: Settings, attempt: Attempt, code: str) -> dict:
    """Exchange the code and return the claims, or raise with a plain reason."""
    provider = PROVIDERS[provider_key]
    secret = settings.client_secret
    if provider.signed_secret:
        secret = _apple_secret(settings)

    answer = _post(
        provider.token,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": settings.client_id,
            "client_secret": secret,
            "code_verifier": attempt.verifier,
        },
    )
    token = answer.get("id_token")
    if not token:
        raise ValueError("the provider returned no identity token")
    claims = _claims_from(token)

    # The three checks that are this program's job rather than the channel's.
    if claims.get("nonce") != attempt.nonce:
        raise ValueError("that sign-in does not match the one that was started")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.client_id not in audiences:
        raise ValueError("that token was issued for a different application")
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires < time.time():
        raise ValueError("that token had already expired")
    return claims


def email_of(claims: dict) -> str:
    """The verified email, or "" — an unverified one is not an identity.

    Both providers will hand over an address they have not checked under some
    conditions, and matching an account on one would let anybody who can claim
    an address at a provider sign in as its owner.
    """
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if not verified:
        return ""
    return str(claims.get("email") or "").strip().lower()


def _apple_secret(settings: Settings) -> str:  # pragma: no cover - needs crypto
    """Apple's client secret: a short-lived ES256 JWT signed with their key."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "signing in with Apple needs a crypto library this install does not "
            "have; `pip install cryptography` and restart"
        ) from exc

    def segment(payload: dict) -> bytes:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = segment({"alg": "ES256", "kid": settings.key_id})
    body = segment(
        {
            "iss": settings.team_id,
            "iat": now,
            # Apple allows up to six months; an hour is plenty and a secret
            # that expires quickly is one that is worth less if it leaks.
            "exp": now + 3600,
            "aud": "https://appleid.apple.com",
            "sub": settings.client_id,
        }
    )
    signing_input = header + b"." + body
    key = serialization.load_pem_private_key(settings.private_key.encode("utf-8"), password=None)
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return (signing_input + b"." + base64.urlsafe_b64encode(raw_signature).rstrip(b"=")).decode()
