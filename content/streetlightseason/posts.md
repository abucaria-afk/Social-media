# @streetlightseason — content plan

Week of 18–24 August 2026. Fifteen posts across three platforms, spaced by the
scheduling rules (4 h minimum gap per service, 3 per service per day) and placed
inside the optimal posting windows (07, 08, 11, 12, 17, 18, 19, 20, 21 UTC).

---

## Theme

Quiet urban and estuary landscapes, shot in the hours most people miss — early
morning, last light, after dark. The name is the brand: streetlights, seasons,
the in-between moments.

---

## Posts

### Monday 18 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 1 | 12:00 | Instagram Reel | Harbour lights after the last ferry. Moody grade, slow push-in on reflections. 20s. |
| 2 | 18:00 | TikTok | Streetlights switching on, one at a time. Real-time, no speed ramp. Hook: first light on frame 1. 25s. |

### Tuesday 19 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 3 | 08:00 | YouTube Short | Morning fog on the estuary. Wide static shot, mist lifting. Steel grade. 30s. |
| 4 | 17:00 | Instagram Reel | Rain on windscreen, dashboard reflected. Interior POV. Moody grade. 20s. |
| 5 | 21:00 | TikTok | Abandoned bus stop at night. Single streetlight, long shadow. Noir grade. 25s. |

### Wednesday 20 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 6 | 11:00 | YouTube Short | Heron on the mudflat. Patient hold, then takeoff. Natural light, steel grade. 30s. |
| 7 | 19:00 | Instagram Reel | Last six minutes of golden hour on the water. Warm grade, slow dissolves. 25s. |

### Thursday 21 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 8 | 07:00 | TikTok | City at 05:30 — empty, different acoustic. Push-in on pavement. Moody grade. 20s. |
| 9 | 12:00 | Instagram Reel | Overhead wires and undecided sky. Minimal composition, steel grade. 15s. |
| 10 | 18:00 | YouTube Short | Puddle reflections at 2x speed. City inverted. 25s. |

### Friday 22 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 11 | 17:00 | TikTok | Forgotten staircase, raking afternoon light. Architecture, moody grade. 25s. |
| 12 | 20:00 | Instagram Reel | Bridge at night, no traffic. Neon grade, long exposure feel. 20s. |

### Saturday 23 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 13 | 11:00 | YouTube Short | Wind through reeds — no music, natural sound only. Wide, static. 30s. |
| 14 | 19:00 | TikTok | Neon signs on wet tarmac. Night walk, handheld. Neon grade. 25s. |

### Sunday 24 Aug

| # | Time (UTC) | Platform | Brief |
|---|---|---|---|
| 15 | 12:00 | Instagram Reel | Empty Sunday streets, long shadows. Minimal, moody. 20s. |

---

## Production notes

- **Looks used:** moody, steel, neon, warm, noir, neutral
- **No AI narration.** Captions only; the footage speaks.
- **Music:** synthesised beds from `demo/make_track.py` to avoid copyright strikes
  (muted-audio failure mode is 11% of recorded failures).
- **Hook rule:** strongest frame at t=0, first cut by 1.2s.
- **Loop rule:** no fade-to-black, last shot returns to the opening clip.
- **Cover frames:** extracted at 20% of runtime (not frame 1).

---

## How to use

```bash
# Edit and render a post
python -m auteur workflow run instagram-reel ./rushes "harbour lights, moody, 20 seconds" \
    --schedule "2026-08-18T12:00:00Z"

# Check the queue
python -m auteur schedule

# After posting
python -m auteur schedule done a1b2c3d4
```

The schedule lives in `content/streetlightseason/schedule.json` and follows the
format `auteur schedule` reads. Videos are added to the `video` field once
rendered.
