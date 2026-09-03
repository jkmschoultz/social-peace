"""TikTok / Instagram publisher skeletons: importable, wired in, and fail
gracefully (a PublishResult, never an exception) when unconfigured. No network."""
import importlib

import pytest

from social_peace.publish import instagram, tiktok
from social_peace.publish.base import PublishResult
from social_peace.publish.runner import PUBLISHERS as _PUBLISHERS

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


def test_instagram_unconfigured_needs_token(tmp_path):
    res = instagram.publish(tmp_path / "v.mp4", {"platforms": {"instagram": {"caption": "hi"}}})
    assert isinstance(res, PublishResult)
    assert res.ok is False and res.status == "error"
    assert "INSTAGRAM_ACCESS_TOKEN" in res.error


class _Resp:
    def __init__(self, ok, payload=None, text="{}"):
        self.ok = ok
        self.status_code = 200 if ok else 400
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_instagram_bad_token_reports_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "garbage")
    monkeypatch.setattr(instagram.requests, "get",
                        lambda *a, **k: _Resp(False, text='{"error":{"code":190}}'))
    res = instagram.publish(tmp_path / "v.mp4", {"platforms": {"instagram": {"caption": "hi"}}})
    assert res.ok is False and "auth failed" in res.error


def test_instagram_instagram_login_token_detected(tmp_path, monkeypatch):
    # graph.instagram.com/me returns 200 -> Instagram-Login setup, no USER_ID needed
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "IGtok")
    monkeypatch.delenv("INSTAGRAM_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)  # stop before real publish
    monkeypatch.setattr(instagram.requests, "get",
                        lambda *a, **k: _Resp(True, {"user_id": "178414999", "username": "x"}))
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    res = instagram.publish(vid, {"platforms": {"instagram": {"caption": "hi"}}})
    # got past auth into the tunnel step
    assert res.ok is False and "cloudflared" in res.error


def test_instagram_missing_sidecar_block(tmp_path):
    res = instagram.publish(tmp_path / "v.mp4", {"platforms": {}})
    assert res.ok is False and "no instagram block" in res.error


def test_youtube_clean_strips_angle_brackets():
    from social_peace.publish.youtube import _clean
    assert _clean("a daily reset <3") == "a daily reset ‹3"
    assert "<" not in _clean("<a> <b>") and ">" not in _clean("<a> <b>")
    assert _clean(None) == ""


def test_publish_one_reports_already_published(tmp_path, monkeypatch):
    from social_peace.config import Config
    from social_peace.publish import runner

    cfg = Config.load()
    monkeypatch.setattr(cfg, "path", lambda k: tmp_path)
    monkeypatch.setattr(runner.ledger, "already_published", lambda *a, **k: True)

    res = runner.publish_one(cfg, tmp_path / "v.mp4", "youtube")
    assert res.ok and res.status == "already-published"
    assert res.status in runner.DONE_STATUSES
