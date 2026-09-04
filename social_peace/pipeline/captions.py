"""LLM-generated platform captions (YouTube title/description, TikTok/Instagram
caption) written fresh for each video, in the style of the example lines in
config.yaml, instead of picking one of them verbatim.

Falls back to `None` (caller uses the old template `rng.choice` behaviour) when
captions are disabled, ANTHROPIC_API_KEY is unset, the `anthropic` package isn't
installed, or the API call fails for any reason — caption generation is a nice-to-
have, never a reason to fail a render.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

import yaml

from social_peace.config import CAPTION_BANK_FILE, Config, merge_caption_bank

log = logging.getLogger(__name__)

_FIELDS = ("youtube_title", "youtube_description", "tiktok_caption", "instagram_caption")

_SYSTEM = (
    "You write short, calming social captions for a faceless short-form video "
    "channel about rest, breathing, and nature. You are shown this channel's "
    "existing lines purely as a style reference — matching tone, length, and "
    "structure — and must write brand-new wording, never reusing an example "
    "verbatim or lightly rephrased. Reply with ONLY a single JSON object, no "
    "prose, no markdown code fences, with exactly these string keys: "
    '"youtube_title", "youtube_description", "tiktok_caption", "instagram_caption". '
    "youtube_title may end with a short hashtag the way the examples do (e.g. "
    "#shorts) — that is styling, not a hashtag list. The other three fields must "
    "contain no hashtags at all; a fixed hashtag list is appended to each "
    "automatically after you respond."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _examples(cfg: Config) -> str:
    md = cfg.raw.get("metadata", {})
    yt = md.get("youtube", {})
    tiktok = md.get("tiktok", {})
    instagram = md.get("instagram", {})

    def bullets(items):
        return "\n".join(f"- {t}" for t in items) or "(none)"

    return (
        f"Existing YouTube titles:\n{bullets(yt.get('title_templates', []))}\n\n"
        f"YouTube description style (write a new one in this voice):\n"
        f"{yt.get('description', '').strip()}\n\n"
        f"Existing TikTok captions:\n{bullets(tiktok.get('caption_templates', []))}\n\n"
        f"Existing Instagram captions:\n{bullets(instagram.get('caption_templates', []))}"
    )


def _prompt(cfg: Config, hook: str, template_name: str) -> str:
    return (
        f"This video's on-screen text is: \"{hook}\"\n"
        f"Visual mood/template: {template_name}\n\n"
        f"{_examples(cfg)}\n\n"
        "Write a fresh YouTube title + description, TikTok caption, and Instagram "
        "caption for this specific video, in the channel's voice, referencing its "
        "theme. Keep each roughly the same length as the examples shown."
    )


def _parse(text: str) -> dict:
    data = json.loads(_FENCE_RE.sub("", text.strip()).strip())
    return {k: str(data[k]).strip() for k in _FIELDS}


def generate_platform_captions(cfg: Config, hook: str, template_name: str) -> dict | None:
    ccfg = cfg.raw.get("captions", {})
    if not ccfg.get("enabled", True):
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("captions: ANTHROPIC_API_KEY not set, using template captions")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("captions: `anthropic` not installed (pip install -e .[llm]), using template captions")
        return None

    model = ccfg.get("model", "claude-opus-5")
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=int(ccfg.get("max_output_tokens", 700)),
            system=_SYSTEM,
            output_config={"effort": ccfg.get("effort", "low")},
            messages=[{"role": "user", "content": _prompt(cfg, hook, template_name)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("captions: generation failed (%s), using template captions", exc)
        return None


# --------------------------------------------------------------- caption idea bank

_BANK_KEYS = (
    "overlay_text", "youtube_title_templates",
    "tiktok_caption_templates", "instagram_caption_templates",
)

_BANK_SYSTEM = (
    "You write short, calming copy for a faceless short-form video channel about "
    "rest, breathing and nature. Given the channel's existing lines as style "
    "references, invent BRAND-NEW lines in the same voice, length and structure — "
    "never reuse or lightly reword an example. Reply with ONLY a JSON object, no "
    "prose, no code fences, with these array keys: \"overlay_text\" (on-screen "
    "lines, 3-8 words, gentle imperative/permissive), \"youtube_title_templates\", "
    "\"tiktok_caption_templates\", \"instagram_caption_templates\". Use the literal "
    "token {hook} exactly where the examples use it (it becomes the video's "
    "on-screen line at render time). Keep an inline hashtag only where the examples "
    "have one; do not add hashtag lists."
)


def expand_caption_bank(cfg: Config, *, per_list: int = 6) -> dict:
    """Ask Claude for `per_list` new lines for each caption list. Returns
    {overlay_text: [...], youtube_title_templates: [...], ...} or {"error": "..."}.
    Needs ANTHROPIC_API_KEY — there is no non-LLM source for new caption copy."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set — new caption ideas need the Claude API"}
    try:
        import anthropic
    except ImportError:
        return {"error": "`anthropic` not installed — run: pip install -e .[llm]"}

    ccfg = cfg.raw.get("captions", {})
    md = cfg.raw.get("metadata", {})

    def bul(items):
        return "\n".join(f"- {t}" for t in items) or "(none)"

    prompt = (
        f"Write {per_list} new lines for EACH list.\n\n"
        f"overlay_text examples:\n{bul(cfg.raw.get('overlay_text', []))}\n\n"
        f"youtube_title_templates examples:\n{bul(md.get('youtube', {}).get('title_templates', []))}\n\n"
        f"tiktok_caption_templates examples:\n{bul(md.get('tiktok', {}).get('caption_templates', []))}\n\n"
        f"instagram_caption_templates examples:\n{bul(md.get('instagram', {}).get('caption_templates', []))}"
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=ccfg.get("model", "claude-opus-5"),
            max_tokens=max(1500, int(ccfg.get("max_output_tokens", 700)) * 2),
            system=_BANK_SYSTEM,
            output_config={"effort": ccfg.get("effort", "low")},
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(_FENCE_RE.sub("", text.strip()).strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("caption bank: generation failed (%s)", exc)
        return {"error": f"generation failed: {exc}"}

    return {k: [str(x).strip() for x in (data.get(k) or []) if str(x).strip()] for k in _BANK_KEYS}


_BANK_HEADER = (
    "# Extra caption / overlay template lines, merged on top of the matching\n"
    "# lists in config.yaml at load time (social_peace.config.merge_caption_bank).\n"
    "# Grow it with:  python -m social_peace captions-bank   (needs ANTHROPIC_API_KEY)\n"
    "# or the assets page in the review UI (captions section).\n"
)


def _bank_path(cfg: Config) -> Path:
    return cfg.root / CAPTION_BANK_FILE


def read_caption_bank(cfg: Config) -> dict:
    """The raw contents of config/caption_bank.yaml ({} if it doesn't exist)."""
    path = _bank_path(cfg)
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_bank(cfg: Config, bank: dict) -> None:
    path = _bank_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".caption_bank-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_BANK_HEADER)
            fh.write(yaml.safe_dump({k: v for k, v in bank.items() if v},
                                    sort_keys=True, allow_unicode=True, width=100))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_caption_bank(cfg: Config, additions: dict) -> dict:
    """Merge `additions` into config/caption_bank.yaml (dedup, atomic write) and
    into the running config. Returns {added: {key: n}, total: {key: n}, fetched}."""
    bank = read_caption_bank(cfg)
    added: dict[str, int] = {}
    for key in _BANK_KEYS:
        cur = list(bank.get(key, []))
        new = [ln for ln in (additions.get(key) or []) if ln and ln not in cur]
        bank[key] = cur + new
        added[key] = len(new)

    _write_bank(cfg, bank)
    merge_caption_bank(cfg.raw, additions)  # reflect immediately in this process
    return {
        "added": added,
        "total": {k: len(bank.get(k, [])) for k in _BANK_KEYS},
        "fetched": sum(added.values()),
    }


def _config_list(cfg: Config, key: str) -> list:
    """The live merged list (config.yaml + bank) behind a bank key."""
    from social_peace.config import _BANK_MAP
    node = cfg.raw
    for seg in _BANK_MAP[key]:
        node = node.get(seg, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, list) else []


def add_caption_bank_line(cfg: Config, key: str, text: str) -> dict:
    """Append one hand-typed line to a caption-bank list. Returns
    {ok, key, added, total} — added is 0 if the line was already present."""
    if key not in _BANK_KEYS:
        raise ValueError(f"unknown caption list {key!r}")
    text = (text or "").strip()
    if not text:
        raise ValueError("empty line")
    added = 1
    if text in _config_list(cfg, key):  # already in config.yaml or the bank
        added = 0
    else:
        bank = read_caption_bank(cfg)
        bank[key] = list(bank.get(key, [])) + [text]
        _write_bank(cfg, bank)
        merge_caption_bank(cfg.raw, {key: [text]})
    return {"ok": True, "key": key, "added": added, "total": len(_config_list(cfg, key))}


def remove_caption_bank_line(cfg: Config, key: str, text: str) -> dict:
    """Drop one line from a caption-bank list. Only lines that live in
    caption_bank.yaml can go — built-in config.yaml lines are left alone.
    Returns {ok, key, removed, total}."""
    if key not in _BANK_KEYS:
        raise ValueError(f"unknown caption list {key!r}")
    bank = read_caption_bank(cfg)
    cur = list(bank.get(key, []))
    if text not in cur:
        return {"ok": False, "key": key, "removed": 0, "error": "not a removable (bank) line",
                "total": len(_config_list(cfg, key))}
    bank[key] = [ln for ln in cur if ln != text]
    _write_bank(cfg, bank)
    live = _config_list(cfg, key)
    if text in live:
        live.remove(text)  # keep the running process in sync
    return {"ok": True, "key": key, "removed": 1, "total": len(live)}
