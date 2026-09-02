"""captions.generate_platform_captions and its wiring into build_metadata.

No real network/API calls: a fake `anthropic` module is injected into
sys.modules so the lazy `import anthropic` inside the function picks it up.
"""
import sys
import types
from pathlib import Path

from social_peace.config import Config
from social_peace.pipeline import captions
from social_peace.pipeline.metadata import build_metadata
from social_peace.pipeline.selectors import AudioBed, Clip, Selection

_VALID_JSON = (
    '{"youtube_title": "A slow morning #shorts", '
    '"youtube_description": "Sit with the quiet for a minute.", '
    '"tiktok_caption": "just breathe for a second", '
    '"instagram_caption": "a small pause, for you"}'
)


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _install_fake_anthropic(monkeypatch, *, reply=None, raise_exc=None):
    fake = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = self

        def create(self, **kwargs):
            if raise_exc:
                raise raise_exc
            return _Resp(reply)

    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def _sel(seed=1):
    return Selection(
        template={"name": "warm-dawn"},
        clips=[Clip(Path("c.mp4"), 30.0)],
        audio=[AudioBed(Path("a.mp3"), 90.0)],
        text="Breathe.",
        footer="",
        target_duration=30.0,
        segment_duration=30.0,
        seed=seed,
    )


def test_disabled_returns_none(monkeypatch):
    cfg = Config.load()
    monkeypatch.setitem(cfg.raw, "captions", {"enabled": False})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    assert captions.generate_platform_captions(cfg, "hook", "warm-dawn") is None


def test_no_api_key_returns_none(monkeypatch):
    cfg = Config.load()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert captions.generate_platform_captions(cfg, "hook", "warm-dawn") is None


def test_valid_json_parsed(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply=_VALID_JSON)

    out = captions.generate_platform_captions(cfg, "Breathe", "warm-dawn")

    assert out == {
        "youtube_title": "A slow morning #shorts",
        "youtube_description": "Sit with the quiet for a minute.",
        "tiktok_caption": "just breathe for a second",
        "instagram_caption": "a small pause, for you",
    }


def test_fenced_json_stripped(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply=f"```json\n{_VALID_JSON}\n```")

    out = captions.generate_platform_captions(cfg, "Breathe", "warm-dawn")
    assert out["youtube_title"] == "A slow morning #shorts"


def test_malformed_json_returns_none_not_raise(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply="not json at all")
    assert captions.generate_platform_captions(cfg, "hook", "warm-dawn") is None


def test_missing_key_in_json_returns_none(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply='{"youtube_title": "only one field"}')
    assert captions.generate_platform_captions(cfg, "hook", "warm-dawn") is None


def test_api_error_returns_none_not_raise(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, raise_exc=RuntimeError("boom"))
    assert captions.generate_platform_captions(cfg, "hook", "warm-dawn") is None


def test_build_metadata_uses_generated_captions_and_appends_hashtags(monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(
        "social_peace.pipeline.metadata.generate_platform_captions",
        lambda cfg, hook, template: {
            "youtube_title": "Fresh title #shorts",
            "youtube_description": "Fresh description body.",
            "tiktok_caption": "fresh tiktok line",
            "instagram_caption": "fresh instagram line",
        },
    )
    md = build_metadata(cfg, _sel(), Path("/tmp/o/x_warm-dawn_1.mp4"))

    assert md["platforms"]["youtube"]["title"] == "Fresh title #shorts"
    assert md["platforms"]["youtube"]["description"].startswith("Fresh description body.")
    # configured hashtags still get appended after the generated body text
    for tag in cfg.raw["metadata"]["hashtags"]:
        assert tag in md["platforms"]["youtube"]["description"]
    assert md["platforms"]["tiktok"]["caption"].startswith("fresh tiktok line")
    for tag in cfg.raw["metadata"]["tiktok"]["hashtags"]:
        assert tag in md["platforms"]["tiktok"]["caption"]
    assert md["platforms"]["instagram"]["caption"].startswith("fresh instagram line")


def test_build_metadata_falls_back_when_generation_returns_none(monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(
        "social_peace.pipeline.metadata.generate_platform_captions",
        lambda cfg, hook, template: None,
    )
    md = build_metadata(cfg, _sel(), Path("/tmp/o/x_warm-dawn_1.mp4"))
    # falls back to the deterministic template path exercised in test_captions.py
    assert md["platforms"]["youtube"]["title"]
    assert md["platforms"]["tiktok"]["caption"]
