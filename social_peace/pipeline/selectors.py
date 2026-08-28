"""Seeded, weighted selection of a template + the clips / audio / copy for one video."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.ffmpeg_utils import ffprobe_duration, has_audio_stream

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


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


def _sample_by_tags(
    rng: random.Random, cfg: Config, kind: str, pool: list[Path], wanted: list[str], k: int
) -> list[Path]:
    """Prefer tag matches, fall back to the rest. Returns up to k distinct paths."""
    k = min(k, len(pool))
    scored = sorted(pool, key=lambda p: (-_tag_score(cfg, kind, p, wanted), rng.random()))
    preferred = [p for p in scored if _tag_score(cfg, kind, p, wanted) > 0]
    rest = [p for p in scored if p not in preferred]
    rng.shuffle(preferred)
    rng.shuffle(rest)
    return (preferred + rest)[:k]


def build_selection(
    cfg: Config,
    *,
    seed: int,
    template_name: str | None = None,
    duration: float | None = None,
) -> Selection:
    rng = random.Random(seed)

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

    # --- clips ---
    video_pool = _list_media(cfg.path("video_assets"), VIDEO_EXTS)
    if not video_pool:
        raise FileNotFoundError(
            f"no video clips in {cfg.path('video_assets')} "
            f"({', '.join(sorted(VIDEO_EXTS))})"
        )
    n = max(1, _pick_range(rng, template.get("clip_count", 2)))
    n = min(n, len(video_pool))

    xdur = float(template.get("transition_duration", 0.0)) if template.get("transition") == "fade" else 0.0
    # segment_duration such that n segments (overlapped by xdur) sum to `target`
    seg = (target + (n - 1) * xdur) / n

    wanted_v = list(template.get("video_tags", []))
    candidates = _sample_by_tags(rng, cfg, "video", video_pool, wanted_v, k=len(video_pool))
    chosen: list[Clip] = []
    for p in candidates:
        if len(chosen) == n:
            break
        try:
            d = ffprobe_duration(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unreadable clip %s: %s", p.name, exc)
            continue
        if d >= seg + 0.4:
            chosen.append(Clip(p, d))

    if len(chosen) < n:
        # not enough long-enough clips: take the longest available and shrink segments
        by_len = sorted(
            (Clip(p, ffprobe_duration(p)) for p in candidates if p.suffix.lower() in VIDEO_EXTS),
            key=lambda c: -c.duration,
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
    rng.shuffle(chosen)

    # --- audio ---
    audio_pool = _list_media(cfg.path("audio_assets"), AUDIO_EXTS)
    if not audio_pool:
        raise FileNotFoundError(
            f"no audio in {cfg.path('audio_assets')} ({', '.join(sorted(AUDIO_EXTS))})"
        )
    stems = max(1, int(template.get("audio", {}).get("stems", 1)))
    wanted_a = list(template.get("audio", {}).get("tags", []))
    audio_paths = _sample_by_tags(rng, cfg, "audio", audio_pool, wanted_a, k=stems)
    beds: list[AudioBed] = []
    for p in audio_paths:
        if not has_audio_stream(p):
            log.warning("no audio stream in %s, skipping", p.name)
            continue
        beds.append(AudioBed(p, ffprobe_duration(p)))
    if not beds:
        raise FileNotFoundError("no usable audio beds after probing")

    # --- copy ---
    text = rng.choice(cfg.raw["overlay_text"])
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
