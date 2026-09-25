"""Keep rendering from overloading the machine.

`render_slot(cfg)` wraps each ffmpeg render:
  1. one render at a time machine-wide — a file lock shared by the review server
     (every Generate / Fill queue / re-roll job), the nightly timer and the CLI,
     so extra jobs queue instead of running ffmpeg side by side;
  2. then wait while the machine is busy — overall CPU above
     render.throttle.max_cpu_percent or free memory below min_free_memory_gb —
     re-checking every check_seconds.

While waiting it reports through a per-thread status hook, so the review UI's
job strip can show "queued" / "waiting: CPU 96% busy".
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

from social_peace.config import Config

log = logging.getLogger(__name__)

_DEFAULTS = {
    "max_cpu_percent": 85,
    "min_free_memory_gb": 4,
    "check_seconds": 10,
    "max_wait_minutes": 0,        # 0 = wait as long as it takes
}

_local = threading.local()


def set_status_hook(fn) -> None:
    """Called with a short status string (or None when the render starts) from
    the current thread while it waits for a render slot."""
    _local.hook = fn


def _status(msg: str | None) -> None:
    hook = getattr(_local, "hook", None)
    if hook:
        try:
            hook(msg)
        except Exception:  # noqa: BLE001
            pass


def settings(cfg: Config) -> dict:
    return {**_DEFAULTS, **(cfg.render.get("throttle") or {})}


def _cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            vals = [int(v) for v in fh.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)       # idle + iowait
    return sum(vals), idle


def cpu_percent(interval: float = 1.0) -> float | None:
    """Whole-machine CPU busy % over `interval` seconds (Linux); None elsewhere."""
    a = _cpu_times()
    if a is None:
        return None
    time.sleep(interval)
    b = _cpu_times()
    total, idle = b[0] - a[0], b[1] - a[1]
    return 100.0 * (1 - idle / total) if total > 0 else 0.0


def free_memory_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError):
        pass
    return None


def busy_reason(cfg: Config) -> str | None:
    """Why rendering should wait right now, or None if the machine has room."""
    s = settings(cfg)
    cpu = cpu_percent()
    if cpu is not None and cpu > float(s["max_cpu_percent"]):
        return f"CPU {cpu:.0f}% busy"
    mem = free_memory_gb()
    if mem is not None and mem < float(s["min_free_memory_gb"]):
        return f"only {mem:.1f} GB memory free"
    return None


@contextlib.contextmanager
def _machine_lock(cfg: Config):
    try:
        import fcntl
    except ImportError:          # Windows: no cross-process lock, in-process only
        with _FALLBACK:
            yield
        return
    p = cfg.path("logs") / "render.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _status("queued behind another render")
            log.info("render: queued behind another render")
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_FALLBACK = threading.Lock()


@contextlib.contextmanager
def render_slot(cfg: Config):
    s = settings(cfg)
    with _machine_lock(cfg):
        deadline = time.time() + float(s["max_wait_minutes"]) * 60 if s["max_wait_minutes"] else None
        while (why := busy_reason(cfg)) is not None:
            if deadline and time.time() > deadline:
                log.warning("render: still busy (%s) after %s min, rendering anyway",
                            why, s["max_wait_minutes"])
                break
            _status(f"waiting: {why}")
            log.info("render: waiting — %s", why)
            time.sleep(float(s["check_seconds"]))
        _status(None)
        yield
