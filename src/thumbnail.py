"""Thumbnail: imagem gerada por IA (Pollinations, momento mais marcante da
história) OU foto real oficial (quando a cena é sobre uma pessoa real
nomeada — ver `real_photo_url`) + gancho curto (2-4 palavras, não o título
inteiro) escrito em cima da própria imagem, via Pillow. YouTube exige
recomendado 1280x720.

Estilo: imagem em tela cheia (sem moldura nem faixa sólida comendo espaço —
isso é o que faz thumbnail automática parecer "slide" em vez de um momento
real) + texto grande com contorno preto grosso (funciona em cima de
qualquer fundo, é a técnica padrão de thumbnail que bomba) + gradiente
escuro sutil por trás do texto como garantia extra de contraste.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .visuals import generate_image

log = logging.getLogger("thumbnail")

THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

# Wikimedia Commons/Wikipedia bloqueiam requisição sem User-Agent
# identificável (política deles: meta.wikimedia.org/wiki/User-Agent_policy)
# — achado de verdade: foto oficial de Flávio Dino (fonte real cadastrada
# no banco) devolveu 403 sem isso, mesmo a URL estando certa.
_HTTP_HEADERS = {"User-Agent": "YoutubeAIPipeline/1.0 (https://github.com/brumarx/CreateVideoYoutube)"}

STROKE_WIDTH = 8  # contorno preto grosso — dá contraste em cima de QUALQUER imagem
GRADIENT_RATIO = 0.45  # gradiente escuro nos últimos 45% da altura, por trás do texto

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fit_hook_text(
    text: str, max_width: int, base_size: int = 110, min_size: int = 56,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
    """Encaixa `text` em no máximo 2 linhas medindo largura real (não
    contagem de caractere — igual já feito pra legenda em assemble.py).
    Vai diminuindo a fonte até caber; NUNCA descarta palavra — se nem no
    tamanho mínimo couber em 2 linhas, aceita 3+ linhas em vez de cortar
    a última palavra silenciosamente (aconteceu de verdade: "RACHANDO"
    sumindo do final de "O mundo está rachando")."""
    size = base_size
    font = _load_font(size)
    lines: list[str] = []
    while True:
        words = text.split()
        lines = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if font.getlength(trial) <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= 2 or size <= min_size:
            return font, lines
        size -= 8
        font = _load_font(size)


def _hex_to_rgb(hexcolor: str) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _fetch_real_photo(url: str) -> Image.Image | None:
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True, headers=_HTTP_HEADERS)
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
    img = img.convert("RGBA")

    accent_rgb = _hex_to_rgb(accent)

    # gradiente escuro nos últimos GRADIENT_RATIO da altura, por trás do
    # texto — contorno preto já garante contraste sozinho, isso é reforço
    # extra pra imagem muito clara (ex.: céu, neve) não "comer" o contorno.
    gradient_h = int(THUMB_HEIGHT * GRADIENT_RATIO)
    gradient_y0 = THUMB_HEIGHT - gradient_h
    gradient = Image.new("RGBA", (THUMB_WIDTH, gradient_h), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(gradient)
    for row in range(gradient_h):
        alpha = int(150 * (row / gradient_h))
        grad_draw.line([(0, row), (THUMB_WIDTH, row)], fill=(0, 0, 0, alpha))
    img.paste(gradient, (0, gradient_y0), gradient)

    draw = ImageDraw.Draw(img)

    # texto direto na imagem (tela cheia, sem faixa nem moldura comendo
    # espaço) — contorno preto grosso funciona em cima de qualquer fundo,
    # é a técnica padrão de thumbnail que realmente bomba.
    font, lines = _fit_hook_text(hook_text.upper(), max_width=THUMB_WIDTH - 80)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent + 12
    total_height = line_height * len(lines)
    y = THUMB_HEIGHT - 40 - total_height

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=STROKE_WIDTH)
        text_w = bbox[2] - bbox[0]
        x = (THUMB_WIDTH - text_w) / 2
        draw.text(
            (x, y), line, font=font, fill="white",
            stroke_width=STROKE_WIDTH, stroke_fill="black",
        )
        y += line_height

    # barrinha fina na cor do canal no topo — identidade visual discreta,
    # sem roubar espaço da imagem como a moldura antiga fazia.
    draw.rectangle([0, 0, THUMB_WIDTH, 10], fill=accent_rgb)

    img = img.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=92)
    return output_path
