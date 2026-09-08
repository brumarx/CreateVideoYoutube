"""Thumbnail: imagem gerada por IA (Pollinations, momento mais marcante da
história) OU foto real oficial (quando a cena é sobre uma pessoa real
nomeada — ver `real_photo_url`) + faixa colorida na cor do canal + gancho
curto (2-4 palavras, não o título inteiro) por cima, via Pillow. YouTube
exige recomendado 1280x720.

Estilo pensado pra thumbnail "de verdade" (o que thumbnail boa hoje em dia
faz): pouco texto, bem grande, numa faixa sólida que já garante contraste
com qualquer imagem por trás — em vez do título inteiro quebrado em 3
linhas encostado direto na foto, que fica com cara de rascunho.
"""
from __future__ import annotations

import io
import logging
import textwrap
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .visuals import generate_image

log = logging.getLogger("thumbnail")

THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

BORDER_PX = 14  # moldura na cor do canal — identidade visual consistente na grade de vídeos
BAND_RATIO = 0.30  # faixa colorida ocupa os últimos 30% da altura

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _hex_to_rgb(hexcolor: str) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _fetch_real_photo(url: str) -> Image.Image | None:
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as exc:
        log.warning("não consegui baixar a foto real (%s): %s", url, exc)
        return None


def _compose_person_photo(photo: Image.Image, width: int, height: int) -> Image.Image:
    """Fotos oficiais de deputado/senador/magistrado são retrato (ex.:
    354x472) — esticar pra 16:9 distorceria o rosto. Em vez disso: fundo
    desfocado/escurecido preenchendo o quadro todo + a foto nítida por cima,
    centralizada, do jeito que thumbnail de "reação" costuma fazer."""
    bg = photo.copy()
    bg_ratio = width / height
    photo_ratio = bg.width / bg.height
    if photo_ratio > bg_ratio:
        new_h = height
        new_w = int(new_h * photo_ratio)
    else:
        new_w = width
        new_h = int(new_w / photo_ratio)
    bg = bg.resize((new_w, new_h))
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    bg = bg.crop((left, top, left + width, top + height))
    bg = bg.filter(ImageFilter.GaussianBlur(24))
    bg = ImageEnhance.Brightness(bg).enhance(0.5)

    fg_h = height
    fg_w = int(fg_h * photo_ratio)
    fg = photo.resize((fg_w, fg_h))
    canvas = bg
    canvas.paste(fg, ((width - fg_w) // 2, 0))
    return canvas


def make_thumbnail(
    prompt: str,
    hook_text: str,
    output_path: Path,
    accent: str = "#ffffff",
    real_photo_url: str | None = None,
) -> Path:
    """`hook_text` deve ser curto (2-4 palavras) — texto longo quebrado em
    3 linhas é exatamente o que faz uma thumbnail parecer amadora.
    `real_photo_url` (foto OFICIAL de deputado/senador/magistrado, ver
    src/politica_data.py) usa a foto de verdade da pessoa em vez de pedir
    pra IA "inventar" o rosto dela — mais preciso e evita o risco de gerar
    uma cara errada atribuída a alguém real."""
    img = None
    if real_photo_url:
        photo = _fetch_real_photo(real_photo_url)
        if photo:
            img = _compose_person_photo(photo, THUMB_WIDTH, THUMB_HEIGHT)

    if img is None:
        raw = generate_image(prompt, width=THUMB_WIDTH, height=THUMB_HEIGHT)
        img = Image.open(io.BytesIO(raw)).convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT))

    # mais contraste/saturação = imagem "pop" mais na lista de vídeos —
    # thumbnail boa quase sempre tem cor mais viva que uma foto crua.
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Brightness(img).enhance(1.02)

    accent_rgb = _hex_to_rgb(accent)
    text_color = "black" if _luminance(accent_rgb) > 150 else "white"

    draw = ImageDraw.Draw(img)

    band_h = int(THUMB_HEIGHT * BAND_RATIO)
    band_y0 = THUMB_HEIGHT - band_h
    draw.rectangle([0, band_y0, THUMB_WIDTH, THUMB_HEIGHT], fill=accent_rgb)

    font = _load_font(96)
    lines = textwrap.wrap(hook_text.upper(), width=12)[:2]
    line_height = 104
    total_height = line_height * len(lines)
    y = band_y0 + max((band_h - total_height) // 2, 10)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (THUMB_WIDTH - text_w) / 2
        draw.text((x, y), line, font=font, fill=text_color)
        y += line_height

    # moldura na cor do canal — mesma identidade em todos os vídeos
    draw.rectangle([0, 0, THUMB_WIDTH - 1, THUMB_HEIGHT - 1], outline=accent_rgb, width=BORDER_PX)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=92)
    return output_path
