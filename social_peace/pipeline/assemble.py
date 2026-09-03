"""Turn a Selection into an ffmpeg command and render a 9:16 mp4."""
from __future__ import annotations

import logging
import random
from datetime import datetime
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.ffmpeg_utils import run_ffmpeg
from social_peace.pipeline.metadata import build_metadata, write_sidecar
from social_peace.pipeline.overlays import render_overlay
from social_peace.pipeline.selectors import Selection, build_selection

log = logging.getLogger(__name__)


def _f(x: float) -> str:
    return f"{x:.3f}"


def build_filtergraph(cfg: Config, sel: Selection, overlay_idx: int) -> tuple[str, float]:
    """Return (filter_complex, total_duration). Input order assumed by caller:
    clips 0..n-1, audio n..n+m-1, overlay png at `overlay_idx` (== n+m)."""
    w, h = cfg.render["resolution"]
    fps = int(cfg.render["fps"])
    n = len(sel.clips)
    m = len(sel.audio)
    seg = sel.segment_duration
    tpl = sel.template
    is_fade = tpl.get("transition") == "fade" and n > 1
    xdur = float(tpl.get("transition_duration", 0.0)) if is_fade else 0.0
    total = n * seg - (n - 1) * xdur

    eq = tpl.get("color", {}).get("eq", "").strip()
    extra = tpl.get("color", {}).get("extra", "").strip()
    motion = tpl.get("motion", "none")

    chains: list[str] = []

    # --- per-clip normalisation to 9:16 ---
    for i in range(n):
        steps = [
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase",
            f"crop={w}:{h}",
            f"fps={fps}",
            "setsar=1",
        ]
        if motion == "zoom_in":
            steps.append(
                f"zoompan=z='min(zoom+0.0009,1.12)':d=1:s={w}x{h}:fps={fps}"
            )
        if eq:
            steps.append(f"eq={eq}")
        if extra:
            steps.append(extra)
        steps.append("format=yuv420p")
        chains.append(",".join(steps) + f"[v{i}]")

    # --- stitch ---
    if n == 1:
        video_label = "[v0]"
    elif is_fade:
        prev = "v0"
        for k in range(1, n):
            off = k * (seg - xdur)
            label = f"m{k}" if k < n - 1 else "vc"
            chains.append(
                f"[{prev}][v{k}]xfade=transition=fade:duration={_f(xdur)}:offset={_f(off)}[{label}]"
            )
            prev = label
        video_label = "[vc]"
    else:
        joins = "".join(f"[v{i}]" for i in range(n))
        chains.append(f"{joins}concat=n={n}:v=1:a=0[vc]")
        video_label = "[vc]"

    # --- text overlay ---
    fade = min(0.8, total / 4)
    chains.append(
        f"[{overlay_idx}:v]format=rgba,"
        f"fade=t=in:st=0:d={_f(fade)}:alpha=1,"
        f"fade=t=out:st={_f(total - fade)}:d={_f(fade)}:alpha=1[ov]"
    )
    chains.append(f"{video_label}[ov]overlay=0:0[vout]")

    # --- audio beds ---
    for j in range(m):
        idx = n + j
        vol = 0.9 if j == 0 else 0.55
        chains.append(
            f"[{idx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=duration={_f(total)},asetpts=N/SR/TB,volume={vol}[a{j}]"
        )
    if m == 1:
        mixed = "[a0]"
    else:
        ins = "".join(f"[a{j}]" for j in range(m))
        chains.append(f"{ins}amix=inputs={m}:normalize=0:dropout_transition=0[amx]")
        mixed = "[amx]"

    fin = float(cfg.render.get("audio_fade_in", 2.0))
    fout = float(cfg.render.get("audio_fade_out", 3.0))
    lufs = float(cfg.render.get("loudness_lufs", -14.0))
    chains.append(
        f"{mixed}afade=t=in:d={_f(fin)},"
        f"afade=t=out:st={_f(max(total - fout, 0))}:d={_f(fout)},"
        f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[aout]"
    )

    return ";".join(chains), total


def build_one(
    cfg: Config,
    *,
    seed: int,
    template_name: str | None = None,
    duration: float | None = None,
    dry_run: bool = False,
    used_video: set[str] | None = None,
    used_audio: set[str] | None = None,
) -> dict:
    sel = build_selection(
        cfg, seed=seed, template_name=template_name, duration=duration,
        used_video=used_video, used_audio=used_audio,
    )

    w, h = cfg.render["resolution"]
    fps = int(cfg.render["fps"])
    out_dir = cfg.path("output")
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}_{sel.template['name']}_{seed}"
    video_path = out_dir / f"{stem}.mp4"
    overlay_png = out_dir / f"{stem}.overlay.png"

    ov = sel.template.get("overlay", {})
    render_overlay(
        text=sel.text,
        footer=sel.footer,
        size=(w, h),
        out_path=overlay_png,
        fonts_dir=cfg.path("fonts"),
        style=ov.get("style", "lower_third"),
        scrim=ov.get("scrim", "gradient"),
        color=ov.get("color", "#FDFDF8"),
        font_size=int(ov.get("size", 62)),
    )

    # --- assemble ffmpeg input list (order matters, matches build_filtergraph) ---
    n = len(sel.clips)
    m = len(sel.audio)
    overlay_idx = n + m
    inputs: list[str] = []

    start_rng = random.Random(seed ^ 0x5F3759DF)
    for clip in sel.clips:
        max_start = max(0.0, clip.duration - sel.segment_duration - 0.1)
        start = start_rng.uniform(0.0, max_start)
        inputs += ["-ss", _f(start), "-t", _f(sel.segment_duration), "-i", str(clip.path)]

    filtergraph, total = build_filtergraph(cfg, sel, overlay_idx)

    for bed in sel.audio:
        inputs += ["-stream_loop", "-1", "-i", str(bed.path)]
    inputs += ["-loop", "1", "-t", _f(total), "-i", str(overlay_png)]

    args = [
        *inputs,
        "-filter_complex", filtergraph,
        "-map", "[vout]", "-map", "[aout]",
        "-r", str(fps),
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-preset", str(cfg.render.get("preset", "medium")),
        "-crf", str(cfg.render.get("crf", 19)),
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", _f(total),
        "-metadata", f"comment=social-peace seed={seed} template={sel.template['name']}",
        str(video_path),
    ]

    run_ffmpeg(args, dry_run=dry_run)

    if dry_run:
        overlay_png.unlink(missing_ok=True)
        return {
            "id": stem, "video": None, "template": sel.template["name"], "seed": seed,
            "sources": {
                "video": [c.path.name for c in sel.clips],
                "audio": [a.path.name for a in sel.audio],
            },
            "dry_run": True,
        }

    metadata = build_metadata(cfg, sel, video_path)
    metadata["duration_seconds"] = round(total, 2)
    sidecar = write_sidecar(metadata, video_path)

    if not cfg.render.get("keep_overlay_png", False):
        overlay_png.unlink(missing_ok=True)

    log.info("rendered %s (%.1fs)", video_path.name, total)
    return {
        "id": stem,
        "video": str(video_path),
        "sidecar": str(sidecar),
        "template": sel.template["name"],
        "seed": seed,
        "duration": round(total, 2),
        "sources": metadata["sources"],
        "dry_run": False,
    }
