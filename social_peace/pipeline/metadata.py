"""Build the per-video metadata sidecar (<stem>.json) that review + publish consume."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.captions import generate_platform_captions
from social_peace.pipeline.selectors import Selection

REVIEW_STATES = ("pending", "approved", "rejected")


def _hook_from_text(text: str) -> str:
    return text.rstrip(" .!").strip()


def _source_urls(cfg: Config, sel: Selection) -> dict:
    return {
        "video": {c.path.name: cfg.source_for("video", c.path.name) for c in sel.clips},
        "audio": {a.path.name: cfg.source_for("audio", a.path.name) for a in sel.audio},
    }


def _with_hashtags(text: str, tags: list[str]) -> str:
    text = text.strip()
    if tags:
        text = (text + "\n\n" + " ".join(tags)).strip()
    return text


def _caption_body(rng: random.Random, block: dict, hook: str) -> str:
    templates = block.get("caption_templates") or ["{hook}"]
    return rng.choice(templates).format(hook=hook)


def build_metadata(cfg: Config, sel: Selection, video_path: Path) -> dict:
    rng = random.Random(sel.seed ^ 0xA5A5A5)
    md = cfg.raw["metadata"]
    yt = md["youtube"]
    tiktok = md.get("tiktok", {})
    instagram = md.get("instagram", {})
    hashtags = list(md.get("hashtags", []))

    hook = _hook_from_text(sel.text)

    # Try an LLM-written caption set, inspired by (never copied from) the template
    # lists below; fall back to picking one of those templates verbatim.
    generated = generate_platform_captions(cfg, hook, sel.template["name"])
    if generated:
        title = generated["youtube_title"]
        description = generated["youtube_description"]
        tiktok_body = generated["tiktok_caption"]
        instagram_body = generated["instagram_caption"]
    else:
        title = rng.choice(yt["title_templates"]).format(hook=hook)
        description = yt["description"].strip()
        tiktok_body = _caption_body(rng, tiktok, hook)
        instagram_body = _caption_body(rng, instagram, hook)

    description = _with_hashtags(description, hashtags)

    return {
        "id": video_path.stem,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video": video_path.name,
        "duration_seconds": sel.target_duration,
        "seed": sel.seed,
        "template": sel.template["name"],
        "overlay_text": sel.text,
        "sources": {
            "video": [c.path.name for c in sel.clips],
            "audio": [a.path.name for a in sel.audio],
        },
        "source_urls": _source_urls(cfg, sel),
        "review": {"state": "pending", "decided_utc": None, "note": ""},
        "platforms": {
            "youtube": {
                "title": title[:100],
                "description": description[:4900],
                "tags": list(yt.get("tags", [])),
                "categoryId": str(yt.get("category_id", "22")),
                "privacyStatus": yt.get("privacy", "public"),
                "madeForKids": bool(yt.get("made_for_kids", False)),
            },
            "tiktok": {
                "caption": _with_hashtags(tiktok_body, tiktok.get("hashtags", []))[:2200],
                "hashtags": list(tiktok.get("hashtags", [])),
                "privacy": tiktok.get("privacy", "SELF_ONLY"),
            },
            "instagram": {
                "caption": _with_hashtags(instagram_body, instagram.get("hashtags", []))[:2200],
                "hashtags": list(instagram.get("hashtags", [])),
            },
        },
        "status": {"youtube": "pending", "instagram": "pending", "tiktok": "skipped"},
    }


def write_sidecar(metadata: dict, video_path: Path) -> Path:
    side = video_path.with_suffix(".json")
    side.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return side


def load_sidecar(video_path: Path) -> dict:
    return json.loads(video_path.with_suffix(".json").read_text(encoding="utf-8"))


def sources_in_use(output_dir: Path, *, exclude_rejected: bool = True) -> tuple[set[str], set[str]]:
    """(video_names, audio_names) already used by renders in output/. Rejected
    renders release their assets; pending + approved ones hold them."""
    vids: set[str] = set()
    auds: set[str] = set()
    for side in output_dir.glob("*.json"):
        try:
            md = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if exclude_rejected and md.get("review", {}).get("state") == "rejected":
            continue
        src = md.get("sources", {})
        vids.update(src.get("video", []))
        auds.update(src.get("audio", []))
    return vids, auds


def _write(sidecar_path: Path, md: dict) -> None:
    sidecar_path.write_text(json.dumps(md, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_status(
    sidecar_path: Path,
    platform: str,
    state: str,
    *,
    url: str | None = None,
    remote_id: str | None = None,
) -> dict:
    md = json.loads(sidecar_path.read_text(encoding="utf-8"))
    md.setdefault("status", {})[platform] = state
    if url or remote_id:
        md.setdefault("published", {})[platform] = {
            "url": url,
            "remote_id": remote_id,
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    _write(sidecar_path, md)
    return md


def set_review(
    sidecar_path: Path,
    state: str,
    *,
    note: str = "",
    captions: dict | None = None,
) -> dict:
    if state not in REVIEW_STATES:
        raise ValueError(f"invalid review state {state!r} (want one of {REVIEW_STATES})")
    md = json.loads(sidecar_path.read_text(encoding="utf-8"))
    md.setdefault("review", {})
    md["review"]["state"] = state
    md["review"]["decided_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    md["review"]["note"] = note or ""
    if captions:
        plats = md.setdefault("platforms", {})
        for plat, fields in captions.items():
            if plat in plats and isinstance(fields, dict):
                plats[plat].update({k: v for k, v in fields.items() if k in plats[plat]})
    _write(sidecar_path, md)
    return md
