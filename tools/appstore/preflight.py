"""Everything App Store Connect would reject, checked before the upload.

The failures this is standing against all have the same shape: they are found
*after* an archive has been built, signed and uploaded, they arrive as an email
naming something adjacent to the real problem, and the fix is one line. An icon
with an alpha channel. A build number already used. A privacy policy URL that
404s. A permission string missing for a permission the app asks for.

Run it before every upload:

    python3 tools/appstore/preflight.py

Exit code 0 means everything checkable from here is right. It is not a promise
that review will pass — no program can make that one — but everything it checks
is a thing that has actually stopped a submission, and every check is a
measurement rather than a reminder.

Three checks reach the network and skip themselves without one, so this works
offline; `--online` makes an unreachable policy URL a failure rather than a
note, which is what CI should use.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur.identity import IDENTITY, pending, problems  # noqa: E402

IOS = ROOT / "ios"
APP = IOS / "Auteur"
STATIC = ROOT / "auteur" / "web" / "static"

#: The screenshot sizes App Store Connect accepts, in pixels. From Apple's own
#: page, fetched rather than remembered:
#: developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications/
#:
#: A screenshot one pixel off is refused by the upload form with no
#: explanation of which dimension is wrong — and a list that is *too short* is
#: as bad, because this check would then reject a size Apple accepts. The
#: first version of this list was written from memory and was missing four
#: valid iPhone sizes and two iPad ones.
#:
#: Portrait only here; `check_screenshots` compares both ways up.
IPHONE_SIZES = {
    (1290, 2796),  # 6.9" — the required slot
    (1320, 2868),  # 6.9", newer
    (1206, 2622),  # 6.3"
    (1179, 2556),  # 6.1"
    (1284, 2778),  # 6.5"
    (1242, 2688),  # 6.5" — accepted if no 6.9" is supplied
    (1170, 2532),  # 5.8"/6.1"
    (1125, 2436),
    (1080, 2340),
    (1242, 2208),  # 5.5"
    (750, 1334),  # 4.7"
    (640, 1136),  # 4"
}
IPAD_SIZES = {
    (2064, 2752),  # 13" — the required slot when the app runs on iPad
    (2048, 2732),  # 12.9", accepted in the same class
    (1668, 2420),  # 11" / iPad (A16)
    (1668, 2388),  # 11"
    (1668, 2224),  # 10.5"
    (1536, 2048),  # 9.7"
}

#: Every permission iOS will refuse to ask for without a string, mapped to the
#: thing in this app that asks. A usage description with nothing behind it is
#: a reviewer's question; a permission with no description is a crash at the
#: moment it is needed.
PERMISSIONS = {
    "NSPhotoLibraryAddUsageDescription": "saving a finished film to the camera roll",
    "NSCalendarsWriteOnlyAccessUsageDescription": "writing a planned shoot into the calendar",
    "NSLocalNetworkUsageDescription": "reaching an instance on your own wifi",
}


@dataclass
class Note:
    ok: bool
    what: str
    detail: str = ""
    #: A note rather than a failure — something worth seeing that does not
    #: stop a submission.
    soft: bool = False


def _plist(path: Path) -> dict:
    with path.open("rb") as handle:
        return plistlib.load(handle)


def check_identity() -> list[Note]:
    waiting = problems()
    if not waiting:
        return [
            Note(True, "publisher details are filled in", IDENTITY.bundle_id),
        ]
    return [Note(False, "publisher details", line) for line in waiting]


def check_info_plist() -> list[Note]:
    out: list[Note] = []
    path = APP / "Info.plist"
    if not path.is_file():
        return [Note(False, "Info.plist", "missing")]
    info = _plist(path)

    if info.get("CFBundleShortVersionString") != IDENTITY.marketing_version:
        out.append(
            Note(
                False,
                "Info.plist version",
                f"{info.get('CFBundleShortVersionString')!r} does not match "
                f"identity.py's {IDENTITY.marketing_version!r} — run "
                "ios/scripts/build_bundle.py",
            )
        )
    else:
        out.append(Note(True, "version matches identity.py", IDENTITY.marketing_version))

    if info.get("CFBundleVersion") != IDENTITY.build_number:
        out.append(Note(False, "Info.plist build number", "run ios/scripts/build_bundle.py"))

    # Every permission the app asks for has a string, and every string it
    # carries is for a permission something actually asks for. Both directions:
    # the first is a crash, the second is a reviewer asking why.
    for key, why in PERMISSIONS.items():
        said = str(info.get(key) or "")
        if not said:
            out.append(Note(False, f"{key} missing", f"the app asks for this, for {why}"))
        elif len(said) < 20 or not said.rstrip().endswith("."):
            out.append(
                Note(
                    False,
                    f"{key} is not a sentence",
                    f"{said!r} — reviewers read these, and a fragment reads as unfinished",
                )
            )
    unused = [
        key
        for key in info
        if key.endswith("UsageDescription") and key not in PERMISSIONS
        # The calendar has two keys, one of which is the pre-iOS-17 spelling of
        # the other, and both are needed to cover both.
        and key != "NSCalendarsUsageDescription"
    ]
    for key in unused:
        out.append(Note(False, f"{key} is declared and nothing asks for it", "remove it or use it"))
    if not unused:
        out.append(Note(True, "every permission string matches something that asks", ""))

    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        out.append(
            Note(
                False,
                "ITSAppUsesNonExemptEncryption",
                "must be present and false, or every upload stops for an export "
                "compliance question that has the same answer every time",
            )
        )
    else:
        out.append(Note(True, "export compliance answered in the bundle", "no non-exempt crypto"))

    caps = info.get("UIRequiredDeviceCapabilities") or []
    if "armv7" in caps:
        out.append(Note(False, "armv7 in UIRequiredDeviceCapabilities", "32-bit died with iOS 11"))

    launch = (info.get("UILaunchScreen") or {}).get("UIColorName")
    if launch:
        colour = APP / "Assets.xcassets" / f"{launch}.colorset" / "Contents.json"
        if not colour.is_file():
            out.append(
                Note(False, "launch colour", f"{launch} is named and not in the asset catalogue")
            )
    return out


def check_privacy_manifest() -> list[Note]:
    """The privacy manifest, required for every app since May 2024."""
    path = APP / "PrivacyInfo.xcprivacy"
    if not path.is_file():
        return [Note(False, "PrivacyInfo.xcprivacy", "required for every app since May 2024")]
    manifest = _plist(path)
    out = [Note(True, "privacy manifest present", str(path.relative_to(ROOT)))]
    if manifest.get("NSPrivacyTracking") is not False:
        out.append(Note(False, "NSPrivacyTracking", "this app tracks nothing; say so"))
    if manifest.get("NSPrivacyCollectedDataTypes"):
        out.append(
            Note(
                False,
                "the manifest declares collected data",
                "and the App Privacy answers have to agree with it exactly",
            )
        )
    # Required-reason APIs, checked against the Swift rather than against a
    # memory of what the app does. Apple mails an ITMS-91053 about a missing
    # one after the upload and names the category, not the file — and this
    # found a real one: `Instance.swift` remembers the instance address in
    # UserDefaults and the manifest did not say so.
    declared = {
        str(row.get("NSPrivacyAccessedAPIType"))
        for row in (manifest.get("NSPrivacyAccessedAPITypes") or [])
    }
    swift = "\n".join(f.read_text(encoding="utf-8") for f in sorted(APP.rglob("*.swift")))
    needs = {
        "NSPrivacyAccessedAPICategoryUserDefaults": "UserDefaults",
        "NSPrivacyAccessedAPICategoryFileTimestamp": "modificationDate",
        "NSPrivacyAccessedAPICategoryDiskSpace": "volumeAvailableCapacity",
    }
    for category, needle in needs.items():
        if needle in swift and category not in declared:
            out.append(
                Note(
                    False,
                    f"{category} is used and not declared",
                    f"{needle} appears in the Swift; add it to PrivacyInfo.xcprivacy",
                )
            )
    if declared:
        out.append(Note(True, "required-reason APIs declared", ", ".join(sorted(declared))))
    for row in manifest.get("NSPrivacyAccessedAPITypes") or []:
        if not row.get("NSPrivacyAccessedAPITypeReasons"):
            out.append(
                Note(
                    False,
                    f"{row.get('NSPrivacyAccessedAPIType')} has no reason code",
                    "a declared category with no reason is the same as undeclared",
                )
            )
    return out


def check_icon() -> list[Note]:
    from PIL import Image

    folder = APP / "Assets.xcassets" / "AppIcon.appiconset"
    icon = folder / "icon-1024.png"
    if not icon.is_file():
        return [Note(False, "1024 icon", "missing")]
    out: list[Note] = []
    with Image.open(icon) as art:
        if art.mode == "RGBA" or "transparency" in art.info:
            out.append(
                Note(
                    False,
                    "the 1024 icon has an alpha channel",
                    "App Store Connect refuses it, by email, after the upload",
                )
            )
        else:
            out.append(Note(True, "1024 icon has no alpha", f"{art.size[0]}x{art.size[1]}"))
        if art.size != (1024, 1024):
            out.append(Note(False, "1024 icon is not 1024x1024", f"{art.size}"))
    contents = folder / "Contents.json"
    if contents.is_file():
        try:
            json.loads(contents.read_text(encoding="utf-8"))
            out.append(Note(True, "the icon set's Contents.json parses", ""))
        except ValueError as exc:
            out.append(Note(False, "the icon set's Contents.json", str(exc)))
    return out


def _size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as art:
        return art.size


def check_screenshots(folder: Path) -> list[Note]:
    from PIL import Image

    if not folder.is_dir():
        return [
            Note(
                False,
                "no screenshots",
                f"{folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder} "
                "— run tools/appstore/screenshots.py",
            )
        ]
    shots = sorted(p for p in folder.rglob("*.png"))
    if not shots:
        return [Note(False, "no screenshots", "run tools/appstore/screenshots.py")]

    out: list[Note] = []
    phone = ipad = 0
    for shot in shots:
        with Image.open(shot) as art:
            size = art.size
        turned = (size[1], size[0])
        if size in IPHONE_SIZES or turned in IPHONE_SIZES:
            phone += 1
        elif size in IPAD_SIZES or turned in IPAD_SIZES:
            ipad += 1
        else:
            out.append(
                Note(
                    False,
                    f"{shot.name} is {size[0]}x{size[1]}",
                    "not a size App Store Connect accepts",
                )
            )
    # Apple requires at least one iPhone screenshot; three is the number that
    # actually fills the product page without a gap.
    # The two slots Apple actually requires, rather than "some of each".
    if (1290, 2796) not in {_size(p) for p in shots} and (1320, 2868) not in {
        _size(p) for p in shots
    }:
        out.append(
            Note(
                False,
                "no 6.9-inch iPhone screenshot",
                "1290x2796 or 1320x2868 — the slot every iPhone submission needs",
            )
        )
    if ipad and (2064, 2752) not in {_size(p) for p in shots}:
        out.append(
            Note(
                True,
                "no 13-inch iPad screenshot",
                "2064x2752 is the required iPad slot; smaller ones are scaled up from it",
                soft=True,
            )
        )
    if phone < 3:
        out.append(
            Note(False, f"only {phone} iPhone screenshot(s)", "three or more fills the page")
        )
    else:
        out.append(Note(True, f"{phone} iPhone screenshots at an accepted size", ""))
    if ipad:
        out.append(Note(True, f"{ipad} iPad screenshots at an accepted size", ""))
    else:
        out.append(
            Note(
                True,
                "no iPad screenshots",
                "required only if the app is offered on iPad",
                soft=True,
            )
        )
    return out


def check_documents() -> list[Note]:
    out: list[Note] = []
    for name, must in (
        ("PRIVACY.md", "nothing goes anywhere you did not put it"),
        ("TERMS.md", "no tolerance for objectionable content"),
    ):
        path = ROOT / name
        # Whitespace normalised before matching. These documents are hard
        # wrapped at 79 columns, so a phrase that spans a line break is not in
        # the file as far as `in` is concerned — which has now caught the same
        # check out twice.
        raw = path.read_text(encoding="utf-8") if path.is_file() else ""
        text = " ".join(raw.split()).lower()
        if must.lower() in text:
            out.append(Note(True, f"{name} says what it has to", ""))
        else:
            out.append(Note(False, f"{name}", f"does not contain {must!r}"))
    return out


def check_urls(online: bool) -> list[Note]:
    """The three URLs Apple asks for, actually fetched.

    A privacy policy URL that does not resolve is the single most common
    metadata rejection, and it is the one thing on this list that cannot be
    checked by reading a file.
    """
    urls = [
        ("support", IDENTITY.support_url),
        ("privacy policy", IDENTITY.privacy_url),
        ("terms", IDENTITY.terms_url),
    ]
    if not online:
        return [
            Note(True, f"{label} URL not fetched", f"{url} — pass --online", soft=True)
            for label, url in urls
        ]

    import urllib.error
    import urllib.request

    out: list[Note] = []
    for label, url in urls:
        try:
            with urllib.request.urlopen(url, timeout=15) as answer:  # noqa: S310 - https only
                code = answer.status
            out.append(
                Note(code == 200, f"{label} URL answers", f"{url} → {code}")
                if code == 200
                else Note(False, f"{label} URL", f"{url} → {code}")
            )
        except urllib.error.HTTPError as exc:
            out.append(Note(False, f"{label} URL", f"{url} → {exc.code}"))
        except Exception as exc:  # noqa: BLE001 - any failure is a failure
            out.append(Note(False, f"{label} URL", f"{url} → {exc}"))
    return out


def check_age() -> list[Note]:
    """The rating, and whether the app actually holds itself to it.

    A rating is a claim, and the questionnaire is where it is made. This is
    the other half: the sign-up gate, the restriction, and the lock all have
    to exist, and the number the app refuses below has to be the number the
    listing declares. Two places holding one number is how they end up
    disagreeing, so both are read here rather than remembered.
    """
    from auteur.web.auth import ADULT_AGE, MINIMUM_AGE

    server = (ROOT / "auteur" / "web" / "server.py").read_text(encoding="utf-8")
    profile_js = (STATIC / "profile.js").read_text(encoding="utf-8")
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    listing = (ROOT / "tools" / "appstore" / "listing.py").read_text(encoding="utf-8")

    out = [Note(True, f"the app refuses anybody under {MINIMUM_AGE}", f"adult at {ADULT_AGE}")]

    for what, ok in (
        ("sign-up asks for a year", 'id="signup-born"' in login),
        ("the server checks it", "MINIMUM_AGE" in server and "age_from" in server),
        ("the restriction has a route", '"/api/restriction"' in server),
        ("and a control", "restriction-row" in profile_js),
        ("lifting it can need a code", "check_restriction_lock" in server),
    ):
        out.append(Note(ok, f"12+: {what}", "" if ok else "missing"))

    # The listing says 12+; if the app's own floor ever moves, one of these
    # two has to move with it and this is what notices.
    if "**The rating is 12+.**" not in listing:
        out.append(Note(False, "the listing does not declare 12+", "tools/appstore/listing.py"))
    elif MINIMUM_AGE != 12:
        out.append(
            Note(
                False,
                f"the app refuses under {MINIMUM_AGE} and the listing says 12+",
                "move one of them",
            )
        )
    else:
        out.append(Note(True, "the listing and the app agree on 12+", ""))

    terms = " ".join((ROOT / "TERMS.md").read_text(encoding="utf-8").split())
    if f"for people {MINIMUM_AGE} and over" not in terms:
        out.append(Note(False, "the terms do not state the minimum age", "TERMS.md"))
    else:
        out.append(Note(True, "the terms state the minimum age", ""))
    return out


def check_safety() -> list[Note]:
    """Guideline 1.2, checked against the code rather than against a promise.

    Every one of these is a control a reviewer will look for by using the app,
    so what is checked is that the route and the control both exist — a server
    endpoint with no button is as absent as a button with no endpoint.
    """
    server = (ROOT / "auteur" / "web" / "server.py").read_text(encoding="utf-8")
    safety = (STATIC / "safety.js").read_text(encoding="utf-8")
    profile = (STATIC / "profile.js").read_text(encoding="utf-8")
    wants = [
        ("report content", '"/api/report"' in server and "auteurSafety" in safety),
        ("block a person", "/block" in server and "function block(" in safety),
        ("delete the account", '"/api/profile/delete"' in server and "delete-go" in profile),
        ("moderation tools", "def _run_moderate" in (ROOT / "auteur" / "cli.py").read_text()),
        ("terms are reachable", '"/terms"' in server),
    ]
    return [
        Note(ok, f"guideline 1.2: {what}", "" if ok else "missing on one side")
        for what, ok in wants
    ]


def check_play() -> list[Note]:
    """What Google Play asks that the App Store does not.

    This file was written when there was one store, and a check that only
    covers one store reports "ready to submit" to somebody who is about to
    submit to two. Play's blockers are its own: a Data safety declaration it
    will not infer from the binary, working access for a reviewer when anything
    sits behind a sign-in, and a listing whose short description is 80
    characters rather than Apple's 30.
    """
    from auteur import brand

    notes: list[Note] = []

    over = brand.too_long("play")
    notes.append(
        Note(
            not over,
            "every field fits Play's limits" if not over else "; ".join(over),
        )
    )

    pack = ROOT / "tools" / "play" / "listing.py"
    notes.append(
        Note(
            pack.is_file(), "the Play listing generates" if pack.is_file() else f"{pack} is missing"
        )
    )

    # Both stores tell the same story, or the copy has drifted again.
    apple = ROOT / "tools" / "appstore" / "listing.py"
    shared = apple.is_file() and "brand" in apple.read_text(encoding="utf-8")
    notes.append(
        Note(
            shared,
            (
                "both listings read the same copy"
                if shared
                else "the App Store listing keeps its own copy — it will drift from Play's"
            ),
            soft=not shared,
        )
    )

    # The package name is the one Play rejects outright, same as Apple.
    reserved = IDENTITY.bundle_id.startswith("com.example")
    notes.append(
        Note(
            not reserved,
            (
                f"package name is {IDENTITY.bundle_id}"
                if not reserved
                else f"package name is still {IDENTITY.bundle_id} — Play refuses com.example too"
            ),
        )
    )
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--online", action="store_true", help="fetch the three URLs rather than skipping them"
    )
    parser.add_argument(
        "--screenshots",
        default=str(ROOT / "build" / "appstore" / "screenshots"),
        help="where the store screenshots are",
    )
    args = parser.parse_args()

    groups = [
        ("Publisher", check_identity()),
        ("The bundle", check_info_plist()),
        ("Privacy manifest", check_privacy_manifest()),
        ("Icon", check_icon()),
        ("Screenshots", check_screenshots(Path(args.screenshots))),
        ("Documents", check_documents()),
        ("URLs", check_urls(args.online)),
        ("Safety", check_safety()),
        ("Age", check_age()),
        ("Google Play", check_play()),
    ]

    bad = 0
    print()
    print("  before the upload")
    print()
    for name, notes in groups:
        print(f"  {name}")
        for note in notes:
            if note.ok:
                mark = "  ·" if note.soft else "  ✓"
            else:
                mark = "  ✗"
                bad += 1
            line = f"    {mark} {note.what}"
            print(line)
            if note.detail:
                # Wrapped by hand rather than by textwrap: these are read in a
                # terminal at whatever width it happens to be.
                for chunk in re.findall(r".{1,72}(?:\s|$)", note.detail):
                    print(f"          {chunk.rstrip()}")
        print()

    # Everything above is a fact about this repository. This is the other
    # half, and printing it here is the point: "everything checkable from
    # here is right" is a true sentence that reads like "ready to submit",
    # and it is not the same sentence. The domain being registered is not
    # checkable from here, and it blocks the upload just as hard as a missing
    # icon does.
    waiting = pending()
    if waiting:
        print("  still waiting on the world")
        for item in waiting:
            print(f"      ☐ {item.what}")
            for chunk in re.findall(r".{1,68}(?:\s|$)", item.consequence):
                if chunk.strip():
                    print(f"          {chunk.rstrip()}")
            print(f"          confirm with: {item.confirm}")
        print()

    # The count of what is not checkable from here is printed either way.
    # It used to sit only on the success path, so the moment anything else
    # was red the tool went quiet about the domain, the entity and the
    # mailbox — which is the case where somebody is most likely to fix the
    # one red line and take that for ready. A clean checkout is exactly that
    # case: the store screenshots live in `build/`, which is generated and
    # gitignored, so every CI run reports one failure and would have lost
    # this line.
    if waiting:
        print(f"  {len(waiting)} thing(s) above are not checkable from here.")

    if bad:
        print(f"  {bad} thing(s) would come back from a store review. Fix them first.")
        print()
        return 1
    print("  everything checkable from here is right.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
