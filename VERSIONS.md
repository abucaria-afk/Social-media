# Published versions

Every build published to the shared link, newest first. The link never
changes — <https://claude.ai/code/artifact/f399bd38-1934-4fff-a04d-d73b21af1ece>
— so this is how to tell which one you are looking at, and the artifact's own
version picker can roll back to any of them by the label in the second column.

The page shows its own version in the banner, so a screenshot is enough to
know what was in it.

| # | label | what changed |
|---|---|---|
| 5 | `v5-accounts-and-connections` | Claim the app from your phone — a sign-up screen instead of reading a password off a terminal (`auteur serve --claim`). New "Where it goes" tab: link Instagram and TikTok so a film is cut to the right shape with its caption ready. Nothing here posts. |
| 4 | `v4-animation-tab` | Animation tab: eight shapes, seven movements, each chip a live canvas running the same code the renderer draws with. Graphics land on the film. Fixed three appearance switches where only the first was ever wired. |
| 3 | `v3-scholar-and-structure` | The Scholar answers from what it studied when it has no model. A thirty second reel stopped being a five second loop six times — hook, movements, and a return to the opening frame so it loops. |
| 2 | `v2-photos-and-prompt-fixes` | Photographs stopped being silently dropped. Each shot framed separately so cuts are visible. Eight looks instead of five regexes; quoted words go on screen; the film says what it heard. |
| 1 | `v1-edit-room` | The edit room, the studio, and a renderer that cuts a real film in the browser. |

## What is not in the published page

The page is the app's own front end with a browser renderer standing in for
ffmpeg. These need the full program on a machine:

- **Templates** — reading a reel shot by shot and cutting your photographs to
  its timing (`auteur template`). CLI and library only.
- **Scrolling a feed** — the Scholar being served reels and measuring what
  arrived (`auteur scholar scroll`). Needs a route to YouTube.
- **Posting** — no build posts anything anywhere, published or not.
