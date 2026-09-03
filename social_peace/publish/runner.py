"""Shared publish orchestration for the CLI (`publish` / `publish-approved`) and
the review UI's Publish button. One place that maps platform -> module, records
the ledger event, and writes `status` back into the sidecar."""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

from social_peace import ledger
from social_peace.config import Config
from social_peace.pipeline.metadata import load_sidecar, mark_status
from social_peace.publish.base import PublishResult

log = logging.getLogger(__name__)

PUBLISHERS = {
    "youtube": "social_peace.publish.youtube",
    "tiktok": "social_peace.publish.tiktok",
    "instagram": "social_peace.publish.instagram",
}

# sidecar status values that mean "already live on this platform — don't re-post"
DONE_STATUSES = ("uploaded", "published", "already-published")


def target_platforms(cfg: Config) -> list[str]:
    return list(cfg.raw.get("project", {}).get("target_platforms", []))


def available_platforms(cfg: Config) -> list[str]:
    """Configured targets that actually have a publisher module."""
    return [p for p in target_platforms(cfg) if p in PUBLISHERS]


def publish_one(cfg: Config, video_path: Path, platform: str) -> PublishResult:
    if platform not in PUBLISHERS:
        return PublishResult(platform, ok=False, status="error",
                             error=f"no publisher for {platform!r}")
    if ledger.already_published(cfg.path("logs"), video_path.stem, platform):
        log.info("%s already on %s, skipping", video_path.name, platform)
        return PublishResult(platform, ok=True, status="already-published")

    metadata = load_sidecar(video_path)
    mod = importlib.import_module(PUBLISHERS[platform])
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
    return result


def publish_sidecar(
    cfg: Config,
    sidecar_path: Path,
    *,
    platforms: list[str] | None = None,
    dry_run: bool = False,
    require_approved: bool = True,
) -> dict:
    """Publish one video to `platforms` (default: available_platforms(cfg)),
    updating its sidecar `status`. Returns
    {"id", "results": [{"platform","ok","status","url","error"}], "error"|None}."""
    stem = sidecar_path.stem
    video_path = sidecar_path.with_suffix(".mp4")
    md = json.loads(sidecar_path.read_text(encoding="utf-8"))

    if require_approved and md.get("review", {}).get("state") != "approved":
        return {"id": stem, "results": [], "error": "not approved"}
    if not video_path.is_file():
        return {"id": stem, "results": [], "error": "mp4 missing"}

    plats = platforms if platforms is not None else available_platforms(cfg)
    if not plats:
        return {"id": stem, "results": [],
                "error": "no available platforms (set project.target_platforms)"}

    results: list[dict] = []
    for platform in plats:
        if platform not in PUBLISHERS:
            if not dry_run:
                mark_status(sidecar_path, platform, "skipped")
            results.append({"platform": platform, "ok": False, "status": "skipped",
                            "url": None, "error": "no publisher"})
            continue
        if dry_run:
            results.append({"platform": platform, "ok": True, "status": "dry-run",
                            "url": None, "error": None})
            continue
        res = publish_one(cfg, video_path, platform)
        mark_status(sidecar_path, platform, res.status if res.ok else "error",
                    url=res.url, remote_id=res.remote_id)
        results.append({"platform": platform, "ok": res.ok, "status": res.status,
                        "url": res.url, "error": res.error})
    return {"id": stem, "results": results, "error": None}


def iter_approved(cfg: Config):
    for side in sorted(cfg.path("output").glob("*.json")):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if md.get("review", {}).get("state") == "approved":
            yield side


def publish_all_approved(
    cfg: Config,
    *,
    platforms: list[str] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> list[dict]:
    sides = list(iter_approved(cfg))
    if limit is not None:
        sides = sides[:limit]
    return [
        publish_sidecar(cfg, s, platforms=platforms, dry_run=dry_run) for s in sides
    ]
