from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class PublishResult:
    platform: str
    ok: bool
    status: str                 # e.g. "uploaded", "skipped", "error"
    url: str | None = None
    remote_id: str | None = None
    error: str | None = None


class Publisher(Protocol):
    platform: str

    def publish(self, video_path: Path, metadata: dict) -> PublishResult:
        """Upload `video_path` using the platform-specific block in `metadata['platforms']`."""
        ...
