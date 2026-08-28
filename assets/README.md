# Assets

Drop your **original or properly licensed** media here. Nothing in `video/`, `audio/`,
or `fonts/` is committed to git (see `.gitignore`) — this folder just defines the layout
and the licensing rules.

```
assets/
  video/    nature clips, ideally already vertical or at least 1080px tall
  audio/    calming beds: rain, ocean, forest, lo-fi, ambient, binaural
  fonts/    at least one .ttf/.otf for the text overlay (e.g. Inter, Poppins)
```

## Licensing rules (enforced by you, not the code)

- **Video / audio:** only Pixabay (Pixabay Content License), Pexels (Pexels License),
  Coverr, or media you shot/produced yourself. All four allow commercial use and
  redistribution without attribution — but attribution is still polite, and *some*
  individual uploads carry extra restrictions, so check each file's page.
- **No** ripping clips from other creators' TikToks/Reels/Shorts, no "royalty-free"
  torrents, no AI-music services whose TOS forbids multi-platform or automated posting.
- **Subscription libraries (Epidemic, Artlist, Uppbeat, etc.):** several prohibit
  "faceless"/compilation channels or per-platform reuse. Read the TOS before adding
  their tracks. When in doubt, stick to Pixabay CC0 audio.

## `manifest.yaml` (optional but recommended)

Create `assets/manifest.yaml` (gitignored) to record provenance and to drive
tag-based selection. Files not listed still work — they're matched by filename and
get no tags. See `manifest.example.yaml` for the format.
