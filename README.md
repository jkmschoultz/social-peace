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
  config.yaml          global settings: paths, durations, metadata, fetch keys
  templates.yaml       templated variation: color grades, transitions, overlay + audio pairing
assets/
  video/ audio/ fonts/  your curated library (gitignored); add a .ttf to fonts/
  manifest.yaml         optional: per-file tags + license provenance
social_peace/
  config.py  logging_setup.py  ledger.py  cli.py
  pipeline/            selectors -> overlays -> assemble (ffmpeg filtergraph) -> metadata sidecar
  fetch/              pixabay.py / pexels.py  (top-up stock clips for review)
  publish/           base.py + youtube.py (skeleton); tiktok/instagram later
scripts/
  run_daily.ps1        scheduler entrypoint
  register_task.ps1    registers the Windows Scheduled Task
output/                rendered <name>.mp4 + <name>.json sidecar (gitignored)
logs/                  social_peace.log + posts.jsonl ledger (gitignored)
```

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

```powershell
# run daily at 09:00 and 18:00
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
- [ ] YouTube: finish config/consent walkthrough, quota-aware cadence
- [ ] TikTok Content Posting API publisher
- [ ] Instagram Graph API (Reels) publisher
- [ ] Per-platform caption/hashtag variants in the sidecar
