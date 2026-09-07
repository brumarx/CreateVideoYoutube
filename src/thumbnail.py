"""Thumbnail: imagem gerada por IA (Pollinations) + texto do título por cima
(Pillow). YouTube exige recomendado 1280x720."""
from __future__ import annotations

import io
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .visuals import generate_image

THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_thumbnail(prompt: str, title: str, output_path: Path) -> Path:
    raw = generate_image(prompt, width=THUMB_WIDTH, height=THUMB_HEIGHT)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT))

    draw = ImageDraw.Draw(img)
    font = _load_font(72)
    lines = textwrap.wrap(title.upper(), width=16)[:3]

    line_height = 84
    total_height = line_height * len(lines)
    y = THUMB_HEIGHT - total_height - 60

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (THUMB_WIDTH - text_w) / 2
        # contorno preto pra legibilidade em cima de qualquer fundo
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x + dx, y + dy), line, font=font, fill="black")
        draw.text((x, y), line, font=font, fill="white")
        y += line_height

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=92)
    return output_path
