"""`social-peace pipeline`: scrape stock assets, auto-promote them into the render
pool, then render a batch of videos that land in the review queue (review.state =
"pending"). Publishing is a separate step (`publish-approved`)."""
from __future__ import annotations

import logging
import random
from pathlib import Path

from social_peace import ledger
from social_peace.config import Config
from social_peace.fetch.common import promote_incoming
from social_peace.pipeline.assemble import build_one
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media

log = logging.getLogger(__name__)

_SEED_MAX = 2**31 - 1


def _fetch_video(cfg: Config, source: str, query: str, limit: int) -> list[str]:
    if source == "pexels":
        from social_peace.fetch import pexels as mod
    elif source == "pixabay":
        from social_peace.fetch import pixabay as mod
    else:
        log.warning("pipeline: unknown fetch source %r, skipping", source)
        return []
    return mod.fetch(cfg, query, limit=limit)


def _fetch_audio(cfg: Config, query: str, limit: int) -> list[str]:
    from social_peace.fetch import freesound

    return freesound.fetch(cfg, query, limit=limit)


def _promote_and_count(cfg: Config, kind: str) -> tuple[int, int]:
    folder = cfg.path("video_assets" if kind == "video" else "audio_assets")
    exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
    moved = len(promote_incoming(folder))
    return moved, len(_list_media(folder, exts))


def fetch_video_library(cfg: Config, *, limit: int | None = None, query: str | None = None) -> dict:
    """Pull stock clips and auto-promote them into assets/video/. `query` fetches
    that exact term from every source; otherwise one rotating pipeline.queries
    term per source."""
    pcfg = cfg.raw.get("pipeline", {})
    limit = int(limit or pcfg.get("fetch_per_run", 6))
    vqueries = pcfg.get("queries", {}).get("video", []) or [None]
    rng = random.Random()
    fetched, notes = 0, []
    for source in pcfg.get("fetch_sources", ["pexels", "pixabay"]):
        q = query or rng.choice(vqueries)
        try:
            got = _fetch_video(cfg, source, q, limit)
            fetched += len(got)
            notes.append(f"{source} '{q}': {len(got)}")
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_video_library: %s '%s' failed: %s", source, q, exc)
            notes.append(f"{source} '{q}': failed")
    moved, pool = _promote_and_count(cfg, "video")
    return {"kind": "video", "fetched": fetched, "promoted": moved, "pool_size": pool, "notes": notes}


def fetch_audio_library(cfg: Config, *, limit: int | None = None, query: str | None = None) -> dict:
    """Pull CC0 beds from Freesound and auto-promote them into assets/audio/.
    `query` overrides the rotating pipeline.queries.audio term. Needs
    FREESOUND_API_KEY; without it, a no-op."""
    pcfg = cfg.raw.get("pipeline", {})
    limit = int(limit or pcfg.get("fetch_per_run", 6))
    aqueries = pcfg.get("queries", {}).get("audio", []) or [None]
    q = query or random.Random().choice(aqueries)
    fetched, notes = 0, []
    try:
        got = _fetch_audio(cfg, q, limit)
        fetched = len(got)
        notes.append(f"freesound '{q}': {len(got)}")
    except RuntimeError as exc:            # no FREESOUND_API_KEY
        notes.append(f"skipped ({exc})")
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_audio_library: %s", exc)
        notes.append(f"freesound '{q}': failed")
    moved, pool = _promote_and_count(cfg, "audio")
    return {"kind": "audio", "fetched": fetched, "promoted": moved, "pool_size": pool, "notes": notes}


def run_pipeline(
    cfg: Config,
    *,
    batch: int | None = None,
    seed: int | None = None,
    do_fetch: bool = True,
    dry_run: bool = False,
) -> dict:
    pcfg = cfg.raw.get("pipeline", {})
    batch = int(batch if batch is not None else pcfg.get("batch_size", 4))
    base_seed = seed if seed is not None else random.randrange(_SEED_MAX)

    promoted = {"video": 0, "audio": 0}

    pool_size = len(_list_media(cfg.path("video_assets"), VIDEO_EXTS))
    if do_fetch and pool_size >= int(pcfg.get("min_clip_pool", 8)):
        log.info("pipeline: video pool already has %d clips, skipping fetch", pool_size)
        do_fetch = False

    fetched_n = 0
    if do_fetch:
        v = fetch_video_library(cfg)
        fetched_n += v["fetched"]
        promoted["video"] = v["promoted"]
        if pcfg.get("fetch_audio", False):
            a = fetch_audio_library(cfg)
            fetched_n += a["fetched"]
            promoted["audio"] = a["promoted"]
    elif pcfg.get("auto_promote", True):
        promoted["video"] = len(promote_incoming(cfg.path("video_assets")))
        promoted["audio"] = len(promote_incoming(cfg.path("audio_assets")))
    log.info("pipeline: fetched %d, promoted %s", fetched_n, promoted)

    from social_peace.pipeline.metadata import sources_in_use

    used_v, used_a = sources_in_use(cfg.path("output"))
    built: list[str] = []
    rc = 0
    for i in range(batch):
        s = (base_seed + i) % _SEED_MAX
        try:
            res = build_one(cfg, seed=s, dry_run=dry_run, used_video=used_v, used_audio=used_a)
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline: build failed (seed=%s)", s)
            if not dry_run:
                ledger.record(cfg.path("logs"), "build", seed=s, ok=False, error=str(exc))
            rc = 1
            continue
        if not dry_run:
            ledger.record(
                cfg.path("logs"), "build",
                video_id=res["id"], template=res["template"], seed=s,
                duration=res.get("duration"), sources=res.get("sources"), ok=True,
            )
            print(res["video"])
        built.append(res["id"])
        srcs = res.get("sources") or {}
        used_v.update(srcs.get("video", []))
        used_a.update(srcs.get("audio", []))

    summary = {
        "seed": base_seed,
        "fetched": fetched_n,
        "promoted": promoted,
        "built": built,
        "ok": rc == 0,
    }
    if not dry_run:
        ledger.record(cfg.path("logs"), "pipeline", **summary)
    log.info("pipeline: %s", summary)
    return summary
