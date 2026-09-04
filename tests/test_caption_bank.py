"""caption idea bank: merge_caption_bank / expand_caption_bank / append_caption_bank
and the /api/captions-bank review endpoint.

No real API calls — a fake `anthropic` module is injected for the happy path.
"""
import sys
import types

import yaml

from conftest import _wait_job
from social_peace.config import CAPTION_BANK_FILE, Config, merge_caption_bank
from social_peace.pipeline import captions

_VALID_JSON = (
    '{"overlay_text": ["Let the day be slow", "Nothing needs you yet"], '
    '"youtube_title_templates": ["{hook} #shorts", "A pause: {hook}"], '
    '"tiktok_caption_templates": ["{hook}"], '
    '"instagram_caption_templates": ["{hook}", "{hook} \\u2014 breathe"]}'
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


# --------------------------------------------------------------- merge_caption_bank

def test_merge_caption_bank_appends_and_dedupes():
    raw = {
        "overlay_text": ["Existing line"],
        "metadata": {
            "youtube": {"title_templates": ["{hook} #shorts"]},
            "tiktok": {"caption_templates": []},
        },
    }
    bank = {
        "overlay_text": ["Existing line", "Brand new line"],
        "youtube_title_templates": ["A pause: {hook}"],
        "tiktok_caption_templates": ["{hook}"],
        "instagram_caption_templates": ["{hook} then rest"],
    }
    merge_caption_bank(raw, bank)

    assert raw["overlay_text"] == ["Existing line", "Brand new line"]
    assert raw["metadata"]["youtube"]["title_templates"] == ["{hook} #shorts", "A pause: {hook}"]
    assert raw["metadata"]["tiktok"]["caption_templates"] == ["{hook}"]
    # nested path created where missing
    assert raw["metadata"]["instagram"]["caption_templates"] == ["{hook} then rest"]


def test_merge_caption_bank_ignores_blank_and_missing_keys():
    raw = {"overlay_text": ["a"]}
    merge_caption_bank(raw, {"overlay_text": ["  ", ""], "unknown_key": ["x"]})
    assert raw["overlay_text"] == ["a"]


def test_config_load_merges_bank_file(tmp_path, monkeypatch):
    cfg = Config.load()
    root = cfg.root
    bank_file = root / CAPTION_BANK_FILE
    marker = "A one-off test overlay line please remove"
    existing = yaml.safe_load(bank_file.read_text(encoding="utf-8")) if bank_file.is_file() else {}
    try:
        data = dict(existing or {})
        data.setdefault("overlay_text", [])
        assert marker not in data["overlay_text"]
        data["overlay_text"] = list(data["overlay_text"]) + [marker]
        bank_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

        reloaded = Config.load()
        assert marker in reloaded.raw["overlay_text"]
    finally:
        if existing:
            bank_file.write_text(yaml.safe_dump(existing, allow_unicode=True), encoding="utf-8")
        elif bank_file.exists():
            bank_file.unlink()


# --------------------------------------------------------------- expand_caption_bank

def test_expand_no_api_key_returns_error(monkeypatch):
    cfg = Config.load()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = captions.expand_caption_bank(cfg)
    assert "error" in out and "ANTHROPIC_API_KEY" in out["error"]


def test_expand_parses_json(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply=_VALID_JSON)

    out = captions.expand_caption_bank(cfg, per_list=2)
    assert out["overlay_text"] == ["Let the day be slow", "Nothing needs you yet"]
    assert out["youtube_title_templates"] == ["{hook} #shorts", "A pause: {hook}"]
    assert out["tiktok_caption_templates"] == ["{hook}"]
    assert out["instagram_caption_templates"] == ["{hook}", "{hook} — breathe"]


def test_expand_fenced_json(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, reply=f"```json\n{_VALID_JSON}\n```")
    out = captions.expand_caption_bank(cfg)
    assert out["overlay_text"][0] == "Let the day be slow"


def test_expand_api_error_returns_error(monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _install_fake_anthropic(monkeypatch, raise_exc=RuntimeError("boom"))
    out = captions.expand_caption_bank(cfg)
    assert "error" in out and "boom" in out["error"]


# --------------------------------------------------------------- append_caption_bank

def test_append_writes_file_and_merges_into_running_config(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setitem(cfg.raw, "overlay_text", ["Original"])
    monkeypatch.setitem(
        cfg.raw, "metadata",
        {"youtube": {"title_templates": []}, "tiktok": {"caption_templates": []},
         "instagram": {"caption_templates": []}},
    )

    additions = {
        "overlay_text": ["Fresh one", "Fresh two"],
        "youtube_title_templates": ["{hook} #shorts"],
        "tiktok_caption_templates": ["{hook}"],
        "instagram_caption_templates": ["{hook}"],
    }
    res = captions.append_caption_bank(cfg, additions)

    assert res["fetched"] == 5
    assert res["added"]["overlay_text"] == 2
    assert res["total"]["overlay_text"] == 2

    bank_file = tmp_path / CAPTION_BANK_FILE
    assert bank_file.is_file()
    saved = yaml.safe_load(bank_file.read_text(encoding="utf-8"))
    assert saved["overlay_text"] == ["Fresh one", "Fresh two"]

    # running config reflects the new lines immediately
    assert "Fresh one" in cfg.raw["overlay_text"]
    assert "{hook} #shorts" in cfg.raw["metadata"]["youtube"]["title_templates"]


def test_append_is_idempotent(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setitem(cfg.raw, "overlay_text", [])
    monkeypatch.setitem(
        cfg.raw, "metadata",
        {"youtube": {"title_templates": []}, "tiktok": {"caption_templates": []},
         "instagram": {"caption_templates": []}},
    )
    additions = {"overlay_text": ["Only line"]}

    captions.append_caption_bank(cfg, additions)
    res2 = captions.append_caption_bank(cfg, additions)

    assert res2["added"]["overlay_text"] == 0
    assert res2["total"]["overlay_text"] == 1
    saved = yaml.safe_load((tmp_path / CAPTION_BANK_FILE).read_text(encoding="utf-8"))
    assert saved["overlay_text"] == ["Only line"]


# --------------------------------------------------------------- /api/captions-bank

def test_api_captions_bank_happy_path(client, monkeypatch):
    c, _side, _logs = client
    monkeypatch.setattr(
        captions, "expand_caption_bank",
        lambda cfg, **kw: {k: ["{hook} fresh"] for k in captions._BANK_KEYS},
    )

    r = c.post("/api/captions-bank")
    job_id = r.get_json()["job_id"]
    done = _wait_job(c, job_id)

    assert done["state"] == "done", done
    assert done["result"]["fetched"] == len(captions._BANK_KEYS)


def test_api_captions_bank_surfaces_error(client, monkeypatch):
    c, _side, _logs = client
    monkeypatch.setattr(
        captions, "expand_caption_bank",
        lambda cfg, **kw: {"error": "ANTHROPIC_API_KEY not set — new caption ideas need the Claude API"},
    )

    job_id = c.post("/api/captions-bank").get_json()["job_id"]
    done = _wait_job(c, job_id)

    assert done["state"] == "error"
    assert "ANTHROPIC_API_KEY" in done["error"]


# ------------------------------------------------- add / remove single bank lines

def test_add_and_remove_bank_line(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setitem(cfg.raw, "overlay_text", ["Base line"])

    res = captions.add_caption_bank_line(cfg, "overlay_text", "  A typed line  ")
    assert res == {"ok": True, "key": "overlay_text", "added": 1, "total": 2}
    assert "A typed line" in cfg.raw["overlay_text"]
    saved = yaml.safe_load((tmp_path / CAPTION_BANK_FILE).read_text(encoding="utf-8"))
    assert saved["overlay_text"] == ["A typed line"]

    # dedupe: adding again (or adding a base line) is a no-op
    assert captions.add_caption_bank_line(cfg, "overlay_text", "A typed line")["added"] == 0
    assert captions.add_caption_bank_line(cfg, "overlay_text", "Base line")["added"] == 0

    rem = captions.remove_caption_bank_line(cfg, "overlay_text", "A typed line")
    assert rem["ok"] and rem["removed"] == 1 and rem["total"] == 1
    assert "A typed line" not in cfg.raw["overlay_text"]


def test_remove_refuses_base_line(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "root", tmp_path)
    monkeypatch.setitem(cfg.raw, "overlay_text", ["Only from config.yaml"])
    res = captions.remove_caption_bank_line(cfg, "overlay_text", "Only from config.yaml")
    assert res["ok"] is False and res["removed"] == 0
    assert "Only from config.yaml" in cfg.raw["overlay_text"]  # untouched


def test_add_remove_reject_unknown_key(tmp_path, monkeypatch):
    cfg = Config.load()
    monkeypatch.setattr(cfg, "root", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        captions.add_caption_bank_line(cfg, "nope", "x")


# ---------------------------------------------------------- /api/caption endpoint

def test_api_caption_add_then_delete(client):
    c, _side, _logs = client
    line = "{hook}\n\ncome back here when the day gets loud"

    r = c.post("/api/caption/instagram_caption_templates/add", json={"text": line})
    assert r.status_code == 200 and r.get_json()["added"] == 1

    r2 = c.post("/api/caption/instagram_caption_templates/delete", json={"text": line})
    assert r2.status_code == 200 and r2.get_json()["removed"] == 1


def test_api_caption_delete_base_line_is_400(client):
    c, *_ = client
    from social_peace.config import Config as _C
    base = _C.load().raw["overlay_text"][0]
    r = c.post("/api/caption/overlay_text/delete", json={"text": base})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_api_caption_guards(client):
    c, *_ = client
    assert c.post("/api/caption/bogus_list/add", json={"text": "x"}).status_code == 404
    assert c.post("/api/caption/overlay_text/frobnicate", json={"text": "x"}).status_code == 404
    assert c.post("/api/caption/overlay_text/add", json={"text": "   "}).status_code == 400


def test_assets_captions_only_view(client):
    c, *_ = client
    html = c.get("/assets?filter=captions-only").get_data(as_text=True)
    assert "captions only" in html            # the show-filter option
    assert 'id="cap-overlay_text"' in html    # the caption section rendered
    assert "class=\"crow\"" in html
