"""Re-render one video with some dimensions re-picked and the rest held fixed —
"I like this render, but give me different audio / clips / text"."""
from __future__ import annotations

import json
from collections.abc import Iterable

from social_peace.config import Config
from social_peace.pipeline.assemble import build_one
from social_peace.pipeline.metadata import sources_in_use

CHANGEABLE = ("video", "audio", "text")

# reason -> (dimension to re-roll, tick-box label, note written on the render)
REJECT_REASONS = {
    "text-overused": ("text", "text overused", "on-screen text used too recently"),
    "video-overused": ("video", "clips overused", "clips used too recently"),
    "audio-overused": ("audio", "audio overused", "audio used too recently"),
    "audio-mismatch": ("audio", "audio doesn't fit", "audio doesn't fit the clips"),
}


def _changes(change: str | Iterable[str]) -> list[str]:
    """Normalise one dimension or several to an ordered, de-duplicated list."""
    wanted = {change} if isinstance(change, str) else set(change)
    bad = wanted - set(CHANGEABLE)
    if bad or not wanted:
        raise ValueError(f"change must be one or more of {CHANGEABLE}, got {sorted(bad) or 'nothing'}")
    return [c for c in CHANGEABLE if c in wanted]


def changes_for(reasons: Iterable[str]) -> list[str]:
    """The dimensions a set of reasons asks to re-roll."""
    return _changes(REJECT_REASONS[r][0] for r in reasons)


def make_variant(
    cfg: Config,
    original: dict,
    change: str | Iterable[str],
    *,
    prefer: str | None = None,
    dry_run: bool = False,
) -> dict:
    """`original` is a loaded sidecar dict; `change` is one dimension or several
    ("video" / "audio" / "text"). Returns the build_one result for the new
    render (state pending, tagged variant_of / variant_change). `prefer` is a
    free-text scene/sound wish that biases re-rolled clips / audio."""
    changes = _changes(change)

    src = original.get("sources", {})
    orig_clips = list(src.get("video", []))
    orig_audio = list(src.get("audio", []))
    orig_text = original.get("overlay_text")
    prefer = (prefer or "").strip() or None
    new_v, new_a, new_t = ("video" in changes), ("audio" in changes), ("text" in changes)

    pin: dict = {}
    if not new_v:
        pin["video"] = orig_clips
    if not new_a:
        pin["audio"] = orig_audio
    if not new_t:
        pin["text"] = orig_text

    meta = {"variant_of": original["id"], "variant_change": "+".join(changes)}
    if prefer and (new_v or new_a):
        meta["variant_prefer"] = prefer

    # weak nudge away from assets already spent by other accepted/published renders
    su_v, su_a = sources_in_use(cfg.path("output"))

    return build_one(
        cfg,
        seed=int(original["seed"]),
        template_name=original["template"],
        duration=original.get("duration_seconds"),
        dry_run=dry_run,
        used_video=set(orig_clips) if new_v else None,
        used_audio=set(orig_audio) if new_a else None,
        soft_used_video=su_v if new_v else None,
        soft_used_audio=su_a if new_a else None,
        exclude_text=orig_text if new_t else None,
        prefer_video=prefer if new_v else None,
        prefer_audio=prefer if new_a else None,
        pin=pin,
        extra_meta=meta,
    )


def check_removable(cfg: Config, original: dict, kind: str, name: str) -> list[str]:
    """The sources left once `name` is dropped from `original`'s `kind` ("video"
    / "audio"). Raises ValueError if that isn't possible — it would leave none,
    or a kept file has since left the library (the pin would silently re-pick)."""
    if kind not in ("video", "audio"):
        raise ValueError("kind must be 'video' or 'audio'")
    names = list(original.get("sources", {}).get(kind, []))
    if name not in names:
        raise ValueError(f"{name!r} is not one of this render's {kind} sources")
    if len(names) < 2:
        raise ValueError(f"{name!r} is this render's only {kind} source")
    keep = [n for n in names if n != name]
    other = "audio" if kind == "video" else "video"
    gone = [
        n
        for k, ns in ((kind, keep), (other, original.get("sources", {}).get(other, [])))
        for n in ns
        if not (cfg.path(f"{k}_assets") / n).is_file()
    ]
    if gone:
        raise ValueError(f"no longer in the library: {', '.join(gone)}")
    return keep


def _original_starts(cfg: Config, original: dict) -> dict[str, float]:
    """{clip name: in-point} the original render used — recorded in its sidecar,
    or (renders from before that) the seeded draw replayed."""
    from social_peace.pipeline.assemble import clip_starts
    from social_peace.pipeline.ffmpeg_utils import ffprobe_duration

    clips = list(original.get("sources", {}).get("video", []))
    starts = original.get("clip_starts")
    if not starts or len(starts) != len(clips):
        tpl = next((t for t in cfg.templates if t["name"] == original["template"]), {})
        xdur = float(tpl.get("transition_duration", 0.0)) if tpl.get("transition") == "fade" else 0.0
        n = len(clips)
        seg = (float(original["duration_seconds"]) + (n - 1) * xdur) / n
        folder = cfg.path("video_assets")
        starts = clip_starts(int(original["seed"]),
                             [ffprobe_duration(folder / c) for c in clips], seg)
    return dict(zip(clips, starts))


def make_without(cfg: Config, original: dict, kind: str, name: str, *, dry_run: bool = False) -> dict:
    """Re-render `original` exactly — same seed, template, length, text, captions
    and every other source — minus one clip or audio bed. Dropping a clip keeps
    the length, so the rest play longer from the same in-points."""
    keep = check_removable(cfg, original, kind, name)
    src = original.get("sources", {})
    pin = {
        "video": list(src.get("video", [])),
        "audio": list(src.get("audio", [])),
        "text": original.get("overlay_text"),
    }
    pin[kind] = keep
    return build_one(
        cfg,
        seed=int(original["seed"]),
        template_name=original["template"],
        duration=original.get("duration_seconds"),
        dry_run=dry_run,
        pin=pin,
        start_at=_original_starts(cfg, original),
        platforms=original.get("platforms"),
        extra_meta={
            "variant_of": original["id"],
            "variant_change": f"remove-{kind}",
            "variant_removed": name,
        },
    )


def note_reasons(cfg: Config, md: dict, reasons: Iterable[str]) -> None:
    """Act on what the reviewer ticked about a render: an "-overused" reason
    sends that line / those clips / beds to the back of their queue;
    "audio-mismatch" remembers the pairing so selection avoids it."""
    from social_peace.pipeline import recency

    src = md.get("sources", {})
    for reason in reasons:
        if reason not in REJECT_REASONS:
            raise ValueError(f"reason must be one of {list(REJECT_REASONS)}")
        if reason == "text-overused" and md.get("overlay_text"):
            recency.demote(cfg, "text", [md["overlay_text"]])
        elif reason == "video-overused":
            recency.demote(cfg, "video", list(src.get("video", [])))
        elif reason == "audio-overused":
            recency.demote(cfg, "audio", list(src.get("audio", [])))
        elif reason == "audio-mismatch":
            recency.record_mismatch(cfg, list(src.get("audio", [])), list(src.get("video", [])))


def reject_for(cfg: Config, sidecar_path, reasons: str | Iterable[str], *, note: str = "") -> dict:
    """Reject a render for one or more reasons (see note_reasons for the
    bookkeeping). Returns the now-rejected sidecar dict — the caller re-rolls
    with make_variant(cfg, md, changes_for(reasons))."""
    from social_peace.pipeline.metadata import set_review

    reasons = [reasons] if isinstance(reasons, str) else list(dict.fromkeys(reasons))
    if not reasons or any(r not in REJECT_REASONS for r in reasons):
        raise ValueError(f"reasons must be one or more of {list(REJECT_REASONS)}")
    labels = "; ".join(REJECT_REASONS[r][2] for r in reasons)
    md = set_review(sidecar_path, "rejected", note=f"{labels}. {note}".strip(" ."))
    md["review"]["reasons"] = reasons
    sidecar_path.write_text(json.dumps(md, indent=2, ensure_ascii=False), encoding="utf-8")
    note_reasons(cfg, md, reasons)
    return md
