"""The App Store Connect form, written out with every field already filled in.

Submitting an app means typing about thirty answers into a web form, and the
answers are not obvious: the App Privacy questionnaire has to agree exactly
with `PrivacyInfo.xcprivacy`, the age rating questions have a right answer for
an app with a message inbox, and three of the text fields have character limits
that are enforced after you have written past them.

So the answers live here, generated with the identity and checked against
Apple's limits, and the result is a document to work down rather than a form to
guess at.

    python3 tools/appstore/listing.py [outdir]

Every character limit below is App Store Connect's own, and going over one is
a failure of this script rather than a discovery in the browser.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur.identity import (  # noqa: E402
    DESCRIPTION_LIMIT,
    IDENTITY,
    KEYWORDS_LIMIT,
    NAME_LIMIT,
    PROMOTIONAL_LIMIT,
    SUBTITLE_LIMIT,
)

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "appstore"

# 28 characters. The obvious "Cut a film from your camera roll" is 32, which
# App Store Connect refuses — the length table below is what caught it, which
# is the whole reason this file generates rather than being typed into a form.
SUBTITLE = "A film from your camera roll"

PROMOTIONAL = (
    "Say what you want in a sentence. It frames every shot, cuts to the "
    "cadence of your words, grades it, and hands you the film."
)

KEYWORDS = "video editor,reels,montage,film,cut,edit,grade,vhs,super 8,offline,privacy"

DESCRIPTION = """\
Auteur turns what is already on your phone into a finished film.

Say what you want in a sentence — "the long way home, unhurried, 90s" — and it
frames every shot, cuts to the cadence your words ask for, grades the whole
thing, and puts anything you wrote "in quotes" on screen. A hypercut really does
come back cut at a sixth of a second a shot.

WHAT IT DOES

• Reads your clips and photographs and works out what is in them
• Cuts to a rhythm rather than to a fixed interval
• Grades for a decade if you ask: Super 8, VHS, Kodak, Y2K flash, faded 2010s
• Cuts to the timing of a reel you liked, if you hand it one
• Puts type and stickers on the beat
• Delivers at the shape each platform wants

PLAN BEFORE YOU SHOOT

The manager plans a post before the photograph exists: a shot list grouped into
setups you can actually shoot in one go, a caption, and a check on the things
that decide whether anybody sees it. It does everything except post — every
share is a deliberate action you take.

IT RUNS ON YOUR DEVICE

There is no account with anybody. The app makes no network requests on its own,
works in aeroplane mode, and contains no analytics, no crash reporting service,
no advertising identifier and no third-party software of any kind.

If you run Auteur on your own computer, the app can connect to it — that is
where the feed, the messages and the planning board live, on hardware you own,
in a folder you chose. Nothing goes anywhere you did not put it.

FOR EVERYONE

Text size, reduced motion and increased contrast are settings in the app, and
the system's own accessibility settings are always honoured.
"""

REVIEW_NOTES = """\
WHAT THIS APP IS

An offline video editor. It cuts a film from clips and photographs already on
the device, in the device's own browser engine, using canvas and MediaRecorder.
Nothing is uploaded and there is no service behind it.

TO SEE IT WORK, WITH NO ACCOUNT

Open the app, tap Create, choose two or more clips or photographs from the
camera roll, type a sentence such as "the long way home, unhurried" and tap
"Make my film". It renders on the device and offers to save to Photos. No sign
in is required for any of this — the app has no account of its own.

THE FEED, MESSAGES AND PROFILES

These are shown only when the app is pointed at a copy of Auteur that the
person themselves is running (You → the instance row). They are not a service
we operate; they are the person's own computer on their own network. The app is
fully usable without ever doing this, which is how it launches.

If you would like to review those screens, we can supply a temporary instance
address and account on request — please ask through App Review Messages and we
will provide one that is live for the duration of the review.

GUIDELINE 1.2 — USER-GENERATED CONTENT

Present and reachable from the app even though the content is one household's:

• Report — on every film (the ⋯ on the film's rail), every conversation (the ⋯
  in its header) and every person (the ⋯ on their profile). Eight reasons, a
  free-text note, and blocking offered in the same step.
• Block — immediate, needs no approval, and works in both directions: neither
  person can see the other's films or write to them.
• Filtering and takedown — the person running the instance can remove any film
  and close any account (`auteur moderate`), and reports about a child's
  safety, violence or anything illegal are shown to them first.
• Terms with no tolerance for objectionable content or abusive users, agreed to
  when an account is made, at {terms}.
• Published contact information: {email}.

GUIDELINE 5.1.1(v) — ACCOUNT DELETION

You → Delete my account. It asks for the password and for the word "delete" to
be typed, then removes the account, every film and its files, every
conversation, the profile and picture, and the planned posts — immediately,
with no copy kept.

PERMISSIONS

• Photos (add only) — saving a finished film to the camera roll.
• Calendar (write only) — adding a planned shoot, only when asked.
• Local network — reaching the person's own computer, only if they enter its
  address.

None is requested at launch; each is requested at the moment it is used.
"""

PRIVACY_ANSWERS = """\
Every answer is "No", and `ios/Auteur/PrivacyInfo.xcprivacy` says the same —
they have to agree exactly or the upload is held.

| Question | Answer |
| --- | --- |
| Do you or your third-party partners collect data from this app? | **No** |
| Does the app use data for tracking? | **No** |
| Contact info, health, financial, location, contacts, user content, search history, identifiers, usage data, diagnostics | **None collected** |
| Third-party SDKs | **None.** The app has no dependencies. |
| Required-reason APIs declared | UserDefaults (CA92.1), file timestamp (C617.1), disk space (E174.1) |

The App Privacy section will show "Data Not Collected", which is accurate: the
app makes no network request of its own, and the only server it can be pointed
at is one the person runs themselves.
"""

AGE_RATING = """\
Answer the questionnaire as follows. The result is **4+** on the content
questions, but see the last row.

| Question | Answer |
| --- | --- |
| Cartoon or fantasy violence, realistic violence, sexual content, nudity, profanity, alcohol/tobacco/drugs, horror, gambling, contests | **None** |
| Unrestricted web access | **No** — the app opens no arbitrary web content |
| **Does the app include user-generated content?** | **Yes** |

The last one is the one that matters and the one people get wrong. The feed and
the messages are user-generated content, even though they exist only on an
instance the person runs. Answering "Yes" moves the rating to **12+** and makes
the guideline 1.2 controls a requirement — which this app has. Answering "No"
to avoid the higher rating is the single most common cause of a rejection for
apps of this shape.
"""

EXPORT = """\
`ITSAppUsesNonExemptEncryption` is **false** in Info.plist, so the upload does
not stop to ask.

That is accurate: the app itself performs no encryption. Passwords on an
instance are hashed with scrypt, but that is the Python program on the person's
own computer, not the submitted binary — and password hashing is exempt
regardless. HTTPS, where used, is the system's own and is exempt.
"""


def _fits(label: str, text: str, limit: int) -> str:
    length = len(text)
    mark = "ok" if length <= limit else "OVER"
    return f"| {label} | {length} / {limit} | {mark} |"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fields = [
        ("Name", IDENTITY.app_name, NAME_LIMIT),
        ("Subtitle", SUBTITLE, SUBTITLE_LIMIT),
        ("Keywords", KEYWORDS, KEYWORDS_LIMIT),
        ("Promotional text", PROMOTIONAL, PROMOTIONAL_LIMIT),
        ("Description", DESCRIPTION, DESCRIPTION_LIMIT),
    ]
    over = [name for name, text, limit in fields if len(text) > limit]

    page = f"""# App Store Connect — every field, answered

Generated by `tools/appstore/listing.py` from `auteur/identity.py`. Re-run it
after changing either, rather than editing this file.

## The listing

| Field | Value |
| --- | --- |
| Name | {IDENTITY.app_name} |
| Subtitle | {SUBTITLE} |
| Bundle ID | `{IDENTITY.bundle_id}` |
| SKU | `{IDENTITY.bundle_id.replace(".", "-")}` |
| Primary category | Photo & Video |
| Secondary category | Productivity |
| Price | Free |
| Version | {IDENTITY.marketing_version} (build {IDENTITY.build_number}) |
| Support URL | {IDENTITY.support_url} |
| Marketing URL | {IDENTITY.support_url} |
| Privacy Policy URL | {IDENTITY.privacy_url} |
| Copyright | {IDENTITY.developer} |

### Lengths, against Apple's own limits

| Field | Used | |
| --- | --- | --- |
{chr(10).join(_fits(name, text, limit) for name, text, limit in fields)}

### Keywords

```
{KEYWORDS}
```

Comma-separated with no spaces after the commas — a space costs a character and
App Store Connect counts it.

### Promotional text

{PROMOTIONAL}

### Description

```
{DESCRIPTION}```

## Age rating

{AGE_RATING}

## App Privacy

{PRIVACY_ANSWERS}

## Export compliance

{EXPORT}

## Notes for App Review

```
{REVIEW_NOTES.format(terms=IDENTITY.terms_url, email=IDENTITY.support_email)}```

## Screenshots

`python3 tools/appstore/screenshots.py` writes them at the two sizes the form
accepts, from the running app:

* `build/appstore/screenshots/iphone-6.9/` — 1290 x 2796, the required slot
* `build/appstore/screenshots/ipad-12.9/` — 2048 x 2732, needed because this
  app is offered on iPad

## Before you press submit

```sh
python3 tools/appstore/preflight.py --online
```

It fails on everything that would otherwise come back as an email after the
upload: a placeholder identifier, a missing permission string, an icon with an
alpha channel, a screenshot at a size the form will not take, a privacy policy
URL that does not answer.
"""
    path = OUT / "listing.md"
    path.write_text(page, encoding="utf-8")
    print(f"{path}  {len(page)} bytes")
    for name, text, limit in fields:
        print(f"  {name:<18} {len(text):>5} / {limit}")
    if over:
        print(f"\n  over the limit: {', '.join(over)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
