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
- **The repository contains no credential material** — not a password, and not
  a hash of one either. The first run against an empty folder mints a random
  password, creates the one account with it, and prints it once; nothing is
  written down anywhere a stranger can read. Set `AUTEUR_USERNAME`,
  `AUTEUR_EMAIL` and `AUTEUR_PASSWORD` to choose your own instead, and nothing
  is ever generated.
- **`/api/stripe/webhook` is reachable without signing in, and that is the
  point.** Stripe has no session here, so the endpoint is authenticated by
  signature instead of by cookie: HMAC-SHA256 over the exact bytes received,
  compared in constant time, with a 300-second window so a captured signature
  cannot be replayed for ever. The raw body is read directly rather than
  through the JSON helper, because that helper decodes with
  `errors="replace"` and a single replaced byte is a signature that can never
  match.
- **With no `STRIPE_WEBHOOK_SECRET` set, that endpoint refuses everything.**
  Not "accepts unsigned events", not "warns and proceeds" — refuses. An
  instance deployed without it would otherwise be serving an unauthenticated
  grant-me-a-subscription endpoint on the public internet, and would look like
  it was working. "I cannot check this" and "this is fine" are opposite
  answers.
- **Nothing a browser can reach changes what somebody has paid for.** The
  webhook is the only writer of `Account.plan`; a test parses `server.py` and
  fails if any other function writes it or calls `apply_grant`.
- `AUTEUR_HOSTED=1` marks a copy that Auteur Studies runs and charges for, and
  is the only thing that turns the paid gate on. Unset — a copy on somebody's
  own machine — nothing is gated, which is correct: the tiers describe an
  instance that runs when you are not.
- **Closing a copy to new people is never gated on a plan.** Closing is what
  somebody does *because* an invite code got further than they meant, and a
  copy that cannot be shut because a card expired has turned its billing state
  into a security problem.

- A password must be at least 12 characters, must not appear on the usual
  guessing lists, must use more than a handful of distinct characters, and must
  not be built out of the username or email it protects. The same rules apply
  to the CLI, the reset form and the API — there is one function, and all three
  call it.
- Earlier releases shipped a scrypt hash for the seeded account. It has been
  removed, but removing something from a file does not remove it from git
  history: **any password that was ever the seeded one must be treated as
  compromised** and must not be reused here or anywhere else.
- Finished films and production notes are the user's own footage and sit behind
  the sign-in. Jobs are owned: being signed in is not permission to read
  somebody else's.

Reporting a problem
- Open a private security advisory on the repository rather than a public issue.
