"""Posting schedule: slot maths, queue order, publish_due ticks, partial-failure
retries, and the adaptive pipeline batch. Publishers are stubbed — no network."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from social_peace.config import Config
from social_peace.publish import runner, schedule
from social_peace.publish.base import PublishResult

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = Config.load()
    out, logs = tmp_path / "output", tmp_path / "logs"
    out.mkdir()
    logs.mkdir()
    monkeypatch.setattr(cfg, "path", lambda k: {"output": out, "logs": logs}[k])
    monkeypatch.setitem(cfg.raw["project"], "timezone", "Europe/Berlin")
    monkeypatch.setitem(cfg.raw["project"], "target_platforms", ["youtube", "instagram"])
    monkeypatch.setitem(cfg.raw, "schedule", {
        "enabled": True, "slots": ["08:00", "12:30", "19:00"], "per_slot": 1,
        "grace_minutes": 90, "max_attempts": 2, "low_queue_days": 1.0,
    })
    monkeypatch.setitem(cfg.raw["pipeline"], "target_queue_days", 2)
    monkeypatch.setitem(cfg.raw["pipeline"], "max_batch", 12)
    return cfg


def _render(cfg, stem, state="approved", **extra):
    out = cfg.path("output")
    md = {"id": stem, "review": {"state": state}, "platforms": {}, "status": {}, **extra}
    (out / f"{stem}.json").write_text(json.dumps(md), encoding="utf-8")
    (out / f"{stem}.mp4").write_bytes(b"x")
    return out / f"{stem}.json"


@pytest.fixture
def posts(monkeypatch):
    """Stub publish_one; `fail` holds platforms that should error."""
    calls, fail = [], set()

    def fake(cfg, video_path, platform):
        calls.append((video_path.stem, platform))
        if platform in fail:
            return PublishResult(platform, ok=False, status="error", error="boom")
        return PublishResult(platform, ok=True, status="uploaded",
                             url=f"https://x/{video_path.stem}", remote_id="r")

    monkeypatch.setattr(runner, "publish_one", fake)
    return calls, fail


def at(h, m=0, day=25):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def test_slot_maths(cfg):
    assert schedule.latest_slot(cfg, at(7, 0)) == at(19, 0, day=24)
    assert schedule.latest_slot(cfg, at(12, 30)) == at(12, 30)
    assert schedule.next_slots(cfg, at(18, 0), 3) == [at(19), at(8, day=26), at(12, 30, day=26)]
    assert schedule.posts_per_day(cfg) == 3


def test_queue_order_and_move(cfg):
    _render(cfg, "20260101-a")
    _render(cfg, "20260102-b")
    _render(cfg, "20260103-c")
    _render(cfg, "20260104-p", state="pending")
    _render(cfg, "20260105-live", status={"youtube": "uploaded"})
    assert [s.stem for s in schedule.queue(cfg)] == ["20260101-a", "20260102-b", "20260103-c"]

    assert schedule.move(cfg, "20260103-c", "top") == ["20260103-c", "20260101-a", "20260102-b"]
    assert schedule.move(cfg, "20260101-a", "down") == ["20260103-c", "20260102-b", "20260101-a"]
    # a newly approved render joins at the back of a reordered queue
    _render(cfg, "20260100-new")
    assert schedule.queue(cfg)[-1].stem == "20260100-new"
    with pytest.raises(ValueError):
        schedule.move(cfg, "20260104-p", "up")


def test_publish_due_fires_once_per_slot(cfg, posts):
    calls, _ = posts
    _render(cfg, "20260101-a")
    _render(cfg, "20260102-b")

    # the first ever tick serves the latest slot (yesterday 19:00 is past grace)
    res = schedule.publish_due(cfg, now=at(7, 0))
    assert not res["due"] and res["reason"] == "missed (past grace)"
    assert calls == []

    res = schedule.publish_due(cfg, now=at(8, 3))
    assert res["due"] and [r["id"] for r in res["posted"]] == ["20260101-a"]
    assert calls == [("20260101-a", "youtube"), ("20260101-a", "instagram")]

    assert not schedule.publish_due(cfg, now=at(8, 10))["due"]     # same slot, served
    res = schedule.publish_due(cfg, now=at(12, 31))
    assert [r["id"] for r in res["posted"]] == ["20260102-b"]
    res = schedule.publish_due(cfg, now=at(19, 0))
    assert res["due"] and res["posted"] == [] and res["reason"] == "queue empty"


def test_disabled_schedule_posts_nothing(cfg, posts, monkeypatch):
    monkeypatch.setitem(cfg.raw["schedule"], "enabled", False)
    _render(cfg, "20260101-a")
    assert schedule.publish_due(cfg, now=at(8, 1))["reason"] == "schedule disabled"
    assert posts[0] == []


def test_partial_failure_retried_then_given_up(cfg, posts):
    calls, fail = posts
    fail.add("instagram")
    _render(cfg, "20260101-a")
    _render(cfg, "20260102-b")
    _render(cfg, "20260103-c")

    schedule.publish_next(cfg)                       # a: youtube ok, instagram fails
    assert [s.stem for s, _ in schedule.partial(cfg)] == ["20260101-a"]
    assert [s.stem for s in schedule.queue(cfg)] == ["20260102-b", "20260103-c"]

    calls.clear()
    res = schedule.publish_next(cfg)                 # retry a's instagram + post b
    assert ("20260101-a", "instagram") in calls and ("20260101-a", "youtube") not in calls
    assert [r["id"] for r in res["posted"]] == ["20260102-b"]
    md = json.loads((cfg.path("output") / "20260101-a.json").read_text())
    assert md["schedule"]["attempts"]["instagram"] == 2
    assert md["schedule"]["errors"]["instagram"] == "boom"

    calls.clear()
    fail.clear()
    schedule.publish_next(cfg)                       # a is out of attempts; b's retry succeeds
    assert ("20260101-a", "instagram") not in calls
    assert ("20260102-b", "instagram") in calls


def test_failing_head_is_retried_not_skipped(cfg, posts):
    """If every platform fails (e.g. auth expired) the head stays at the front
    instead of burning through the queue — until max_attempts."""
    _, fail = posts
    fail.update({"youtube", "instagram"})
    _render(cfg, "20260101-a")
    _render(cfg, "20260102-b")
    schedule.publish_next(cfg)
    assert schedule.queue(cfg)[0].stem == "20260101-a"
    schedule.publish_next(cfg)
    assert [s.stem for s in schedule.queue(cfg)] == ["20260102-b"]


def test_iter_approved_skips_fully_published(cfg):
    _render(cfg, "20260101-done", status={"youtube": "uploaded", "instagram": "published"})
    _render(cfg, "20260102-half", status={"youtube": "uploaded"})
    _render(cfg, "20260103-new")
    assert [s.stem for s in runner.iter_approved(cfg)] == ["20260102-half", "20260103-new"]


def test_status_plan(cfg):
    for i in range(4):
        _render(cfg, f"2026010{i}-v")
    st = schedule.status(cfg, now=at(10, 0))
    assert st["next_slot"] == at(12, 30)
    assert st["plan"]["20260103-v"] == at(12, 30, day=26)
    assert st["runs_out"] == at(19, 0, day=26)
    assert st["queue_days"] == pytest.approx(4 / 3) and not st["low"]


def test_plan_batch_accounts_for_queue_pending_and_approval_rate(cfg):
    from social_peace import ledger
    from social_peace.pipeline.auto import plan_batch

    # target = 2 days * 3/day = 6; 2 queued, 1 pending; default rate 0.6
    _render(cfg, "20260101-a")
    _render(cfg, "20260102-b")
    _render(cfg, "20260103-p", state="pending")
    assert plan_batch(cfg)["batch"] == 6          # ceil(4 / 0.6) - 1

    for i in range(10):                           # all approved -> rate 1.0
        ledger.record(cfg.path("logs"), "review", video_id=f"v{i}", state="approved")
    assert plan_batch(cfg)["batch"] == 3          # 4 - 1

    for i in range(4):
        _render(cfg, f"2026020{i}-q")
    assert plan_batch(cfg)["batch"] == 0          # queue already covers the target
