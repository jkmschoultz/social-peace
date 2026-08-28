"""Render the text overlay as a transparent PNG (composited by ffmpeg's overlay filter).

Pillow is used instead of ffmpeg's drawtext so we get real word-wrapping, letter
spacing, and a legibility scrim without fighting drawtext's escaping on Windows.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


def _load_font(fonts_dir: Path, size: int) -> ImageFont.FreeTypeFont:
    for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        for f in sorted(fonts_dir.glob(ext)):
            try:
                return ImageFont.truetype(str(f), size=size)
            except OSError:
                continue
    log.warning("no usable font in %s — falling back to Pillow's bitmap default", fonts_dir)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_tracked_centered(draw, text, font, canvas_w, y, fill, tracking):
    total = sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(len(text) - 1, 0)
    x = (canvas_w - total) / 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _paint_bottom_gradient(img: Image.Image, start_y: int, max_alpha: int = 180) -> None:
    w, h = img.size
    start_y = max(0, min(start_y, h - 1))
    grad = Image.new("L", (1, h), 0)
    for y in range(start_y, h):
        t = (y - start_y) / max(h - start_y, 1)
        grad.putpixel((0, y), int(max_alpha * (t ** 0.9)))
    alpha = grad.resize((w, h))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    img.alpha_composite(Image.composite(black, Image.new("RGBA", (w, h), (0, 0, 0, 0)), alpha))


def render_overlay(
    *,
    text: str,
    footer: str,
    size: tuple[int, int],
    out_path: Path,
    fonts_dir: Path,
    style: str = "lower_third",
    scrim: str = "gradient",
    color: str = "#FDFDF8",
    font_size: int = 62,
) -> Path:
    w, h = size
    margin = int(w * 0.10)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = _load_font(fonts_dir, font_size)
    footer_font = _load_font(fonts_dir, max(int(font_size * 0.42), 18))

    lines = _wrap(draw, text, font, w - 2 * margin)
    line_h = int(font_size * 1.34)
    block_h = line_h * len(lines)
    footer_gap = int(font_size * 0.9) if footer else 0
    footer_h = getattr(footer_font, "size", max(int(font_size * 0.42), 18)) if footer else 0
    full_h = block_h + footer_gap + footer_h

    if style == "center":
        y0 = (h - full_h) // 2
    else:  # lower_third
        y0 = int(h * 0.74) - full_h // 2

    if scrim == "gradient":
        _paint_bottom_gradient(img, start_y=y0 - margin)
    elif scrim == "box":
        pad = int(margin * 0.55)
        draw.rounded_rectangle(
            [margin - pad, y0 - pad, w - margin + pad, y0 + full_h + pad],
            radius=int(pad * 0.8),
            fill=(0, 0, 0, 115),
        )

    # soft shadow then text
    for i, ln in enumerate(lines):
        y = y0 + i * line_h
        _draw_tracked_centered(draw, ln, font, w, y + 2, (0, 0, 0, 140), tracking=1.5)
        _draw_tracked_centered(draw, ln, font, w, y, color, tracking=1.5)

    if footer:
        fy = y0 + block_h + footer_gap
        _draw_tracked_centered(draw, footer.upper(), footer_font, w, fy, (255, 255, 255, 200), tracking=3)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
