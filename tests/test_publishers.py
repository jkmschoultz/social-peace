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
    assert res.ok is False
    assert res.status == "needs-login" and "access token" in res.error.lower()


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


# ------------------------------------------------------------------ tiktok, mocked
class _TTResp:
    def __init__(self, payload=None, status=200):
        self._payload, self.status_code = payload, status
        self.ok = status < 400
        self.content = b"x"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def _tt_ok(data):
    return _TTResp({"data": data, "error": {"code": "ok"}})


def test_tiktok_chunk_plan():
    MB = 1024 * 1024
    assert tiktok.chunk_plan(30 * MB) == (30 * MB, 1)
    assert tiktok.chunk_plan(64 * MB) == (64 * MB, 1)
    size, n = tiktok.chunk_plan(95 * MB)
    assert (size, n) == (10 * MB, 9)          # last chunk carries the 15 MB tail


def test_tiktok_parse_redirect():
    url = "https://example.com/cb?code=abc%2A123&scopes=video.publish&state=s1"
    assert tiktok.parse_redirect(url, "s1") == "abc*123"
    assert tiktok.parse_redirect("  rawcode ", "s1") == "rawcode"
    with pytest.raises(RuntimeError, match="state"):
        tiktok.parse_redirect(url, "other")
    with pytest.raises(RuntimeError, match="access_denied"):
        tiktok.parse_redirect("https://example.com/cb?error=access_denied&code=", "s1")


def test_tiktok_publish_flow_refreshes_expired_token(tmp_path, monkeypatch):
    import json
    import time

    tok = tmp_path / "tok.json"
    tok.write_text(json.dumps({"access_token": "old", "refresh_token": "r",
                               "expires_at": time.time() - 10}))
    monkeypatch.setattr(tiktok, "_TOKEN_FILE", tok)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setattr(tiktok.time, "sleep", lambda _s: None)

    calls = []

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append(url.split("tiktokapis.com")[1])
        if url.endswith("/oauth/token/"):
            return _TTResp({"access_token": "new", "refresh_token": "r2", "expires_in": 86400})
        assert headers["Authorization"] == "Bearer new"
        if url.endswith("/creator_info/query/"):
            return _tt_ok({"creator_username": "calm", "privacy_level_options": ["SELF_ONLY"],
                        "max_video_post_duration_sec": 600})
        if url.endswith("/video/init/"):
            body = json.loads(data)
            assert body["post_info"]["privacy_level"] == "SELF_ONLY"
            assert body["source_info"]["total_chunk_count"] == 1
            return _tt_ok({"publish_id": "p1", "upload_url": "https://up/1"})
        if url.endswith("/status/fetch/"):
            return _tt_ok({"status": "PUBLISH_COMPLETE"})
        raise AssertionError(url)

    puts = []
    monkeypatch.setattr(tiktok.requests, "post", fake_post)
    monkeypatch.setattr(tiktok.requests, "put",
                        lambda url, headers, data, timeout: puts.append(headers) or _TTResp({}))

    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x" * 2048)
    res = tiktok.publish(vid, {"duration_seconds": 40,
                               "platforms": {"tiktok": {"caption": "hi", "privacy": "SELF_ONLY"}}})

    assert res.ok, res.error
    assert res.remote_id == "p1" and res.url == "https://www.tiktok.com/@calm"
    assert calls[0] == "/v2/oauth/token/"
    assert puts[0]["Content-Range"] == "bytes 0-2047/2048"
    saved = json.loads(tok.read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r2" and "expires_at" in saved


def test_tiktok_rejects_disallowed_privacy(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "t")
    monkeypatch.setattr(tiktok.requests, "post", lambda *a, **k: _tt_ok(
        {"creator_username": "calm", "privacy_level_options": ["SELF_ONLY"]}))
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    res = tiktok.publish(vid, {"platforms": {"tiktok": {"caption": "hi", "privacy": "PUBLIC_TO_EVERYONE"}}})
    assert res.ok is False and "not allowed" in res.error


def _tt_app_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok, "_TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "https://example.com/callback.html")


def test_tiktok_start_and_finish_login(tmp_path, monkeypatch):
    import json
    from urllib.parse import parse_qs, urlparse

    _tt_app_env(tmp_path, monkeypatch)
    url = tiktok.start_login()
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback.html" in url
    state = parse_qs(urlparse(url).query)["state"][0]

    sent = {}

    def fake_post(url, data=None, **_k):
        sent.update(data)
        return _TTResp({"access_token": "a", "refresh_token": "r", "expires_in": 86400,
                        "scope": "user.info.basic,video.publish"})

    monkeypatch.setattr(tiktok.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="latest login"):
        tiktok.finish_login("https://example.com/callback.html?code=x&state=forged")

    data = tiktok.finish_login(f"https://example.com/callback.html?code=abc%2A1&state={state}")
    assert sent["code"] == "abc*1" and sent["grant_type"] == "authorization_code"
    assert data["access_token"] == "a"
    assert json.loads((tmp_path / "tok.json").read_text())["refresh_token"] == "r"
    with pytest.raises(RuntimeError, match="latest login"):  # a state is single-use
        tiktok.finish_login(f"https://example.com/callback.html?code=abc&state={state}")


def test_tiktok_revoked_refresh_token_reports_needs_login(tmp_path, monkeypatch):
    import json
    import time

    tok = tmp_path / "tok.json"
    tok.write_text(json.dumps({"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 10}))
    monkeypatch.setattr(tiktok, "_TOKEN_FILE", tok)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setattr(tiktok.requests, "post", lambda *a, **k: _TTResp(
        {"error": "invalid_grant", "error_description": "revoked"}, status=400))
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    res = tiktok.publish(vid, {"platforms": {"tiktok": {"caption": "hi"}}})
    assert res.status == "needs-login" and "revoked" in res.error
