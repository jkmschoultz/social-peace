"""Append-only JSONL ledger of everything built and posted: logs/posts.jsonl.

Each line is one event, e.g.:
  {"ts": "...", "event": "build",   "video_id": "...", "template": "...", "seed": 123, "ok": true}
  {"ts": "...", "event": "publish", "video_id": "...", "platform": "youtube",
   "status": "uploaded", "url": "https://youtu.be/...", "ok": true}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


def _ledger_path(log_dir: Path) -> Path:
    return log_dir / "posts.jsonl"


def record(log_dir: Path, event: str, **fields: Any) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
    entry.update(fields)
    with _ledger_path(log_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("ledger: %s %s", event, {k: v for k, v in fields.items() if k != "error"})
    return entry


def read_all(log_dir: Path) -> Iterator[dict]:
    path = _ledger_path(log_dir)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def already_published(log_dir: Path, video_id: str, platform: str) -> bool:
    for e in read_all(log_dir):
        if (
            e.get("event") == "publish"
            and e.get("video_id") == video_id
            and e.get("platform") == platform
            and e.get("ok")
        ):
            return True
    return False
