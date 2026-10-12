"""make_variant re-picks exactly one dimension. Uses dry_run so no ffmpeg."""
import glob
import json

import pytest

from social_peace.config import Config
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media
from social_peace.pipeline.variant import check_removable, make_variant, make_without

_cfg = Config.load()
_enough = (
    len(_list_media(_cfg.path("video_assets"), VIDEO_EXTS)) >= 6
    and len(_list_media(_cfg.path("audio_assets"), AUDIO_EXTS)) >= 3
)
pytestmark = pytest.mark.skipif(not _enough, reason="needs a local asset library")


def _a_sidecar():
    for f in sorted(glob.glob("output/*.json")):
        d = json.load(open(f))
        if d.get("review", {}).get("state") in ("approved", "pending") and d["sources"]["video"]:
            return d
    pytest.skip("no render to vary")


def test_invalid_change():
    with pytest.raises(ValueError):
        make_variant(_cfg, _a_sidecar(), "colour", dry_run=True)


def test_change_audio_keeps_clips_and_text():
    o = _a_sidecar()
    r = make_variant(Config.load(), o, "audio", dry_run=True)
    assert r["sources"]["video"] == o["sources"]["video"]
    assert r["overlay_text"] == o["overlay_text"]
    assert r["sources"]["audio"] != o["sources"]["audio"]
    assert r["variant_of"] == o["id"] and r["variant_change"] == "audio"


def test_change_text_keeps_clips_and_audio():
    o = _a_sidecar()
    r = make_variant(Config.load(), o, "text", dry_run=True)
    assert r["sources"]["video"] == o["sources"]["video"]
    assert r["sources"]["audio"] == o["sources"]["audio"]
    assert r["overlay_text"] != o["overlay_text"]


def test_change_video_keeps_audio_and_text():
    o = _a_sidecar()
    r = make_variant(Config.load(), o, "video", dry_run=True)
    assert r["sources"]["audio"] == o["sources"]["audio"]
    assert r["overlay_text"] == o["overlay_text"]


def test_prefer_biases_audio_reroll():
    """A 'rain' preference should pull the reroll onto a rain/river bed when one
    is available and not the original."""
    from social_peace.pipeline.selectors import AUDIO_EXTS, _list_media
    cfg = Config.load()
    beds = [p.name for p in _list_media(cfg.path("audio_assets"), AUDIO_EXTS)]
    rainish = [b for b in beds if "rain" in b or "river" in b]
    if len(rainish) < 2:
        pytest.skip("need a couple of rain/river beds to test the preference")
    o = _a_sidecar()
    r = make_variant(cfg, o, "audio", prefer="gentle rain", dry_run=True)
    assert any("rain" in n or "river" in n for n in r["sources"]["audio"])
    assert r["sources"]["audio"] != o["sources"]["audio"]  # still a fresh pick
    assert r["sources"]["video"] == o["sources"]["video"]   # video pinned
    assert r["variant_prefer"] == "gentle rain"


def _sidecar_with(kind):
    for f in sorted(glob.glob("output/*.json")):
        d = json.load(open(f))
        if len(d.get("sources", {}).get(kind, [])) >= 2:
            try:
                check_removable(_cfg, d, kind, d["sources"][kind][0])
            except ValueError:
                continue
            return d
    pytest.skip(f"no render with 2+ {kind} sources still in the library")


def test_check_removable_refuses_unknown_and_last():
    o = {"sources": {"video": ["a.mp4"], "audio": ["x.mp3", "y.mp3"]}}
    with pytest.raises(ValueError, match="not one of"):
        check_removable(_cfg, o, "audio", "z.mp3")
    with pytest.raises(ValueError, match="only video"):
        check_removable(_cfg, o, "video", "a.mp4")
    with pytest.raises(ValueError, match="kind"):
        check_removable(_cfg, o, "text", "x.mp3")


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_remove_one_source_keeps_everything_else(kind):
    from social_peace.pipeline.variant import _original_starts

    o = _sidecar_with(kind)
    gone = o["sources"][kind][-1]
    r = make_without(_cfg, o, kind, gone, dry_run=True)
    other = "video" if kind == "audio" else "audio"
    assert r["sources"][kind] == [n for n in o["sources"][kind] if n != gone]
    assert r["sources"][other] == o["sources"][other]
    assert r["overlay_text"] == o["overlay_text"]
    assert r["seed"] == o["seed"] and r["template"] == o["template"]
    assert r["variant_change"] == f"remove-{kind}" and r["variant_removed"] == gone
    # kept clips start where they did (or as near as the longer segment allows)
    before = _original_starts(_cfg, o)
    for clip, start in zip(r["sources"]["video"], r["clip_starts"]):
        assert start <= round(before[clip], 3) + 1e-6
        if kind == "audio":
            assert start == pytest.approx(before[clip], abs=0.01)
