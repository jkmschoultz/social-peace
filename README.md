# social-peace

Assembles calming, original short-form video (nature footage + ambient sound) and
posts it on a schedule to YouTube Shorts — with TikTok and Instagram Reels to follow.

**Content policy:** original or properly licensed assets only (Pixabay / Pexels /
Coverr / your own). No scraping other creators, no fake engagement, no re-uploading
third-party clips. See [assets/README.md](assets/README.md) for the licensing rules.

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

# 2. review: play each render, edit captions, Approve / Reject
python -m social_peace review                  # http://127.0.0.1:8756

# 3. publish everything that got approved (and isn't already posted)
python -m social_peace publish-approved
python -m social_peace publish-approved --dry-run   # show what would go, post nothing
```

`pipeline` fetches from the sources in `pipeline.fetch_sources` (rotating through
`pipeline.queries`), moves `_incoming/` straight into the render pool
(`auto_promote`), and renders `pipeline.batch_size` videos — each sidecar starts
at `review.state: pending`. Audio scraping needs `FREESOUND_API_KEY`; without it
that step is skipped and the existing `assets/audio/` is used. The review UI
shows every source clip's Pexels/Pixabay/Freesound link so the licence +
model-release check happens per video. `publish-approved` only touches sidecars
with `review.state: approved`; `tiktok` / `instagram` in `target_platforms` are
logged as skipped stubs until those publishers exist.

**Captions:** the YouTube title/description and TikTok/Instagram captions are
written fresh per video by Claude (`captions:` in `config.yaml`) — the existing
lines under `metadata:` are shown to the model purely as a style reference, never
picked verbatim. Needs `ANTHROPIC_API_KEY` + `pip install -e .[llm]`; without
either, or on any API error, it falls back to the old behaviour of picking one of
the template lines at random. All editable in the review UI regardless of which
path produced them. The on-screen overlay text burned into the video is
unaffected — still the seeded `overlay_text` list in `config.yaml`.

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

**Linux / macOS** — `scripts/run_daily.sh [pipeline|publish-approved|all]`. Example
`crontab -e` (render a batch at 09:00, post whatever you approved by 18:00):

```cron
0 9  * * *  /path/to/social-peace/scripts/run_daily.sh pipeline         >> /path/to/social-peace/logs/cron.log 2>&1
0 18 * * *  /path/to/social-peace/scripts/run_daily.sh publish-approved >> /path/to/social-peace/logs/cron.log 2>&1
```

**Windows** — `scripts/run_daily.ps1` still runs the older one-shot `run` (build +
publish, no review gate):

```powershell
.\scripts\register_task.ps1 -Times "09:00","18:00"
Start-ScheduledTask -TaskName social-peace-daily   # test it
```

The machine must be on at those times. `-StartWhenAvailable` makes a missed run fire
late rather than skip.

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
- [x] TikTok Content Posting API publisher (skeleton — not yet run live)
- [x] Instagram Graph API (Reels) publisher (skeleton — needs a public video URL)
- [ ] YouTube: finish config/consent walkthrough, quota-aware cadence
- [ ] Exercise the TikTok / Instagram publishers against the real APIs
