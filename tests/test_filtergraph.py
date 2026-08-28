from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline.assemble import build_filtergraph
from social_peace.pipeline.selectors import AudioBed, Clip, Selection


def _sel(template, n_clips, n_audio, seg=15.0):
    return Selection(
        template=template,
        clips=[Clip(Path(f"c{i}.mp4"), 60.0) for i in range(n_clips)],
        audio=[AudioBed(Path(f"a{i}.mp3"), 120.0) for i in range(n_audio)],
        text="Take 60 seconds. Breathe.",
        footer="",
        target_duration=seg * n_clips,
        segment_duration=seg,
        seed=42,
    )


def _tpl(**over):
    base = {
        "name": "t",
        "transition": "fade",
        "transition_duration": 1.0,
        "motion": "none",
        "color": {"eq": "contrast=1.03", "extra": ""},
        "audio": {"stems": 1},
    }
    base.update(over)
    return base


def test_fade_chain_multiclip():
    cfg = Config.load()
    fg, total = build_filtergraph(cfg, _sel(_tpl(), 3, 2), overlay_idx=5)
    assert "xfade=transition=fade" in fg
    assert fg.count("xfade") == 2
    assert "amix=inputs=2" in fg
    assert "[vout]" in fg and "[aout]" in fg
    assert abs(total - (3 * 15.0 - 2 * 1.0)) < 1e-6


def test_single_clip_no_concat_no_xfade():
    cfg = Config.load()
    fg, total = build_filtergraph(cfg, _sel(_tpl(transition="none"), 1, 1), overlay_idx=2)
    assert "concat=" not in fg
    assert "xfade" not in fg
    assert "[v0][ov]overlay" in fg
    assert abs(total - 15.0) < 1e-6


def test_concat_hardcut():
    cfg = Config.load()
    fg, _ = build_filtergraph(cfg, _sel(_tpl(transition="none"), 2, 1), overlay_idx=3)
    assert "concat=n=2:v=1:a=0" in fg
    assert "xfade" not in fg
