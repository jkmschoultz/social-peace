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


def test_index_and_filters(client):
    c, _, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/api/videos?filter=pending").get_json()[0]["id"].endswith("_1")
    assert c.get("/api/videos?filter=approved").get_json() == []


def test_index_sort_param_accepted_and_falls_back(client):
    c, *_ = client
    for s in ("newest", "oldest", "seed", "template", "status", "bogus"):
        assert c.get("/?filter=all&sort=" + s).status_code == 200


def test_assets_filter_param(client, tmp_path):
    c, *_ = client
    (tmp_path / "video" / "u.mp4").write_bytes(b"x" * 16)
    for f in ("all", "unused", "used", "pending", "published", "bogus"):
        assert c.get("/assets?filter=" + f).status_code == 200
    assert "u.mp4" in c.get("/assets?filter=unused").get_data(as_text=True)
    assert "u.mp4" not in c.get("/assets?filter=used").get_data(as_text=True)


def test_prune_rejected_endpoint(client, tmp_path):
    c, *_ = client
    out = tmp_path / "output"
    stem = "20200101-000000_x_9"
    (out / f"{stem}.json").write_text(json.dumps({"id": stem, "review": {"state": "rejected", "decided_utc": None}}))
    (out / f"{stem}.mp4").write_bytes(b"x")
    r = c.post("/api/prune-rejected", json={"all": True})
    assert r.status_code == 200
    assert stem in r.get_json()["removed"]
    assert not (out / f"{stem}.mp4").exists()


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


def test_publish_endpoint_runs_available_platforms(client, monkeypatch):
    c, side, _ = client
    side.with_suffix(".mp4").write_bytes(b"x" * 32)
    # approve it first
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})

    from social_peace.publish import runner
    from social_peace.publish.base import PublishResult

    calls = []

    def fake_publish_one(cfg, video_path, platform):
        calls.append(platform)
        return PublishResult(platform, ok=True, status="uploaded", url=f"https://x/{platform}")

    monkeypatch.setattr(runner, "publish_one", fake_publish_one)

    r = c.post("/api/publish/20260101-000000_warm-dawn_1")
    body = r.get_json()
    assert r.status_code == 200
    assert calls == ["youtube", "instagram"]  # config target_platforms
    assert {res["platform"] for res in body["results"]} == {"youtube", "instagram"}
    assert all(res["ok"] and res["status"] == "uploaded" for res in body["results"])
    # status + per-platform url written back into the sidecar
    md = json.loads(side.read_text())
    assert md["status"]["youtube"] == "uploaded"
    assert md["published"]["youtube"]["url"] == "https://x/youtube"
    assert md["published"]["instagram"]["url"] == "https://x/instagram"


def test_publish_endpoint_single_platform(client, monkeypatch):
    c, side, _ = client
    side.with_suffix(".mp4").write_bytes(b"x" * 8)
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})

    from social_peace.publish import runner
    from social_peace.publish.base import PublishResult

    calls = []

    def fake(cfg, vp, p):
        calls.append(p)
        return PublishResult(p, ok=True, status="uploaded")

    monkeypatch.setattr(runner, "publish_one", fake)

    r = c.post("/api/publish/20260101-000000_warm-dawn_1", json={"platform": "instagram"})
    assert r.status_code == 200
    assert calls == ["instagram"]
    assert json.loads(side.read_text())["status"]["instagram"] == "uploaded"


def test_publish_endpoint_rejects_unknown_platform(client):
    c, _, _ = client
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})
    r = c.post("/api/publish/20260101-000000_warm-dawn_1", json={"platform": "myspace"})
    assert r.status_code == 400


def test_publish_endpoint_refuses_unapproved(client):
    c, side, _ = client
    side.with_suffix(".mp4").write_bytes(b"x")
    r = c.post("/api/publish/20260101-000000_warm-dawn_1")
    assert r.get_json()["error"] == "not approved"


def test_publish_endpoint_bad_stem_404(client):
    c, _, _ = client
    assert c.post("/api/publish/..%2f..%2fx").status_code == 404
    assert c.post("/api/publish/nope").status_code == 404


def test_variant_endpoint_runs_job(client, monkeypatch):
    c, side, _ = client
    from social_peace.pipeline import variant as vmod
    monkeypatch.setattr(
        vmod, "make_variant",
        lambda cfg, orig, change, **kw: {
            "id": "NEW_" + change, "template": orig["template"], "seed": orig["seed"],
            "sources": {"video": [], "audio": []}, "duration": 10,
        },
    )
    r = c.post("/api/variant/20260101-000000_warm-dawn_1", json={"change": "audio"})
    assert r.status_code == 200
    j = _wait_job(c, r.get_json()["job_id"])
    assert j["state"] == "done" and j["result"]["id"] == "NEW_audio"


def test_variant_endpoint_rejects_bad_change(client):
    c, *_ = client
    assert c.post("/api/variant/20260101-000000_warm-dawn_1", json={"change": "x"}).status_code == 400


def test_variant_endpoint_404_for_unknown(client):
    c, *_ = client
    assert c.post("/api/variant/nope", json={"change": "audio"}).status_code == 404


def test_generate_endpoint_runs_job(client, monkeypatch):
    c, *_ = client
    from social_peace.pipeline import auto as amod
    monkeypatch.setattr(amod, "run_pipeline", lambda cfg, **kw: {"built": ["a", "b"], "ok": True})
    r = c.post("/api/generate", json={})
    j = _wait_job(c, r.get_json()["job_id"])
    assert j["state"] == "done" and j["result"]["count"] == 2


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_fetch_endpoint_runs_job(client, monkeypatch, kind):
    c, *_ = client
    from social_peace.pipeline import auto as amod
    monkeypatch.setattr(amod, "fetch_video_library",
                        lambda cfg, **kw: {"kind": "video", "fetched": 8, "promoted": 8, "pool_size": 70, "notes": []})
    monkeypatch.setattr(amod, "fetch_audio_library",
                        lambda cfg, **kw: {"kind": "audio", "fetched": 4, "promoted": 4, "pool_size": 12, "notes": []})
    j = _wait_job(c, c.post("/api/fetch/" + kind).get_json()["job_id"])
    assert j["state"] == "done" and j["result"]["kind"] == kind and j["result"]["fetched"] > 0


def test_fetch_endpoint_bad_kind(client):
    c, *_ = client
    assert c.post("/api/fetch/fonts").status_code == 400


def test_assets_page_lists_files(client, tmp_path):
    c, *_ = client
    (tmp_path / "video" / "clip-one.mp4").write_bytes(b"x" * 2048)
    (tmp_path / "audio" / "bed-one.mp3").write_bytes(b"y" * 1024)
    html = c.get("/assets").get_data(as_text=True)
    assert c.get("/assets").status_code == 200
    assert "clip-one.mp4" in html and "bed-one.mp3" in html


def test_asset_delete_removes_file(client, tmp_path):
    c, *_ = client
    f = tmp_path / "audio" / "bed-two.mp3"
    f.write_bytes(b"z" * 512)
    r = c.post("/api/asset/audio/bed-two.mp3/delete")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert not f.exists()


def test_asset_delete_bad_kind_and_missing(client):
    c, *_ = client
    assert c.post("/api/asset/fonts/x.ttf/delete").status_code == 400
    assert c.post("/api/asset/audio/nope.mp3/delete").status_code == 404
    assert c.post("/api/asset/audio/..%2fx/delete").status_code == 404


def test_asset_media_serves_and_guards(client, tmp_path):
    c, *_ = client
    (tmp_path / "video" / "vv.mp4").write_bytes(b"0123456789" * 20)
    assert c.get("/asset-media/video/vv.mp4").status_code == 200
    assert c.get("/asset-media/video/missing.mp4").status_code == 404
    assert c.get("/asset-media/fonts/x").status_code == 404


def test_publish_all_approved_endpoint(client, monkeypatch):
    c, side, _ = client
    side.with_suffix(".mp4").write_bytes(b"x" * 16)
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})

    from social_peace.publish import runner
    from social_peace.publish.base import PublishResult
    monkeypatch.setattr(runner, "publish_one",
                        lambda cfg, vp, p: PublishResult(p, ok=True, status="uploaded"))

    r = c.post("/api/publish-approved")
    reports = r.get_json()["reports"]
    assert len(reports) == 1 and reports[0]["id"] == "20260101-000000_warm-dawn_1"
    assert reports[0]["results"][0]["ok"] is True
