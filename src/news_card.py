"""Manchete na tela no começo de cada notícia de um vídeo de várias notícias
(portal do Botafogo): "NOTÍCIA 2 DE 5", o título da matéria e o site — quem
assiste sabe que mudou de assunto e onde ler a matéria inteira. Por cima da
foto da cena, ou num cartão escuro quando a notícia não tem foto."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .assemble import WATERMARK_FONT
from .thumbnail import _hex_to_rgb, _thumb_font

MAX_LINES = 4


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= max_w:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: float):
    """Maior fonte em que a manchete cabe em MAX_LINES linhas; se nem a
    mínima couber, corta com reticências."""
    while True:
        font = _thumb_font(size)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= MAX_LINES or size <= 34:
            break
        size = int(size * 0.9)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1].rstrip(",.;:") + "…"
    return font, lines, size


def headline_card(
    base: bytes | None, kicker: str, headline: str, site: str,
    width: int, height: int, accent: str = "#ffffff",
) -> bytes:
    """Quadro da cena com a manchete: no vertical, no terço de cima (a
    legenda karaokê fica embaixo e a interface do Shorts nos 8% do topo); no
    16:9, em cima à esquerda. `base` None = cartão escuro só com a manchete."""
    vertical = height > width
    if base is not None:
        img = Image.open(io.BytesIO(base)).convert("RGB").resize((width, height))
    else:
        r, g, b = _hex_to_rgb(accent)
        img = Image.new("RGB", (width, height), (12, 12, 14))
        glow = Image.new("RGB", (width, height), (r // 4, g // 4, b // 4))
        mask = Image.radial_gradient("L").resize((width, height)).point(lambda v: 255 - v)
        img = Image.composite(glow, img, mask)

    margin = int(width * 0.07)
    box_w = width - 2 * margin if vertical else int(width * 0.62)
    draw = ImageDraw.Draw(img)
    pad = int(width * 0.03)
    font, lines, size = _fit(draw, headline, int(width / (12 if vertical else 22)), box_w - 2 * pad)
    line_h = int(size * 1.18)
    kicker_font = ImageFont.truetype(WATERMARK_FONT, int(size * 0.5))
    site_font = ImageFont.truetype(WATERMARK_FONT, int(size * 0.42))
    kicker_h = int(size * 0.8)
    box_h = kicker_h + pad + len(lines) * line_h + int(size * 0.9) + 2 * pad
    if base is None:
        top = (height - box_h) // 2
    else:
        top = int(height * (0.11 if vertical else 0.07))
    left = margin

    if base is not None:
        # fundo do bloco: a própria foto desfocada e escurecida (legível em
        # qualquer foto, sem tarja chapada)
        region = (left, top, left + box_w, top + box_h)
        patch = img.crop(region).filter(ImageFilter.GaussianBlur(18)).point(lambda v: int(v * 0.35))
        mask = Image.new("L", patch.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *patch.size), radius=int(size * 0.35), fill=255)
        img.paste(patch, region[:2], mask)
        draw = ImageDraw.Draw(img)

    # selo "NOTÍCIA 2 DE 5" na cor do canal
    r, g, b = _hex_to_rgb(accent)
    ink = (10, 10, 10) if (r * 299 + g * 587 + b * 114) / 1000 > 140 else (255, 255, 255)
    kw = draw.textlength(kicker, font=kicker_font)
    kx, ky = left + pad, top + pad
    draw.rounded_rectangle((kx, ky, kx + kw + 2 * pad, ky + kicker_h), radius=kicker_h // 4, fill=(r, g, b))
    draw.text((kx + pad, ky + kicker_h / 2), kicker, font=kicker_font, fill=ink, anchor="lm")

    y = ky + kicker_h + pad
    for line in lines:
        draw.text((left + pad, y), line, font=font, fill=(255, 255, 255))
        y += line_h
    if site:
        draw.text((left + pad, y + int(size * 0.3)), f"Leia em {site}", font=site_font, fill=(r, g, b))

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
