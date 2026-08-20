# Published versions

Every build published to the shared link, newest first. The link never
changes — <https://claude.ai/code/artifact/f399bd38-1934-4fff-a04d-d73b21af1ece>
— so this is how to tell which one you are looking at, and the artifact's own
version picker can roll back to any of them by the label in the second column.

The page shows its own version in the banner, so a screenshot is enough to
know what was in it.

| # | label | what changed |
|---|---|---|
| 6 | `v6-cuts-grades-templates` | Cuts became decisions: eight transitions (portal, subject carry, whip, push, luma, slice, flash, match) where before every join in every film was a hard cut. Seven camera gestures including *hold*, which did not exist. Grading is real per-pixel work — tone curves, split toning, halation, grain — measured, where the old fallback moved the picture 6.6/255 and was invisible. Six eras: 70s, 80s, 90s, 2000s, 2010s, 2020s. Every one of the 18 reference reels is a template you can cut to. A decade in the prompt is no longer read as a runtime — "a 90s hypercut, 12 seconds" was making a 60 second film. |
| 5 | `v5-accounts-and-connections` | Claim the app from your phone — a sign-up screen instead of reading a password off a terminal (`auteur serve --claim`). New "Where it goes" tab: link Instagram and TikTok so a film is cut to the right shape with its caption ready. Nothing here posts. |
| 4 | `v4-animation-tab` | Animation tab: eight shapes, seven movements, each chip a live canvas running the same code the renderer draws with. Graphics land on the film. Fixed three appearance switches where only the first was ever wired. |
| 3 | `v3-scholar-and-structure` | The Scholar answers from what it studied when it has no model. A thirty second reel stopped being a five second loop six times — hook, movements, and a return to the opening frame so it loops. |
| 2 | `v2-photos-and-prompt-fixes` | Photographs stopped being silently dropped. Each shot framed separately so cuts are visible. Eight looks instead of five regexes; quoted words go on screen; the film says what it heard. |
| 1 | `v1-edit-room` | The edit room, the studio, and a renderer that cuts a real film in the browser. |

## Templates

Every reference reel is read shot by shot and its timeline shipped in the page:
eighteen of them, one per distinct reel. Choosing one cuts your photographs to
where that reel's cuts actually fall, not to an average of its speed. No
footage from any reel is included or reachable — a template is a list of
numbers.

## What is not in the published page

The page is the app's own front end with a browser renderer standing in for
ffmpeg. These need the full program on a machine:

- **Scrolling a feed** — the Scholar being served reels and measuring what
  arrived (`auteur scholar scroll`). Needs a route to YouTube.
- **Posting** — no build posts anything anywhere, published or not.
