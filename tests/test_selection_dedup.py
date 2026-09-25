"""Batch-aware selection: unused clips/audio are preferred so a run doesn't keep
reaching for the same asset."""
import pytest

from social_peace.config import Config
from social_peace.pipeline.selectors import (
    AUDIO_EXTS,
    VIDEO_EXTS,
    _list_media,
    _themed,
    build_selection,
)

_cfg = Config.load()
_pool_v = _list_media(_cfg.path("video_assets"), VIDEO_EXTS)
_pool_a = _list_media(_cfg.path("audio_assets"), AUDIO_EXTS)
_enough = len(_pool_v) >= 6 and len(_pool_a) >= 4

pytestmark = pytest.mark.skipif(not _enough, reason="needs a local asset library (>=6 clips, >=4 beds)")


@pytest.fixture(autouse=True)
def _no_review_history(monkeypatch):
    """These use the real asset library; keep the real review history (recent
    use, demotions, mismatches in logs/) out of the ranking under test."""
    from social_peace.pipeline import selectors
    from social_peace.pipeline.recency import KINDS, Recency
    monkeypatch.setattr(selectors.Recency, "load", classmethod(
        lambda cls, cfg: Recency({k: {} for k in KINDS}, {k: {} for k in KINDS}, [])))


def test_second_render_avoids_first_renders_assets():
    cfg = Config.load()
    s1 = build_selection(cfg, seed=101, template_name="warm-dawn")
    used_v = {c.path.name for c in s1.clips}
    used_a = {a.path.name for a in s1.audio}

    s2 = build_selection(
        cfg, seed=102, template_name="warm-dawn",
        used_video=used_v, used_audio=used_a,
    )
    assert used_v.isdisjoint({c.path.name for c in s2.clips})
    assert used_a.isdisjoint({a.path.name for a in s2.audio})


def test_prefers_the_lone_fresh_clip():
    from social_peace.pipeline.ffmpeg_utils import ffprobe_duration

    cfg = Config.load()
    # leave exactly one fresh clip, and make it a long one so it always clears
    # the per-segment duration gate
    longest = max(_pool_v, key=lambda p: ffprobe_duration(p))
    used_v = {p.name for p in _pool_v if p.name != longest.name}
    sel = build_selection(cfg, seed=7, template_name="warm-dawn", used_video=used_v)
    assert longest.name in [c.path.name for c in sel.clips]


def test_themed_expands_rain_to_water_scenes():
    ex = _themed({"rain"})
    assert {"water", "waterfall", "stream"} <= ex
    assert "rain" in ex


def test_soft_used_audio_is_a_low_priority_nudge():
    """With every-but-one bed marked soft-used, the fresh one is picked — but an
    explicit prefer still overrides soft-used."""
    cfg = Config.load()
    beds = [p.name for p in _list_media(cfg.path("audio_assets"), AUDIO_EXTS)]
    fresh = beds[-1]
    sel = build_selection(
        cfg, seed=3, template_name="cool-tide",  # 1 stem
        soft_used_audio=set(beds) - {fresh},
    )
    assert fresh in [a.path.name for a in sel.audio]

    # a preference beats the soft-used nudge
    rainish = [b for b in beds if "rain" in b or "river" in b]
    if rainish:
        sel2 = build_selection(
            cfg, seed=3, template_name="cool-tide",
            soft_used_audio=set(rainish),      # all rain beds "used elsewhere"
            prefer_audio="gentle rain",        # but I explicitly want rain
        )
        assert any("rain" in a.path.name or "river" in a.path.name for a in sel2.audio)


def test_favourite_audio_is_preferred():
    cfg = Config.load()
    beds = _list_media(cfg.path("audio_assets"), AUDIO_EXTS)
    target = beds[-1].name
    cfg.manifest = {"audio": {target: {"favourite": True}}}
    hits = sum(
        target in [a.path.name for a in build_selection(cfg, seed=s, template_name="warm-dawn").audio]
        for s in range(15)
    )
    assert hits >= 14  # favourites rank right after freshness → picked almost always
