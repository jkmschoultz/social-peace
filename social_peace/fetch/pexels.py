"""Pexels video search + download. Docs: https://www.pexels.com/api/documentation/

Auth: set PEXELS_API_KEY in .env. The Pexels License permits free commercial use with
no attribution required; attribution to Pexels and the photographer is appreciated.
Do not scrape beyond the API and respect the rate limit (200 req/hr by default).
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
    query_tags,
    seen_ids,
    slugify,
)

log = logging.getLogger(__name__)
API = "https://api.pexels.com/videos/search"


def _headers() -> dict:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        raise RuntimeError("PEXELS_API_KEY not set (put it in .env)")
    return {"Authorization": key}


def _best_file(video: dict, out_w: int = 1080, out_h: int = 1920) -> dict | None:
    """Smallest mp4 rendition that covers the output frame (see pick_rendition)."""
    mp4s = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
    return pick_rendition(mp4s, out_w, out_h)


def search(query: str, *, orientation: str = "portrait", size: str = "large", per_page: int = 15,
           page: int = 1) -> list[dict]:
    params = {"query": query, "orientation": orientation, "size": size, "per_page": per_page,
              "page": page}
    resp = requests.get(API, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("videos", [])


def fetch(cfg: Config, query: str | None = None, *, limit: int = 5) -> list[str]:
    fc = cfg.raw.get("fetch", {}).get("pexels", {})
    query = query or fc.get("default_query", "calm nature vertical")
    dest_dir = incoming_dir(cfg.path("video_assets"))
    manifest_path = cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml")
    seen = seen_ids(cfg.path("video_assets"), "pexels")
    saved: list[str] = []
    # page past results the library already has, so a repeated query still yields new clips
    for page in range(1, int(fc.get("max_pages", 3)) + 1):
        videos = search(
            query,
            orientation=fc.get("orientation", "portrait"),
            size=fc.get("size", "large"),
            per_page=int(fc.get("per_page", 15)),
            page=page,
        )
        for v in videos:
            if len(saved) >= limit:
                break
            if str(v["id"]) in seen:
                continue
            f = _best_file(v, *output_size(cfg))
            if not f:
                continue
            name = f"pexels-{v['id']}-{slugify(query)}.mp4"
            dest = dest_dir / name
            seen.add(str(v["id"]))
            if download(f["link"], dest):
                mark_seen(cfg.path("video_assets"), "pexels", v["id"])
                append_manifest_stub(
                    manifest_path, "video", name,
                    {
                        "tags": query_tags(query),
                        "source": f"Pexels — {v.get('user', {}).get('name', 'unknown')}",
                        "license": "Pexels License",
                        "url": v.get("url", ""),
                    },
                )
                saved.append(str(dest))
        if len(saved) >= limit or len(videos) < int(fc.get("per_page", 15)):
            break
    log.info("pexels: saved %d clip(s) to %s", len(saved), dest_dir)
    return saved
