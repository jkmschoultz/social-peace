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

from social_peace.config import Config

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
