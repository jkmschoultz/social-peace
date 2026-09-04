"""Re-render one video with exactly one dimension re-picked and everything else
held fixed — "I like this render, but give me different audio / clips / text"."""
from __future__ import annotations

from social_peace.config import Config
from social_peace.pipeline.assemble import build_one
from social_peace.pipeline.metadata import sources_in_use

CHANGEABLE = ("video", "audio", "text")


def make_variant(
    cfg: Config, original: dict, change: str, *, prefer: str | None = None, dry_run: bool = False
) -> dict:
    """`original` is a loaded sidecar dict. Returns the build_one result for the
    new render (state pending, tagged variant_of / variant_change). `prefer` is a
    free-text scene/sound wish that biases the re-rolled dimension."""
    if change not in CHANGEABLE:
        raise ValueError(f"change must be one of {CHANGEABLE}, got {change!r}")

    src = original.get("sources", {})
    orig_clips = list(src.get("video", []))
    orig_audio = list(src.get("audio", []))
    orig_text = original.get("overlay_text")
    prefer = (prefer or "").strip() or None

    pin: dict = {}
    if change != "video":
        pin["video"] = orig_clips
    if change != "audio":
        pin["audio"] = orig_audio
    if change != "text":
        pin["text"] = orig_text

    meta = {"variant_of": original["id"], "variant_change": change}
    if prefer and change in ("video", "audio"):
        meta["variant_prefer"] = prefer

    # weak nudge away from assets already spent by other accepted/published renders
    su_v, su_a = sources_in_use(cfg.path("output"))

    return build_one(
        cfg,
        seed=int(original["seed"]),
        template_name=original["template"],
        duration=original.get("duration_seconds"),
        dry_run=dry_run,
        used_video=set(orig_clips) if change == "video" else None,
        used_audio=set(orig_audio) if change == "audio" else None,
        soft_used_video=su_v if change == "video" else None,
        soft_used_audio=su_a if change == "audio" else None,
        exclude_text=orig_text if change == "text" else None,
        prefer_video=prefer if change == "video" else None,
        prefer_audio=prefer if change == "audio" else None,
        pin=pin,
        extra_meta=meta,
    )
