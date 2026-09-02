import json

import pytest

from social_peace.config import Config
from social_peace.review.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config.load()
    out = tmp_path / "output"
    logs = tmp_path / "logs"
    out.mkdir()
    logs.mkdir()
    monkeypatch.setitem(cfg.raw["paths"], "output", str(out))
    monkeypatch.setitem(cfg.raw["paths"], "logs", str(logs))
    # Config.path resolves against cfg.root; use absolute overrides instead.
    monkeypatch.setattr(cfg, "path", lambda k: {"output": out, "logs": logs}[k])

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


def test_index_and_filters(client):
    c, _, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/api/videos?filter=pending").get_json()[0]["id"].endswith("_1")
    assert c.get("/api/videos?filter=approved").get_json() == []


def test_decision_persists_state_captions_and_ledger(client):
    c, side, logs = client
    r = c.post("/api/decision/20260101-000000_warm-dawn_1", json={
        "state": "approved", "note": "nice",
        "captions": {"tiktok": {"caption": "new tt"}, "youtube": {"title": "new title"}},
    })
    assert r.status_code == 200
    md = json.loads(side.read_text(encoding="utf-8"))
    assert md["review"]["state"] == "approved"
    assert md["review"]["note"] == "nice"
    assert md["platforms"]["tiktok"]["caption"] == "new tt"
    assert md["platforms"]["youtube"]["title"] == "new title"
    ledger_lines = (logs / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(x)["event"] == "review" for x in ledger_lines)


def test_bad_state_rejected(client):
    c, _, _ = client
    assert c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "maybe"}).status_code == 400


def test_path_traversal_blocked(client):
    c, _, _ = client
    assert c.get("/media/..%2f..%2fetc%2fpasswd.mp4").status_code == 404
    assert c.post("/api/decision/..%2f..%2fx", json={"state": "approved"}).status_code == 404


def test_media_404_when_no_sidecar(client):
    c, _, _ = client
    assert c.get("/media/nonexistent.mp4").status_code == 404
