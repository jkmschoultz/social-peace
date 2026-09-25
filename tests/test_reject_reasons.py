"""Reject-with-reason: overused items go to the back of their queue, overlay text
rotates least-recently-used, "audio doesn't fit" re-rolls toward beds that suit
the clips and remembers the bad pairing. Fake library, no ffmpeg."""
import json
import random

import pytest

from social_peace.config import Config
from social_peace.pipeline import recency, selectors
from social_peace.pipeline.recency import Recency

CLIPS = ["pexels-1-ocean-waves.mp4", "pexels-2-ocean-sunset.mp4",
         "pexels-3-forest-light.mp4", "pexels-4-waterfall.mp4"]
BEDS = ["freesound-1-ocean-ambience.mp3", "freesound-2-forest-birds.mp3",
        "freesound-3-calm-mindful-piano.mp3"]
LINES = ["Breathe.", "Unclench your jaw.", "Look up for a moment.", "Drop your shoulders."]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = Config.load()
    paths = {k: tmp_path / k for k in ("output", "logs", "video_assets", "audio_assets")}
    for d in paths.values():
        d.mkdir()
    for n in CLIPS:
        (paths["video_assets"] / n).write_bytes(b"x")
    for n in BEDS:
        (paths["audio_assets"] / n).write_bytes(b"x")
    monkeypatch.setattr(cfg, "path", lambda k: paths[k])
    monkeypatch.setattr(cfg, "manifest", {})
    monkeypatch.setitem(cfg.raw, "overlay_text", list(LINES))
    monkeypatch.setattr(selectors, "ffprobe_duration", lambda p: 60.0)
    monkeypatch.setattr(selectors, "has_audio_stream", lambda p: True)
    return cfg


def _render(cfg, stem, created, *, text="Breathe.", video=(), audio=(), state="approved"):
    (cfg.path("output") / f"{stem}.json").write_text(json.dumps({
        "id": stem, "created_utc": created, "overlay_text": text, "seed": 5,
        "template": "cool-tide", "duration_seconds": 40,
        "review": {"state": state}, "sources": {"video": list(video), "audio": list(audio)},
    }), encoding="utf-8")
    return cfg.path("output") / f"{stem}.json"


def test_text_rotates_least_recently_used(cfg):
    _render(cfg, "a", "2026-09-01T00:00:00+00:00", text="Breathe.")
    _render(cfg, "b", "2026-09-02T00:00:00+00:00", text="Unclench your jaw.")
    rec = Recency.load(cfg)
    picks = {rec.pick_text(random.Random(i), LINES) for i in range(20)}
    assert picks == {"Look up for a moment.", "Drop your shoulders."}   # never-used first

    _render(cfg, "c", "2026-09-03T00:00:00+00:00", text="Look up for a moment.")
    _render(cfg, "d", "2026-09-04T00:00:00+00:00", text="Drop your shoulders.")
    rec = Recency.load(cfg)
    assert {rec.pick_text(random.Random(i), LINES) for i in range(20)} == {"Breathe."}  # oldest


def test_rejected_renders_do_not_count_as_used(cfg):
    _render(cfg, "a", "2026-09-01T00:00:00+00:00", text="Breathe.", state="rejected")
    assert "Breathe." not in Recency.load(cfg).touches["text"]


def test_demoted_item_waits_until_the_rest_of_the_pool_is_used(cfg):
    recency.demote(cfg, "video", [CLIPS[0]])
    assert Recency.load(cfg).behind("video", CLIPS) == {CLIPS[0]}
    for i, c in enumerate(CLIPS[1:3]):
        _render(cfg, f"r{i}", "2999-01-01T00:00:00+00:00", video=[c])
    assert Recency.load(cfg).behind("video", CLIPS) == {CLIPS[0]}      # CLIPS[3] still unused
    _render(cfg, "r9", "2999-01-01T00:00:00+00:00", video=[CLIPS[3]])
    assert Recency.load(cfg).behind("video", CLIPS) == set()           # its turn again


def test_selection_skips_a_demoted_clip(cfg):
    recency.demote(cfg, "video", CLIPS[:3])
    sel = selectors.build_selection(cfg, seed=1, template_name="still-forest")
    assert [c.path.name for c in sel.clips][0] == CLIPS[3]


def test_audio_reroll_suits_the_fixed_clips_and_skips_mismatches(cfg):
    ocean = [CLIPS[0], CLIPS[1]]
    sel = selectors.build_selection(cfg, seed=3, template_name="still-forest",
                                    pin={"video": ocean, "text": "Breathe."},
                                    used_audio={BEDS[2]})
    assert sel.audio[0].path.name == BEDS[0]              # ocean bed for ocean clips

    recency.record_mismatch(cfg, [BEDS[0]], ocean)
    sel = selectors.build_selection(cfg, seed=3, template_name="still-forest",
                                    pin={"video": ocean, "text": "Breathe."},
                                    used_audio={BEDS[2]})
    assert sel.audio[0].path.name != BEDS[0]
    # and the free pick keeps that bed off those clips
    sel = selectors.build_selection(cfg, seed=3, template_name="still-forest",
                                    pin={"audio": [BEDS[0]], "text": "Breathe."})
    assert not set(ocean) & {c.path.name for c in sel.clips}


@pytest.mark.parametrize("reason,kind,expect", [
    ("text-overused", "text", ["Breathe."]),
    ("video-overused", "video", CLIPS[:2]),
    ("audio-overused", "audio", BEDS[:1]),
])
def test_reject_for_demotes_the_right_thing(cfg, reason, kind, expect):
    from social_peace.pipeline.variant import reject_for

    side = _render(cfg, "x", "2026-09-01T00:00:00+00:00", video=CLIPS[:2], audio=BEDS[:1])
    md = reject_for(cfg, side, reason, note="meh")
    assert md["review"]["state"] == "rejected" and md["review"]["reasons"] == [reason]
    assert md["review"]["note"].endswith("meh")
    stored = json.loads((cfg.path("logs") / "demoted.json").read_text())
    assert sorted(stored[kind]) == sorted(expect)


def test_reject_endpoint_rejects_and_rerolls(client, monkeypatch):
    c, side, logs = client
    from social_peace.pipeline import variant
    seen = {}

    def fake_variant(cfg, md, change, **kw):
        seen["change"] = change
        return {"id": "new", "template": "t", "seed": 1, "sources": {}}

    monkeypatch.setattr(variant, "make_variant", fake_variant)
    md = json.loads(side.read_text())
    md["sources"] = {"video": ["a.mp4"], "audio": ["b.mp3"]}
    side.write_text(json.dumps(md))
    r = c.post("/api/reject/20260101-000000_warm-dawn_1", json={"reason": "audio-mismatch"})
    from conftest import _wait_job
    j = _wait_job(c, r.get_json()["job_id"])
    assert j["result"]["id"] == "new" and seen["change"] == ["audio"]
    md = json.loads(side.read_text())
    assert md["review"]["state"] == "rejected" and md["review"]["reasons"] == ["audio-mismatch"]
    assert json.loads((logs / "demoted.json").read_text())["mismatch"][0]["audio"] == "b.mp3"
    assert c.post("/api/reject/20260101-000000_warm-dawn_1", json={"reason": "bogus"}).status_code == 400
    assert "reject &amp; re-roll" not in c.get("/?filter=rejected").get_data(as_text=True)



def test_make_variant_rerolls_several_parts_at_once(cfg, monkeypatch):
    from social_peace.pipeline import variant
    seen = {}
    monkeypatch.setattr(variant, "build_one", lambda cfg, **kw: seen.update(kw) or {"id": "v"})
    orig = {"id": "o", "seed": 3, "template": "cool-tide", "duration_seconds": 40,
            "overlay_text": "Breathe.", "sources": {"video": CLIPS[:2], "audio": BEDS[:1]}}
    variant.make_variant(cfg, orig, ["audio", "text"])
    assert seen["pin"] == {"video": CLIPS[:2]}                   # only clips held fixed
    assert seen["used_audio"] == set(BEDS[:1]) and seen["exclude_text"] == "Breathe."
    assert seen["used_video"] is None
    assert seen["extra_meta"]["variant_change"] == "audio+text"
    assert variant.changes_for(["audio-mismatch", "audio-overused", "text-overused"]) == ["audio", "text"]
    with pytest.raises(ValueError):
        variant.make_variant(cfg, orig, [])


def _fake_variant(monkeypatch, seen):
    from social_peace.pipeline import variant

    def fake(cfg, md, change, **kw):
        seen["change"] = list(change)
        seen["prefer"] = kw.get("prefer")
        return {"id": "new", "template": "t", "seed": 1, "sources": {}}

    monkeypatch.setattr(variant, "make_variant", fake)


def _with_sources(side):
    md = json.loads(side.read_text())
    md["sources"] = {"video": ["a.mp4"], "audio": ["b.mp3"]}
    side.write_text(json.dumps(md))


def test_reject_with_several_ticks_queues_one_combined_reroll(client, monkeypatch):
    from conftest import _wait_job
    c, side, logs = client
    seen = {}
    _fake_variant(monkeypatch, seen)
    _with_sources(side)
    r = c.post(f"/api/reject/{side.stem}",
               json={"reasons": ["text-overused", "audio-mismatch"], "note": "meh"})
    _wait_job(c, r.get_json()["job_id"])
    assert seen["change"] == ["audio", "text"]
    md = json.loads(side.read_text())
    assert md["review"]["reasons"] == ["text-overused", "audio-mismatch"]
    assert md["review"]["note"] == "on-screen text used too recently; audio doesn't fit the clips. meh"
    stored = json.loads((logs / "demoted.json").read_text())
    assert list(stored["text"]) == ["Breathe."] and stored["mismatch"][0]["audio"] == "b.mp3"
    assert c.post(f"/api/reject/{side.stem}", json={"reasons": []}).status_code == 400


def test_reroll_uses_ticks_without_rejecting(client, monkeypatch):
    from conftest import _wait_job
    c, side, logs = client
    seen = {}
    _fake_variant(monkeypatch, seen)
    _with_sources(side)
    r = c.post(f"/api/variant/{side.stem}",
               json={"reasons": ["video-overused"], "prefer": "river"})
    _wait_job(c, r.get_json()["job_id"])
    assert seen == {"change": ["video"], "prefer": "river"}
    assert json.loads(side.read_text())["review"]["state"] == "pending"    # original kept
    assert list(json.loads((logs / "demoted.json").read_text())["video"]) == ["a.mp4"]
    assert c.post(f"/api/variant/{side.stem}", json={}).status_code == 400  # nothing ticked
    html = c.get("/").get_data(as_text=True)
    assert 'class="rj-box" value="audio-mismatch"' in html and "↻ Re-roll" in html
