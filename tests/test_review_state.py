import json

import pytest
import yaml

from conftest import _wait_job  # shared helper; `client` fixture auto-resolves


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

    j = _wait_job(c, c.post("/api/publish/20260101-000000_warm-dawn_1").get_json()["job_id"])
    body = j["result"]
    assert j["state"] == "done"
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

    j = _wait_job(c, c.post("/api/publish/20260101-000000_warm-dawn_1",
                            json={"platform": "instagram"}).get_json()["job_id"])
    assert j["state"] == "done" and calls == ["instagram"]
    assert json.loads(side.read_text())["status"]["instagram"] == "uploaded"


def test_publish_endpoint_rejects_unknown_platform(client):
    c, _, _ = client
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})
    r = c.post("/api/publish/20260101-000000_warm-dawn_1", json={"platform": "myspace"})
    assert r.status_code == 400


def test_publish_endpoint_refuses_unapproved(client):
    c, side, _ = client
    side.with_suffix(".mp4").write_bytes(b"x")
    j = _wait_job(c, c.post("/api/publish/20260101-000000_warm-dawn_1").get_json()["job_id"])
    assert j["result"]["error"] == "not approved"


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


def test_generate_auto_runs_the_nightly_pipeline(client, monkeypatch):
    c, *_ = client
    from social_peace.pipeline import auto as amod
    seen = {}

    def fake(cfg, **kw):
        seen.update(kw)
        return {"built": [], "fetched": 0, "plan": {"batch": 0, "queued": 9, "pending": 0}, "ok": True}

    monkeypatch.setattr(amod, "run_pipeline", fake)
    j = _wait_job(c, c.post("/api/generate", json={"auto": True}).get_json()["job_id"])
    assert seen["batch"] is None and seen["do_fetch"] is True    # adaptive batch + top-up
    assert j["result"]["count"] == 0 and j["result"]["plan"]["queued"] == 9
    assert "Fill queue" in c.get("/").get_data(as_text=True)


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


def test_fetch_endpoint_passes_custom_query(client, monkeypatch):
    c, *_ = client
    seen = {}
    from social_peace.pipeline import auto as amod
    monkeypatch.setattr(amod, "fetch_video_library",
                        lambda cfg, **kw: seen.update(kw) or {"kind": "video", "fetched": 1, "promoted": 1, "pool_size": 9, "notes": []})
    _wait_job(c, c.post("/api/fetch/video", json={"query": "slow river"}).get_json()["job_id"])
    assert seen.get("query") == "slow river"


def test_asset_update_label_and_favourite(client, tmp_path):
    c, *_ = client
    (tmp_path / "audio" / "b.mp3").write_bytes(b"x" * 8)
    r = c.post("/api/asset/audio/b.mp3/update", json={"label": "rainy calm", "favourite": True})
    assert r.status_code == 200
    j = r.get_json()
    assert j["label"] == "rainy calm" and j["favourite"] is True
    man = yaml.safe_load((tmp_path / "assets" / "manifest.yaml").read_text())
    assert man["audio"]["b.mp3"]["favourite"] is True
    # clearing
    r2 = c.post("/api/asset/audio/b.mp3/update", json={"label": "", "favourite": False})
    j2 = r2.get_json()
    assert j2["label"] is None and j2["favourite"] is False


def test_asset_update_guards(client, tmp_path):
    c, *_ = client
    (tmp_path / "audio" / "x.mp3").write_bytes(b"x")
    assert c.post("/api/asset/audio/x.mp3/update", json={}).status_code == 400
    assert c.post("/api/asset/audio/missing.mp3/update", json={"favourite": True}).status_code == 404
    assert c.post("/api/asset/fonts/x/update", json={"favourite": True}).status_code == 400


def test_assets_favourite_filter(client, tmp_path):
    c, *_ = client
    (tmp_path / "video" / "fav.mp4").write_bytes(b"x")
    (tmp_path / "video" / "plain.mp4").write_bytes(b"y")
    c.post("/api/asset/video/fav.mp4/update", json={"favourite": True})
    html = c.get("/assets?filter=favourite").get_data(as_text=True)
    assert "fav.mp4" in html and "plain.mp4" not in html


def test_assets_sort_and_kind_only(client, tmp_path):
    c, *_ = client
    (tmp_path / "video" / "v1.mp4").write_bytes(b"x" * 100)
    (tmp_path / "audio" / "a1.mp3").write_bytes(b"y" * 10)
    for s in ("favourite", "name", "size", "duration", "newest", "bogus"):
        assert c.get("/assets?sort=" + s).status_code == 200
    vonly = c.get("/assets?filter=video-only").get_data(as_text=True)
    assert "v1.mp4" in vonly and "a1.mp3" not in vonly
    aonly = c.get("/assets?filter=audio-only").get_data(as_text=True)
    assert "a1.mp3" in aonly and "v1.mp4" not in aonly


def test_variant_endpoint_accepts_prefer(client, monkeypatch):
    c, *_ = client
    seen = {}
    from social_peace.pipeline import variant as vmod
    monkeypatch.setattr(
        vmod, "make_variant",
        lambda cfg, orig, change, **kw: seen.update(kw) or {
            "id": "X", "template": orig["template"], "seed": orig["seed"],
            "sources": {"video": [], "audio": []}, "duration": 5,
        },
    )
    j = _wait_job(c, c.post("/api/variant/20260101-000000_warm-dawn_1",
                            json={"change": "audio", "prefer": "river ambience"}).get_json()["job_id"])
    assert j["state"] == "done" and seen.get("prefer") == "river ambience"


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

    j = _wait_job(c, c.post("/api/publish-approved").get_json()["job_id"])
    reports = j["result"]["reports"]
    assert len(reports) == 1 and reports[0]["id"] == "20260101-000000_warm-dawn_1"
    assert reports[0]["results"][0]["ok"] is True


def test_jobs_list_endpoint(client):
    c, *_ = client
    import social_peace.review.app as appmod
    jid = appmod._start_job("test", lambda: {"ok": 1}, "a test job")
    _wait_job(c, jid)
    listing = c.get("/api/jobs").get_json()["jobs"]
    assert any(x["id"] == jid and x["label"] == "a test job" for x in listing)


def test_approved_tab_shows_queue_and_reorders(client, tmp_path):
    c, side, _ = client
    out = tmp_path / "output"
    c.post("/api/decision/20260101-000000_warm-dawn_1", json={"state": "approved"})
    stem2 = "20260102-000000_cool-tide_2"
    md = json.loads(side.read_text(encoding="utf-8"))
    md["id"] = stem2
    (out / f"{stem2}.json").write_text(json.dumps(md), encoding="utf-8")
    for s in ("20260101-000000_warm-dawn_1", stem2):
        (out / f"{s}.mp4").write_bytes(b"x")

    html = c.get("/?filter=approved").get_data(as_text=True)
    assert "next post" in html and "#1 in queue" in html and "#2 in queue" in html
    assert html.index("warm-dawn_1") < html.index("cool-tide_2")   # queue order

    r = c.post(f"/api/queue/{stem2}/move", json={"to": "top"})
    assert r.get_json()["queue"] == [stem2, "20260101-000000_warm-dawn_1"]
    assert c.get("/api/schedule").get_json()["queue"][0] == stem2
    assert c.post(f"/api/queue/{stem2}/move", json={"to": "sideways"}).status_code == 400
