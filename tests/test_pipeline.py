import pytest

from social_peace.config import Config
from social_peace.fetch.common import promote_incoming
from social_peace.pipeline.auto import run_pipeline
from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media

_cfg = Config.load()
_have_assets = bool(
    _list_media(_cfg.path("video_assets"), VIDEO_EXTS)
    and _list_media(_cfg.path("audio_assets"), AUDIO_EXTS)
)


def test_promote_incoming_moves_and_skips_collisions(tmp_path):
    vid = tmp_path / "video"
    inc = vid / "_incoming"
    inc.mkdir(parents=True)
    (inc / "a.mp4").write_text("x")
    (inc / "b.mp4").write_text("y")
    (inc / "c.mp4.part").write_text("partial")
    (vid / "b.mp4").write_text("already here")

    moved = promote_incoming(vid)

    assert {p.name for p in moved} == {"a.mp4"}
    assert (vid / "a.mp4").is_file()
    assert (inc / "b.mp4").is_file()          # collision left in place
    assert (inc / "c.mp4.part").is_file()     # .part ignored


@pytest.mark.skipif(not _have_assets, reason="needs a local assets/video + assets/audio library")
def test_run_pipeline_dry_run_builds_batch_of_pending(tmp_path, monkeypatch):
    cfg = Config.load()
    out = tmp_path / "output"
    logs = tmp_path / "logs"
    out.mkdir()
    logs.mkdir()
    real_path = cfg.path
    monkeypatch.setattr(
        cfg, "path",
        lambda k: {"output": out, "logs": logs}.get(k) or real_path(k),
    )

    summary = run_pipeline(cfg, batch=2, seed=999, do_fetch=False, dry_run=True)

    assert summary["ok"] is True
    assert len(summary["built"]) == 2
    assert summary["fetched"] == 0
    # dry-run renders nothing and writes no sidecars
    assert list(out.glob("*.mp4")) == []
