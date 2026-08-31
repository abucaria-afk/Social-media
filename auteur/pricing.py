"""What it costs, and the arithmetic that got there.

A price is the one number on a site that nobody can check by reading the code,
which is exactly why it is the number most likely to drift: somebody types
"$12" into a landing page, somebody else types "$12.49" into Stripe, and the
first person to notice is a customer whose card was charged the other one.

So the prices here are **derived rather than typed**. The comparison set below
is a list of real, dated, sourced competitor prices; the average of each tier
is computed from it; the instruction was to sit fifteen per cent under that
average, and `_charm` picks the largest ordinary-looking price that actually
satisfies it. Change a rival's price or add one, and the price this ships
changes with it — there is no second copy to forget.

**What is measured and what is chosen.** The comparison prices are measured:
each carries the page it was read from and the month it was read. `UNDERCUT`
and `TOP_TIER_OFF` are instructions — fifteen per cent under, ten per cent off
the top tier. `TRIAL_DAYS` is neither: fourteen days is a chosen default, not a
figure anybody measured, and it is marked as such here rather than being
allowed to look like the others.

**Which rivals count is a judgement, and it is written down.** Two exclusions
are deliberate. Runway's $76 Max tier is priced around generative credits —
frames it renders on its own hardware, which cost it money per film. This app
renders on the machine in your hand, so there is no marginal cost per film to
price against, and matching a credit tier would be pricing a cost we do not
have. CapCut's $24.99 Team plan is a small-team plan, not a top tier, and it
would drag the top average down against tiers it does not compete with.

**The free tier is not a trial.** The browser build already does everything on
this page marked `on_device` in `brand.FEATURES` — six of the eight features —
and it does it without an account. What money buys is a hosted instance: the
feed that learns, the messages and the planning board, which are the two
features that need a copy running somewhere. Charging for what already runs
free on the device would be charging for nothing.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass

#: When the rival prices below were read off the rivals' own pages. Same
#: discipline as `brand.AS_OF` and `workflows/platforms.py`: a number nobody
#: dates is a number nobody re-checks.
AS_OF = "2026-08"

#: The instruction, as a fraction. Fifteen per cent under the market average.
UNDERCUT = 0.15

#: Off the highest tier, advertised. A coupon in Stripe carries the same
#: number, and a test compares the two rather than trusting them to match.
TOP_TIER_OFF = 0.10

#: Days of free trial. **Chosen, not measured.** Nothing in the comparison set
#: produced this figure; fourteen days is a common default and it is long
#: enough to make a film, sit on it, and come back. It is called out in the
#: module docstring for the same reason it is commented here: a chosen number
#: sitting in a list of measured ones reads as measured.
TRIAL_DAYS = 14


@dataclass(frozen=True)
class Rival:
    """One competitor's price for one tier, and where it was read."""

    name: str
    #: US dollars a month. Where a rival quotes an annual price, this is that
    #: price divided by twelve — the number they advertise, not the higher
    #: month-to-month one, because the annual price is the one they compete on.
    dollars: float
    #: The page it came from. Every entry has one; a price with no source is a
    #: price somebody remembered.
    source: str


#: The tier a person starts on: one seat, month to month, advertised price.
ENTRY_RIVALS: list[Rival] = [
    Rival("Runway Standard", 12.00, "runway.com/pricing — $12/mo billed annually"),
    Rival("CapCut Pro", 15.00, "capcut.com — $179.99/yr"),
    Rival("Descript Hobbyist", 16.00, "descript.com — annual, per editor"),
    Rival("Kapwing Pro", 16.00, "kapwing.com — $192/yr per member"),
]

#: The top tier each of them sells to a working team, on the same basis.
TOP_RIVALS: list[Rival] = [
    Rival("VEED Pro", 49.00, "veed.io/pricing — billed annually"),
    Rival("Descript Business", 50.00, "descript.com — annual, per editor"),
    Rival("Kapwing Business", 50.00, "kapwing.com — $600/yr"),
]

#: Left out on purpose, with the reason, so the next person to look can
#: disagree with the judgement rather than having to reconstruct it.
EXCLUDED: dict[str, str] = {
    "Runway Max ($76)": (
        "priced around generative credits — frames rendered on their hardware. "
        "This app renders on your device, so there is no per-film cost to price."
    ),
    "CapCut Team ($24.99)": "a small-team plan, not a top tier.",
    "Adobe Premiere Pro": "a professional NLE, not an AI-first consumer editor.",
}


def average(rivals: list[Rival]) -> float:
    """The mean advertised price of a comparison set, in dollars."""
    return sum(rival.dollars for rival in rivals) / len(rivals)


def _charm(ceiling: float) -> float:
    """The largest price ending in .49 or .99 that is strictly under `ceiling`.

    Rounding *down* rather than to the nearest is the whole point. "Fifteen per
    cent under the market" is a claim, and $12.54 rounded up to $12.99 makes it
    false by eleven per cent while still looking like it holds. Every price
    this returns satisfies the claim as arithmetic, which is what
    `undercut_of` then checks.
    """
    cents = int(ceiling * 100)
    while cents > 0:
        if cents % 100 in (49, 99):
            return cents / 100
        cents -= 1
    raise ValueError(f"no ordinary price sits under {ceiling}")


def undercut_of(price: float, rivals: list[Rival]) -> float:
    """How far under that comparison set's average a price actually is."""
    market = average(rivals)
    return (market - price) / market


#: Where a person actually pays, per tier key. Empty until the live payment
#: links exist, which is deliberate rather than unfinished.
#:
#: `tools/stripe/sync_pricing.py` prints a URL per tier on every run; those go
#: here. Until then the site says the plan is not open yet rather than showing
#: a button that goes nowhere — the same answer the store section already
#: gives for a build that is not submitted.
#:
#: **A test-mode link must never land here.** Stripe's test links live on
#: `buy.stripe.com/test_...` and look exactly like the real ones, and I have
#: had a pair of them in hand while writing this file. A test link on the
#: public site takes a card number that is not a card number and tells the
#: customer it worked. `_checkout_problem` refuses them by shape.
CHECKOUT: dict[str, str] = {
    # Read off the live site on 2026-08-31, where they had been hand-typed
    # into `docs/index.html` — a generated file, so the next `build_site.py`
    # run would have deleted both buttons and nobody could have bought
    # anything. Here they survive a rebuild and reach the app as well.
    "solo": "https://buy.stripe.com/8x2bJ16fV8K5eRieHR1B602",
    "studio": "https://buy.stripe.com/3cIaEX9s72lHdNearB1B601",
}


def _checkout_problem(url: str) -> str:
    """Why this URL cannot go on the public site, or "" if it can."""
    if not url:
        return ""
    if not url.startswith("https://"):
        return "a checkout has to be https"
    if "/test_" in url or url.startswith("https://buy.stripe.com/test"):
        return "that is a Stripe test link — it takes fake cards and says they worked"
    return ""


@dataclass(frozen=True)
class Tier:
    """One thing a person can buy, or not buy."""

    key: str
    name: str
    #: Dollars a month. Zero is a real answer and means free, not unpriced.
    dollars: float
    #: The one line under the name.
    blurb: str
    #: What is in it, in the words a person would use.
    includes: tuple[str, ...]
    #: The comparison set this price was derived from, or None for the free
    #: tier, which is not competing with anybody's paid plan.
    rivals: list[Rival] | None = None
    #: What Stripe calls this price. The lookup key joins this file to the
    #: account; without it the two are a pair of numbers nobody compares.
    lookup_key: str = ""

    @property
    def monthly(self) -> str:
        return "Free" if not self.dollars else f"${self.dollars:.2f}"

    @property
    def cents(self) -> int:
        """The price as Stripe wants it: an integer number of cents."""
        return round(self.dollars * 100)


#: Everything on the device, with no account and no payment. This is the build
#: the site already links to, so the free tier is a description of what is
#: shipping rather than a promise.
FREE = Tier(
    key="free",
    name="In your browser",
    dollars=0.0,
    blurb="Everything that runs on your device, which is most of it.",
    includes=(
        "Say the film you want in a sentence and get it cut",
        "Every grade, every shape, type and stickers on the beat",
        "Works in aeroplane mode — nothing leaves the device",
        "No account",
    ),
)

#: One seat on a hosted instance.
SOLO = Tier(
    key="solo",
    # The name on the invoice, the Stripe product and the checkout is the
    # name the plan is called: Solo. "A copy that is yours" was the whole
    # name for a while, and it is a good line — but a person comparing plans
    # needs a handle before they need a sentence, and a card statement
    # reading "A copy that is yours" is a chargeback waiting to happen. The
    # line kept its job, one field down.
    name="Solo",
    dollars=_charm(average(ENTRY_RIVALS) * (1 - UNDERCUT)),
    blurb="A copy that is yours — an instance that is running when you are not.",
    includes=(
        "Everything in the browser build",
        "A feed that learns from what you finish, on your instance",
        "The planning board and messages, kept between devices",
        "Read back how a post did on an account you connect yourself",
    ),
    rivals=ENTRY_RIVALS,
    lookup_key="atlas_solo_monthly",
)

#: The same instance with more than one person on it. This is the invite
#: system that already exists in `auteur/web/auth.py`, priced.
STUDIO = Tier(
    key="studio",
    name="Studio",
    dollars=_charm(average(TOP_RIVALS) * (1 - UNDERCUT)),
    blurb="A copy for the room — one instance, everybody making the work on it.",
    includes=(
        # Named by reading the other tier, not by retyping it. This line said
        # "Everything in a copy that is yours" and would have gone on saying
        # it after the tier stopped being called that.
        f"Everything in {SOLO.name}",
        "Invite the rest of the room onto the same instance",
        "One planning board, one shot list, one calendar",
        "The feed ranks across everything the room has made",
    ),
    rivals=TOP_RIVALS,
    lookup_key="atlas_studio_monthly",
)

TIERS: list[Tier] = [FREE, SOLO, STUDIO]


def checkout_for(tier: Tier) -> str:
    """The tier's checkout URL, or "" if there is not a usable one yet.

    The environment wins over the table, the same way `identity._env` lets a
    support address be overridden without a commit. A payment link is
    deployment configuration rather than a fact about the product: it differs
    between the live account and a test one, it is regenerated whenever a
    price changes, and somebody staging the site should be able to point it at
    a test checkout for an afternoon without editing a file that a test then
    refuses to let them commit.

        AUTEUR_CHECKOUT_SOLO=https://buy.stripe.com/... python3 tools/site/build_site.py

    Both paths go through the same refusal, so the environment is not a way
    around it — a test link is rejected however it arrives.
    """
    raw = os.environ.get(f"AUTEUR_CHECKOUT_{tier.key.upper()}")
    if raw is not None:
        # Set-but-empty means **closed**, and that is the useful half. Before
        # there were real links in `CHECKOUT` the environment only ever added
        # one, so there was no way to take a plan off sale without editing
        # this file — no staging build without a checkout, and no switching
        # the shop off in a hurry. A variable that is present speaks for the
        # tier whatever it says, including saying nothing.
        url = raw.strip()
    else:
        url = CHECKOUT.get(tier.key, "").strip()
    problem = _checkout_problem(url)
    if problem:
        raise ValueError(f"{tier.key}: {problem} ({url})")
    return url


def checkout_for_person(tier: Tier, username: str) -> str:
    """The tier's checkout with the buyer's name attached, or "" if not open.

    Stripe hands `client_reference_id` back on `checkout.session.completed`,
    and it is the *only* thing in that event that names a person: every later
    subscription event knows the Stripe customer and nothing else. So a
    checkout opened without it is a card charged and an account nobody can
    find — the money arrives and the door stays shut, which is the defect the
    whole billing path exists to prevent.

    A blank username returns "" rather than a bare link. Offering a checkout
    to somebody the app cannot name is offering to take their money and lose
    it, and a button that is absent is better than one that does that.
    """
    url = checkout_for(tier)
    if not url or not username.strip():
        return ""
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}client_reference_id={urllib.parse.quote(username.strip())}"


#: The tier the ten per cent comes off. Named rather than indexed, because
#: `TIERS[-1]` silently follows a new tier appended to the end.
TOP_TIER = STUDIO

#: The code a customer types to get the discount above.
#:
#: Derived from the tier and the percentage rather than written, because all
#: three are the same fact stated three times and the copies go stale
#: silently. A coupon changed to fifteen per cent leaves "STUDIO10" printed on
#: the site advertising a number that is no longer the number; a tier renamed
#: leaves the code naming a plan nobody sells. It was `ROOM10` — from "A copy
#: for the room" — and it outlived that name by exactly one commit, which is
#: how long a typed copy of a derived fact ever lasts.
#:
#: It has to be advertised, not merely to exist. A Stripe payment link takes
#: `allow_promotion_codes` and nothing else — it will not carry a coupon of
#: its own — so a discount nobody can type is a discount nobody can have. The
#: first version of this created the coupon and stopped there, which is a 10%
#: saving on the site and no way to claim it at the checkout.
PROMO_CODE = f"{TOP_TIER.name.upper()}{round(TOP_TIER_OFF * 100)}"


def discounted(tier: Tier = TOP_TIER) -> float:
    """The top tier after the advertised ten per cent, rounded to the cent.

    Rounded down, for the same reason `_charm` rounds down: the advertised
    saving has to be at least what it says, never a cent less.
    """
    return int(tier.dollars * (1 - TOP_TIER_OFF) * 100) / 100


def open_for_business() -> bool:
    """Whether anything on this page can actually be bought today."""
    return any(checkout_for(tier) for tier in TIERS if tier.dollars)


def headline() -> str:
    """The one sentence a landing page leads with.

    Two sentences, depending on whether there is a checkout. Leading with
    "14 days free" above two plans that both say "Not open yet" is a page
    arguing with itself — it offers a trial and then, four inches lower,
    admits there is nowhere to start one. The offer follows the state rather
    than being written once and going stale the moment the state changes.
    """
    under = round(undercut_of(SOLO.dollars, ENTRY_RIVALS) * 100)
    if open_for_business():
        return (
            f"{TRIAL_DAYS} days free, then ${SOLO.dollars:.2f} a month — "
            f"{under}% under what the same thing costs everywhere else."
        )
    return (
        f"Free in your browser today. ${SOLO.dollars:.2f} a month when the "
        f"hosted plans open — {under}% under what the same thing costs "
        "everywhere else."
    )


def working() -> list[str]:
    """The arithmetic, spelled out, for anybody who wants to check it.

    Printed by `python3 -m auteur.pricing` and read by the test suite. A
    price nobody can retrace is a price nobody can correct.
    """
    lines = [f"Comparison prices read {AS_OF}. Monthly, at each rival's advertised rate."]
    for tier in (SOLO, STUDIO):
        rivals = tier.rivals or []
        lines.append("")
        lines.append(f"{tier.name} — {tier.monthly}/mo")
        for rival in rivals:
            lines.append(f"  ${rival.dollars:>6.2f}  {rival.name:<20} {rival.source}")
        lines.append(
            f"  average ${average(rivals):.2f} → {UNDERCUT:.0%} under is ${tier.dollars:.2f}"
        )
        lines.append(f"  actually {undercut_of(tier.dollars, rivals):.1%} under")
    lines += ["", "Left out of the comparison:"]
    lines += [f"  {name} — {why}" for name, why in EXCLUDED.items()]
    lines += [
        "",
        f"{TOP_TIER_OFF:.0%} off {TOP_TIER.name}: ${TOP_TIER.dollars:.2f} → "
        f"${discounted():.2f}/mo with the code {PROMO_CODE}",
        f"Free trial: {TRIAL_DAYS} days. A chosen default, not a measured figure.",
    ]
    return lines


if __name__ == "__main__":  # pragma: no cover - a script, not a code path
    print("\n".join(working()))
