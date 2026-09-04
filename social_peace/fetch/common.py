from __future__ import annotations

import logging
import re
from pathlib import Path

import requests
import yaml

from social_peace.fetch import INCOMING_SUBDIR

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, maxlen: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:maxlen] or "clip"


def query_tags(query: str) -> list[str]:
    """Descriptive tokens from a search query, kept as manifest tags so the
    selector can pair e.g. a 'rain' audio bed with a 'rain' clip."""
    return [t for t in slugify(query).split("-") if len(t) >= 3]


def incoming_dir(video_assets: Path) -> Path:
    d = video_assets / INCOMING_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def incoming_audio_dir(audio_assets: Path) -> Path:
    d = audio_assets / INCOMING_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def promote_incoming(asset_dir: Path) -> list[Path]:
    """Move every file out of `asset_dir/_incoming/` up into `asset_dir`.

    Manifest entries are keyed by bare filename, so they keep resolving after the
    move. Name collisions in the destination are left in place and skipped.
    """
    src = asset_dir / INCOMING_SUBDIR
    if not src.is_dir():
        return []
    moved: list[Path] = []
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name.endswith(".part"):
            continue
        dest = asset_dir / f.name
        if dest.exists():
            log.info("promote: %s already in %s, skipping", f.name, asset_dir.name)
            continue
        f.rename(dest)
        log.info("promote: %s -> %s", f.name, asset_dir.name)
        moved.append(dest)
    return moved


def download(url: str, dest: Path, *, timeout: int = 60) -> bool:
    if dest.exists():
        log.info("exists, skipping: %s", dest.name)
        return False
    log.info("downloading %s -> %s", url[:80], dest.name)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
        tmp.rename(dest)
    return True


def append_manifest_stub(manifest_path: Path, kind: str, filename: str, entry: dict) -> None:
    """Merge a provenance entry into assets/manifest.yaml (created if absent)."""
    data = {}
    if manifest_path.exists():
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    data.setdefault(kind, {})
    data[kind].setdefault(filename, entry)
    manifest_path.write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8"
    )


def update_manifest_entry(manifest_path: Path, kind: str, filename: str, updates: dict) -> dict:
    """Merge `updates` into one manifest entry, creating it if absent. Keys set to
    None are removed. Returns the resulting entry."""
    data = {}
    if manifest_path.exists():
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entry = data.setdefault(kind, {}).setdefault(filename, {})
    for k, v in updates.items():
        if v is None:
            entry.pop(k, None)
        else:
            entry[k] = v
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8"
    )
    return entry


def remove_manifest_entry(manifest_path: Path, kind: str, filename: str) -> bool:
    """Drop one provenance entry from assets/manifest.yaml. Returns True if removed."""
    if not manifest_path.exists():
        return False
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if filename in (data.get(kind) or {}):
        del data[kind][filename]
        manifest_path.write_text(
            yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8"
        )
        return True
    return False
