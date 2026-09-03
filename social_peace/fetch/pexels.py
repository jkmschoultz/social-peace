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
    query_tags,
    slugify,
)

log = logging.getLogger(__name__)
API = "https://api.pexels.com/videos/search"


def _headers() -> dict:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        raise RuntimeError("PEXELS_API_KEY not set (put it in .env)")
    return {"Authorization": key}


def _best_file(video: dict, min_height: int = 1280) -> dict | None:
    files = sorted(
        video.get("video_files", []),
        key=lambda f: (f.get("height") or 0) * (f.get("width") or 0),
        reverse=True,
    )
    for f in files:
        if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= min_height:
            return f
    return files[0] if files else None


def search(query: str, *, orientation: str = "portrait", size: str = "large", per_page: int = 15) -> list[dict]:
    params = {"query": query, "orientation": orientation, "size": size, "per_page": per_page}
    resp = requests.get(API, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("videos", [])


def fetch(cfg: Config, query: str | None = None, *, limit: int = 5) -> list[str]:
    fc = cfg.raw.get("fetch", {}).get("pexels", {})
    query = query or fc.get("default_query", "calm nature vertical")
    videos = search(
        query,
        orientation=fc.get("orientation", "portrait"),
        size=fc.get("size", "large"),
        per_page=int(fc.get("per_page", 15)),
    )
    dest_dir = incoming_dir(cfg.path("video_assets"))
    manifest_path = cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml")
    saved: list[str] = []
    for v in videos[:limit]:
        f = _best_file(v)
        if not f:
            continue
        name = f"pexels-{v['id']}-{slugify(query)}.mp4"
        dest = dest_dir / name
        if download(f["link"], dest):
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
    log.info("pexels: saved %d clip(s) to %s", len(saved), dest_dir)
    return saved
