"""TikTok / Instagram publisher skeletons: importable, wired in, and fail
gracefully (a PublishResult, never an exception) when unconfigured. No network."""
import importlib

import pytest

from social_peace.cli import _PUBLISHERS
from social_peace.publish import instagram, tiktok
from social_peace.publish.base import PublishResult

_TIKTOK_ENV = ("TIKTOK_ACCESS_TOKEN", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN")
_IG_ENV = ("INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_PUBLIC_BASE_URL")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch, tmp_path):
    for k in (*_TIKTOK_ENV, *_IG_ENV):
        monkeypatch.delenv(k, raising=False)
    # point the tiktok token cache at a path that does not exist
    monkeypatch.setattr(tiktok, "_TOKEN_FILE", tmp_path / "nope.json")


def test_all_three_publishers_registered():
    assert set(_PUBLISHERS) == {"youtube", "tiktok", "instagram"}
    for dotted in _PUBLISHERS.values():
        mod = importlib.import_module(dotted)
        assert hasattr(mod, "publish")
        assert isinstance(getattr(mod, "platform"), str)


def test_tiktok_unconfigured_returns_error_result(tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x" * 1024)
    res = tiktok.publish(vid, {"platforms": {"tiktok": {"caption": "hi", "privacy": "SELF_ONLY"}}})
    assert isinstance(res, PublishResult)
    assert res.ok is False and res.status == "error"
    assert "access token" in res.error.lower()


def test_tiktok_missing_sidecar_block(tmp_path):
    res = tiktok.publish(tmp_path / "v.mp4", {"platforms": {}})
    assert res.ok is False and "no tiktok block" in res.error


def test_instagram_unconfigured_lists_missing_vars(tmp_path):
    res = instagram.publish(tmp_path / "v.mp4", {"platforms": {"instagram": {"caption": "hi"}}})
    assert isinstance(res, PublishResult)
    assert res.ok is False and res.status == "error"
    assert "INSTAGRAM_USER_ID" in res.error


def test_instagram_missing_sidecar_block(tmp_path):
    res = instagram.publish(tmp_path / "v.mp4", {"platforms": {}})
    assert res.ok is False and "no instagram block" in res.error
