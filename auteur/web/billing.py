"""What somebody has paid for, and how this copy comes to believe it.

`auteur/pricing.py` describes what is for sale and `tools/stripe/sync_pricing.py`
makes the Stripe account say the same thing. Between them a customer can reach
a checkout and be charged. Then nothing happened: `pricing` was imported by the
site builder and the sync script and by nothing under `auteur/web/`, `Account`
had seventeen fields and not one of them was "paid", and none of the server's
thirty-four API routes was a webhook. Money could go in and no door opened.

This is the door. It is deliberately small, and three of its decisions are
worth stating because each one is a way this could take money and get it wrong.

**A plan is granted by Stripe, never by the browser.** There is no route a
signed-in person can call to change their own plan. The only thing that writes
a plan is a webhook whose signature verified, which means the only way to
become a paying customer is to have paid.

**No secret configured means the endpoint refuses everything.** Not "accepts
unsigned events", not "logs a warning and proceeds" — refuses. An instance
deployed without `STRIPE_WEBHOOK_SECRET` would otherwise be serving an
unauthenticated grant-me-a-subscription endpoint on the public internet, and
the failure would be invisible because the software would appear to work.

**The tiers are read out of `pricing`, never retyped.** A plan key here that
`pricing` does not sell is the same defect this project keeps finding: two
copies of one fact, one of which goes stale. `PLANS` is built from
`pricing.TIERS` at import, so a tier added, renamed or withdrawn there is
added, renamed or withdrawn here with no second edit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from .. import pricing

#: Every plan key this copy will accept, derived from what is actually sold.
PLANS: frozenset[str] = frozenset(tier.key for tier in pricing.TIERS)

#: The plan an account has until something says otherwise. `pricing.FREE` is a
#: real tier with a real description, so this is a reference and not a literal.
DEFAULT_PLAN: str = pricing.FREE.key

#: Stripe's lookup key -> the tier it sells. This is the join between an event
#: arriving from the payments account and the tier list in this repository; it
#: is why `lookup_key` exists on `Tier` at all.
BY_LOOKUP_KEY: dict[str, pricing.Tier] = {
    tier.lookup_key: tier for tier in pricing.TIERS if tier.lookup_key
}

#: How far out of step a webhook's timestamp may be before it is refused, in
#: seconds. **Chosen** — it is Stripe's own documented default, and the reason
#: it exists is replay: without it a signature captured once stays valid for
#: ever, and a signed "you are now on Studio" could be posted back a thousand
#: times, or a signed cancellation replayed after somebody re-subscribed.
TOLERANCE = 300

#: The subscription states Stripe reports for a subscription that is being paid
#: for or is inside a trial somebody agreed to. Anything outside this set —
#: `past_due`, `canceled`, `unpaid`, `incomplete_expired` — is not entitlement.
#: Listed positively on purpose: a new state Stripe invents lands outside the
#: set and reads as "not paid", which is the safe direction to be wrong in.
LIVE_STATES = frozenset({"active", "trialing"})


def signature_problem(body: bytes, header: str, secret: str, *, now: float | None = None) -> str:
    """Why this is not a webhook from Stripe, or "" if it is one.

    Stripe signs `"{timestamp}.{body}"` with the endpoint secret and sends
    `Stripe-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]`. More than one `v1`
    appears while a secret is being rotated, so every one is checked and any
    match is a pass.

    Returns a sentence rather than raising, because the caller's job is to
    answer a request and every one of these is a 400. The sentences are for
    the log — the response says only that it was refused, since an endpoint
    that explains *which* part of a forgery was wrong is an oracle for making
    a better one.
    """
    if not secret:
        # First, and before the body is looked at. An unconfigured instance
        # has no way to tell a real event from a posted one, so it must not
        # try: "I cannot check this" and "this is fine" are opposite answers.
        return "no STRIPE_WEBHOOK_SECRET is set, so no event can be verified"
    if not header:
        return "no Stripe-Signature header"

    stamp = ""
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            stamp = value
        elif key == "v1":
            signatures.append(value)
    if not stamp or not signatures:
        return "the Stripe-Signature header has no timestamp or no v1 signature"

    try:
        sent = int(stamp)
    except ValueError:
        return "the Stripe-Signature timestamp is not a number"

    drift = abs((time.time() if now is None else now) - sent)
    if drift > TOLERANCE:
        return f"the event is {drift:.0f}s out of step, past the {TOLERANCE}s tolerance"

    signed = f"{sent}.".encode() + body
    want = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    # compare_digest on every candidate rather than `want in signatures`: the
    # `in` operator on strings short-circuits on the first differing byte, and
    # this is a value an attacker can send repeatedly while timing it.
    if any(hmac.compare_digest(want, candidate) for candidate in signatures):
        return ""
    return "the signature does not match"


def _tier_of(subscription: dict) -> pricing.Tier | None:
    """Which tier a subscription is for, by its price's lookup key.

    The lookup key is used rather than the price id because a price id changes
    every time a number changes — `sync_pricing` deactivates the old price and
    makes a new one carrying the same lookup key precisely so that the join
    survives a price rise.
    """
    for item in (subscription.get("items") or {}).get("data") or []:
        key = ((item.get("price") or {}).get("lookup_key") or "").strip()
        if key in BY_LOOKUP_KEY:
            return BY_LOOKUP_KEY[key]
    return None


class Grant:
    """One entitlement change an event asks for."""

    def __init__(self, *, customer: str, plan: str, until: float, username: str = ""):
        self.customer = customer
        self.plan = plan
        self.until = until
        #: Only `checkout.session.completed` knows the account by name — it
        #: carries the `client_reference_id` the checkout was opened with.
        #: Later events know only the customer, and are matched by the id this
        #: first one stored.
        self.username = username

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Grant {self.username or self.customer} -> {self.plan}>"


def grant_from(event: dict) -> Grant | None:
    """What this event changes, or None if it changes nothing.

    Unknown event types are None rather than an error. Stripe sends whatever
    the account is subscribed to and adds new types over time; an endpoint that
    500s on an event it does not recognise teaches Stripe to retry it for three
    days and eventually disable the endpoint that was working fine.
    """
    kind = str(event.get("type") or "")
    body = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        # A checkout that has not been paid for is not a sale. Stripe sends
        # this event for `payment_status: unpaid` too, on delayed methods.
        if str(body.get("payment_status") or "") != "paid":
            return None
        username = str(body.get("client_reference_id") or "").strip()
        customer = str(body.get("customer") or "").strip()
        if not username or not customer:
            return None
        # The session names the price it sold through the same lookup key.
        tier = _tier_of(body.get("subscription_details") or body)
        if tier is None:
            return None
        return Grant(
            customer=customer,
            plan=tier.key,
            until=float(body.get("expires_at") or 0) or 0.0,
            username=username,
        )

    if kind in ("customer.subscription.updated", "customer.subscription.created"):
        customer = str(body.get("customer") or "").strip()
        if not customer:
            return None
        state = str(body.get("status") or "")
        tier = _tier_of(body)
        if state not in LIVE_STATES or tier is None:
            # A lapsed subscription is a downgrade, not silence. Saying
            # nothing here would leave somebody on Studio for ever after
            # their card stopped working.
            return Grant(customer=customer, plan=DEFAULT_PLAN, until=0.0)
        return Grant(
            customer=customer,
            plan=tier.key,
            until=float(body.get("current_period_end") or 0) or 0.0,
        )

    if kind == "customer.subscription.deleted":
        customer = str(body.get("customer") or "").strip()
        if not customer:
            return None
        return Grant(customer=customer, plan=DEFAULT_PLAN, until=0.0)

    return None


def read_event(body: bytes) -> dict | None:
    """The event, or None if the body is not one."""
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None
