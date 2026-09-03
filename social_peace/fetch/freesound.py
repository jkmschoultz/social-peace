"""Freesound CC0 audio search + download. Docs: https://freesound.org/docs/api/

Auth: set FREESOUND_API_KEY in .env (an API token from
https://freesound.org/apiv2/apply/). The token alone is enough to search and to
download the HQ mp3 *preview* of each sound, which is what we use here — the
full-resolution `download` endpoint needs a full OAuth2 dance that isn't worth it
for an ambient bed that gets looped and normalised to -14 LUFS anyway.

Only sounds released under Creative Commons 0 are requested, so no attribution is
required; provenance is still recorded in assets/manifest.yaml.
"""
from __future__ import annotations

import logging
import os

import requests

from social_peace.config import Config
from social_peace.fetch.common import (
    append_manifest_stub,
    download,
    incoming_audio_dir,
    query_tags,
    slugify,
)

log = logging.getLogger(__name__)
API = "https://freesound.org/apiv2/search/text/"
CC0_FILTER = 'license:"Creative Commons 0"'


def _token() -> str:
    key = os.environ.get("FREESOUND_API_KEY")
    if not key:
        raise RuntimeError("FREESOUND_API_KEY not set (put it in .env)")
    return key


def search(query: str, *, page_size: int = 15, min_duration: float = 20.0) -> list[dict]:
    params = {
        "query": query,
        "filter": CC0_FILTER,
        "fields": "id,name,previews,license,url,username,duration",
        "page_size": max(1, min(page_size, 150)),
        "token": _token(),
    }
    resp = requests.get(API, params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r for r in results if float(r.get("duration") or 0) >= min_duration]


def fetch(cfg: Config, query: str | None = None, *, limit: int = 5) -> list[str]:
    fc = cfg.raw.get("fetch", {}).get("freesound", {})
    query = query or fc.get("default_query", "ambient drone")
    hits = search(
        query,
        page_size=int(fc.get("page_size", 15)),
        min_duration=float(fc.get("min_duration", 20.0)),
    )
    dest_dir = incoming_audio_dir(cfg.path("audio_assets"))
    manifest_path = cfg.root / cfg.raw.get("assets_manifest", "assets/manifest.yaml")
    saved: list[str] = []
    for h in hits[:limit]:
        previews = h.get("previews", {})
        url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
        if not url:
            continue
        name = f"freesound-{h['id']}-{slugify(query)}.mp3"
        dest = dest_dir / name
        if download(url, dest):
            append_manifest_stub(
                manifest_path, "audio", name,
                {
                    "tags": query_tags(query),
                    "source": f"Freesound — {h.get('username', 'unknown')}",
                    "license": "CC0",
                    "url": h.get("url", ""),
                },
            )
            saved.append(str(dest))
    log.info("freesound: saved %d clip(s) to %s", len(saved), dest_dir)
    return saved
