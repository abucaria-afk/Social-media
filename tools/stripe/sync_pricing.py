"""Make the Stripe account say what `auteur/pricing.py` says.

A price exists in two places and only one of them is in this repository. The
test suite can check the half that is here — that every paid tier carries a
lookup key, that each price really is fifteen per cent under the average it
claims — but nothing in a test run can see the account that actually charges
people. So the second half is not typed either: this writes it.

    STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe/sync_pricing.py
    STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe/sync_pricing.py --apply
    STRIPE_SECRET_KEY=sk_live_... python3 tools/stripe/sync_pricing.py --apply --live

**It shows before it does.** With no flags it prints the calls it would make
and writes nothing, because the failure mode of a script pointed at a payments
account is not "it did not run", it is "it ran twice". `--apply` is the flag
that means it.

**A live key is not enough on its own.** `--live` has to be passed as well, so
an `sk_live_` sitting in a shell from an hour ago cannot quietly create real
products because somebody re-ran the wrong command. The two together are the
only way anything reaches livemode.

**Running it again is safe.** Products are found by their `auteur_tier`
metadata and prices by their lookup key, so a second run reconciles rather
than duplicating. Stripe prices are immutable: when a number here changes, the
old price is deactivated, a new one is created carrying the same lookup key,
and the product's default price is re-pointed at it. Anybody already
subscribed stays on the price they signed up at, which is the behaviour you
want and the reason the old price is deactivated rather than deleted.

**The trial and the discount come from the module too.** `TRIAL_DAYS` becomes
the payment link's trial period and `TOP_TIER_OFF` becomes the coupon's
percentage, so the site and the checkout cannot advertise two different
numbers — which is the whole defect this file exists to close.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur import pricing  # noqa: E402

API = "https://api.stripe.com/v1"

#: The coupon's id, which is also how it is found on a re-run. Fixed rather
#: than generated: a generated id makes a second coupon every time.
COUPON_ID = "atlas-top-tier"


class Stripe:
    """Just enough of the API, over stdlib, with the writes behind a flag."""

    def __init__(self, key: str, *, apply: bool) -> None:
        self.key = key
        self.apply = apply
        self.did: list[str] = []

    def _form(self, params: dict, prefix: str = "") -> list[tuple[str, str]]:
        """Stripe takes nested values as `metadata[key]`, not as JSON."""
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            name = f"{prefix}[{key}]" if prefix else key
            if isinstance(value, dict):
                pairs += self._form(value, name)
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        pairs += self._form(item, f"{name}[{index}]")
                    else:
                        pairs.append((f"{name}[{index}]", str(item)))
            elif isinstance(value, bool):
                pairs.append((name, "true" if value else "false"))
            elif value is not None:
                pairs.append((name, str(value)))
        return pairs

    def get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(self._form(params or {}))
        return self._send("GET", f"{path}?{query}" if query else path, None)

    def delete(self, path: str) -> None:
        self.did.append(f"DELETE {path}")
        if self.apply:
            self._send("DELETE", path, None)

    def post(self, path: str, params: dict) -> dict:
        body = urllib.parse.urlencode(self._form(params)).encode()
        self.did.append(f"POST {path} {json.dumps(params, sort_keys=True)}")
        if not self.apply:
            # A dry run has to return something shaped like the object, or
            # every caller downstream needs its own "if not apply" branch and
            # the flag stops being one decision in one place.
            return {"id": f"(would create via {path})", "dry_run": True}
        return self._send("POST", path, body)

    def _send(self, method: str, path: str, body: bytes | None) -> dict:
        request = urllib.request.Request(f"{API}{path}", data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.key}")
        # No pinned Stripe-Version, deliberately. The account's own default
        # applies, which is the version this was verified against — and the
        # shapes are not stable across versions: creating a promotion code
        # took a top-level `coupon` on older versions and takes
        # `promotion[type]=coupon` on current ones. Pinning a version this
        # script has never actually been run against would trade a visible
        # failure for a silent mismatch. If Stripe rejects a parameter, the
        # error below names it.
        if body is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = error.read().decode()
            raise SystemExit(f"Stripe said {error.code} to {method} {path}:\n{detail}") from None


def _product_for(client: Stripe, tier: pricing.Tier) -> dict:
    """The product for a tier, found by metadata or created."""
    for product in client.get("/products", {"limit": 100, "active": True}).get("data", []):
        if product.get("metadata", {}).get("auteur_tier") == tier.key:
            return product

    rivals = tier.rivals or []
    return client.post(
        "/products",
        {
            "name": f"Auteur Atlas — {tier.name}",
            "description": tier.blurb + " " + " ".join(f"{line}." for line in tier.includes),
            "statement_descriptor": "AUTEUR ATLAS",
            "metadata": {
                # Why the price is the price, carried on the object itself, so
                # the dashboard answers the question without anybody having to
                # find this repository.
                "auteur_tier": tier.key,
                "derived_by": "auteur/pricing.py",
                "market_average_usd": f"{pricing.average(rivals):.2f}",
                "undercut": f"{pricing.undercut_of(tier.dollars, rivals):.1%}",
                "comparison_set": ", ".join(f"{r.name} ${r.dollars:.2f}" for r in rivals),
                "prices_read": pricing.AS_OF,
            },
        },
    )


def _price_for(client: Stripe, tier: pricing.Tier, product: dict) -> dict:
    """The price for a tier, by lookup key, replaced rather than edited.

    Stripe prices cannot be amended — a price is what somebody agreed to pay,
    so the API will not let it change under them. Changing a number here
    therefore means a new price object, and the lookup key moving to it.
    """
    found = client.get("/prices", {"lookup_keys": [tier.lookup_key], "limit": 1}).get("data", [])
    if found and found[0]["unit_amount"] == tier.cents and found[0]["active"]:
        return found[0]

    if found:
        client.post(f"/prices/{found[0]['id']}", {"active": False, "lookup_key": ""})

    price = client.post(
        "/prices",
        {
            "product": product["id"],
            "currency": "usd",
            "unit_amount": tier.cents,
            "recurring": {"interval": "month"},
            "lookup_key": tier.lookup_key,
            "transfer_lookup_key": bool(found),
            "nickname": f"{tier.name} — ${tier.dollars:.2f}/mo",
        },
    )
    if not product.get("dry_run"):
        client.post(f"/products/{product['id']}", {"default_price": price["id"]})
    return price


def _coupon_params(product_id: str) -> dict:
    """What the coupon is, separately from the deciding whether to send it.

    Pulled out so the percentage can be checked against `TOP_TIER_OFF` by a
    test that does not need a Stripe key or a network. The alternative is a
    test that asserts the function exists, which is not a check.
    """
    return {
        "id": COUPON_ID,
        "percent_off": pricing.TOP_TIER_OFF * 100,
        "duration": "forever",
        "name": f"{pricing.TOP_TIER_OFF:.0%} off {pricing.TOP_TIER.name}",
        "applies_to": {"products": [product_id]},
        "metadata": {"derived_by": "auteur/pricing.py"},
    }


def _coupon(client: Stripe, product: dict) -> dict:
    """Ten per cent off the top tier, at the percentage the site advertises."""
    for coupon in client.get("/coupons", {"limit": 100}).get("data", []):
        if coupon["id"] == COUPON_ID:
            if coupon.get("percent_off") == pricing.TOP_TIER_OFF * 100:
                return coupon
            # A coupon's percentage is as immutable as a price. Wrong number
            # means a new coupon, and the old one deleted so nobody can still
            # redeem the number the site stopped advertising.
            client.delete(f"/coupons/{COUPON_ID}")
            break

    return client.post("/coupons", _coupon_params(product["id"]))


def _promotion_code(client: Stripe, coupon: dict) -> dict:
    """The code a customer types. A coupon on its own cannot be redeemed.

    This is the step the first version of this file did not have, and the gap
    only showed up by driving the real API: a Stripe payment link takes
    `allow_promotion_codes` and will not carry a coupon of its own — it was
    tried, and `discounts` is not a parameter a payment link accepts. So a
    coupon with no promotion code is a discount advertised on the website that
    nobody at the checkout can claim. The site prints `PROMO_CODE` for exactly
    this reason.

    The code is derived from the percentage, so a coupon changed to fifteen
    per cent cannot leave `ROOM10` redeemable next to it.
    """
    for existing in client.get("/promotion_codes", {"limit": 100, "active": True}).get("data", []):
        if existing.get("code") == pricing.PROMO_CODE:
            return existing

    return client.post(
        "/promotion_codes",
        {
            "promotion": {"type": "coupon", "coupon": coupon["id"]},
            "code": pricing.PROMO_CODE,
            "metadata": {"derived_by": "auteur/pricing.py", "auteur_tier": pricing.TOP_TIER.key},
        },
    )


def _link(client: Stripe, tier: pricing.Tier, price: dict) -> dict:
    """A payment link carrying the trial, so the site can just point at it.

    Found by metadata like everything else here, and the price it sells is
    carried in that metadata rather than read back off the object: a payment
    link does not return its line items unless they are expanded, and a
    comparison that silently reads `None` both times is a comparison that
    always passes. This was the one object the first version of this file
    created unconditionally — a second run left two live links for the same
    tier, and whichever one the site was pointing at was the one that got
    used. A test running the whole script twice found it.

    A link's line items are fixed once it exists, so a changed price means a
    new link and the old one deactivated. The old URL then stops working,
    which is why the URLs are printed on every run: the site has to be
    re-pointed at the new one.
    """
    for link in client.get("/payment_links", {"limit": 100, "active": True}).get("data", []):
        if link.get("metadata", {}).get("auteur_tier") != tier.key:
            continue
        if link.get("metadata", {}).get("price") == price["id"]:
            return link
        client.post(f"/payment_links/{link['id']}", {"active": False})
        break

    return client.post(
        "/payment_links",
        {
            "line_items": [{"price": price["id"], "quantity": 1}],
            "subscription_data": {
                "trial_period_days": pricing.TRIAL_DAYS,
                "metadata": {"auteur_tier": tier.key},
            },
            "allow_promotion_codes": True,
            "metadata": {
                "auteur_tier": tier.key,
                "price": price["id"],
                "derived_by": "auteur/pricing.py",
            },
        },
    )


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    live = "--live" in argv

    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        print("STRIPE_SECRET_KEY is not set. It is a secret: pass it in the")
        print("environment for one command, and never write it into a file here.")
        return 2
    if key.startswith("sk_live_") and not live:
        print("That is a live key and --live was not passed. Refusing.")
        print("Real products, on a real account, is a thing to say twice.")
        return 2
    if live and not key.startswith("sk_live_"):
        print("--live was passed and the key is not a live key. Refusing.")
        return 2

    client = Stripe(key, apply=apply)

    print("\n".join(pricing.working()))
    print()

    for tier in pricing.TIERS:
        if not tier.dollars:
            print(f"{tier.name}: free — nothing for Stripe to hold.")
            continue
        product = _product_for(client, tier)
        price = _price_for(client, tier, product)
        link = _link(client, tier, price)
        print(f"{tier.name}: {product['id']}  {price['id']}  {link['id']}")
        if link.get("url"):
            print(f"  {link['url']}")

    top = _product_for(client, pricing.TOP_TIER)
    coupon = _coupon(client, top)
    promotion = _promotion_code(client, coupon)
    print(
        f"{pricing.TOP_TIER_OFF:.0%} off: coupon {coupon['id']}, "
        f"redeemed by typing {promotion.get('code') or pricing.PROMO_CODE}"
    )

    if not apply:
        print()
        print(f"Dry run. {len(client.did)} call(s) not made:")
        for call in client.did:
            print(f"  {call}")
        print("Re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
