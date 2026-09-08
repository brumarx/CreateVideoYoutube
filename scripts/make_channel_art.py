#!/usr/bin/env python3
"""Gera avatar (ícone quadrado) e banner (capa do canal) pra um canal, no
mesmo estilo visual do Fractal Curioso (ícone minimalista flat, fundo
escuro, glow neon). A IA de imagem nunca escreve texto direito — então o
nome do canal no banner é sempre desenhado por cima com Pillow (mesma
técnica do thumbnail.py), nunca pedido no prompt da IA.

Uso:
  .venv/bin/python3 scripts/make_channel_art.py --channel tech_news --title "Núcleo Tech BR" --icon-prompt "..." --accent "#3ec6ff"
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.visuals import generate_image  # noqa: E402

OUT_DIR = ROOT / "output" / "channel_art"

AVATAR_SIZE = 800
BANNER_W, BANNER_H = 2560, 1440
# "safe area" do YouTube — parte do banner que aparece em qualquer
# dispositivo (celular corta as bordas) — todo texto/logo tem que caber
# dentro disso.
SAFE_W, SAFE_H = 1546, 423

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_avatar(icon_prompt: str, out_path: Path) -> Path:
    raw = generate_image(icon_prompt, width=AVATAR_SIZE, height=AVATAR_SIZE)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((AVATAR_SIZE, AVATAR_SIZE))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def make_banner(bg_prompt: str, title: str, accent: str, out_path: Path) -> Path:
    raw = generate_image(bg_prompt, width=BANNER_W, height=BANNER_H)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((BANNER_W, BANNER_H))

    # escurece um pouco pra garantir contraste do texto em cima de qualquer fundo
    overlay = Image.new("RGB", img.size, (0, 0, 0))
    img = Image.blend(img, overlay, 0.35)

    draw = ImageDraw.Draw(img)
    font = _load_font(96)
    bbox = draw.textbbox((0, 0), title, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    safe_x0 = (BANNER_W - SAFE_W) // 2
    safe_y0 = (BANNER_H - SAFE_H) // 2
    x = safe_x0 + (SAFE_W - text_w) / 2
    y = safe_y0 + (SAFE_H - text_h) / 2 - bbox[1]

    for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
        draw.text((x + dx, y + dy), title, font=font, fill="black")
    draw.text((x, y), title, font=font, fill=accent)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--title", required=True, help="nome exibido no banner")
    parser.add_argument("--icon-prompt", required=True)
    parser.add_argument("--banner-prompt", default=None, help="default: variação mais ampla do icon-prompt")
    parser.add_argument("--accent", default="#ffffff", help="cor do texto do banner, hex")
    args = parser.parse_args()

    banner_prompt = args.banner_prompt or (args.icon_prompt + ", wide panoramic composition, subtle, dark, cinematic")

    avatar_path = OUT_DIR / f"avatar_{args.channel}.png"
    banner_path = OUT_DIR / f"banner_{args.channel}.png"

    make_avatar(args.icon_prompt, avatar_path)
    print(f"avatar salvo: {avatar_path}")

    make_banner(banner_prompt, args.title, args.accent, banner_path)
    print(f"banner salvo: {banner_path}")


if __name__ == "__main__":
    main()
