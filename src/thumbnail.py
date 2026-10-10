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
import re
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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


def title_card(label: str, width: int, height: int, accent: str = "#ffffff") -> bytes:
    """Último recurso de visual de cena: fundo escuro com leve gradiente na
    cor do canal e o nome dele — nunca sai do contexto. Só entra quando
    nada foi aprovado (sem foto da matéria, banco/IA recusados ou fora do
    ar) e não há visual anterior pra repetir (job 242 quebrou assim)."""
    r, g, b = _hex_to_rgb(accent)
    img = Image.new("RGB", (width, height), (12, 12, 14))
    glow = Image.new("RGB", (width, height), (r // 4, g // 4, b // 4))
    mask = Image.radial_gradient("L").resize((width, height)).point(lambda v: 255 - v)
    img = Image.composite(glow, img, mask)
    draw = ImageDraw.Draw(img)
    size = max(height // 9, 32)
    font = _thumb_font(size)
    w = draw.textlength(label, font=font)
    # altura/9 no vertical (1080x1920) estoura a largura: "Fractal Curioso"
    # saía cortado nas bordas (Short ZEEU6fhq0KY)
    while w > width * 0.86 and size > 32:
        size = int(size * 0.9)
        font = _thumb_font(size)
        w = draw.textlength(label, font=font)
    draw.text(((width - w) / 2, height * 0.42), label, font=font, fill=(235, 235, 235))
    out = io.BytesIO()
    img.save(out, format="PNG")
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


_NO_PLURAL = {"MI", "MIL", "BI", "TRI", "KM", "KG", "MB", "GB", "HP", "X", "A", "E", "O", "DE", "EM"}


def _plural(word: str) -> str | None:
    if word.endswith("ÃO"):
        return word[:-2] + "ÕES"
    if word.endswith("AL"):
        return word[:-2] + "AIS"
    if word[-1] in "AEIOUÁÉÍÓÚÊÔ":
        return word + "S"
    if word[-1] in "RZ":
        return word + "ES"
    return None


def fix_number_agreement(text: str) -> str:
    """ "9 MORTE" -> "9 MORTES": o LLM às vezes erra a concordância do
    número na thumbnail (publicado assim no curiosidades, 10/10)."""
    def swap(m: re.Match) -> str:
        num, star, word, tail = m[1], m[2], m[3], m[4]
        if int(num) < 2 or word in _NO_PLURAL or word.endswith("S") or len(word) < 3:
            return m[0]
        plural = _plural(word)
        return f"{num} {star}{plural}{tail}" if plural else m[0]

    return re.sub(r"(?<![\d,.])(\d+) (\*?)([A-ZÀ-Ý]+)(\*?[?!]*)(?=\s|$)", swap, text)


_LINE_END_WEAK = {"E", "O", "A", "OS", "AS", "DE", "DO", "DA", "EM", "NO", "NA", "UM", "UMA", "SEM", "COM", "PRA", "POR", "QUE", "SE"}


def _split_hook(text: str) -> tuple[list[list[str]], set[int]]:
    """Quebra o gancho em 1-3 linhas curtas (estilo pôster empilhado) e
    devolve quais palavras vão em amarelo. `*palavra*` marca destaque
    explícito; sem marca, a última linha inteira fica em destaque."""
    raw_words = fix_number_agreement(text.upper()).split()
    # "R$ 2 MILHÕES" / "9 MORTES" nunca quebram entre moeda, número e o que
    # ele conta ("R$ 50" numa linha e "MI" na outra — teste de 10/10)
    i = 0
    while i < len(raw_words) - 1:
        if raw_words[i].strip("*") in {"R$", "US$", "€", "$"}:
            raw_words[i:i + 2] = [f"{raw_words[i]}\u00a0{raw_words[i + 1]}"]
            continue
        if re.search(r"\d", raw_words[i]) and re.fullmatch(r"\*?[A-ZÀ-Ý%]+\*?[?!]*", raw_words[i + 1]):
            raw_words[i:i + 2] = [f"{raw_words[i]}\u00a0{raw_words[i + 1]}"]
        i += 1
    words, marked = [], set()
    for i, w in enumerate(raw_words):
        # "*MILHÕES*?" também é destaque — o "*" saía impresso na thumbnail
        if "*" in w:
            marked.add(i)
        words.append(w.replace("*", ""))
    words = [w for w in words if w] or ["?"]
    n = len(words)
    if n <= 2:
        layout = [[w] for w in words] if n == 2 and sum(map(len, words)) > 9 else [words]
    elif n <= 4:
        # 2 linhas: a quebra mais equilibrada que não deixa palavra solta
        # no fim da linha ("9 MORTES SEM / EXPLICAÇÃO" -> "9 MORTES / SEM
        # EXPLICAÇÃO")
        def score(k: int) -> int:
            top, bottom = " ".join(words[:k]), " ".join(words[k:])
            return max(len(top), len(bottom)) + (100 if words[k - 1] in _LINE_END_WEAK else 0)

        k = min(range(1, n), key=score)
        layout = [words[:k], words[k:]]
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


_REMBG_SESSIONS: list | None = None


def _subject_mask(photo: Image.Image) -> Image.Image | None:
    """Máscara do sujeito (pessoa/grupo/objeto principal) via rembg. União
    de 2 modelos: u2net pega cabelo/bordas finas mas perde roupa escura
    (terno de político sumia), isnet pega o corpo inteiro mas come cabelo
    crespo — juntos cobrem um o buraco do outro. ~15s no Raspberry.
    None se rembg não estiver instalado ou falhar."""
    global _REMBG_SESSIONS
    try:
        from rembg import new_session, remove

        if _REMBG_SESSIONS is None:
            _REMBG_SESSIONS = [new_session("u2net"), new_session("isnet-general-use")]
        masks = [remove(photo, session=s, only_mask=True).convert("L") for s in _REMBG_SESSIONS]
    except Exception as exc:  # noqa: BLE001 — recorte é bônus, layout sem ele continua bom
        log.warning("recorte do sujeito indisponível: %s", exc)
        return None
    mask = masks[0]
    for m in masks[1:]:
        mask = ImageChops.lighter(mask, m)
    return mask.point(lambda v: 255 if v > 110 else int(v * 255 / 110))


def _radial_light(size: tuple[int, int], center: tuple[int, int], radius: int, rgb: tuple[int, int, int], strength: int) -> Image.Image:
    """Mancha de luz suave (elipse desfocada) — separa o sujeito do fundo."""
    light = Image.new("L", size, 0)
    cx, cy = center
    ImageDraw.Draw(light).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=strength)
    light = light.filter(ImageFilter.GaussianBlur(radius // 2))
    layer = Image.new("RGBA", size, rgb + (0,))
    layer.putalpha(light)
    return layer


def _cutout_composite(photo: Image.Image, accent_rgb: tuple[int, int, int]) -> Image.Image | None:
    """Sujeito recortado, GRANDE no lado direito (encosta no rodapé quando
    o corpo continua), com luz de contorno na cor do canal, sombra forte e
    uma luz atrás dele sobre a própria foto desfocada e escurecida. O
    contorno branco de "figurinha" que existia antes era o que mais deixava
    a thumbnail com cara de amadora (feedback do dono, 10/10). None (cai pro
    layout de foto inteira) quando o recorte não acha um sujeito claro —
    paisagem, cenário, recorte que pegou quase tudo."""
    W, H = THUMB_WIDTH, THUMB_HEIGHT
    work = photo.copy()
    work.thumbnail((1024, 1024), Image.LANCZOS)
    mask = _subject_mask(work)
    if mask is None:
        return None
    bbox = mask.point(lambda v: 255 if v > 128 else 0).getbbox()
    if not bbox:
        return None
    coverage = sum(mask.point(lambda v: 1 if v > 128 else 0).getdata()) / (mask.width * mask.height)
    wide = (bbox[2] - bbox[0]) > (bbox[3] - bbox[1]) * 1.3  # grupo abraçado: recortado fica miúdo
    if wide or not 0.05 <= coverage <= 0.75 or (bbox[3] - bbox[1]) < work.height * 0.4:
        log.info("recorte descartado (cobertura %.2f, bbox %s) — foto inteira", coverage, bbox)
        return None

    subject = work.convert("RGBA")
    subject.putalpha(mask)
    subject = subject.crop(bbox)
    # sujeito inteiro cortado na borda de baixo da foto (corpo continua)
    # pode passar da altura e "sair" pelo rodapé — parece mais perto
    touches_bottom = bbox[3] >= work.height - 4
    scale = min(H * (1.08 if touches_bottom else 0.92) / subject.height, W * 0.62 / subject.width)
    subject = subject.resize((int(subject.width * scale), int(subject.height * scale)), Image.LANCZOS)
    subject = ImageEnhance.Contrast(subject).enhance(1.12)
    alpha = subject.getchannel("A")

    pad = 60
    canvas_alpha = Image.new("L", (subject.width + 2 * pad, subject.height + 2 * pad), 0)
    canvas_alpha.paste(alpha, (pad, pad))
    rim = canvas_alpha.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(7))
    shadow = canvas_alpha.filter(ImageFilter.GaussianBlur(26))

    bg = _cover(photo, W, H).filter(ImageFilter.GaussianBlur(24))
    bg = ImageEnhance.Brightness(bg).enhance(0.38).convert("RGBA")
    x = W - subject.width - 30 - pad
    y = (H - subject.height - pad + int(H * 0.06)) if touches_bottom else (H - subject.height) // 2 - pad
    cx, cy = x + pad + subject.width // 2, y + pad + subject.height // 2
    bg.alpha_composite(_radial_light((W, H), (cx, cy), int(max(subject.width, subject.height) * 0.62), accent_rgb, 150))
    bg.paste(Image.new("RGBA", canvas_alpha.size, (0, 0, 0, 255)), (x - 22, y + 14), shadow.point(lambda v: int(v * 0.8)))
    rim_rgb = tuple(min(255, c + 70) for c in accent_rgb)
    bg.paste(Image.new("RGBA", canvas_alpha.size, rim_rgb + (255,)), (x, y), rim.point(lambda v: int(v * 0.55)))
    bg.alpha_composite(subject, (x + pad, y + pad))
    return bg.convert("RGB")


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
        while size > 60 and font.getlength(text) + 2 * stroke + 4 * 14 > max_w:  # 14 = folga da faixa de destaque
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

    # palavra de destaque: faixa amarela sólida com letra preta (o padrão
    # dos canais grandes) — letra amarela sobre fundo escuro sumia no feed
    shadow_off, box_pad = 9, 14
    block = Image.new("RGBA", (max_w + 40 + 2 * box_pad, total + 40 + 2 * box_pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", block.size, (0, 0, 0, 0))
    draw, sdraw = ImageDraw.Draw(block), ImageDraw.Draw(shadow)
    y = 10 + box_pad
    for (parts, font), h in zip(rendered, heights(rendered)):
        top_off = font.getbbox("ÁG", stroke_width=stroke)[1]
        x = 10 + stroke + box_pad
        space = font.getlength(" ")
        i = 0
        while i < len(parts):
            # palavras destacadas seguidas dividem a mesma faixa
            j = i
            while j < len(parts) and parts[j][1] == parts[i][1]:
                j += 1
            text = " ".join(w for w, _ in parts[i:j])
            w_px = font.getlength(text)
            if parts[i][1]:
                box = [x - box_pad, y - 4, x + w_px + box_pad, y + h + 4]
                sdraw.rectangle([box[0] + shadow_off, box[1] + shadow_off, box[2] + shadow_off, box[3] + shadow_off], fill=(0, 0, 0, 200))
                draw.rectangle(box, fill=HIGHLIGHT_YELLOW + (255,))
                draw.text((x, y - top_off + stroke), text, font=font, fill=(0, 0, 0))
            else:
                sdraw.text((x + shadow_off, y - top_off + shadow_off), text, font=font, fill=(0, 0, 0, 200),
                           stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
                draw.text((x, y - top_off), text, font=font, fill=(255, 255, 255), stroke_width=stroke, stroke_fill="black")
            x += w_px + space + (2 * box_pad if parts[i][1] else 0)
            i = j
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
    fallback_frame: Path | None = None,
) -> Path:
    """`hook_text` deve ser curto (2-4 palavras); `*palavra*` marca o que
    vai em amarelo. `real_photo_url` (foto OFICIAL de deputado/senador/
    magistrado, ver src/politica_data.py, ou foto da notícia no Botafogo)
    usa a foto de verdade em vez de pedir pra IA "inventar" o rosto.

    Layout "thumbnail de 2026": texto gigante em fonte condensada no lado
    esquerdo (empilhado, inclinado, palavra-chave em amarelo, sombra dura),
    imagem/pessoa em destaque no lado direito, cor bem saturada e borda
    na cor do canal."""
    photo = _fetch_real_photo(real_photo_url) if real_photo_url else None
    if photo is None:
        try:
            raw = generate_image(prompt, width=THUMB_WIDTH, height=THUMB_HEIGHT)
            photo = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            # Pollinations fora (402/500) derrubava o job DEPOIS do vídeo
            # pronto — em 03/10 os 3 vídeos do dia morreram aqui. Um frame
            # do próprio vídeo é melhor que nenhum vídeo publicado.
            if fallback_frame is None or not fallback_frame.exists():
                raise
            photo = Image.open(fallback_frame).convert("RGB")
    img = _cutout_composite(photo, _hex_to_rgb(accent)) or _background(photo)

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

    # vinheta: escurece os cantos e puxa o olho pro centro. A moldura na cor
    # do canal que ficava aqui era o que mais dava cara de amador (10/10)
    vignette = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vignette).ellipse([-W * 0.15, -H * 0.25, W * 1.15, H * 1.25], fill=255)
    vignette = ImageOps.invert(vignette.filter(ImageFilter.GaussianBlur(120))).point(lambda v: int(v * 0.7))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dark.putalpha(vignette)
    img.alpha_composite(dark)

    img = img.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=92)
    return output_path
