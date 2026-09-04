"""Seeded, weighted selection of a template + the clips / audio / copy for one video."""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.ffmpeg_utils import ffprobe_duration, has_audio_stream

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

_KW_RE = re.compile(r"[a-z]{3,}")
# generic filler that would create spurious clip<->audio "matches"
_KW_STOP = {
    "the", "and", "for", "loop", "calm", "slow", "nature", "video", "audio",
    "sound", "ambient", "ambience", "freesound", "pexels", "pixabay", "mp3", "mp4",
    "music", "track", "clip",
}


def _keywords(cfg: Config, kind: str, name: str) -> set[str]:
    """Loose descriptive tokens for a file: manifest tags + label + filename words."""
    toks = set(_KW_RE.findall(name.lower()))
    toks |= {str(t).lower() for t in cfg.tags_for(kind, name)}
    label = cfg.label_for(kind, name)
    if label:
        toks |= set(_KW_RE.findall(label.lower()))
    return toks - _KW_STOP


def _pref_tokens(text: str | None) -> set[str]:
    """Themed descriptive tokens from a free-text preference like 'ocean waves'."""
    if not text:
        return set()
    return _themed(set(_KW_RE.findall(text.lower())) - _KW_STOP)


# Broaden an audio keyword to the visual scenes that suit it, so a "rain" bed
# lands on a waterfall/stream clip even when neither literally says "rain".
_THEME = {
    "rain": {"rain", "water", "wet", "storm", "drizzle", "waterfall", "stream", "river", "creek", "droplet", "puddle"},
    "storm": {"storm", "rain", "cloud", "clouds", "wind", "sky"},
    "ocean": {"ocean", "sea", "wave", "waves", "surf", "coast", "beach", "tide", "water", "shore"},
    "waves": {"ocean", "sea", "wave", "waves", "surf", "coast", "beach", "tide", "water", "shore"},
    "water": {"water", "river", "stream", "lake", "waterfall", "ocean", "sea", "rain"},
    "forest": {"forest", "woods", "woodland", "tree", "trees", "leaves", "jungle", "moss", "fern"},
    "birds": {"bird", "birds", "dawn", "forest", "meadow", "garden"},
    "wind": {"wind", "grass", "field", "meadow", "dune", "cloud", "clouds"},
    "drone": {"drone", "cloud", "clouds", "fog", "mist", "mountain", "sky", "dusk", "night", "aurora"},
    "meditation": {"still", "calm", "mist", "mountain", "water", "sunrise", "dawn"},
    "piano": {"snow", "mist", "dusk", "window", "still"},
}


def _themed(words: set[str]) -> set[str]:
    out = set(words)
    for w in words:
        out |= _THEME.get(w, set())
    return out


@dataclass
class Clip:
    path: Path
    duration: float


@dataclass
class AudioBed:
    path: Path
    duration: float


@dataclass
class Selection:
    template: dict
    clips: list[Clip]
    audio: list[AudioBed]
    text: str
    footer: str
    target_duration: float
    segment_duration: float
    seed: int


def _list_media(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in exts and p.is_file())


def _weighted_choice(rng: random.Random, items: list[dict], weight_key: str = "weight"):
    weights = [max(float(it.get(weight_key, 1)), 0.0001) for it in items]
    return rng.choices(items, weights=weights, k=1)[0]


def _pick_range(rng: random.Random, val) -> int:
    """`val` is an int or a [lo, hi] list."""
    if isinstance(val, (list, tuple)):
        return rng.randint(int(val[0]), int(val[1]))
    return int(val)


def _tag_score(cfg: Config, kind: str, path: Path, wanted: list[str]) -> int:
    if not wanted:
        return 0
    tags = set(cfg.tags_for(kind, path.name))
    return len(tags & set(wanted))


def _resolve_pins(folder: Path, names: list[str], exts: set[str]) -> list[Path] | None:
    paths = [folder / n for n in names]
    if all(p.is_file() and p.suffix.lower() in exts for p in paths):
        return paths
    log.warning("pinned files missing in %s (%s) — ignoring the pin", folder, names)
    return None


def build_selection(
    cfg: Config,
    *,
    seed: int,
    template_name: str | None = None,
    duration: float | None = None,
    used_video: set[str] | None = None,
    used_audio: set[str] | None = None,
    soft_used_video: set[str] | None = None,
    soft_used_audio: set[str] | None = None,
    pin: dict | None = None,
    exclude_text: str | None = None,
    prefer_video: str | None = None,
    prefer_audio: str | None = None,
) -> Selection:
    """`used_video` / `used_audio` are filenames the pick must avoid where it can
    (hard preference — top of the ranking). `soft_used_*` is a weaker "already
    spent by an accepted/published render elsewhere" nudge, ranked below an
    explicit `prefer_*` wish but above chance. Unspent assets win; used ones only
    surface when the fresh pool runs out.

    `pin` = {"video": [names], "audio": [names], "text": str} forces those
    dimensions to exact values (used to re-render a video with only one thing
    changed). `exclude_text` drops one line from the overlay-text pool so a
    text-only re-roll always differs from the original.

    `prefer_video` / `prefer_audio` are free-text scene/sound wishes ("river
    ambience") that strongly bias the unpinned dimension toward matching assets."""
    rng = random.Random(seed)
    used_video = set(used_video or ())
    used_audio = set(used_audio or ())
    soft_used_video = set(soft_used_video or ()) - used_video
    soft_used_audio = set(soft_used_audio or ()) - used_audio
    pin = pin or {}
    pref_a_tok = _pref_tokens(prefer_audio)
    pref_v_tok = _pref_tokens(prefer_video)

    # --- template ---
    if template_name:
        matches = [t for t in cfg.templates if t["name"] == template_name]
        if not matches:
            raise ValueError(f"unknown template {template_name!r}")
        template = matches[0]
    else:
        template = _weighted_choice(rng, cfg.templates)

    # --- target duration ---
    if duration is None:
        lo, hi = cfg.render["duration_seconds"]
        target = rng.uniform(float(lo), float(hi))
    else:
        target = float(duration)

    # --- audio (picked first: its keywords steer the clip choice) ---
    audio_pool = _list_media(cfg.path("audio_assets"), AUDIO_EXTS)
    if not audio_pool:
        raise FileNotFoundError(
            f"no audio in {cfg.path('audio_assets')} ({', '.join(sorted(AUDIO_EXTS))})"
        )
    stems = max(1, int(template.get("audio", {}).get("stems", 1)))
    wanted_a = list(template.get("audio", {}).get("tags", []))
    pinned_a = _resolve_pins(cfg.path("audio_assets"), pin["audio"], AUDIO_EXTS) if pin.get("audio") else None
    if pinned_a is not None:
        a_ranked = pinned_a
        stems = len(pinned_a)
    else:
        a_ranked = sorted(
            audio_pool,
            key=lambda p: (
                p.name in used_audio,                                        # unused first
                -len(_keywords(cfg, "audio", p.name) & pref_a_tok),          # user's wish
                not cfg.favourite("audio", p.name),                         # then favourites
                p.name in soft_used_audio,                                   # avoid reusing accepted/published
                -_tag_score(cfg, "audio", p, wanted_a),                     # template tag pref
                rng.random(),
            ),
        )
    beds: list[AudioBed] = []
    for p in a_ranked:
        if len(beds) == stems:
            break
        if not has_audio_stream(p):
            log.warning("no audio stream in %s, skipping", p.name)
            continue
        beds.append(AudioBed(p, ffprobe_duration(p)))
    if not beds:
        raise FileNotFoundError("no usable audio beds after probing")

    audio_kw: set[str] = set()
    for b in beds:
        audio_kw |= _keywords(cfg, "audio", b.path.name)
    audio_kw = _themed(audio_kw)

    # --- clips: unused first, then affinity to the audio's keywords ---
    video_pool = _list_media(cfg.path("video_assets"), VIDEO_EXTS)
    if not video_pool:
        raise FileNotFoundError(
            f"no video clips in {cfg.path('video_assets')} "
            f"({', '.join(sorted(VIDEO_EXTS))})"
        )
    n_seeded = max(1, _pick_range(rng, template.get("clip_count", 2)))  # keep rng in step
    pinned_v = _resolve_pins(cfg.path("video_assets"), pin["video"], VIDEO_EXTS) if pin.get("video") else None
    n = len(pinned_v) if pinned_v is not None else min(n_seeded, len(video_pool))

    xdur = float(template.get("transition_duration", 0.0)) if template.get("transition") == "fade" else 0.0
    # segment_duration such that n segments (overlapped by xdur) sum to `target`
    seg = (target + (n - 1) * xdur) / n

    wanted_v = list(template.get("video_tags", []))
    if pinned_v is not None:
        candidates = pinned_v
    else:
        candidates = sorted(
            video_pool,
            key=lambda p: (
                p.name in used_video,                                     # unused first
                -len(_keywords(cfg, "video", p.name) & pref_v_tok),       # user's wish
                not cfg.favourite("video", p.name),                       # then favourites
                p.name in soft_used_video,                                # avoid reusing accepted/published
                -len(_keywords(cfg, "video", p.name) & audio_kw),         # match the audio
                -_tag_score(cfg, "video", p, wanted_v),                   # template tag pref
                rng.random(),
            ),
        )
    chosen: list[Clip] = []
    for p in candidates:
        if len(chosen) == n:
            break
        try:
            d = ffprobe_duration(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unreadable clip %s: %s", p.name, exc)
            continue
        if pinned_v is not None or d >= seg + 0.4:
            chosen.append(Clip(p, d))

    if len(chosen) < n:
        # not enough long-enough clips: take the longest available and shrink
        # segments — still preferring clips this batch hasn't used.
        by_len = sorted(
            (Clip(p, ffprobe_duration(p)) for p in candidates if p.suffix.lower() in VIDEO_EXTS),
            key=lambda c: (c.path.name in used_video, c.path.name in soft_used_video, -c.duration),
        )
        chosen = by_len[:n] if by_len else []
        if not chosen:
            raise FileNotFoundError("no usable video clips after probing")
        seg = min(c.duration for c in chosen) - 0.4
        target = n * seg - (n - 1) * xdur
        log.warning(
            "clips shorter than requested; shrank segment to %.1fs, target duration now %.1fs",
            seg, target,
        )

    if pinned_v is not None:
        shortest = min(c.duration for c in chosen)
        if shortest < seg + 0.4:
            seg = max(shortest - 0.4, 1.0)
            target = n * seg - (n - 1) * xdur
            log.warning("pinned clips shorter than target; segment -> %.1fs", seg)
    else:
        rng.shuffle(chosen)

    # --- copy ---
    if pin.get("text") is not None:
        text = pin["text"]
    else:
        pool = cfg.raw["overlay_text"]
        if exclude_text:
            pool = [t for t in pool if t != exclude_text] or pool
        text = rng.choice(pool)
    footer = str(cfg.raw.get("overlay_footer", "") or "")

    sel = Selection(
        template=template,
        clips=chosen,
        audio=beds,
        text=text,
        footer=footer,
        target_duration=round(target, 3),
        segment_duration=round(seg, 3),
        seed=seed,
    )
    log.info(
        "selection: template=%s clips=%s audio=%s dur=%.1fs seg=%.1fs seed=%d",
        template["name"],
        [c.path.name for c in sel.clips],
        [a.path.name for a in sel.audio],
        sel.target_duration,
        sel.segment_duration,
        seed,
    )
    return sel
