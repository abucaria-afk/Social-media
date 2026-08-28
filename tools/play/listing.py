"""Google Play Console — every field, answered.

The project had an App Store pack and nothing for Play, which is not a
formatting difference. Play asks a set of questions Apple never asks, and two
of them are the kind that stop a review dead:

  - **Data safety.** A declaration, made by the developer, of what the app
    collects and shares. It is not derived from the binary, so an app that
    collects nothing still has to say so, item by item, and a form left
    unanswered blocks release.
  - **App access.** If any part of the app is behind a sign-in, Play requires
    working credentials or a demo route, or the reviewer sees a login wall and
    fails it. This app has a sign-in for the instance features, so this section
    is not optional for it.

The rest is shape: Play's short description is 80 characters where Apple's
subtitle is 30, Play has no keyword field, and Play wants a 1024x500 feature
graphic that has no App Store equivalent.

Copy comes from `auteur/brand.py` — the same words the App Store listing and
the site use, so the three cannot drift apart again.

    python3 tools/play/listing.py [out-dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur import brand  # noqa: E402
from auteur.identity import IDENTITY  # noqa: E402
from auteur.web.auth import ADULT_AGE, MINIMUM_AGE  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "play"
LIMITS = brand.LIMITS["play"]

#: Play's graphic requirements. Sizes are exact — Play rejects anything else
#: rather than scaling it.
GRAPHICS = [
    ("App icon", "512 x 512", "32-bit PNG with alpha. Play generates the rounding."),
    ("Feature graphic", "1024 x 500", "Shown at the top of the listing. No App Store equivalent."),
    ("Phone screenshots", "min 2, up to 8", "9:16, between 320px and 3840px on each side."),
    ("Tablet screenshots", "optional", "Wanted if the listing claims tablet support."),
]

#: Play's Data safety form, answered. Every one of these is a claim about the
#: shipped build, and every one is checkable: the app makes no network request
#: of its own, carries no third-party SDK, and has no advertising identifier.
DATA_SAFETY = """\
**Does your app collect or share any of the required user data types?**
Nothing is shared. Something is collected, and only on a server the user runs.

The honest answer needs the distinction, because Play defines collection as
transmitting data off the device — not as the developer receiving it. This app
has no backend. There is no service operated by the publisher for anything to
be transmitted *to*. What exists is `auteur serve`: a copy of the server a
person runs on their own machine, which their phone talks to over their own
network.

| Question | Answer | Why |
| --- | --- | --- |
| Shared with the developer | None | There is no developer-operated service. |
| Third-party SDKs | None | No analytics, no crash reporting, no ad network. |
| Third-party services | TikTok and Instagram, only if the user connects one | See below. Not an SDK: ordinary HTTPS requests to their published APIs, made only after the user completes their consent screen. |
| Advertising ID | Not used | Not requested, not linked, not present. |
| Data encrypted in transit | Yes, where configured | An instance is reached over the user's own network; HTTPS when they set it up. The platform APIs are HTTPS always. |
| Deletion request route | Yes | Account deletion is in the app, and erases the watch history and any platform tokens with it. |

**Connecting a platform account.** On the Schedule screen a person may connect
a TikTok or Instagram account in order to read back how a post did. This is the
one thing in the app that contacts a company other than the person running the
instance, so it is declared plainly rather than folded into the paragraph above:

* It happens only when somebody taps Connect and completes the platform's own
  consent screen, which they can refuse.
* The scopes requested are **read-only** — `user.info.basic,user.info.stats,video.list`
  on TikTok, `instagram_business_basic,instagram_business_manage_insights` on
  Instagram. Publishing is a separate permission on both and is not requested,
  so the app cannot post as anybody even in error.
* What comes back — follower counts and per-post figures — is stored on the
  instance, which is the user's own machine. It is not sent to the publisher,
  because there is nowhere to send it.
* Disconnecting deletes the access token. So does deleting the account.

The previous version of this file said "Third-party SDKs: None" and stopped
there, which was true of the build it was written for. A Data safety
declaration that was accurate when tested and inaccurate when shipped is a
policy strike rather than a rejection you can fix and resubmit, so this section
lands in the same change as the code that makes it necessary.

**With no instance, nothing is collected at all.** Making a film — choosing
footage, writing the brief, cutting, grading, saving — happens entirely on the
device and involves no network of any kind. A person who never runs a server
never transmits anything.

**With an instance, these are collected, to that person's own machine:**

| Type | What | Why |
| --- | --- | --- |
| App activity — app interactions | How long each film was watched, whether it finished, whether it looped, whether share was tapped | Ranking the feed. A feed with no measurements is a shuffle. |
| Photos and videos | The films the user makes and posts | That is the product. |
| Messages | Messages between accounts on that instance | That is the feature. |
| Personal info — name | The account name and profile the user chose | Identifying who made what. |

Marked **collected** and **not shared**, linked to the user, and not used for
advertising or tracking. The watch history is deletable in-app, which Play asks
about separately and which is answered by the same account-deletion route
Apple's guideline 5.1.1(v) requires.

**Why this is not answered "No".** The previous version of this file said No,
on the reasoning that the user's own hardware is not collection. That reasoning
is defensible and it is not what Play's definition says: data leaves the device.
Answering No because the destination is sympathetic is how a Data safety
declaration becomes wrong, and a wrong declaration is a policy strike rather
than a rejection you can fix and resubmit.
"""

#: Play requires working access for the reviewer when anything is behind a
#: sign-in. This is where reviews of apps with accounts most often fail.
APP_ACCESS = """\
**Is any functionality restricted by a login?** Yes, partly.

Everything that makes a film works with no account at all: choosing footage,
writing the brief, cutting, grading, and saving the result. A reviewer can
exercise the whole product without signing in to anything.

The feed, the messages and the planning board require an instance — a copy of
the server the user runs on their own machine. There is no account on any
service operated by the developer, so there are no credentials to hand over.

**Instructions for the reviewer:**

1. Open the app. No sign-in is presented.
2. Choose several photographs from the device.
3. Type a sentence — for example "a montage of the walk home, 12 seconds".
4. Tap Make it. The film is cut and rendered on the device.
5. Save or share the result.

The instance features are unreachable without a server the reviewer would have
to run, and nothing about the on-device product depends on them.
"""

#: The IARC questionnaire is where an app that carries other people's content
#: has to be honest about it, whether or not the feature ships enabled.
CONTENT_RATING = f"""\
Play uses the IARC questionnaire, which is a series of yes/no answers rather
than a rating you pick. The answers this app gives:

| Question | Answer |
| --- | --- |
| Violence, sexuality, profanity, controlled substances | No |
| Does the app allow users to interact or exchange content? | Yes |
| Does it share the user's location with other users? | No |
| Does it allow users to purchase digital goods? | No |
| Is unrestricted internet access provided? | No |
| Does the app contain user-generated content? | Yes, on an instance |

The last two are the ones that matter. The app has no browser and no open
network access. It does carry user-generated content once somebody runs an
instance, which is why reporting, blocking and moderation ship in the build
rather than being promised — the same requirement Apple's guideline 1.2 sets.

Target age group: **{MINIMUM_AGE} and over.** An account belonging to somebody
under {ADULT_AGE} starts with sensitive films hidden, and that setting can be
locked with a code.
"""

#: Play's technical gates. These change on a schedule Google publishes, and the
#: current target-API rule is the one that most often blocks an update.
TECHNICAL = """\
| Requirement | This project |
| --- | --- |
| Upload format | Android App Bundle (`.aab`). Play stopped accepting new APKs in 2021. |
| Target API level | Must meet Play's current floor, which rises every year. Check the console before building — a bundle below the floor is refused at upload. |
| 64-bit | Required. The web view carries no native code, so this is satisfied by the toolchain. |
| App signing | Play App Signing. Google holds the signing key; you keep the upload key. |
| Privacy policy URL | Required for every app, not only those that collect data. |

**On what the Android build is.** The same web front end the iOS build wraps,
in a WebView, with the same CSP and the same no-network posture. The page is
generated by `tools/artifact/build_artifact.py`, so Android and iOS ship the
same product rather than two that drift.
"""


def _fits(label: str, text: str, limit: int) -> str:
    used = len(text)
    mark = "ok" if used <= limit else f"**{used - limit} over**"
    return f"| {label} | {used}/{limit} | {mark} |"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    fields = [
        ("App name", IDENTITY.app_name, LIMITS.title),
        ("Short description", brand.PROMISE_SHORT, LIMITS.short),
        ("Full description", brand.description(), LIMITS.full),
    ]
    over = brand.too_long("play")

    page = f"""# Google Play Console — every field, answered

Generated by `tools/play/listing.py` from `auteur/brand.py` and
`auteur/identity.py`. Re-run it after changing either, rather than editing this
file. The copy is the same copy the App Store listing and the site use.

Store limits checked against Play's guidance as of {brand.AS_OF}.

## The listing

| Field | Value |
| --- | --- |
| App name | {IDENTITY.app_name} |
| Package name | `{IDENTITY.bundle_id}` |
| Default language | English (United States) |
| App or game | App |
| Category | Video Players & Editors |
| Tags | Video editing, Photography |
| Free or paid | Free |
| Contact email | {IDENTITY.support_email} |
| Website | {IDENTITY.support_url} |
| Privacy Policy URL | {IDENTITY.privacy_url} |

### Lengths, against Play's own limits

| Field | Used | |
| --- | --- | --- |
{chr(10).join(_fits(name, text, limit) for name, text, limit in fields)}

### Short description

```
{brand.PROMISE_SHORT}
```

### Full description

```
{brand.description()}
```

## Graphics

| Asset | Size | Note |
| --- | --- | --- |
{chr(10).join(f"| {n} | {s} | {w} |" for n, s, w in GRAPHICS)}

Screenshots are captured from the running app by
`tools/appstore/screenshots.py` — the same captures, at Play's sizes. Nothing
here is a mock-up.

## Data safety

{DATA_SAFETY}

## App access

{APP_ACCESS}

## Content rating

{CONTENT_RATING}

## Technical requirements

{TECHNICAL}

## What is not done

{"- Everything in this file fits." if not over else chr(10).join("- " + line for line in over)}

The three publisher values — package name, developer name and support address —
come from the environment. Until they are set, this file carries placeholders
and Play will refuse the upload: `com.example.*` is a reserved domain there
exactly as it is on the App Store.
"""

    target = OUT / "play-listing.md"
    target.write_text(page, encoding="utf-8")
    print(target, len(page), "bytes")
    if over:
        for line in over:
            print("  over:", line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
