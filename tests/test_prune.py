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
