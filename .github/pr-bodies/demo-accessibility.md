demo(web): accessibility improvements + CI audits

- Added skip link, ARIA roles (banner/main/contentinfo), visible keyboard focus styles.
- Added prefers-reduced-motion support, touch-target sizing >=44×44 CSS px, alt text guidance.
- Added .github/workflows/demo-accessibility.yml: private CI that serves demo/web inside the runner and runs pa11y + Lighthouse (mobile & desktop). Reports are uploaded as workflow artifacts (report-only).
- No public deploys; audits run locally inside the Action runner.
