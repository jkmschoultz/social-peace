"""Automatic library top-up: rotating search terms, need-based fetch sizing, and
fetchers skipping stock ids the library already has (paging past them)."""
import json

import pytest

from social_peace.config import Config
from social_peace.fetch import pexels, pixabay
from social_peace.pipeline import auto


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = Config.load()
    paths = {k: tmp_path / k for k in ("output", "logs", "video_assets", "audio_assets")}
    for d in paths.values():
        d.mkdir()
    monkeypatch.setattr(cfg, "path", lambda k: paths[k])
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setitem(cfg.raw, "assets_manifest", str(tmp_path / "manifest.yaml"))
    monkeypatch.setitem(cfg.raw["pipeline"], "queries", {"video": ["a", "b", "c"], "audio": ["x", "y"]})
    monkeypatch.setitem(cfg.raw["pipeline"], "fetch_sources", ["pexels", "pixabay"])
    monkeypatch.setitem(cfg.raw["pipeline"], "min_unused_clips", 8)
    monkeypatch.setitem(cfg.raw["pipeline"], "min_unused_audio", 4)
    return cfg


def test_queries_rotate_across_calls(cfg):
    assert auto.next_queries(cfg, "video", 2) == ["a", "b"]
    assert auto.next_queries(cfg, "video", 2) == ["c", "a"]
    assert auto.next_queries(cfg, "audio", 1) == ["x"]
    assert auto.next_queries(cfg, "video", 1) == ["b"]


def test_fetch_video_library_gives_each_source_the_next_term(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(auto, "_fetch_video", lambda c, src, q, n: seen.append((src, q)) or [])
    auto.fetch_video_library(cfg)
    auto.fetch_video_library(cfg)
    assert seen == [("pexels", "a"), ("pixabay", "b"), ("pexels", "c"), ("pixabay", "a")]
    seen.clear()
    auto.fetch_video_library(cfg, query="fog")          # custom query doesn't rotate
    assert seen == [("pexels", "fog"), ("pixabay", "fog")]
    auto.fetch_video_library(cfg)
    assert seen[-2:] == [("pexels", "b"), ("pixabay", "c")]


def _touch(d, *names):
    for n in names:
        (d / n).write_bytes(b"x")


def test_fetch_plan_counts_only_unused_assets(cfg):
    vids = [f"v{i}.mp4" for i in range(10)]
    _touch(cfg.path("video_assets"), *vids)
    _touch(cfg.path("audio_assets"), "a1.mp3", "a2.mp3", "a3.mp3", "a4.mp3", "a5.mp3")
    assert auto.fetch_plan(cfg, 2) == {"video": 0, "audio": 0}   # 10 free clips >= 8

    def render(stem, state, v, a):
        (cfg.path("output") / f"{stem}.json").write_text(json.dumps(
            {"id": stem, "review": {"state": state}, "sources": {"video": v, "audio": a}}))

    render("r1", "approved", vids[:3], ["a1.mp3", "a2.mp3"])
    render("r2", "rejected", vids[3:6], ["a3.mp3"])              # rejected frees its assets
    assert auto.unused_assets(cfg) == {"video": 7, "audio": 3}
    assert auto.fetch_plan(cfg, 2) == {"video": 1, "audio": 1}
    # a big batch wants ~2.5 clips / ~1.5 beds per render
    assert auto.fetch_plan(cfg, 6) == {"video": 15 - 7, "audio": 9 - 3}


def test_run_pipeline_skips_fetch_when_library_is_stocked(cfg, monkeypatch):
    _touch(cfg.path("video_assets"), *[f"v{i}.mp4" for i in range(12)])
    _touch(cfg.path("audio_assets"), *[f"a{i}.mp3" for i in range(6)])
    called = []
    monkeypatch.setattr(auto, "fetch_video_library", lambda *a, **k: called.append("v"))
    monkeypatch.setattr(auto, "fetch_audio_library", lambda *a, **k: called.append("a"))
    monkeypatch.setattr(auto, "build_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    auto.run_pipeline(cfg, batch=1, do_fetch=True)
    assert called == []


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_pexels_skips_known_ids_and_pages_on(cfg, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    monkeypatch.setitem(cfg.raw["fetch"]["pexels"], "per_page", 2)
    pages = {1: [1, 2], 2: [3, 4], 3: [5, 6]}

    def fake_get(url, params=None, headers=None, timeout=None):
        return _Resp({"videos": [
            {"id": i, "url": f"https://pexels/{i}", "user": {"name": "u"},
             "video_files": [{"file_type": "video/mp4", "height": 1920, "width": 1080,
                              "link": f"https://cdn/{i}.mp4"}]}
            for i in pages.get(params["page"], [])]})

    def fake_download(url, dest, timeout=60):
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr(pexels.requests, "get", fake_get)
    monkeypatch.setattr(pexels, "download", fake_download)
    # clip 1 is already in the library (under another query's name); 2 was deleted
    _touch(cfg.path("video_assets"), "pexels-1-some-other-query.mp4")
    (cfg.path("video_assets") / ".fetched.json").write_text(json.dumps({"pexels": ["2"]}))

    saved = pexels.fetch(cfg, "calm sea", limit=3)
    assert [p.split("/")[-1] for p in saved] == [
        "pexels-3-calm-sea.mp4", "pexels-4-calm-sea.mp4", "pexels-5-calm-sea.mp4"]
    seen = json.loads((cfg.path("video_assets") / ".fetched.json").read_text())["pexels"]
    assert seen == ["2", "3", "4", "5"]


def test_pixabay_stops_at_last_page(cfg, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["page"])
        hits = [] if params["page"] > 1 else [
            {"id": 9, "tags": "sea", "user": "u", "pageURL": "p",
             "videos": {"large": {"url": "https://cdn/9.mp4", "width": 2160, "height": 3840}}}]
        return _Resp({"hits": hits})

    monkeypatch.setattr(pixabay.requests, "get", fake_get)
    monkeypatch.setattr(pixabay, "download", lambda url, dest, timeout=60: dest.write_bytes(b"x") or True)
    assert len(pixabay.fetch(cfg, "sea", limit=5)) == 1
    assert calls == [1, 2]


def test_pick_rendition_prefers_smallest_that_covers_output():
    from social_peace.fetch.common import pick_rendition

    uhd = {"width": 2160, "height": 3840}
    fhd = {"width": 1080, "height": 1920}
    hd = {"width": 720, "height": 1280}
    land4k = {"width": 3840, "height": 2160}
    assert pick_rendition([uhd, fhd, hd], 1080, 1920) is fhd
    assert pick_rendition([uhd, hd], 1080, 1920) is uhd
    assert pick_rendition([hd, {"width": 540, "height": 960}], 1080, 1920) is hd   # none cover -> largest
    # a landscape clip only covers a 9:16 frame once its height reaches 1920
    assert pick_rendition([land4k, {"width": 1920, "height": 1080}], 1080, 1920) is land4k
    assert pick_rendition([{"width": None, "height": None}], 1080, 1920) is None


def test_pexels_downloads_the_1080p_rendition_of_a_4k_upload(cfg, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    files = [
        {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://cdn/uhd.mp4"},
        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://cdn/fhd.mp4"},
        {"file_type": "video/mp4", "width": 720, "height": 1280, "link": "https://cdn/hd.mp4"},
    ]
    monkeypatch.setattr(pexels.requests, "get", lambda *a, **k: _Resp(
        {"videos": [{"id": 1, "url": "u", "user": {"name": "n"}, "video_files": files}]}))
    got = []
    monkeypatch.setattr(pexels, "download", lambda url, dest, timeout=60: got.append(url) or True)
    pexels.fetch(cfg, "sea", limit=1)
    assert got == ["https://cdn/fhd.mp4"]
