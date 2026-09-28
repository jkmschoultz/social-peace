import json
from datetime import datetime, timedelta, timezone

from social_peace.config import Config
from social_peace.pipeline.prune import prune_rejected


def _mk(out, stem, state, decided=None):
    (out / f"{stem}.json").write_text(
        json.dumps({"id": stem, "review": {"state": state, "decided_utc": decided}})
    )
    (out / f"{stem}.mp4").write_bytes(b"x")


def _cfg(tmp_path, monkeypatch):
    cfg = Config.load()
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(cfg, "path", lambda k: out)
    return cfg, out


def test_drop_all_removes_only_rejected(tmp_path, monkeypatch):
    cfg, out = _cfg(tmp_path, monkeypatch)
    _mk(out, "a_rej", "rejected")
    _mk(out, "b_app", "approved")
    _mk(out, "c_rej", "rejected")
    _mk(out, "d_pending", "pending")

    removed = prune_rejected(cfg, drop_all=True)

    assert set(removed) == {"a_rej", "c_rej"}
    assert not (out / "a_rej.mp4").exists() and not (out / "c_rej.json").exists()
    assert (out / "b_app.json").exists() and (out / "d_pending.mp4").exists()


def test_age_gate(tmp_path, monkeypatch):
    cfg, out = _cfg(tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    _mk(out, "old_rej", "rejected", old)
    _mk(out, "new_rej", "rejected", recent)

    removed = prune_rejected(cfg, older_than_days=30)

    assert removed == ["old_rej"]
    assert (out / "new_rej.json").exists()


def test_missing_decided_utc_uses_mtime(tmp_path, monkeypatch):
    cfg, out = _cfg(tmp_path, monkeypatch)
    _mk(out, "r", "rejected", None)  # just written -> mtime is now
    assert prune_rejected(cfg, older_than_days=30) == []


def _mk_pub(out, stem, status, state="approved"):
    (out / f"{stem}.json").write_text(json.dumps({
        "id": stem, "review": {"state": state}, "status": status,
        "sources": {"video": [f"{stem}.clip.mp4"], "audio": []},
    }))
    for ext in (".mp4", ".thumb.jpg", ".overlay.png"):
        (out / f"{stem}{ext}").write_bytes(b"x")


def test_clear_published_media_keeps_sidecar(tmp_path, monkeypatch):
    from social_peace.pipeline.metadata import sources_in_use
    from social_peace.pipeline.prune import clear_published_media
    from social_peace.publish.runner import available_platforms

    cfg, out = _cfg(tmp_path, monkeypatch)
    everywhere = {p: "uploaded" for p in available_platforms(cfg)}
    _mk_pub(out, "a_live", everywhere)
    _mk_pub(out, "b_queued", {})
    _mk(out, "c_pending", "pending")

    cleared = clear_published_media(cfg)

    assert cleared == ["a_live"]
    assert (out / "a_live.json").exists()
    assert not any((out / f"a_live{e}").exists() for e in (".mp4", ".thumb.jpg", ".overlay.png"))
    assert (out / "b_queued.mp4").exists() and (out / "c_pending.mp4").exists()
    # the cleared render still holds its clip
    assert "a_live.clip.mp4" in sources_in_use(out)[0]


def test_clear_published_media_skips_renders_still_owed(tmp_path, monkeypatch):
    from social_peace.pipeline.prune import clear_published_media
    from social_peace.publish.runner import available_platforms

    cfg, out = _cfg(tmp_path, monkeypatch)
    plats = available_platforms(cfg)
    if len(plats) < 2:
        return
    _mk_pub(out, "a_partial", {plats[0]: "uploaded"})

    assert clear_published_media(cfg) == []
    assert (out / "a_partial.mp4").exists()
