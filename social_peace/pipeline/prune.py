"""Delete rejected renders to reclaim disk. Old ones age out automatically;
`drop_all` clears the lot. Approved / published / pending renders are never
touched, and the ledger keeps the full history.

`clear_published_media` frees the heavy files of published renders but keeps
their sidecar, which is what selection reads to avoid reusing clips / beds /
overlay lines."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from social_peace.config import Config

log = logging.getLogger(__name__)

_EXTS = (".mp4", ".json", ".overlay.png", ".thumb.jpg")


def _decided_at(md: dict, sidecar: Path) -> datetime:
    stamp = (md.get("review") or {}).get("decided_utc")
    if stamp:
        try:
            dt = datetime.fromisoformat(stamp)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(sidecar.stat().st_mtime, tz=timezone.utc)


def prune_rejected(
    cfg: Config, *, older_than_days: int | None = 30, drop_all: bool = False
) -> list[str]:
    """Remove rejected renders. If `drop_all`, every rejected one; otherwise only
    those rejected more than `older_than_days` ago. Returns the deleted stems."""
    out = cfg.path("output")
    cutoff = (
        None if (drop_all or older_than_days is None)
        else datetime.now(timezone.utc) - timedelta(days=older_than_days)
    )
    removed: list[str] = []
    for side in sorted(out.glob("*.json")):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if (md.get("review") or {}).get("state") != "rejected":
            continue
        if cutoff is not None and _decided_at(md, side) > cutoff:
            continue
        stem = side.stem
        for ext in _EXTS:
            (out / f"{stem}{ext}").unlink(missing_ok=True)
        removed.append(stem)
    if removed:
        log.info("pruned %d rejected render(s): %s", len(removed), removed)
    return removed


# the heavy files of a render; the .json sidecar stays
_MEDIA_EXTS = (".mp4", ".overlay.png", ".thumb.jpg")


def clear_published_media(cfg: Config) -> list[str]:
    """Delete the video / thumbnail / overlay of published renders, keeping the
    sidecar. Renders still owed to a platform (scheduler retries pending) are
    left alone. Returns the stems cleared."""
    from social_peace.publish import schedule
    from social_peace.publish.runner import is_published

    out = cfg.path("output")
    owed = {side.stem for side, _ in schedule.partial(cfg)}
    cleared: list[str] = []
    for side in sorted(out.glob("*.json")):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        stem = side.stem
        if (md.get("review") or {}).get("state") != "approved" or not is_published(md):
            continue
        if stem in owed or not (out / f"{stem}.mp4").is_file():
            continue
        for ext in _MEDIA_EXTS:
            (out / f"{stem}{ext}").unlink(missing_ok=True)
        cleared.append(stem)
    if cleared:
        log.info("cleared media of %d published render(s): %s", len(cleared), cleared)
    return cleared
