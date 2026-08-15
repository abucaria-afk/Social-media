safety: sanitize model outputs and clamp numeric inputs

- Sanitize model-derived strings used in ffmpeg filters.
- Clamp durations / offsets and other numeric fields (speed, offsets).
- Added auteur/util.safe_workspace_path to refuse obvious system roots.
- No behavioural changes except stricter validation and conservative defaults.
