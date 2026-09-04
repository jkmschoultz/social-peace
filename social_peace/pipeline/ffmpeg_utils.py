"""Thin wrappers around the ffmpeg / ffprobe binaries."""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
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
