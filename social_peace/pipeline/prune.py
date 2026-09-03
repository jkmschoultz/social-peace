"""Delete rejected renders to reclaim disk. Old ones age out automatically;
`drop_all` clears the lot. Approved / published / pending renders are never
touched, and the ledger keeps the full history."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from social_peace.config import Config

log = logging.getLogger(__name__)

_EXTS = (".mp4", ".json", ".overlay.png")


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
