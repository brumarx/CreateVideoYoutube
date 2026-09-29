"""Thumbnail: imagem gerada por IA (Pollinations, momento mais marcante da
história) OU foto real oficial (quando a cena é sobre uma pessoa real
nomeada — ver `real_photo_url`) + gancho curto (2-4 palavras, não o título
inteiro) escrito em cima da própria imagem, via Pillow. YouTube exige
recomendado 1280x720.

Estilo: ver docstring de `make_thumbnail` (texto condensado empilhado à
esquerda, imagem/pessoa à direita, borda na cor do canal).
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

ANTON_FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Anton-Regular.ttf"

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


def photo_scene_frame(url: str, width: int, height: int) -> bytes | None:
    """Foto real de notícia (paisagem, ~16:9) como quadro de cena: foto
    inteira nítida, encaixada pela largura/altura sem cortar nada, sobre a
    mesma foto desfocada/escurecida preenchendo o resto — no Short vertical,
    cortar uma foto 16:9 pro centro sobrava só um terço dela. PNG, ou None
    se não baixou."""
    photo = _fetch_real_photo(url)
    if photo is None:
        return None
    ratio = photo.width / photo.height
    cover_w, cover_h = (int(height * ratio), height) if ratio > width / height else (width, int(width / ratio))
    bg = photo.resize((cover_w, cover_h))
    left, top = (cover_w - width) // 2, (cover_h - height) // 2
    bg = bg.crop((left, top, left + width, top + height)).filter(ImageFilter.GaussianBlur(24))
    bg = ImageEnhance.Brightness(bg).enhance(0.5)
    fit_w, fit_h = (width, int(width / ratio)) if ratio > width / height else (int(height * ratio), height)
    bg.paste(photo.resize((fit_w, fit_h)), ((width - fit_w) // 2, (height - fit_h) // 2))
    out = io.BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


HIGHLIGHT_YELLOW = (255, 214, 0)
TEXT_BLOCK_RATIO = 0.56  # texto ocupa a metade esquerda; imagem/pessoa respira na direita
TEXT_TILT_DEG = 3  # inclinação leve — bloco reto parece slide, torto parece "chamada"


def _thumb_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Anton (condensada, pesada) é o visual das thumbnails que bombam hoje —
    cabe palavra GRANDE em pouca largura. Cai pra fonte do sistema se sumir."""
    if ANTON_FONT.exists():
        return ImageFont.truetype(str(ANTON_FONT), size)
    return _load_font(size)


def _split_hook(text: str) -> tuple[list[list[str]], set[int]]:
    """Quebra o gancho em 1-3 linhas curtas (estilo pôster empilhado) e
    devolve quais palavras vão em amarelo. `*palavra*` marca destaque
    explícito; sem marca, a última linha inteira fica em destaque."""
    raw_words = text.upper().split()
    # "R$ 2 MILHÕES" nunca quebra entre a moeda e o número
    i = 0
    while i < len(raw_words) - 1:
        if raw_words[i].strip("*") in {"R$", "US$", "€", "$"}:
            raw_words[i:i + 2] = [f"{raw_words[i]}\u00a0{raw_words[i + 1]}"]
        i += 1
    words, marked = [], set()
    for i, w in enumerate(raw_words):
        if w.startswith("*") or w.endswith("*"):
            marked.add(i)
        words.append(w.strip("*"))
    words = [w for w in words if w] or ["?"]
    n = len(words)
    if n <= 2:
        layout = [[w] for w in words] if n == 2 and sum(map(len, words)) > 9 else [words]
    elif n == 3:
        layout = [words[:1], words[1:]] if len(words[0]) >= len(" ".join(words[1:])) else [words[:2], words[2:]]
    elif n == 4:
        layout = [words[:2], words[2:]]
    else:
        per = -(-n // 3)
        layout = [words[i:i + per] for i in range(0, n, per)]
    if not marked:
        last_start = n - len(layout[-1])
        marked = set(range(last_start, n)) if len(layout) > 1 else {n - 1}
    return layout, marked


def _cover(img: Image.Image, width: int, height: int, anchor_x: float = 0.5) -> Image.Image:
    ratio = img.width / img.height
    new_w, new_h = (int(height * ratio), height) if ratio > width / height else (width, int(width / ratio))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = int((new_w - width) * anchor_x)
    top = (new_h - height) // 3  # rosto costuma ficar no terço de cima
    return img.crop((left, top, left + width, top + height))


def _background(photo: Image.Image) -> Image.Image:
    """Foto paisagem: tela cheia, puxada pra direita (o lado esquerdo vai
    ficar sob o texto). Retrato (foto oficial de político): fundo desfocado +
    pessoa nítida colada no lado DIREITO, grande, com sombra — em vez de
    centralizada atrás do texto como antes."""
    W, H = THUMB_WIDTH, THUMB_HEIGHT
    if photo.width / photo.height >= 1.2:
        return _cover(photo, W, H, anchor_x=0.7)
    bg = _cover(photo, W, H).filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    fg_h = int(H * 1.08)
    fg_w = int(fg_h * photo.width / photo.height)
    fg = photo.resize((fg_w, fg_h), Image.LANCZOS)
    x = W - fg_w - 30
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([x - 18, 0, x + fg_w + 18, H], fill=(0, 0, 0, 170))
    bg = bg.convert("RGBA")
    bg.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22)))
    bg = bg.convert("RGB")
    bg.paste(fg, (x, H - fg_h + int(H * 0.04)))
    return bg


def _render_text_block(hook_text: str, max_w: int, max_h: int) -> Image.Image:
    """Cada linha é esticada pra preencher a largura do bloco (linhas de
    tamanhos diferentes = cara de capa de revista/thumbnail profissional),
    com contorno preto + sombra dura deslocada."""
    layout, marked = _split_hook(hook_text)
    stroke, gap = 7, 6
    rendered: list[tuple[list[tuple[str, bool]], ImageFont.FreeTypeFont | ImageFont.ImageFont]] = []
    idx = 0
    for line in layout:
        parts = [(w, i in marked) for i, w in enumerate(line, start=idx)]
        idx += len(line)
        text = " ".join(line)
        size = 210  # teto: palavra curta sozinha na linha não vira um muro
        font = _thumb_font(size)
        while size > 60 and font.getlength(text) + 2 * stroke > max_w:
            size -= 6
            font = _thumb_font(size)
        rendered.append((parts, font))

    def heights(items):
        return [f.getbbox("ÁG", stroke_width=stroke)[3] - f.getbbox("ÁG", stroke_width=stroke)[1] for _, f in items]

    total = sum(heights(rendered)) + gap * (len(rendered) - 1)
    if total > max_h:  # linha curta ("É") estourando altura — encolhe tudo proporcional
        scale = max_h / total
        rendered = [(p, _thumb_font(max(40, int(f.size * scale)))) for p, f in rendered]
        total = sum(heights(rendered)) + gap * (len(rendered) - 1)

    shadow_off = 9
    block = Image.new("RGBA", (max_w + 40, total + 40), (0, 0, 0, 0))
    shadow = Image.new("RGBA", block.size, (0, 0, 0, 0))
    draw, sdraw = ImageDraw.Draw(block), ImageDraw.Draw(shadow)
    y = 10
    for (parts, font), h in zip(rendered, heights(rendered)):
        top_off = font.getbbox("ÁG", stroke_width=stroke)[1]
        x = 10 + stroke
        for word, hl in parts:
            fill = HIGHLIGHT_YELLOW if hl else (255, 255, 255)
            sdraw.text((x + shadow_off, y - top_off + shadow_off), word, font=font, fill=(0, 0, 0, 200),
                       stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
            draw.text((x, y - top_off), word, font=font, fill=fill, stroke_width=stroke, stroke_fill="black")
            x += font.getlength(word + " ")
        y += h + gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(3))
    shadow.alpha_composite(block)
    return shadow


def make_thumbnail(
    prompt: str,
    hook_text: str,
    output_path: Path,
    accent: str = "#ffffff",
    real_photo_url: str | None = None,
) -> Path:
    """`hook_text` deve ser curto (2-4 palavras); `*palavra*` marca o que
    vai em amarelo. `real_photo_url` (foto OFICIAL de deputado/senador/
    magistrado, ver src/politica_data.py, ou foto da notícia no Botafogo)
    usa a foto de verdade em vez de pedir pra IA "inventar" o rosto.

    Layout "thumbnail de 2026": texto gigante em fonte condensada no lado
    esquerdo (empilhado, inclinado, palavra-chave em amarelo, sombra dura),
    imagem/pessoa em destaque no lado direito, cor bem saturada e borda
    na cor do canal."""
    img = None
    if real_photo_url:
        photo = _fetch_real_photo(real_photo_url)
        if photo:
            img = _background(photo)
    if img is None:
        raw = generate_image(prompt, width=THUMB_WIDTH, height=THUMB_HEIGHT)
        img = _background(Image.open(io.BytesIO(raw)).convert("RGB"))

    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.45)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=3))
    img = img.convert("RGBA")

    # escurece só o lado do texto (gradiente horizontal) — o resto da imagem
    # fica viva, em vez de um véu escuro no rodapé inteiro.
    W, H = THUMB_WIDTH, THUMB_HEIGHT
    grad = Image.new("L", (W, 1))
    fade_w = int(W * 0.72)
    for x in range(W):
        grad.putpixel((x, 0), int(215 * max(0.0, 1 - x / fade_w) ** 1.3))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shade.putalpha(grad.resize((W, H)))
    img.alpha_composite(shade)

    block_w = int(W * TEXT_BLOCK_RATIO)
    block = _render_text_block(hook_text, block_w, int(H * 0.78))
    block = block.rotate(TEXT_TILT_DEG, resample=Image.BICUBIC, expand=True)
    bx = 26
    by = max(20, (H - block.height) // 2)
    img.alpha_composite(block, (bx, by))

    # borda na cor do canal (identidade visual em toda a lista de vídeos)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, H - 1], outline=_hex_to_rgb(accent) + (255,), width=12)

    img = img.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=92)
    return output_path
