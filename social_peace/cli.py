"""Command-line entrypoint.

  social-peace build   [--count N] [--seed S] [--template NAME] [--duration SEC] [--dry-run]
  social-peace fetch   [--source pixabay|pexels] [--query "..."] [--limit N]
  social-peace publish  VIDEO.mp4 [--platform youtube]
  social-peace run     [--count N]        # build + publish to project.target_platforms
  social-peace ledger  [--limit N]        # tail the posts.jsonl ledger
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

from social_peace import __version__
from social_peace.config import Config
from social_peace.logging_setup import setup_logging

log = logging.getLogger("social_peace.cli")

_SEED_MAX = 2**31 - 1


def _bootstrap() -> Config:
    cfg = Config.load()
    setup_logging(cfg.path("logs"))
    return cfg


# --------------------------------------------------------------------------- build
def cmd_build(args: argparse.Namespace) -> int:
    from social_peace.pipeline.assemble import build_one
    from social_peace import ledger

    cfg = _bootstrap()
    base_seed = args.seed if args.seed is not None else random.randrange(_SEED_MAX)
    made = []
    for i in range(args.count):
        seed = (base_seed + i) % _SEED_MAX
        try:
            res = build_one(
                cfg, seed=seed, template_name=args.template,
                duration=args.duration, dry_run=args.dry_run,
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
        made.append(res)
    log.info("build: %d video(s)", len(made))
    return 0


# --------------------------------------------------------------------------- fetch
def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    if args.source == "pixabay":
        from social_peace.fetch import pixabay as mod
    else:
        from social_peace.fetch import pexels as mod
    try:
        saved = mod.fetch(cfg, args.query, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log.error("fetch failed: %s", exc)
        return 1
    print(f"saved {len(saved)} file(s) to {cfg.path('video_assets') / '_incoming'}")
    print("review + license-check, then move the keepers up into assets/video/")
    return 0


# ------------------------------------------------------------------------- publish
_PUBLISHERS = {"youtube": "social_peace.publish.youtube"}


def _publish_one(cfg: Config, video_path: Path, platform: str) -> bool:
    import importlib

    from social_peace import ledger
    from social_peace.pipeline.metadata import load_sidecar

    if platform not in _PUBLISHERS:
        log.error("no publisher for %r (have: %s)", platform, ", ".join(_PUBLISHERS))
        return False
    if ledger.already_published(cfg.path("logs"), video_path.stem, platform):
        log.info("%s already published to %s, skipping", video_path.name, platform)
        return True

    metadata = load_sidecar(video_path)
    mod = importlib.import_module(_PUBLISHERS[platform])
    result = mod.publish(video_path, metadata)
    ledger.record(
        cfg.path("logs"), "publish",
        video_id=video_path.stem, platform=platform,
        status=result.status, url=result.url, remote_id=result.remote_id,
        ok=result.ok, error=result.error,
    )
    if result.ok:
        log.info("%s -> %s (%s)", platform, result.url or result.remote_id, result.status)
    else:
        log.error("%s publish failed: %s", platform, result.error)
    return result.ok


def cmd_publish(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    video_path = Path(args.video).resolve()
    if not video_path.is_file():
        log.error("not found: %s", video_path)
        return 1
    if not video_path.with_suffix(".json").is_file():
        log.error("missing sidecar: %s", video_path.with_suffix(".json"))
        return 1
    ok = _publish_one(cfg, video_path, args.platform)
    return 0 if ok else 1


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
            if not _publish_one(cfg, video_path, platform):
                rc = 1
    return rc


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

    f = sub.add_parser("fetch", help="download stock clips into assets/video/_incoming")
    f.add_argument("--source", choices=["pixabay", "pexels"], default="pixabay")
    f.add_argument("--query", default=None)
    f.add_argument("--limit", type=int, default=5)
    f.set_defaults(func=cmd_fetch)

    pub = sub.add_parser("publish", help="publish one rendered video")
    pub.add_argument("video", help="path to output/<name>.mp4")
    pub.add_argument("--platform", choices=list(_PUBLISHERS), default="youtube")
    pub.set_defaults(func=cmd_publish)

    r = sub.add_parser("run", help="build + publish to project.target_platforms (scheduler entrypoint)")
    r.add_argument("--count", type=int, default=1)
    r.set_defaults(func=cmd_run)

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
