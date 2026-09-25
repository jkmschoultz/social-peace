"""Thin wrappers around the ffmpeg / ffprobe binaries."""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


def _resolve_bin(name: str) -> str:
    """`name` is 'ffmpeg' or 'ffprobe'. Honours FFMPEG_BIN / FFPROBE_BIN, else PATH."""
    override = os.environ.get(f"{name.upper()}_BIN")
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"{name!r} not found. Install ffmpeg and put it on PATH, or set "
            f"{name.upper()}_BIN in your .env. On Windows: `winget install Gyan.FFmpeg`."
        )
    return found


def ffprobe_duration(path: str | Path, *, timeout: float = 15.0) -> float:
    cmd = [
        _resolve_bin("ffprobe"), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout).stdout
    return float(json.loads(out)["format"]["duration"])


def ffprobe_streams(path: str | Path) -> list[dict]:
    cmd = [
        _resolve_bin("ffprobe"), "-v", "error",
        "-show_streams", "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("streams", [])


def has_audio_stream(path: str | Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffprobe_streams(path))


def run_ffmpeg(args: list[str], *, dry_run: bool = False) -> None:
    cmd = [_resolve_bin("ffmpeg"), "-hide_banner", "-y", *args]
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        log.info("[dry-run] %s", printable)
        print(printable)
        return
    log.info("running ffmpeg (%d args)", len(args))
    log.debug(printable)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.splitlines()[-40:])
        log.error("ffmpeg failed (exit %s):\n%s", proc.returncode, tail)
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")


# ------------------------------------------------------------------- encoder
# Hardware H.264 encoders to try, best first. Only the final encode moves to the
# GPU — the filtergraph (scale / xfade / zoompan / overlay) stays on the CPU.
_HW_ENCODERS = ("h264_nvenc",)


def _encoder_works(encoder: str) -> bool:
    """`ffmpeg -encoders` lists NVENC even with no NVIDIA GPU / driver, so do a
    tiny real encode instead."""
    try:
        proc = subprocess.run(
            [_resolve_bin("ffmpeg"), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.2",
             "-c:v", encoder, "-f", "null", "-"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@lru_cache(maxsize=None)
def detect_encoder() -> str:
    """First working hardware encoder, else libx264. Probed once per process."""
    for enc in _HW_ENCODERS:
        if _encoder_works(enc):
            log.info("encoder: using %s (GPU)", enc)
            return enc
    log.info("encoder: no GPU encoder available, using libx264 (CPU)")
    return "libx264"


def resolve_encoder(render: dict) -> str:
    """render.encoder: 'auto' (default — GPU if one works) or an explicit name."""
    enc = str(render.get("encoder", "auto"))
    return detect_encoder() if enc == "auto" else enc


def video_encoder_args(render: dict, encoder: str) -> list[str]:
    """Output video codec args. Quality: libx264 uses render.crf; NVENC uses
    constant-quality VBR at render.nvenc_cq (default crf + 2, which lands at
    about the same visual quality)."""
    common = ["-profile:v", "high", "-pix_fmt", "yuv420p"]
    if encoder == "h264_nvenc":
        cq = render.get("nvenc_cq", int(render.get("crf", 19)) + 2)
        return ["-c:v", "h264_nvenc", *common, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    return ["-c:v", encoder, *common,
            "-preset", str(render.get("preset", "medium")),
            "-crf", str(render.get("crf", 19))]
