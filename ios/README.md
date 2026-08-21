# Auteur for iOS

Everything needed to build and submit the app, except the two things that can
only exist on your machine: an Apple Developer account and a signing identity.

## What this is honest about

**This has never been compiled.** It was written in an environment with no
macOS, no Xcode and no Swift toolchain, so the Swift here has been reasoned
about carefully and never run. Expect to fix something the first time you build
it. Everything that *could* be checked without a Mac is checked, and is checked
by the test suite rather than by having been looked at once:

| Checked | Why it matters |
| --- | --- |
| the icon has no alpha channel | App Store Connect refuses it, by email, after the upload, naming something else |
| every plist parses | Xcode reports a malformed one as a failure several files away |
| the usage strings exist and are sentences | asking for a permission with no string crashes at the moment it asks |
| no permission is requested that nothing uses | reviewers reject this, and it is a worse dialog |
| the deployment target clears iOS 15.4 | below that WebKit has no `canvas.captureStream` and no film can be made |
| `arm64`, not `armv7` | 32-bit has not run iOS since 11; declaring it makes modern devices report as unsupported |
| the bundled page reaches nothing outside itself | there is no network entitlement, so an external reference is a blank region |
| the launch colour named in Info.plist exists | a missing one is not an error, the app just launches on a white flash |
| every job the shim sends has a `case` in Swift | otherwise a button does nothing and says nothing |

**It is not a wrapped website.** Guideline 4.2 rejects those, and rightly. The
page is loaded from the bundle and works with the device in aeroplane mode; the
app does real work on the device — it cuts and encodes a film — and the native
layer does the three things the web layer genuinely cannot: save to the camera
roll, the system share sheet, and write a shoot into the calendar. Nothing
leaves the phone, and the project asks for no network entitlement at all.

## Build it

You need a Mac with Xcode, and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`). The `.xcodeproj` is generated rather than committed:
it is a directory of XML that merges badly and hides what changed, and
`project.yml` is the same information in twenty readable lines.

```sh
python3 tools/artifact/build_artifact.py   # the page, from the current palette
python3 ios/scripts/build_bundle.py        # icons, colours, and the payload
cd ios && xcodegen generate
open Auteur.xcodeproj
```

Then, in Xcode: pick your team under Signing & Capabilities, and change
`PRODUCT_BUNDLE_IDENTIFIER` in `project.yml` from `com.example.auteur` to
something you own. Change it in `project.yml` rather than in Xcode, or the next
`xcodegen generate` puts it back.

Run it on a device rather than the simulator for anything involving the camera
roll or a real render.

## Submit it

```sh
xcodebuild -project Auteur.xcodeproj -scheme Auteur \
  -destination 'generic/platform=iOS' -archivePath build/Auteur.xcarchive archive

xcodebuild -exportArchive -archivePath build/Auteur.xcarchive \
  -exportOptionsPlist ExportOptions.plist -exportPath build
```

`ExportOptions.plist` is set to `app-store-connect` with `destination: upload`,
so the second command uploads rather than writing an `.ipa` to disk.

Then in App Store Connect you still have to supply, by hand, the things no
repository can hold: screenshots at the required sizes, a description, a
support URL, a privacy policy URL, and the answers to the privacy
questionnaire. For that last one the honest answers are all the same: this app
collects nothing, tracks nothing, and contacts no server —
`PrivacyInfo.xcprivacy` already says so and the questionnaire has to agree.

## What is likely to need fixing first

Listed in the order I would expect to hit them, because pretending the list is
empty helps nobody:

1. **Swift concurrency.** `SWIFT_STRICT_CONCURRENCY: complete` is on, and
   `Bridge` is `@MainActor` with a `nonisolated` delegate method. That is the
   correct shape and is also exactly where Swift 5.9 and 6 disagree about what
   they want spelled out.
2. **`MediaRecorder` in a web view.** It is present in Safari; web views have
   historically been where WebKit features arrive late or behind a flag. The
   app probes for it at launch and puts a sentence at the top of the page if it
   is missing, so the failure is legible — but if it *is* missing, the app
   cannot make a film and there is no way around that from here.
3. **Encoder output.** The renderer asks for `video/mp4;codecs=avc1` first,
   which is what Safari gives; if a web view returns WebM instead, the file
   saved to Photos will have the wrong extension. Check `recorder.mimeType`
   against the name in `native.js`.
4. **Memory on a long render.** A 1080×1920 canvas plus a recorder plus the
   source images is not small, and iOS kills an app that grows rather than
   telling it to stop.

## Where things are

```
ios/
  project.yml               the Xcode project, as a file worth reading
  ExportOptions.plist       app-store-connect, upload
  scripts/build_bundle.py   icons, accent colour, and the web payload
  Auteur/
    AuteurApp.swift         one screen
    WebHost.swift           the web view and the rules it runs under
    Bridge.swift            save, share, calendar, capabilities
    native.js               injected first; fills in what a web view lacks
    Info.plist              permissions, orientation, launch
    PrivacyInfo.xcprivacy   nothing collected, nothing tracked
    Assets.xcassets/        icon and colours, generated from auteur/theme.py
    Web/index.html          the page, generated from the artifact build
```

Nothing in `Assets.xcassets` or `Web/` is written by hand. Both are generated
from what is already in this repository — the icon from `auteur/web/assets.py`,
the colours from `auteur/theme.py`, the page from the artifact build — so there
is no second copy of any of them to go stale. Re-run `build_bundle.py` after
changing the palette.
