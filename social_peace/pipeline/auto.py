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
from social_peace.pipeline.selectors import VIDEO_EXTS, _list_media

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
    rng = random.Random(base_seed)

    fetched: list[str] = []
    promoted = {"video": 0, "audio": 0}

    pool_size = len(_list_media(cfg.path("video_assets"), VIDEO_EXTS))
    if do_fetch and pool_size >= int(pcfg.get("min_clip_pool", 8)):
        log.info("pipeline: video pool already has %d clips, skipping fetch", pool_size)
        do_fetch = False

    if do_fetch:
        per_run = int(pcfg.get("fetch_per_run", 6))
        vqueries = pcfg.get("queries", {}).get("video", []) or [None]
        for source in pcfg.get("fetch_sources", ["pexels", "pixabay"]):
            q = rng.choice(vqueries)
            try:
                got = _fetch_video(cfg, source, q, per_run)
                fetched += got
                log.info("pipeline: %s '%s' -> %d file(s)", source, q, len(got))
            except Exception as exc:  # noqa: BLE001
                log.warning("pipeline: %s fetch failed (%s): %s", source, q, exc)

        if pcfg.get("fetch_audio", False):
            aqueries = pcfg.get("queries", {}).get("audio", []) or [None]
            q = rng.choice(aqueries)
            try:
                got = _fetch_audio(cfg, q, per_run)
                fetched += got
                log.info("pipeline: freesound '%s' -> %d file(s)", q, len(got))
            except RuntimeError as exc:
                log.warning("pipeline: audio fetch skipped: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("pipeline: freesound fetch failed (%s): %s", q, exc)

    if pcfg.get("auto_promote", True):
        promoted["video"] = len(promote_incoming(cfg.path("video_assets")))
        promoted["audio"] = len(promote_incoming(cfg.path("audio_assets")))
        log.info("pipeline: promoted %d video + %d audio", promoted["video"], promoted["audio"])

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
        "fetched": len(fetched),
        "promoted": promoted,
        "built": built,
        "ok": rc == 0,
    }
    if not dry_run:
        ledger.record(cfg.path("logs"), "pipeline", **summary)
    log.info("pipeline: %s", summary)
    return summary
