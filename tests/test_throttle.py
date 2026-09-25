"""Render throttling: one render at a time machine-wide, and wait while the PC
is busy (CPU / memory), reporting the wait through the status hook."""
import threading
import time

import pytest

from social_peace.config import Config
from social_peace.pipeline import throttle


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "path", lambda k: tmp_path)
    monkeypatch.setitem(cfg.raw["render"], "throttle",
                        {"max_cpu_percent": 85, "min_free_memory_gb": 4, "check_seconds": 0.01})
    return cfg


def test_busy_reason(cfg, monkeypatch):
    assert throttle.busy_reason(cfg) is None                      # idle (conftest)
    monkeypatch.setattr(throttle, "cpu_percent", lambda interval=1.0: 97.0)
    assert throttle.busy_reason(cfg) == "CPU 97% busy"
    monkeypatch.setattr(throttle, "cpu_percent", lambda interval=1.0: 10.0)
    monkeypatch.setattr(throttle, "free_memory_gb", lambda: 1.5)
    assert throttle.busy_reason(cfg) == "only 1.5 GB memory free"


def test_render_waits_until_cpu_frees_up(cfg, monkeypatch):
    readings = iter([99.0, 95.0, 20.0])
    monkeypatch.setattr(throttle, "cpu_percent", lambda interval=1.0: next(readings))
    seen = []
    throttle.set_status_hook(seen.append)
    try:
        with throttle.render_slot(cfg):
            seen.append("rendering")
    finally:
        throttle.set_status_hook(None)
    assert seen == ["waiting: CPU 99% busy", "waiting: CPU 95% busy", None, "rendering"]


def test_max_wait_gives_up_and_renders(cfg, monkeypatch):
    monkeypatch.setitem(cfg.raw["render"]["throttle"], "max_wait_minutes", 0.0005)  # ~30 ms
    monkeypatch.setattr(throttle, "cpu_percent", lambda interval=1.0: 100.0)
    with throttle.render_slot(cfg):
        pass


def test_second_render_queues_behind_the_first(cfg):
    order, started = [], threading.Event()
    statuses = []

    def first():
        with throttle.render_slot(cfg):
            started.set()
            time.sleep(0.3)
            order.append("first done")

    def second():
        started.wait()
        throttle.set_status_hook(statuses.append)
        with throttle.render_slot(cfg):
            order.append("second start")

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert order == ["first done", "second start"]
    assert statuses[0] == "queued behind another render"


def test_ffmpeg_runs_niced(monkeypatch):
    from social_peace.pipeline import ffmpeg_utils as fu
    seen = {}

    class P:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(fu.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and P())
    fu.run_ffmpeg(["-i", "x"], nice=10)
    assert seen["cmd"][:3] == ["nice", "-n", "10"]
