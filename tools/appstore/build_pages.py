"""Build the public site: the privacy policy, the terms, and a page to land on.

App Store Connect will not accept a submission without a privacy policy URL
that resolves, and an app carrying other people's content needs terms at a URL
too. Both documents already exist in this repository as markdown, and both are
already converted for the app itself — so this is the same converter pointed at
an output directory, rather than a second copy of either document that can go
stale against the first.

    python3 tools/appstore/build_pages.py [outdir]

The GitHub Actions workflow in `.github/workflows/pages.yml` runs this and
publishes the result, so the URLs in `auteur/identity.py` are addresses that
actually answer rather than ones to fill in later.

Two things it does that the in-app version does not:

* **Absolute, self-contained pages.** The app serves its stylesheets from
  `/static/`; a Pages site is served from a subdirectory and would 404 on every
  one of them, so the CSS is inlined. The result is three files that work from
  any path, including opened off a disk.
* **A landing page.** The support URL Apple asks for has to go somewhere that
  explains what the app is and how to reach somebody — a bare policy is not a
  support page.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur.identity import COMPANY, IDENTITY  # noqa: E402
from auteur.web import assets  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "pages"
STATIC = ROOT / "auteur" / "web" / "static"


def _inline(markup: str, sheets: list[str]) -> str:
    """Replace the <link> tags with the stylesheets themselves.

    A Pages site lives under `/<repo>/`, so `/static/style.css` resolves to the
    wrong place and the page arrives unstyled — which is exactly the state an
    App Store reviewer would see it in.
    """
    css = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in sheets)
    for name in sheets:
        markup = markup.replace(f'<link rel="stylesheet" href="/static/{name}">\n', "")
        markup = markup.replace(f'<link rel="stylesheet" href="/static/{name}">', "")
    # settings.js is served from /static/ too, and the accessibility settings
    # are worth having on a policy page — so it is inlined rather than dropped.
    script = (STATIC / "settings.js").read_text(encoding="utf-8")
    markup = markup.replace(
        '<script src="/static/settings.js"></script>',
        f"<script>\n{script}\n</script>",
    )
    return markup.replace("</head>", f"<style>\n{css}\n</style>\n</head>")


def _landing() -> str:
    """The support page. Says what the app is, and how to reach somebody."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="description" content="Auteur — support, privacy and terms.">
<title>{IDENTITY.app_name} — support</title>
<script>
{(STATIC / "settings.js").read_text(encoding="utf-8")}
</script>
<style>
{(STATIC / "theme.css").read_text(encoding="utf-8")}
{(STATIC / "style.css").read_text(encoding="utf-8")}
{(STATIC / "prose.css").read_text(encoding="utf-8")}
</style>
</head>
<body>
<main class="prose">
<h1>{IDENTITY.app_name}</h1>
<p>An editor that cuts a film out of what is already on your phone. It runs on
the device; there is no service behind it and no account with anybody.</p>

<h2>Getting help</h2>
<p>Write to <strong>{IDENTITY.support_email}</strong>. That address is read by a
person, and it is the right place for anything about the app itself — a fault,
a question, or a report about the app rather than about something inside it.</p>

<h2>Reporting something inside the app</h2>
<p>Every film, message and person carries a <strong>Report</strong> control, and
reporting also offers to block. <strong>Blocking is immediate</strong> and needs
nobody's permission: it takes both people out of each other's reach, in both
directions, at once.</p>
<p>A report goes to whoever runs the copy of Auteur you are using — which, for
this app, is a person you know, on a computer they own. There is no central
service that could receive it instead, and saying otherwise would be untrue.</p>

<h2>Deleting your account</h2>
<p>In the app: <strong>You &rarr; Delete my account</strong>. It removes the
account, every film you made and its files, every conversation you are part of,
your profile and picture, and your planned posts — immediately, with no copy
kept.</p>

<h2>The documents</h2>
<ul>
  <li><a href="privacy.html">Privacy policy</a></li>
  <li><a href="terms.html">Terms of use</a></li>
</ul>

<p class="fineprint">Published by {IDENTITY.developer}.</p>
</main>
</body>
</html>
"""


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    # Regenerate from the markdown first, so this can never publish a stale
    # copy of a document that has moved on.
    assets.ensure(STATIC)

    sheets = ["theme.css", "style.css", "prose.css"]
    for name in ("privacy.html", "terms.html"):
        page = (STATIC / name).read_text(encoding="utf-8")
        # The app's "back to the app" link goes nowhere on a public site.
        page = page.replace('<p class="prose-away"><a href="/">Back to the app</a></p>', "")
        (OUT / name).write_text(_inline(page, sheets), encoding="utf-8")

    (OUT / "index.html").write_text(_landing(), encoding="utf-8")
    # Jekyll would otherwise try to process this and drop anything it does not
    # recognise; the file is the documented way to tell Pages not to.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    # The custom domain, which Pages reads out of a file in the published
    # output and nowhere else. Without it Pages answers only at
    # <owner>.github.io/<repo>/, and the privacy policy Apple fetches during
    # review would 404 — the single most common metadata rejection there is.
    #
    # The *subdomain*, not the apex. auteurstudies.com is the company's own
    # site and this repository does not build it; putting the apex here would
    # take that site down the moment the DNS moved. The documents live beside
    # the code that makes them true, on a host of their own.
    #
    # Derived rather than typed, for the same reason the bundle identifier is.
    host = COMPANY.documents_for(IDENTITY.slug)
    (OUT / "CNAME").write_text(host + "\n", encoding="utf-8")

    for made in sorted(OUT.iterdir()):
        if made.is_file():
            print(f"{made}  {made.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
