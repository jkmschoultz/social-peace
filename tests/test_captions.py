import json
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.metadata import build_metadata, mark_status, set_review
from social_peace.pipeline.selectors import AudioBed, Clip, Selection


def _sel(seed=42):
    return Selection(
        template={"name": "warm-dawn"},
        clips=[Clip(Path("pexels-5848352-nature.mp4"), 30.0), Clip(Path("unknown.mp4"), 30.0)],
        audio=[AudioBed(Path("bed.mp3"), 90.0)],
        text="Unclench your jaw. Drop your shoulders.",
        footer="",
        target_duration=42.0,
        segment_duration=21.0,
        seed=seed,
    )


def test_metadata_has_all_platform_blocks():
    cfg = Config.load()
    md = build_metadata(cfg, _sel(), Path("/tmp/out/20260101-000000_warm-dawn_42.mp4"))
    for plat in ("youtube", "tiktok", "instagram"):
        assert plat in md["platforms"]
    assert md["review"]["state"] == "pending"
    assert md["status"] == {"youtube": "pending", "instagram": "pending", "tiktok": "skipped"}
    assert md["platforms"]["tiktok"]["caption"]
    assert md["platforms"]["instagram"]["caption"]


def test_hook_substituted_and_deterministic():
    cfg = Config.load()
    a = build_metadata(cfg, _sel(7), Path("/tmp/o/x_warm-dawn_7.mp4"))
    b = build_metadata(cfg, _sel(7), Path("/tmp/o/x_warm-dawn_7.mp4"))
    assert a["platforms"]["tiktok"] == b["platforms"]["tiktok"]
    # {hook} = overlay text minus trailing punctuation
    joined = a["platforms"]["tiktok"]["caption"] + a["platforms"]["youtube"]["title"]
    assert "Unclench your jaw. Drop your shoulders" in joined


def test_source_urls_from_manifest_or_none():
    cfg = Config.load()
    md = build_metadata(cfg, _sel(), Path("/tmp/o/x_warm-dawn_42.mp4"))
    vids = md["source_urls"]["video"]
    assert set(vids) == {"pexels-5848352-nature.mp4", "unknown.mp4"}
    assert vids["unknown.mp4"] is None  # not in manifest


def test_set_review_and_mark_status(tmp_path):
    cfg = Config.load()
    md = build_metadata(cfg, _sel(), Path("/tmp/o/vid_warm-dawn_42.mp4"))
    side = tmp_path / "vid_warm-dawn_42.json"
    side.write_text(json.dumps(md), encoding="utf-8")

    set_review(side, "approved", note="looks good",
               captions={"tiktok": {"caption": "edited caption"}})
    out = json.loads(side.read_text(encoding="utf-8"))
    assert out["review"]["state"] == "approved"
    assert out["review"]["note"] == "looks good"
    assert out["review"]["decided_utc"]
    assert out["platforms"]["tiktok"]["caption"] == "edited caption"

    mark_status(side, "youtube", "uploaded")
    assert json.loads(side.read_text(encoding="utf-8"))["status"]["youtube"] == "uploaded"


def _template_cfg(monkeypatch):
    cfg = Config.load()
    monkeypatch.setitem(cfg.raw["captions"], "enabled", False)  # template path, no API
    return cfg


def test_instagram_gets_youtube_hashtags_minus_shorts(monkeypatch):
    cfg = _template_cfg(monkeypatch)
    md = build_metadata(cfg, _sel(3), Path("/tmp/o/x_warm-dawn_3.mp4"))
    tags = [t for t in cfg.raw["metadata"]["hashtags"] if t.lower() != "#shorts"]
    ig = md["platforms"]["instagram"]
    assert ig["caption"].endswith("\n\n" + " ".join(tags))
    assert "#shorts" not in ig["caption"] and ig["hashtags"] == tags
    assert md["platforms"]["youtube"]["description"].endswith(" ".join(cfg.raw["metadata"]["hashtags"]))

    monkeypatch.setitem(cfg.raw["metadata"]["instagram"], "hashtags", ["#own"])
    md = build_metadata(cfg, _sel(3), Path("/tmp/o/x_warm-dawn_3.mp4"))
    assert md["platforms"]["instagram"]["caption"].endswith("\n\n#own")

