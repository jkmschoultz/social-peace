"""Build the per-video metadata sidecar (<stem>.json) that the publish step consumes."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.selectors import Selection


def _hook_from_text(text: str) -> str:
    return text.rstrip(" .!").strip()


def build_metadata(cfg: Config, sel: Selection, video_path: Path) -> dict:
    rng = random.Random(sel.seed ^ 0xA5A5A5)
    md = cfg.raw["metadata"]
    yt = md["youtube"]
    hashtags = " ".join(md.get("hashtags", []))

    hook = _hook_from_text(sel.text)
    title = rng.choice(yt["title_templates"]).format(hook=hook)
    description = (yt["description"].strip() + "\n\n" + hashtags).strip()

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
        "platforms": {
            "youtube": {
                "title": title[:100],
                "description": description[:4900],
                "tags": list(yt.get("tags", [])),
                "categoryId": str(yt.get("category_id", "22")),
                "privacyStatus": yt.get("privacy", "private"),
                "madeForKids": bool(yt.get("made_for_kids", False)),
            }
        },
        "status": {"youtube": "pending"},
    }


def write_sidecar(metadata: dict, video_path: Path) -> Path:
    side = video_path.with_suffix(".json")
    side.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return side


def load_sidecar(video_path: Path) -> dict:
    return json.loads(video_path.with_suffix(".json").read_text(encoding="utf-8"))
