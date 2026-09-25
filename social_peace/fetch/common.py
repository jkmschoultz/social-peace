from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import requests
import yaml

from social_peace.fetch import INCOMING_SUBDIR

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# serialise read-modify-write of the manifest — concurrent fetch jobs / star /
# rename in the review server would otherwise lose each other's updates
_MANIFEST_LOCK = threading.RLock()


@contextmanager
def manifest_lock():
    with _MANIFEST_LOCK:
        yield


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}


def _dump_manifest(manifest_path: Path, data: dict) -> None:
    """Write the manifest atomically so a concurrent reader (any review-UI
    request calls Config.load) never sees a half-written file."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(manifest_path.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, manifest_path)
    except BaseException:
        os.unlink(tmp)
        raise


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


def pick_rendition(files: list[dict], out_w: int, out_h: int) -> dict | None:
    """The smallest rendition that still covers the output frame without
    upscaling (the render scales to *cover* out_w x out_h, then centre-crops),
    so a 4K upload is fetched as its 1080x1920 version when one exists. Falls
    back to the largest rendition when none is big enough. `files` are dicts
    with width / height."""
    sized = [f for f in files if (f.get("width") or 0) and (f.get("height") or 0)]
    if not sized:
        return None
    area = lambda f: f["width"] * f["height"]  # noqa: E731
    covers = [f for f in sized if max(out_w / f["width"], out_h / f["height"]) <= 1.0]
    return min(covers, key=area) if covers else max(sized, key=area)


def output_size(cfg) -> tuple[int, int]:
    w, h = cfg.render.get("resolution", [1080, 1920])
    return int(w), int(h)


_SEEN_FILE = ".fetched.json"
_SEEN_LOCK = threading.Lock()


def seen_ids(asset_dir: Path, source: str) -> set[str]:
    """Stock ids from `source` this library already has or once had: every
    `<source>-<id>-*` file in asset_dir / _incoming, plus the ids recorded at
    download time in asset_dir/.fetched.json (so a clip you deleted from the
    library is not fetched again). Fetchers skip these, so a search term that
    comes round again in the rotation yields new results rather than the same
    top hits under a new filename."""
    ids: set[str] = set()
    for d in (asset_dir, asset_dir / INCOMING_SUBDIR):
        if d.is_dir():
            ids |= {f.name.split("-")[1] for f in d.iterdir()
                    if f.name.startswith(source + "-") and f.name.count("-") >= 2}
    try:
        ids |= set(json.loads((asset_dir / _SEEN_FILE).read_text(encoding="utf-8")).get(source, []))
    except (FileNotFoundError, ValueError):
        pass
    return ids


def mark_seen(asset_dir: Path, source: str, item_id) -> None:
    with _SEEN_LOCK:
        path = asset_dir / _SEEN_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        ids = data.setdefault(source, [])
        if str(item_id) not in ids:
            ids.append(str(item_id))
        asset_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")


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
    with _MANIFEST_LOCK:
        data = _load_manifest(manifest_path)
        data.setdefault(kind, {}).setdefault(filename, entry)
        _dump_manifest(manifest_path, data)


def update_manifest_entry(manifest_path: Path, kind: str, filename: str, updates: dict) -> dict:
    """Merge `updates` into one manifest entry, creating it if absent. Keys set to
    None are removed. Returns the resulting entry."""
    with _MANIFEST_LOCK:
        data = _load_manifest(manifest_path)
        entry = data.setdefault(kind, {}).setdefault(filename, {})
        for k, v in updates.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        _dump_manifest(manifest_path, data)
        return entry


def remove_manifest_entry(manifest_path: Path, kind: str, filename: str) -> bool:
    """Drop one provenance entry from assets/manifest.yaml. Returns True if removed."""
    with _MANIFEST_LOCK:
        data = _load_manifest(manifest_path)
        if filename in (data.get(kind) or {}):
            del data[kind][filename]
            _dump_manifest(manifest_path, data)
            return True
        return False
