
## Mobile placeholders and iPhone 13 testing

This repository did not include a web frontend; the files under demo/web are minimal mobile-first placeholders to make a future web deployment behave well on iOS (iPhone 13).

Included files (in this branch):

- demo/web/index.html — example page with meta viewport, safe-area CSS, example touch target and responsive image
- demo/web/manifest.json — simple web manifest
- demo/web/apple-touch-icon.svg, icon-192.svg — placeholder icons (SVG)
- demo/web/image-1x.svg, image-2x.svg, image-3x.svg — placeholder responsive images

How to use

1. Serve the demo/web folder over HTTPS from your web server or static host (GitHub Pages, Netlify, Vercel).
2. Open the page on an iPhone 13 (or emulator) and verify:
   - No horizontal scrolling at default zoom
   - Touch targets >= 44x44 px
   - Safe-area spacing around notch and home indicator
   - Images look sharp (replace SVG placeholders with real PNG/WebP @1x/2x/3x assets)

If you want, I can:
- Replace the SVG placeholders with real PNG/WebP files and add a small action to build optimized assets.
- Add a Lighthouse CI workflow to run automated mobile audits against a deployed preview URL.
- Integrate the demo into a simple Flask/FastAPI static-serve route so previews can be generated in the CI.
