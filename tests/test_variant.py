"""make_variant re-picks exactly one dimension. Uses dry_run so no ffmpeg."""
import glob
import json

import pytest

from social_peace.config import Config
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media
from social_peace.pipeline.variant import make_variant

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
    assert r["sources"]["video"] != o["sources"]["video"]
