# Handoff / current state

Snapshot for moving development to another device. Last updated 2026-08-28.

## What this project is

A bot that assembles calming short-form video (licensed nature footage + ambient
audio) and posts it on a schedule to YouTube Shorts, then TikTok / Instagram Reels.
**Original or properly licensed assets only** — no scraping other creators, no fake
engagement. Licensing rules: [assets/README.md](assets/README.md).

## Decisions locked (2026-08-27)

| Area | Choice |
|------|--------|
| Language | Python. Package `social_peace`, flat layout, run as `python -m social_peace`. |
| Content sourcing | Hybrid: curated `assets/video/` is the source of truth; `social-peace fetch` tops up from Pexels/Pixabay into `assets/video/_incoming/` for manual license review. |
| Audio | Pixabay / CC0 packs in `assets/audio/`. Subscription libraries (Epidemic/Artlist/etc.) mostly forbid faceless/automated channels — avoid. |
| Deployment | Local Windows machine + Task Scheduler (`scripts/register_task.ps1` → `scripts/run_daily.ps1` → `social-peace run`). |

## Status

**Done**
- Project scaffold + config (`config/config.yaml`, `config/templates.yaml`).
- ffmpeg assembly pipeline (`social_peace/pipeline/`): seeded selection → Pillow text
  overlay → 9:16 filtergraph (scale-cover + centre-crop, xfade/hardcut, audio loop +
  mix + `loudnorm` −14 LUFS) → `<name>.json` metadata sidecar.
- 4 templates (`warm-dawn`, `cool-tide`, `still-forest`, `night-calm`) varying color
  grade, transition, motion, overlay placement, audio stem count.
- CLI: `build` / `fetch` / `publish` / `run` / `ledger`.
- Stock fetchers (`social_peace/fetch/pixabay.py`, `pexels.py`) — download to
  `_incoming/` and auto-append provenance to `assets/manifest.yaml`.
- JSONL ledger (`logs/posts.jsonl`) of every build + publish.
- YouTube publisher **skeleton** (`social_peace/publish/youtube.py`): OAuth flow +
  resumable upload written; not yet exercised.
- `pytest` — 6 tests pass (config load, weighted selection, filtergraph shapes).

**Not done / unverified**
- **End-to-end render never run** — the dev machine had no `ffmpeg`/`ffprobe` and no
  assets. First real render still needs a visual check (grade strength, overlay
  position, transition length).
- YouTube publish never run against the real API.
- TikTok and Instagram publishers not started.
- No git remote configured.

## Setup on the new device

```bash
# 1. ffmpeg on PATH  (or set FFMPEG_BIN / FFPROBE_BIN in .env)
#    Windows:  winget install Gyan.FFmpeg
#    macOS:    brew install ffmpeg
#    Linux:    apt install ffmpeg

# 2. Python env  (3.10+)
python -m venv .venv
# Windows:  .\.venv\Scripts\Activate.ps1
# POSIX:    source .venv/bin/activate
pip install -e .            # add  .[youtube]  when wiring up YouTube

# 3. Config + assets
cp .env.example .env        # fill in PIXABAY_API_KEY / PEXELS_API_KEY as needed
#   - 3-5 nature clips  -> assets/video/
#   - 2-3 ambient tracks -> assets/audio/
#   - one .ttf           -> assets/fonts/   (e.g. Inter, Poppins)
#   - optional: cp assets/manifest.example.yaml assets/manifest.yaml

# 4. Smoke test
pytest -q
python -m social_peace build --dry-run     # prints the ffmpeg command
python -m social_peace build --count 3     # real render -> output/
```

### Not in git (recreate on the new device)

- `.venv/` — rebuild with the steps above.
- `.env` — copy from `.env.example`, re-enter API keys.
- `assets/video/*`, `assets/audio/*`, `assets/fonts/*` — copy your media library over
  by hand (kept out of git deliberately).
- `assets/manifest.yaml` — copy it over if you want to keep provenance/tags; otherwise
  `fetch` will start a fresh one.
- `secrets/` — YouTube OAuth client secret + cached token (none exist yet).
- `output/`, `logs/` — regenerated.

### Moving the repo

No remote yet. Either:

```bash
# option A: push to a new private remote
git remote add origin <url>
git push -u origin master

# option B: just copy the folder, excluding the venv
#   (assets/ media is gitignored, so copy that separately if you want it)
```

## Next steps (in order)

1. First real render on a machine with ffmpeg + assets; tune templates from what you
   see (color grade, overlay placement, xfade duration).
2. Finish YouTube: create Google Cloud project, enable **YouTube Data API v3**, set up
   OAuth consent (External + add yourself as Test user), download Desktop-app client
   secret to `secrets/youtube_client_secret.json`, `pip install -e .[youtube]`, run
   `python -m social_peace publish output/<file>.mp4` to do the consent flow.
   - Testing-mode consent screen → refresh token expires after 7 days, uploads forced
     `private`. Verification lifts both.
   - Quota 10,000 units/day; `videos.insert` ≈ 1,600 units → ~6 uploads/day. Set
     cadence accordingly.
3. TikTok Content Posting API publisher (`social_peace/publish/tiktok.py`).
4. Instagram Graph API (Reels) publisher — needs a Business/Creator account linked to
   a Facebook Page, and App Review for `instagram_content_publish`.
5. Per-platform caption/hashtag variants in the sidecar.
