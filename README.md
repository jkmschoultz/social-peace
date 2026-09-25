# social-peace

In a world where social media feeds are increasingly filled with AI generated "slop", this program aims to generate more positive "slop"; to encourage more calming, nature filled content to appear in media feeds. Fighting fire with fire, this program assembles calming, original short-form videos (nature footage + ambient sounds) and posts it to YouTube Shorts and Instagram Reels upon user approval.

**Content policy:** original or properly licensed assets only (Pixabay / Pexels /
Coverr / your own). No scraping other creators, no fake engagement, no re-uploading
third-party clips. See [assets/README.md](assets/README.md) for licensing rules.

---

## Layout

```
config/
  config.yaml          global settings: paths, durations, metadata, fetch keys, pipeline
  templates.yaml       templated variation: color grades, transitions, overlay + audio pairing
assets/
  video/ audio/ fonts/  your curated library (gitignored); add a .ttf to fonts/
  manifest.yaml         optional: per-file tags + license provenance
social_peace/
  config.py  logging_setup.py  ledger.py  cli.py
  pipeline/            selectors -> overlays -> assemble (ffmpeg filtergraph) -> metadata sidecar
                      auto.py = the `pipeline` command (scrape -> promote -> render a batch)
  fetch/              pixabay.py / pexels.py (video) + freesound.py (CC0 audio)
  review/            local Flask web UI to approve / reject renders before they post
  publish/           base.py + youtube.py (skeleton); tiktok/instagram are skipped stubs
scripts/
  run_daily.sh         scheduler entrypoint (Linux/macOS)
  run_daily.ps1        scheduler entrypoint (Windows)
  register_task.ps1    registers the Windows Scheduled Task
output/                rendered <name>.mp4 + <name>.json sidecar (gitignored)
logs/                  social_peace.log + posts.jsonl ledger (gitignored)
```

## Automated loop

```bash
# 1. scrape clips + CC0 audio, auto-promote them, render a batch for review
python -m social_peace pipeline               # uses config/config.yaml -> pipeline:
python -m social_peace pipeline --no-fetch     # just render from the current asset pool

# 2. review: tabs pending / approved / published / rejected. Play each render,
#    edit captions, Approve / Reject, then Publish (published renders move to the
#    published tab). Header buttons: "+ Generate new content" (render another
#    batch), "Fetch clips" / "Fetch audio" (top up the asset library for future
#    runs). Every card has "↻ clips / ↻ audio / ↻ text" to re-render it with one
#    thing swapped, with a "prefer …" box to bias the re-roll toward assets
#    matching a scene/sound wish ("river ambience"); the re-roll also leans away
#    from assets already used by accepted/published renders (below "prefer" in
#    priority). Sort dropdown (newest / oldest / seed / template / status).
#    Header right side: sort · custom-query box · fetch clips · fetch audio ·
#    fetch captions · Generate. "⬇ captions" asks Claude for a fresh batch of
#    overlay / title / caption template lines and appends them to
#    config/caption_bank.yaml, growing the pool future renders pick from (needs
#    ANTHROPIC_API_KEY — there is no non-LLM source for new caption copy).
#    A job strip under the header shows any running / just-finished
#    fetch / generate / publish job and persists as you navigate between pages;
#    when a generate / variant / fetch finishes it refreshes the grid or asset
#    list in place (no full reload — playing videos and scroll are kept).
#    The "assets" nav link lists every clip / bed (source, licence, size, which
#    render states use it): ★ favourite (favourites get preferred in selection),
#    ✎ rename (display label, kept in the manifest), remove, sort (favourite /
#    name / size / duration / newest), a show filter (usage, favourite, or
#    video-only / audio-only), and a custom-query fetch bar. Open it as its own
#    page ("assets ↗") or as a right-side drawer ("assets ▸") without leaving the
#    grid. The rejected tab has an "Empty rejected" button.
python -m social_peace review                  # http://127.0.0.1:8756

# reclaim output/ space — drop rejected renders (cron this; the review server
# also does it on startup, per retention.rejected_days in config.yaml)
python -m social_peace prune                    # older than 30 days
python -m social_peace prune --all              # every rejected render

# re-render one video with a single dimension re-picked (CLI equivalent of ↻)
python -m social_peace variant <stem> --change audio

# grow the caption pool: Claude writes new overlay / title / caption template
# lines into config/caption_bank.yaml (merged over config.yaml at load time)
python -m social_peace captions-bank                # ~6 new lines per list
python -m social_peace captions-bank --per-list 10

# 3. or publish from the CLI: everything approved and not already posted
python -m social_peace publish-approved
python -m social_peace publish-approved --dry-run   # show what would go, post nothing
```

`pipeline` (and the UI's Generate button) first checks the library: if there
aren't enough clips / beds that no pending, approved or published render already
uses — enough for the batch (~2.5 clips and ~1.5 beds per render) and at least
`pipeline.min_unused_clips` / `min_unused_audio` — it fetches the shortfall from
`pipeline.fetch_sources` + Freesound. Search terms rotate round-robin through
`pipeline.queries` across runs (position kept in `logs/fetch_state.json`), and
each fetcher skips stock ids the library already has or once had
(`assets/*/.fetched.json`, so deleted clips aren't re-fetched), paging deeper
when a term comes round again. Downloads go straight into the render pool
(`auto_promote`), then the batch renders — each sidecar starts at
`review.state: pending`. Audio scraping needs `FREESOUND_API_KEY`; without it
that step is skipped and the existing `assets/audio/` is used. The review UI
shows every source clip's Pexels/Pixabay/Freesound link so the licence +
model-release check happens per video.

**Asset spread:** within a batch (and against other non-rejected renders) the
selector prefers clips/audio it hasn't used yet, so a run doesn't keep reaching
for the same clip. Rejecting a render frees its assets. Clips are then ranked by
how well their tokens/manifest tags match the chosen audio's — a `rain` bed lands
on a waterfall/stream clip (`_THEME` in `pipeline/selectors.py` maps the
keywords). Fetched files carry their search query as manifest tags to feed this.

**Publishing:** the review page's approved filter has one **Publish** button per
platform on each card, plus a **Publish all approved** button in the header. All
post to `project.target_platforms ∩ {youtube, tiktok, instagram}` and write each
platform's result into the sidecar `status`. A platform button greys out (✓) once
that video's status is `uploaded` / `published` / `already-published`, and the
ledger dedupe blocks a re-post even if you force it.

- **youtube** — live. `<`/`>` are swapped for look-alikes (YouTube rejects them).
  Testing-mode OAuth forces `private` uploads.
- **instagram** — live. Works with either Meta setup (Instagram Login *or*
  Facebook Login — auto-detected from the token; see `.env.example`). Instagram
  pulls the video from a URL, so if `INSTAGRAM_PUBLIC_BASE_URL` is unset the
  publisher spins an ephemeral `cloudflared` quick tunnel over the render for the
  duration of the fetch, then tears it down (needs `cloudflared` on PATH).
- **tiktok** — skeleton WIP; add it to `target_platforms` once its `.env` block is set.

**Captions:** the YouTube title/description and TikTok/Instagram captions are
written fresh per video by Claude (`captions:` in `config.yaml`) — the existing
lines under `metadata:` are shown to the model purely as a style reference, never
picked verbatim. Needs `ANTHROPIC_API_KEY` + `pip install -e .[llm]`; without
either, or on any API error, it falls back to the old behaviour of picking one of
the template lines at random. All editable in the review UI regardless of which
path produced them. The on-screen overlay text burned into the video is
unaffected — still the seeded `overlay_text` list in `config.yaml`.

**Caption bank:** the `⬇ captions` header button and `captions-bank` command ask
Claude for brand-new *template* lines (overlay text, YouTube title, TikTok /
Instagram captions) and append them to `config/caption_bank.yaml`. That file is
merged on top of the matching lists in `config.yaml` every time the config
loads, so the template pool the seeded picker (and the per-render "style
reference" above) draws from keeps growing without editing `config.yaml` by
hand. Needs `ANTHROPIC_API_KEY` — unlike media there is no non-LLM source for
new caption copy, so the button / command just reports an error without it.

The **assets page** has a "caption & overlay templates" section (and a
`captions only` show-filter) listing every line in all four lists, tagged `bank`
(removable, lives in `caption_bank.yaml`) or `base` (from `config.yaml`, edit
that file to change). Each list has an inline box to add a line by hand — no API
key needed for manual adds/removes.

## Setup (Windows)

```powershell
# 1. ffmpeg on PATH (or set FFMPEG_BIN / FFPROBE_BIN in .env)
winget install Gyan.FFmpeg

# 2. Python env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .            # add  .[youtube]  when wiring up YouTube

# 3. Config
copy .env.example .env      # fill in keys as you need them
#   - drop 3-5 nature clips into assets\video\
#   - drop 2-3 ambient tracks into assets\audio\
#   - drop one .ttf into assets\fonts\   (e.g. Inter, Poppins)
#   - optionally: copy assets\manifest.example.yaml assets\manifest.yaml
```

## Use

```powershell
# see the ffmpeg command without rendering
python -m social_peace build --dry-run

# render 3 varied videos
python -m social_peace build --count 3

# reproducible render (same seed => same clips/audio/copy/grade)
python -m social_peace build --seed 12345 --template warm-dawn

# top up the clip library from stock APIs (lands in assets\video\_incoming\)
python -m social_peace fetch --source pexels --query "slow ocean" --limit 8

# publish a rendered file (YouTube; needs .[youtube] + OAuth set up)
python -m social_peace publish output\20260827-120000_warm-dawn_12345.mp4

# build + publish to project.target_platforms  (this is what the scheduler runs)
python -m social_peace run

# what got posted where
python -m social_peace ledger
```

## Scheduling

Approving a render puts it in the **posting queue** (the approved tab, shown in
posting order). At each `schedule.slots` time — `08:00 / 12:30 / 19:00`
`Europe/Berlin` by default — the head of the queue goes out to every
`target_platforms`. Reorder with ⤒ / ↑ / ↓ on a card; the header shows the next
slot, how many days the queue covers, and warns when it drops under
`schedule.low_queue_days`.

```bash
python -m social_peace schedule            # slots + what posts when
python -m social_peace publish-due         # scheduler tick (what the timer runs)
python -m social_peace publish-next        # post the queue head right now
```

- The slot times live only in `config.yaml`. The timer runs `publish-due` every
  5 minutes; it posts only when a slot has passed that hasn't been served
  (`logs/schedule_state.json`), so extra ticks are harmless.
- A slot noticed more than `grace_minutes` late (machine was asleep) is skipped,
  not posted late — no bursts after a wake-up.
- If a render goes live on some platforms but fails on others, the missing ones
  are retried at the following slots (alongside that slot's new post) until they
  succeed or hit `max_attempts`. A render failing everywhere stays at the head of
  the queue — so an expired token doesn't burn through your approved content —
  and drops out after `max_attempts`, with the errors shown on its card.
- "Publish all N now" in the header still posts everything immediately,
  bypassing the schedule.

**Keeping the queue stocked:** a scheduled `pipeline` run sizes its batch from
the queue (`pipeline.adaptive_batch`): enough renders that queued + pending, at
your recent approval rate (from the ledger's review events, floored at 25%),
covers `target_queue_days` of slots, capped at `max_batch`. If the queue is
already stocked it renders nothing. `--batch N` and the UI's Generate button
still render a fixed count.

**Linux (systemd user timers, recommended — `Persistent=` catches up after sleep):**

```bash
cp scripts/systemd/social-peace-* ~/.config/systemd/user/   # edit the paths if the repo isn't ~/Documents/Code/social-peace
systemctl --user daemon-reload
systemctl --user enable --now social-peace-publish.timer social-peace-pipeline.timer
systemctl --user list-timers | grep social-peace
loginctl enable-linger "$USER"      # keep timers running while logged out
```

**cron equivalent** — `scripts/run_daily.sh [pipeline|publish-due|publish-approved|prune|all]`:

```cron
*/5 * * * *  /path/to/social-peace/scripts/run_daily.sh publish-due >> /path/to/social-peace/logs/cron.log 2>&1
0 2 * * *    /path/to/social-peace/scripts/run_daily.sh pipeline    >> /path/to/social-peace/logs/cron.log 2>&1
```

**Windows** — `scripts/run_daily.ps1` still executes a complete `run` (build +
publish, no review gate); not yet switched to the queue.

The machine must be on at slot times (within `grace_minutes`).

## Pipeline notes

- **9:16:** each clip is scaled to *cover* 1080x1920 and centre-cropped. Source
  footage that's already vertical or has headroom crops best.
- **Duration:** random per video within `render.duration_seconds`. Segments are sized
  so N clips (overlapped by the transition) hit that length.
- **Variation:** template (weighted), clip set, in-points, audio beds, and overlay
  copy are all chosen from a seeded RNG. The seed is in the filename and the ledger,
  so any render is reproducible.
- **Audio:** beds are looped to length, mixed, faded, and normalised to
  `render.loudness_lufs` (−14 LUFS default) with ffmpeg `loudnorm`.
- **Overlay:** rendered as a transparent PNG by Pillow, then composited — real
  word-wrap + letter-spacing + legibility scrim, faded in/out.

## Roadmap

- [x] Project scaffold + ffmpeg assembly pipeline
- [x] YouTube publisher skeleton (OAuth + resumable upload)
- [x] Per-platform caption/hashtag variants in the sidecar
- [x] `pipeline` command: scrape + auto-promote + render a review batch
- [x] Freesound CC0 audio fetcher
- [x] Local web review interface (`review`) + `publish-approved`
- [x] Publish button in the review UI (single + all-approved)
- [x] Batch-aware selection: avoid clip reuse, spread audio, pair audio↔scene
- [x] Variants: re-render one video with just clips / audio / text swapped (`variant` + UI ↻)
- [x] "Generate new content" button in the review UI (background render job)
- [x] "Fetch clips" / "Fetch audio" buttons — top up the asset library on demand
- [x] Assets page — list + preview + remove every clip/bed, filter by usage
- [x] Favourite / rename assets; custom-query fetch from the UI
- [x] Sort the review grid; `prune` rejected renders (30-day auto + button)
- [x] TikTok Content Posting API publisher (skeleton — not yet run live)
- [x] Instagram Graph API (Reels) publisher + auto cloudflared tunnel for the fetch
- [x] Posting queue + daily slots (`publish-due`), partial-failure retries, reorder in UI
- [x] Adaptive `pipeline` batch that keeps the queue stocked
- [x] Need-based auto-fetch on a rotating search term; fetchers skip known stock ids
- [ ] YouTube: submit for verification (lifts private-only + 7-day token)
- [ ] Exercise the TikTok publisher against the real API
