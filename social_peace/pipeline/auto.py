"""`social-peace pipeline`: scrape stock assets, auto-promote them into the render
pool, then render a batch of videos that land in the review queue (review.state =
"pending"). Publishing is a separate step (`publish-approved`)."""
from __future__ import annotations

import json
import logging
import math
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


def next_queries(cfg: Config, kind: str, n: int = 1) -> list[str | None]:
    """The next `n` search terms from pipeline.queries[kind], round-robin. The
    position is kept in logs/fetch_state.json so it carries across runs (and
    across the nightly timer and the review UI's buttons)."""
    queries = cfg.raw.get("pipeline", {}).get("queries", {}).get(kind) or []
    if not queries:
        return [None] * n
    path = cfg.path("logs") / "fetch_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    i = int(state.get(kind, 0)) % len(queries)
    out = [queries[(i + k) % len(queries)] for k in range(n)]
    state[kind] = (i + n) % len(queries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return out


def fetch_video_library(cfg: Config, *, limit: int | None = None, query: str | None = None) -> dict:
    """Pull stock clips and auto-promote them into assets/video/. `query` fetches
    that exact term from every source; otherwise each source gets the next term
    in the pipeline.queries.video rotation."""
    pcfg = cfg.raw.get("pipeline", {})
    limit = int(limit or pcfg.get("fetch_per_run", 6))
    sources = pcfg.get("fetch_sources", ["pexels", "pixabay"])
    terms = [query] * len(sources) if query else next_queries(cfg, "video", len(sources))
    fetched, notes = 0, []
    for source, q in zip(sources, terms):
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
    `query` overrides the pipeline.queries.audio rotation. Needs
    FREESOUND_API_KEY; without it, a no-op."""
    pcfg = cfg.raw.get("pipeline", {})
    limit = int(limit or pcfg.get("fetch_per_run", 6))
    q = query or next_queries(cfg, "audio", 1)[0]
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


# clips / beds a render typically consumes (templates use 1-3 clips, 1-2 beds)
_CLIPS_PER_RENDER = 2.5
_BEDS_PER_RENDER = 1.5


def unused_assets(cfg: Config) -> dict[str, int]:
    """Library files not held by any pending / approved / published render."""
    from social_peace.pipeline.metadata import sources_in_use

    used_v, used_a = sources_in_use(cfg.path("output"))
    return {
        "video": sum(p.name not in used_v for p in _list_media(cfg.path("video_assets"), VIDEO_EXTS)),
        "audio": sum(p.name not in used_a for p in _list_media(cfg.path("audio_assets"), AUDIO_EXTS)),
    }


def fetch_plan(cfg: Config, batch: int) -> dict[str, int]:
    """How many new clips / beds to fetch before rendering `batch` videos, so the
    batch (plus a floor of min_unused_*) can be built from footage no other live
    render uses. 0 = the library already has enough."""
    pcfg = cfg.raw.get("pipeline", {})
    free = unused_assets(cfg)
    want = {
        "video": max(int(pcfg.get("min_unused_clips", pcfg.get("min_clip_pool", 8))),
                     math.ceil(batch * _CLIPS_PER_RENDER)),
        "audio": max(int(pcfg.get("min_unused_audio", 4)), math.ceil(batch * _BEDS_PER_RENDER)),
    }
    return {k: max(0, want[k] - free[k]) for k in want}


def _top_up(cfg: Config, batch: int) -> tuple[int, dict]:
    """Fetch whatever fetch_plan says is missing; returns (fetched, promoted)."""
    pcfg = cfg.raw.get("pipeline", {})
    per_run = int(pcfg.get("fetch_per_run", 6))
    cap = int(pcfg.get("max_fetch_per_source", 15))
    short = fetch_plan(cfg, batch)
    log.info("pipeline: unused %s, short %s", unused_assets(cfg), short)
    fetched, promoted = 0, {"video": 0, "audio": 0}
    if short["video"]:
        n_src = max(1, len(pcfg.get("fetch_sources", ["pexels", "pixabay"])))
        v = fetch_video_library(cfg, limit=min(cap, max(per_run, math.ceil(short["video"] / n_src))))
        fetched += v["fetched"]
        promoted["video"] = v["promoted"]
    if short["audio"] and pcfg.get("fetch_audio", False):
        a = fetch_audio_library(cfg, limit=min(cap, max(per_run, short["audio"])))
        fetched += a["fetched"]
        promoted["audio"] = a["promoted"]
    return fetched, promoted


def approval_rate(cfg: Config, *, window: int = 40, default: float = 0.6) -> float:
    """Share of recently reviewed renders that were approved (latest decision per
    video, last `window` videos, from the ledger). `default` until there are 5
    decisions; floored at 0.25 so one bad day can't make a run render dozens."""
    last: dict[str, str] = {}
    for e in ledger.read_all(cfg.path("logs")):
        if e.get("event") == "review" and e.get("state") in ("approved", "rejected"):
            last.pop(e.get("video_id"), None)          # re-insert -> keeps recency order
            last[e.get("video_id")] = e["state"]
    recent = list(last.values())[-window:]
    if len(recent) < 5:
        return default
    return max(0.25, recent.count("approved") / len(recent))


def plan_batch(cfg: Config) -> dict:
    """How many renders a scheduled run should make so that queued (approved,
    unposted) + pending-at-the-approval-rate covers target_queue_days of slots."""
    from social_peace.publish.schedule import posts_per_day, queue

    pcfg = cfg.raw.get("pipeline", {})
    target = math.ceil(float(pcfg.get("target_queue_days", 3)) * posts_per_day(cfg))
    queued = len(queue(cfg))
    pending = 0
    for side in cfg.path("output").glob("*.json"):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if md.get("review", {}).get("state", "pending") == "pending":
            pending += 1
    rate = approval_rate(cfg)
    need = math.ceil(max(0, target - queued) / rate) - pending
    batch = max(0, min(int(pcfg.get("max_batch", 12)), need))
    return {"batch": batch, "target": target, "queued": queued,
            "pending": pending, "approval_rate": round(rate, 2)}


def run_pipeline(
    cfg: Config,
    *,
    batch: int | None = None,
    seed: int | None = None,
    do_fetch: bool = True,
    dry_run: bool = False,
) -> dict:
    pcfg = cfg.raw.get("pipeline", {})
    plan = None
    if batch is None and pcfg.get("adaptive_batch", False):
        plan = plan_batch(cfg)
        batch = plan["batch"]
        log.info("pipeline: adaptive batch %s", plan)
        if batch == 0:
            summary = {"fetched": 0, "promoted": {"video": 0, "audio": 0}, "built": [],
                       "plan": plan, "ok": True}
            if not dry_run:
                ledger.record(cfg.path("logs"), "pipeline", **summary)
            print(f"queue is stocked ({plan['queued']} queued, {plan['pending']} pending"
                  f" for a target of {plan['target']}) — nothing to render")
            return summary
    batch = int(batch if batch is not None else pcfg.get("batch_size", 4))
    base_seed = seed if seed is not None else random.randrange(_SEED_MAX)

    promoted = {"video": 0, "audio": 0}
    fetched_n = 0
    if do_fetch and dry_run:
        log.info("pipeline: [dry-run] would fetch %s", fetch_plan(cfg, batch))
    elif do_fetch:
        fetched_n, promoted = _top_up(cfg, batch)
    if pcfg.get("auto_promote", True):
        promoted["video"] += len(promote_incoming(cfg.path("video_assets")))
        promoted["audio"] += len(promote_incoming(cfg.path("audio_assets")))
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
        "plan": plan,
        "ok": rc == 0,
    }
    if not dry_run:
        ledger.record(cfg.path("logs"), "pipeline", **summary)
    log.info("pipeline: %s", summary)
    return summary
