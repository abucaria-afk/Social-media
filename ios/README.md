# Auteur for iOS

Everything needed to build and submit the app, except the three things that can
only exist on your side: an Apple Developer account, a signing identity, and a
domain you own.

## Fill in three values, once

Everything a publisher has to decide lives in `auteur/identity.py`, and nothing
else in the repository repeats it. Set them as environment variables, or edit
that file:

```sh
export AUTEUR_BUNDLE_ID=com.yourname.auteur     # reverse-DNS on a domain you own
export AUTEUR_DEVELOPER="Your Name"             # the name on the listing
export AUTEUR_SUPPORT_EMAIL=you@yourdomain.com  # required by guideline 1.2
```

`com.example.*` is Apple's reserved documentation domain and is rejected, which
is why the default is one that fails the preflight rather than one that looks
plausible enough to ship by accident.

The three URLs App Store Connect asks for default to this repository's GitHub
Pages addresses, and `.github/workflows/pages.yml` publishes the privacy policy
and the terms there from the same markdown the app itself converts. Turn Pages
on (Settings → Pages → Source: GitHub Actions) and they answer.

## Build it

You need a Mac with Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`).

```sh
python3 tools/artifact/build_artifact.py   # the page, from the current palette
python3 ios/scripts/build_bundle.py        # icons, colours, identity, payload
cd ios && xcodegen generate
open Auteur.xcodeproj
```

Pick your team under Signing & Capabilities. Do not change the bundle
identifier in Xcode — it comes from `Identity.yml`, which `build_bundle.py`
writes, so the next `xcodegen generate` would put it back.

Run on a device rather than the simulator for anything involving the camera
roll or a real render.

## Before every upload

```sh
python3 tools/appstore/screenshots.py      # real captures at the sizes required
python3 tools/appstore/listing.py          # the store copy, within its limits
python3 tools/appstore/preflight.py --online
```

The preflight fails on everything that otherwise comes back as an email *after*
the archive is built, signed and uploaded, naming something adjacent to the
real problem:

| Checked | What it costs to find later |
| --- | --- |
| the bundle identifier is not `com.example.*` | rejected outright |
| the icon has no alpha channel | refused after the upload, by email |
| every plist parses | Xcode reports it as a failure several files away |
| usage strings exist, and are sentences | asking for a permission with no string crashes |
| no permission is declared that nothing uses | a reviewer's question, and a worse dialog |
| every required-reason API the Swift uses is declared | ITMS-91053, after the upload |
| screenshots are a size the form accepts | refused, without saying which dimension |
| the version and build match `identity.py` | a build number already used is refused |
| the three URLs actually answer | the most common metadata rejection there is |
| the listing fits its fields | enforced after you have written past it |
| the guideline 1.2 controls exist on both sides | the rejection this app is most exposed to |

`.github/workflows/appstore.yml` runs it on every push, and weekly, so a
domain that stops answering is found before a submission rather than during
one.

## Then submit

```sh
xcodebuild -project Auteur.xcodeproj -scheme Auteur \
  -destination 'generic/platform=iOS' -archivePath build/Auteur.xcarchive archive

xcodebuild -exportArchive -archivePath build/Auteur.xcarchive \
  -exportOptionsPlist ExportOptions.plist -exportPath build
```

`ExportOptions.plist` is `app-store-connect` with `destination: upload`, so the
second command uploads rather than writing an `.ipa`.

`build/appstore/listing.md` — written by `tools/appstore/listing.py` — has every
field App Store Connect asks for already answered: the description, the
keywords, the age-rating answers, the App Privacy answers (all "no", matching
`PrivacyInfo.xcprivacy` exactly, which they have to), the export compliance
answer, and the notes for App Review.

## The two guidelines this app is most exposed to

**1.2, user-generated content.** The feed and the inbox are other people's
content, even on an instance one household runs, and a reviewer will look for
four things by using the app. All four are there:

* **Report** — on every film (⋯ on its rail), every conversation (⋯ in its
  header) and every person (⋯ on their profile). Eight reasons, a note, and
  blocking offered in the same step.
* **Block** — immediate, needs no approval, and a wall rather than a mute: it
  works in both directions, so neither person can see the other's films or
  write to them.
* **Filtering and takedown** — `auteur moderate` shows what has been reported,
  with anything about a child's safety, violence or illegality first, and can
  remove any film or close any account.
* **Published contact** — `AUTEUR_SUPPORT_EMAIL`, which appears in the terms,
  on the support page and in the review notes.

Plus terms with no tolerance for objectionable content or abusive users, agreed
to where the account is made.

**5.1.1(v), account deletion.** *You → Delete my account*, which asks for the
password and for the word "delete" to be typed, then removes the account, the
films and their files, the conversations, the profile and picture, the plans
and the added reels — immediately, with no copy kept. `auteur moderate close
<person>` does the same from the operator's side.

Both are driven end to end in a real browser by
`tools/artifact/check_safety.py`, which reports what it measured.

## What this is honest about

**This has never been compiled.** It was written in an environment with no
macOS, no Xcode and no Swift toolchain, so the Swift here has been reasoned
about carefully and never run. Expect to fix something the first time you build
it. Everything that *could* be checked without a Mac is checked, and by the
test suite rather than by having been looked at once.

**It is not a wrapped website.** Guideline 4.2 rejects those, and rightly. The
page is loaded from the bundle and works with the device in aeroplane mode; the
app does real work on the device — it cuts and encodes a film — and the native
layer does the four things the web layer genuinely cannot: save to the camera
roll, the system share sheet, write a shoot into the calendar, and reach an
instance on the local network.

## What is likely to need fixing first

In the order I would expect to hit them, because pretending the list is empty
helps nobody:

1. **Swift concurrency.** `SWIFT_STRICT_CONCURRENCY: complete` is on, and
   `Bridge` is `@MainActor` with a `nonisolated` delegate method. That is the
   correct shape and is also exactly where Swift 5.9 and 6 disagree about what
   they want spelled out.
2. **`MediaRecorder` in a web view.** It is present in Safari; web views have
   historically been where WebKit features arrive late. The app probes at
   launch and says so rather than failing into a blank screen.
3. **Encoder output.** The renderer asks for `video/mp4;codecs=avc1` first; if
   a web view returns WebM instead, the file saved to Photos has the wrong
   extension. Check `recorder.mimeType` against the name in `native.js`.
4. **Memory on a long render.** A 1080×1920 canvas plus a recorder plus the
   source images is not small, and iOS kills an app that grows rather than
   telling it to stop.

## Where things are

```
ios/
  project.yml               the Xcode project, as a file worth reading
  Identity.yml              generated from auteur/identity.py — do not edit
  ExportOptions.plist       app-store-connect, upload
  scripts/build_bundle.py   icons, accent colour, identity, and the web payload
  Auteur/
    AuteurApp.swift         one screen
    WebHost.swift           the web view and the rules it runs under
    Bridge.swift            save, share, calendar, capabilities
    Instance.swift          connecting to a copy you run yourself
    native.js               injected first; fills in what a web view lacks
    Info.plist              permissions, orientation, launch
    PrivacyInfo.xcprivacy   nothing collected, nothing tracked
    Assets.xcassets/        icon and colours, generated from auteur/theme.py
    Web/index.html          the page, generated from the artifact build

tools/appstore/
  preflight.py              everything App Store Connect would send back
  listing.py                the store copy and every form answer
  screenshots.py            real captures at 1290x2796 and 2048x2732
  build_pages.py            the privacy policy and terms as a public site
```

Nothing in `Assets.xcassets`, `Web/` or `Identity.yml` is written by hand. All
three are generated from what is already in this repository — the icon from
`auteur/web/assets.py`, the colours from `auteur/theme.py`, the page from the
artifact build, the identity from `auteur/identity.py` — so there is no second
copy of any of them to go stale. Re-run `build_bundle.py` after changing any of
those.
