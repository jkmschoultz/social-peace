"""Pixabay video search + download. Docs: https://pixabay.com/api/docs/

Auth: set PIXABAY_API_KEY in .env. Pixabay Content License allows commercial use and
redistribution without attribution, but a few uploads carry extra terms and people/
brands in frame are not model-released — still review each clip's page.
"""
from __future__ import annotations

import logging
import os

import requests

from social_peace.config import Config
from social_peace.fetch.common import (
    append_manifest_stub,
    download,
    incoming_dir,
    mark_seen,
    output_size,
    pick_rendition,
    seen_ids,
    slugify,
)

log = logging.getLogger(__name__)
API = "https://pixabay.com/api/videos/"


def _key() -> str:
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY not set (put it in .env)")
    return key


def _best_file(hit: dict, out_w: int = 1080, out_h: int = 1920) -> dict | None:
    """Smallest of large / medium / small / tiny that covers the output frame."""
    files = [f for f in (hit.get("videos") or {}).values() if f and f.get("url")]
    return pick_rendition(files, out_w, out_h)


def search(query: str, *, per_page: int = 20, video_type: str = "film", min_width: int = 1080,
           page: int = 1) -> list[dict] | None:
    """Hits at least `min_width` wide; None once past the last page."""
    params = {
        "page": page,
        "key": _key(),
        "q": query,
        "per_page": max(3, min(per_page, 200)),
        "video_type": video_type,
        "safesearch": "true",
    }
    resp = requests.get(API, params=params, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits:
        return None
    out = []
    for h in hits:
        files = h.get("videos", {})
        best = files.get("large") or files.get("medium") or files.get("small") or {}
        if best and best.get("width", 0) >= min_width:
            out.append(h)
    return out


def fetch(cfg: Config, query: str | None = None, *, limit: int = 5) -> list[str]:
    fc = cfg.raw.get("fetch", {}).get("pixabay", {})
    query = query or fc.get("default_query", "nature calm")
    dest_dir = incoming_dir(cfg.path("video_assets"))
    manifest_path = cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml")
    seen = seen_ids(cfg.path("video_assets"), "pixabay")
    saved: list[str] = []
    # page past results the library already has, so a repeated query still yields new clips
    for page in range(1, int(fc.get("max_pages", 3)) + 1):
        hits = search(
            query,
            per_page=int(fc.get("per_page", 20)),
            video_type=fc.get("video_type", "film"),
            min_width=int(fc.get("min_width", 1080)),
            page=page,
        )
        if hits is None:
            break
        for h in hits:
            if len(saved) >= limit:
                break
            if str(h["id"]) in seen:
                continue
            best = _best_file(h, *output_size(cfg))
            if not best:
                continue
            name = f"pixabay-{h['id']}-{slugify(query)}.mp4"
            dest = dest_dir / name
            seen.add(str(h["id"]))
            if download(best["url"], dest):
                mark_seen(cfg.path("video_assets"), "pixabay", h["id"])
                append_manifest_stub(
                    manifest_path, "video", name,
                    {
                        "tags": [t.strip() for t in h.get("tags", "").split(",") if t.strip()],
                        "source": f"Pixabay — {h.get('user', 'unknown')}",
                        "license": "Pixabay Content License",
                        "url": h.get("pageURL", ""),
                    },
                )
                saved.append(str(dest))
        if len(saved) >= limit:
            break
    log.info("pixabay: saved %d clip(s) to %s", len(saved), dest_dir)
    return saved
