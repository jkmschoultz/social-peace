"""Shared fixtures for the review-app tests (see test_review_state.py,
test_caption_bank.py)."""
import json
import time

import pytest

from social_peace.config import Config
from social_peace.review.app import create_app


def _wait_job(client, job_id, tries=60):
    for _ in range(tries):
        j = client.get("/api/jobs/" + job_id).get_json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


@pytest.fixture(autouse=True)
def _idle_machine(monkeypatch):
    """Renders wait while the real PC is busy (pipeline.throttle); tests shouldn't."""
    from social_peace.pipeline import throttle
    monkeypatch.setattr(throttle, "cpu_percent", lambda interval=1.0: 0.0)
    monkeypatch.setattr(throttle, "free_memory_gb", lambda: 64.0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config.load()
    out = tmp_path / "output"
    logs = tmp_path / "logs"
    vids = tmp_path / "video"
    auds = tmp_path / "audio"
    for d in (out, logs, vids, auds):
        d.mkdir()
    monkeypatch.setitem(cfg.raw["paths"], "output", str(out))
    monkeypatch.setitem(cfg.raw["paths"], "logs", str(logs))
    # Config.path resolves against cfg.root; use absolute overrides instead.
    _paths = {"output": out, "logs": logs, "video_assets": vids, "audio_assets": auds}
    monkeypatch.setattr(cfg, "path", lambda k: _paths[k])
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setattr(cfg, "manifest", {})

    side = out / "20260101-000000_warm-dawn_1.json"
    side.write_text(json.dumps({
        "id": "20260101-000000_warm-dawn_1",
        "template": "warm-dawn", "seed": 1, "duration_seconds": 40,
        "overlay_text": "Breathe.",
        "source_urls": {"video": {"a.mp4": None}, "audio": {"b.mp3": None}},
        "review": {"state": "pending", "decided_utc": None, "note": ""},
        "platforms": {
            "youtube": {"title": "T", "description": "D", "tags": [], "categoryId": "22",
                        "privacyStatus": "private", "madeForKids": False},
            "tiktok": {"caption": "tt", "hashtags": [], "privacy": "SELF_ONLY"},
            "instagram": {"caption": "ig", "hashtags": []},
        },
        "status": {"youtube": "pending", "tiktok": "skipped", "instagram": "skipped"},
    }), encoding="utf-8")

    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), side, logs
