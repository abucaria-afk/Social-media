# Security

This project uses optional LLM APIs (Anthropic) and calls out to external binaries (ffmpeg). Follow these guidelines to avoid leaking secrets and to run the project safely.

Secrets / API keys
- Never commit API keys, tokens, or credentials into the repository or its history.
- Store keys in environment variables or a secrets manager. The code expects Anthropic credentials in one of the following environment variables:
  - AUTEUR_ANTHROPIC_KEY (preferred)
  - ANTHROPIC_API_KEY
  - ANTHROPIC_AUTH_TOKEN
- Do not log full request payloads or environment variables. Keys and secrets should be masked in logs.
- On CI, put secrets into the repository or organization secrets (Actions secrets) and never echo them in workflow logs.

Running the model director
- The model director will not run unless:
  - `Settings.use_llm` is enabled (not `--no-llm`), AND
  - an Anthropic API key is present in environment variables (see above), AND
  - the `anthropic` Python package is installed.
- If these conditions are not met, the code falls back to the heuristic director.

External binaries and resource usage
- The project calls ffmpeg/ffprobe extensively. Use a trusted, up-to-date ffmpeg binary.
- Prefer a system ffmpeg installed by your OS package manager (apt / brew) or a vetted static wheel (ffmpeg-binaries) if you cannot install system packages.
- Be cautious running renders on multi-tenant CI; consider sandboxing heavy renders in containers and limit concurrency.

Safe workspace and file handling
- When passing a workspace or output directory, avoid system roots like `/`, `/etc`, `/usr`, `/bin`, `/sbin` and `/var`.
- The code includes a helper to validate workspace paths; use it to prevent accidental writes to system directories.

Reporting vulnerabilities
- Use Dependabot and pip-audit (the project includes CI automation) to detect vulnerable dependencies.
- If you discover a security issue, open an issue privately or contact the repository owner; avoid posting secrets publicly.


The phone app (`auteur serve`)
- It is built for your own network: your computer and your phone on the same
  wifi. `--host 127.0.0.1` keeps it to the one machine.
- **There is no TLS.** On a plain `http://` LAN address the password and the
  session cookie cross the network in the clear. Anything wider than a home
  network should sit behind a reverse proxy with a certificate, and set
  `AUTEUR_PUBLIC_URL` so password-reset links carry the right address.
- Passwords are stored as salted scrypt hashes (n=2^15) in
  `<serve folder>/accounts.json`, outside the repository and gitignored.
  Session and reset tokens are stored hashed, so a copy of that file cannot be
  replayed.
- The repository contains a scrypt hash for the seeded first account — a hash,
  never a password. `python -m auteur account password` replaces it for good,
  and `AUTEUR_PASSWORD=...` on the first run means it is never used at all.
- Finished films and production notes are the user's own footage and sit behind
  the sign-in. Jobs are owned: being signed in is not permission to read
  somebody else's.

Reporting a problem
- Open a private security advisory on the repository rather than a public issue.
