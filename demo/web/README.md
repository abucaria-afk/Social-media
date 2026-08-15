Demo web placeholders — accessibility & testing

This folder contains minimal mobile-first PWA placeholders. The branch "improve/demo-accessibility" adds accessibility improvements and CI audits that run privately in GitHub Actions.

Quick local checks

1) Serve locally
   - python -m http.server 8000
   - open http://localhost:8000/demo/web/index.html

2) Run a quick pa11y scan (if you have npm)
   - npm install -g pa11y
   - pa11y http://localhost:8000/demo/web/index.html

3) Run Lighthouse locally (headless)
   - npx lighthouse http://localhost:8000/demo/web/index.html --preset=mobile --output html --output-path=./report.html

Accessibility changes applied
- Skip link to jump to main content for keyboard/screen-reader users.
- ARIA roles: banner, main, contentinfo.
- Visible keyboard focus styles and focus-visible support.
- prefers-reduced-motion respected.
- Button uses accessible semantics and an aria-label.
- Images include alt text; replace with content-appropriate alt text when you add real assets.
- Touch targets sized >= 44×44 CSS px.

CI testing (private)
- The repository contains a GitHub Actions workflow (demo accessibility) that builds and serves the demo inside the runner and runs pa11y and Lighthouse (report-only). Artifacts are uploaded for review; nothing is published publicly.

Manual device testing
- For iPhone testing use Safari Web Inspector (connect device to Mac) and verify safe-area, touch targets, keyboard navigation, and reduced-motion behaviour.
