import yaml

from social_peace.config import Config
from social_peace.fetch import freesound


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_HITS = {
    "results": [
        {"id": 111, "name": "rain long", "duration": 45.0,
         "username": "alice", "url": "https://freesound.org/s/111/",
         "previews": {"preview-hq-mp3": "https://cdn/111-hq.mp3"}},
        {"id": 222, "name": "too short", "duration": 5.0,
         "username": "bob", "url": "https://freesound.org/s/222/",
         "previews": {"preview-hq-mp3": "https://cdn/222-hq.mp3"}},
    ]
}


def test_search_filters_short_and_sends_cc0_filter(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Resp(_HITS)

    monkeypatch.setenv("FREESOUND_API_KEY", "tok")
    monkeypatch.setattr(freesound.requests, "get", fake_get)

    hits = freesound.search("rain", min_duration=20.0)

    assert seen["params"]["filter"] == 'license:"Creative Commons 0"'
    assert seen["params"]["token"] == "tok"
    assert [h["id"] for h in hits] == [111]  # 222 dropped for duration


def test_fetch_downloads_preview_and_writes_manifest(tmp_path, monkeypatch):
    cfg = Config.load()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    manifest = tmp_path / "manifest.yaml"
    real_path = cfg.path
    monkeypatch.setattr(cfg, "path", lambda k: audio_dir if k == "audio_assets" else real_path(k))
    monkeypatch.setitem(cfg.raw, "assets_manifest", str(manifest))
    monkeypatch.setattr(cfg, "root", tmp_path)

    monkeypatch.setenv("FREESOUND_API_KEY", "tok")
    monkeypatch.setattr(freesound.requests, "get", lambda *a, **k: _Resp(_HITS))

    calls = []

    def fake_download(url, dest, timeout=60):
        calls.append((url, dest.name))
        dest.write_bytes(b"fake mp3")
        return True

    monkeypatch.setattr(freesound, "download", fake_download)

    saved = freesound.fetch(cfg, "rain", limit=5)

    assert len(saved) == 1
    assert calls[0][0] == "https://cdn/111-hq.mp3"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = data["audio"]["freesound-111-rain.mp3"]
    assert entry["license"] == "CC0"
    assert entry["source"] == "Freesound — alice"


def test_no_key_raises(monkeypatch):
    monkeypatch.delenv("FREESOUND_API_KEY", raising=False)
    try:
        freesound.search("rain")
    except RuntimeError as e:
        assert "FREESOUND_API_KEY" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
