"""Encoder choice: GPU (NVENC) auto-detected by a test encode, libx264 otherwise,
and a failed hardware encode falls back to the CPU. No real ffmpeg runs."""
import pytest

from social_peace.pipeline import ffmpeg_utils as fu


@pytest.fixture(autouse=True)
def _fresh_cache():
    fu.detect_encoder.cache_clear()
    yield
    fu.detect_encoder.cache_clear()


def test_detect_prefers_working_gpu_and_caches(monkeypatch):
    probes = []
    monkeypatch.setattr(fu, "_encoder_works", lambda enc: probes.append(enc) or True)
    assert fu.detect_encoder() == "h264_nvenc"
    assert fu.detect_encoder() == "h264_nvenc"
    assert probes == ["h264_nvenc"]               # probed once per process


def test_detect_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(fu, "_encoder_works", lambda enc: False)
    assert fu.detect_encoder() == "libx264"


def test_resolve_respects_explicit_setting(monkeypatch):
    monkeypatch.setattr(fu, "_encoder_works", lambda enc: True)
    assert fu.resolve_encoder({}) == "h264_nvenc"
    assert fu.resolve_encoder({"encoder": "auto"}) == "h264_nvenc"
    assert fu.resolve_encoder({"encoder": "libx264"}) == "libx264"


def test_encoder_args():
    x264 = fu.video_encoder_args({"crf": 19, "preset": "slow"}, "libx264")
    assert x264[:2] == ["-c:v", "libx264"] and "-crf" in x264 and "slow" in x264
    nv = fu.video_encoder_args({"crf": 19}, "h264_nvenc")
    assert nv[:2] == ["-c:v", "h264_nvenc"] and nv[nv.index("-cq") + 1] == "21"
    assert "-crf" not in nv
    assert fu.video_encoder_args({"nvenc_cq": 24}, "h264_nvenc")[-3] == "24"


def test_build_retries_on_cpu_when_gpu_encode_fails(monkeypatch, tmp_path):
    from social_peace.pipeline import assemble

    runs = []

    def fake_run(args, dry_run=False, **kw):
        enc = args[args.index("-c:v") + 1]
        runs.append(enc)
        if enc == "h264_nvenc":
            raise RuntimeError("ffmpeg exited 1")

    class Stop(Exception):
        pass

    monkeypatch.setattr(assemble, "run_ffmpeg", fake_run)
    monkeypatch.setattr(assemble, "resolve_encoder", lambda render: "h264_nvenc")
    # stop right after the encode step
    monkeypatch.setattr(assemble, "build_metadata", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    from social_peace.config import Config
    from social_peace.pipeline.selectors import AUDIO_EXTS, VIDEO_EXTS, _list_media

    cfg = Config.load()
    if not (_list_media(cfg.path("video_assets"), VIDEO_EXTS)
            and _list_media(cfg.path("audio_assets"), AUDIO_EXTS)):
        pytest.skip("needs a local assets library")
    real_path = cfg.path
    monkeypatch.setattr(cfg, "path", lambda k: tmp_path if k == "output" else real_path(k))
    with pytest.raises(Stop):
        assemble.build_one(cfg, seed=7)
    assert runs == ["h264_nvenc", "libx264"]
