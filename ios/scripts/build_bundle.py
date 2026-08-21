"""Assemble everything the Xcode project needs that is not Swift.

Run before opening the project, and again whenever the web build or the palette
changes. Everything it writes is derived from something already in this
repository — the icon from `auteur.web.assets`, the colours from `auteur.theme`,
the page from `tools/artifact/build_artifact.py` — so there is no second copy of
any of them to drift.

Three things it checks rather than assumes, because each one is rejected at
upload time rather than at build time and the message you get is unhelpful:

* an App Store icon may not have an alpha channel. The generated icon is RGBA,
  so it is flattened, and this asserts that it worked.
* the icon may not be a template or contain transparency at the corners; iOS
  masks the corners itself, so the artwork must be a full square.
* the web payload must carry no external reference, because the app loads it
  from the bundle with no network entitlement at all.

    python3 ios/scripts/build_bundle.py
"""

import json
import plistlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IOS = HERE.parent
ROOT = IOS.parent
sys.path.insert(0, str(ROOT))

from auteur import theme  # noqa: E402
from auteur.web import assets  # noqa: E402

#: One 1024 icon is what current Xcode wants. The smaller sizes are still
#: written because older toolchains and some CI images ask for them, and an
#: unused PNG costs nothing next to a submission failing at 2am.
ICON_SIZES = (1024, 180, 167, 152, 120, 87, 80, 76, 60, 58, 40, 29, 20)


def icons() -> Path:
    folder = IOS / "Auteur" / "Assets.xcassets" / "AppIcon.appiconset"
    folder.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    for size in ICON_SIZES:
        drawn = assets._draw(size)
        # No alpha. App Store Connect rejects an icon with a channel it does
        # not use, and the error names the wrong thing when it does.
        flat = Image.new("RGB", drawn.size, theme.rgb_of("ground"))
        flat.paste(drawn, mask=drawn.split()[3])
        path = folder / f"icon-{size}.png"
        flat.save(path, "PNG")
        with Image.open(path) as check:
            assert check.mode == "RGB", f"{path.name} still carries an alpha channel"
            assert check.size == (size, size)

    # Single-size app icon: one 1024 entry, which is what Xcode 14 and later
    # want and what removes the whole class of "missing 76x76" failures.
    (folder / "Contents.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "filename": "icon-1024.png",
                        "idiom": "universal",
                        "platform": "ios",
                        "size": "1024x1024",
                    }
                ],
                "info": {"author": "xcode", "version": 1},
            },
            indent=2,
        )
        + "\n"
    )
    return folder


def accent() -> Path:
    """The tint colour, from the same palette as everything else."""
    folder = IOS / "Auteur" / "Assets.xcassets" / "AccentColor.colorset"
    folder.mkdir(parents=True, exist_ok=True)

    def components(role: str, scheme: str) -> dict:
        r, g, b = theme.rgb_of(role, scheme)
        return {
            "color-space": "srgb",
            "components": {
                "alpha": "1.000",
                "red": f"0x{r:02X}",
                "green": f"0x{g:02X}",
                "blue": f"0x{b:02X}",
            },
        }

    folder.joinpath("Contents.json").write_text(
        json.dumps(
            {
                "colors": [
                    {"color": components("ember", "light"), "idiom": "universal"},
                    {
                        "appearances": [{"appearance": "luminosity", "value": "dark"}],
                        "color": components("ember", "dark"),
                        "idiom": "universal",
                    },
                ],
                "info": {"author": "xcode", "version": 1},
            },
            indent=2,
        )
        + "\n"
    )
    return folder


def web() -> Path:
    """The page the app loads, copied out of the artifact build."""
    source = ROOT / "tools" / "artifact" / "auteur-app.html"
    if not source.is_file():
        raise SystemExit("run tools/artifact/build_artifact.py first")
    body = source.read_text(encoding="utf-8")

    # The app has no network entitlement and loads this from the bundle, so a
    # single external reference is a silently blank region on somebody's phone.
    outside = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', body)
    if outside:
        raise SystemExit(f"the page reaches outside the bundle: {outside[:3]}")

    folder = IOS / "Auteur" / "Web"
    folder.mkdir(parents=True, exist_ok=True)
    # A whole document this time, unlike the artifact host's wrapper.
    page = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover, user-scalable=no">\n'
        '<meta name="color-scheme" content="dark light">\n'
        f"{body}\n</html>\n"
    )
    (folder / "index.html").write_text(page, encoding="utf-8")
    return folder


def check_plists() -> list[Path]:
    """Parse every plist, because Xcode reports a malformed one as a build
    failure three steps away from the file that is wrong."""
    found = []
    for path in sorted(IOS.rglob("*.plist")) + sorted(IOS.rglob("*.xcprivacy")):
        with path.open("rb") as handle:
            plistlib.load(handle)
        found.append(path)
    return found


def main() -> int:
    print(f"  icons   {icons()}")
    print(f"  accent  {accent()}")
    print(f"  web     {web()}")
    for path in check_plists():
        print(f"  plist   {path.relative_to(IOS)} parses")
    size = sum(f.stat().st_size for f in (IOS / "Auteur").rglob("*") if f.is_file())
    print(f"\n  bundle payload {size / 1_048_576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
