Title: Improve CI, add linters, CodeQL, Dependabot, and docs

Description:
This PR brings the repository up to common Python project best-practices and improves mobile readiness for future web frontends.

What
- Add MIT LICENSE
- Update README to reflect Python auteur package and demo
- Add Dependabot for Python (weekly)
- Add lint-and-type workflow (ruff, black, mypy)
- Add pip-audit workflow (weekly)
- Add coverage workflow (uploads coverage.xml artifact)
- Add CodeQL analysis workflow
- Add pre-commit config
- Add mobile/PWA placeholders under demo/web (manifest, icons, sample index tuned for iPhone 13)

Why
- Improves contributor experience and CI quality
- Adds automated dependency and security scanning
- Adds mobile-first placeholders to ease future web deployments

Checklist
- [ ] Review CI workflow runs (pytest, lint, CodeQL)
- [ ] Review pip-audit and Dependabot PRs for vulnerable dependencies
- [ ] Run pre-commit hooks locally and fix issues
- [ ] Replace demo/web placeholder assets with production images if you use the demo site
- [ ] Merge (Squash and merge recommended) after checks complete

Merge guidance
- Recommended merge method: "Squash and merge" to keep a tidy main branch.
- Protect main afterwards: require status checks and at least one approving review.

Notes
- I cannot open the PR on your behalf from this assistant, but the branch is ready at:
  https://github.com/abucaria-afk/Social-media/tree/improve%2Fci-and-docs

- Use this link to open the PR in GitHub with the prepared branch:
  https://github.com/abucaria-afk/Social-media/pull/new/improve/ci-and-docs

If you want, I can also:
- Merge the branch once you confirm and CI passes (I will wait for your instruction),
- Replace SVG placeholders with optimized PNG/WebP assets,
- Add Lighthouse CI if you provide a deployed preview URL.
