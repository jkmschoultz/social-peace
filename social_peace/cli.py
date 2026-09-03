"""Command-line entrypoint.

  social-peace build   [--count N] [--seed S] [--template NAME] [--duration SEC] [--dry-run]
  social-peace fetch   [--source pixabay|pexels|freesound] [--query "..."] [--limit N] [--promote]
  social-peace publish  VIDEO.mp4 [--platform youtube]
  social-peace run     [--count N]        # build + publish to project.target_platforms
  social-peace pipeline [--batch N] [--no-fetch] [--dry-run]   # scrape -> render a review batch
  social-peace review  [--host H] [--port P]                   # web UI to approve/reject
  social-peace variant STEM --change video|audio|text          # re-render, one thing swapped
  social-peace publish-approved [--platform P] [--limit N]     # post the approved renders
  social-peace prune   [--days N] [--all]  # delete rejected renders to free output/ space
  social-peace ledger  [--limit N]        # tail the posts.jsonl ledger
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

from social_peace import __version__
from social_peace.config import Config
from social_peace.logging_setup import setup_logging
from social_peace.publish.runner import PUBLISHERS, publish_all_approved, publish_one

log = logging.getLogger("social_peace.cli")

_SEED_MAX = 2**31 - 1


def _bootstrap() -> Config:
    cfg = Config.load()
    setup_logging(cfg.path("logs"))
    return cfg


# --------------------------------------------------------------------------- build
def cmd_build(args: argparse.Namespace) -> int:
    from social_peace.pipeline.assemble import build_one
    from social_peace.pipeline.metadata import sources_in_use
    from social_peace import ledger

    cfg = _bootstrap()
    base_seed = args.seed if args.seed is not None else random.randrange(_SEED_MAX)
    used_v, used_a = sources_in_use(cfg.path("output"))
    made = []
    for i in range(args.count):
        seed = (base_seed + i) % _SEED_MAX
        try:
            res = build_one(
                cfg, seed=seed, template_name=args.template,
                duration=args.duration, dry_run=args.dry_run,
                used_video=used_v, used_audio=used_a,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("build failed (seed=%s)", seed)
            if not args.dry_run:
                ledger.record(cfg.path("logs"), "build", seed=seed, ok=False, error=str(exc))
            return 1
        if not args.dry_run:
            ledger.record(
                cfg.path("logs"), "build",
                video_id=res["id"], template=res["template"], seed=seed,
                duration=res.get("duration"), sources=res.get("sources"), ok=True,
            )
            print(res["video"])
        srcs = res.get("sources") or {}
        used_v.update(srcs.get("video", []))
        used_a.update(srcs.get("audio", []))
        made.append(res)
    log.info("build: %d video(s)", len(made))
    return 0


# --------------------------------------------------------------------------- fetch
def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    kind = "audio" if args.source == "freesound" else "video"
    mod = __import__(f"social_peace.fetch.{args.source}", fromlist=["fetch"])
    try:
        saved = mod.fetch(cfg, args.query, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log.error("fetch failed: %s", exc)
        return 1
    assets = cfg.path("video_assets" if kind == "video" else "audio_assets")
    print(f"saved {len(saved)} file(s) to {assets / '_incoming'}")
    if args.promote:
        from social_peace.fetch.common import promote_incoming
        n = len(promote_incoming(assets))
        print(f"promoted {n} file(s) into {assets}")
    else:
        print("review + license-check, then move the keepers up (or pass --promote)")
    return 0


# ------------------------------------------------------------------------- publish
def cmd_publish(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    video_path = Path(args.video).resolve()
    if not video_path.is_file():
        log.error("not found: %s", video_path)
        return 1
    if not video_path.with_suffix(".json").is_file():
        log.error("missing sidecar: %s", video_path.with_suffix(".json"))
        return 1
    res = publish_one(cfg, video_path, args.platform)
    return 0 if res.ok else 1


# ----------------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace) -> int:
    from social_peace.pipeline.assemble import build_one
    from social_peace import ledger

    cfg = _bootstrap()
    platforms = cfg.raw.get("project", {}).get("target_platforms", [])
    if not platforms:
        log.error("project.target_platforms is empty")
        return 1

    rc = 0
    for i in range(args.count):
        seed = random.randrange(_SEED_MAX)
        try:
            res = build_one(cfg, seed=seed)
        except Exception as exc:  # noqa: BLE001
            log.exception("run: build failed")
            ledger.record(cfg.path("logs"), "build", seed=seed, ok=False, error=str(exc))
            rc = 1
            continue
        ledger.record(
            cfg.path("logs"), "build",
            video_id=res["id"], template=res["template"], seed=seed,
            duration=res.get("duration"), sources=res.get("sources"), ok=True,
        )
        video_path = Path(res["video"])
        for platform in platforms:
            if not publish_one(cfg, video_path, platform).ok:
                rc = 1
    return rc


# --------------------------------------------------------------------------- variant
def cmd_variant(args: argparse.Namespace) -> int:
    from social_peace import ledger
    from social_peace.pipeline.variant import make_variant

    cfg = _bootstrap()
    side = cfg.path("output") / f"{Path(args.stem).stem}.json"
    if not side.is_file():
        log.error("no sidecar: %s", side)
        return 1
    original = json.loads(side.read_text(encoding="utf-8"))
    res = make_variant(cfg, original, args.change)
    ledger.record(
        cfg.path("logs"), "build", video_id=res["id"], template=res["template"],
        seed=res["seed"], duration=res.get("duration"), sources=res.get("sources"),
        variant_of=original["id"], variant_change=args.change, ok=True,
    )
    print(res["video"])
    return 0


# -------------------------------------------------------------------------- pipeline
def cmd_pipeline(args: argparse.Namespace) -> int:
    from social_peace.pipeline.auto import run_pipeline

    cfg = _bootstrap()
    summary = run_pipeline(
        cfg,
        batch=args.batch,
        seed=args.seed,
        do_fetch=not args.no_fetch,
        dry_run=args.dry_run,
    )
    return 0 if summary["ok"] else 1


# --------------------------------------------------------------------------- review
def cmd_review(args: argparse.Namespace) -> int:
    from social_peace.review.app import serve

    cfg = _bootstrap()
    serve(cfg, host=args.host, port=args.port)
    return 0


# ------------------------------------------------------------------ publish-approved
def cmd_publish_approved(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    platforms = [args.platform] if args.platform else None  # None -> available_platforms
    reports = publish_all_approved(
        cfg, platforms=platforms, dry_run=args.dry_run, limit=args.limit
    )
    if not reports:
        print("no approved videos awaiting publish")
        return 0

    rc = 0
    for rep in reports:
        if rep.get("error"):
            print(f"{rep['id']}: {rep['error']}")
            rc = 1
            continue
        for r in rep["results"]:
            if args.dry_run:
                print(f"would publish {rep['id']} -> {r['platform']}")
            elif r["ok"]:
                print(f"{rep['id']} -> {r['platform']}: {r['status']} {r.get('url') or ''}".rstrip())
            else:
                print(f"{rep['id']} -> {r['platform']}: FAILED {r['error']}")
                rc = 1
    return rc


# ---------------------------------------------------------------------------- prune
def cmd_prune(args: argparse.Namespace) -> int:
    from social_peace.pipeline.prune import prune_rejected

    cfg = _bootstrap()
    days = None if args.all else args.days
    removed = prune_rejected(cfg, older_than_days=days, drop_all=args.all)
    print(f"pruned {len(removed)} rejected render(s)")
    for r in removed:
        print(f"  {r}")
    return 0


# -------------------------------------------------------------------------- ledger
def cmd_ledger(args: argparse.Namespace) -> int:
    from social_peace import ledger

    cfg = _bootstrap()
    rows = list(ledger.read_all(cfg.path("logs")))[-args.limit :]
    for e in rows:
        line = f"{e.get('ts', '?')}  {e.get('event', '?'):7}"
        if e.get("event") == "build":
            line += f"  {e.get('video_id', '?')}  tpl={e.get('template', '?')}  ok={e.get('ok')}"
        elif e.get("event") == "publish":
            line += (
                f"  {e.get('video_id', '?')}  {e.get('platform', '?')}"
                f"  {e.get('status', '?')}  {e.get('url') or ''}  ok={e.get('ok')}"
            )
        print(line)
    if not rows:
        print("(ledger empty)")
    return 0


# ----------------------------------------------------------------------------- arg
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="social-peace", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"social-peace {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="render one or more videos")
    b.add_argument("--count", type=int, default=1)
    b.add_argument("--seed", type=int, default=None, help="base seed (reproducible output)")
    b.add_argument("--template", default=None, help="force a template by name")
    b.add_argument("--duration", type=float, default=None, help="override duration in seconds")
    b.add_argument("--dry-run", action="store_true", help="print the ffmpeg command, render nothing")
    b.set_defaults(func=cmd_build)

    f = sub.add_parser("fetch", help="download stock video/audio into assets/*/_incoming")
    f.add_argument("--source", choices=["pixabay", "pexels", "freesound"], default="pixabay")
    f.add_argument("--query", default=None)
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--promote", action="store_true", help="move the downloads straight into the pool")
    f.set_defaults(func=cmd_fetch)

    pub = sub.add_parser("publish", help="publish one rendered video")
    pub.add_argument("video", help="path to output/<name>.mp4")
    pub.add_argument("--platform", choices=list(PUBLISHERS), default="youtube")
    pub.set_defaults(func=cmd_publish)

    r = sub.add_parser("run", help="build + publish to project.target_platforms (scheduler entrypoint)")
    r.add_argument("--count", type=int, default=1)
    r.set_defaults(func=cmd_run)

    pl = sub.add_parser("pipeline", help="scrape + auto-promote + render a review batch")
    pl.add_argument("--batch", type=int, default=None, help="videos to render (default pipeline.batch_size)")
    pl.add_argument("--seed", type=int, default=None)
    pl.add_argument("--no-fetch", action="store_true", help="skip scraping; render from the current pool")
    pl.add_argument("--dry-run", action="store_true", help="run selection, render nothing")
    pl.set_defaults(func=cmd_pipeline)

    rv = sub.add_parser("review", help="local web UI to approve/reject renders")
    rv.add_argument("--host", default="127.0.0.1")
    rv.add_argument("--port", type=int, default=8756)
    rv.set_defaults(func=cmd_review)

    vr = sub.add_parser("variant", help="re-render one video with new clips / audio / text")
    vr.add_argument("stem", help="sidecar stem (or path) of the render to vary")
    vr.add_argument("--change", required=True, choices=["video", "audio", "text"])
    vr.set_defaults(func=cmd_variant)

    pa = sub.add_parser("publish-approved", help="publish every approved, not-yet-posted render")
    pa.add_argument("--platform", default=None, choices=list(PUBLISHERS),
                    help="override project.target_platforms")
    pa.add_argument("--limit", type=int, default=None, help="cap how many to publish this run")
    pa.add_argument("--dry-run", action="store_true")
    pa.set_defaults(func=cmd_publish_approved)

    pr = sub.add_parser("prune", help="delete rejected renders (default: older than 30 days)")
    pr.add_argument("--days", type=int, default=30)
    pr.add_argument("--all", action="store_true", help="delete every rejected render regardless of age")
    pr.set_defaults(func=cmd_prune)

    lg = sub.add_parser("ledger", help="show recent ledger entries")
    lg.add_argument("--limit", type=int, default=20)
    lg.set_defaults(func=cmd_ledger)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
