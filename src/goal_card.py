"""Cartão do gol pro pós-jogo: minuto, autor, assistência e o desenho do
lance (de onde saiu o chute e onde a bola entrou) a partir da descrição da
ESPN (ver src/botafogo_data.py -> parse_goal). Imagem do gol da transmissão
não dá: é da emissora/clube e o Content ID derruba o vídeo (ou dá strike)
— o desenho é nosso e mostra o lance do jeito que a fonte descreve."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from .assemble import WATERMARK_FONT
from .thumbnail import HIGHLIGHT_YELLOW, _thumb_font

# de onde saiu o chute: (x, y) em fração da meia-quadra desenhada (gol em
# cima, x=0 esquerda, y=0 linha de fundo)
_ORIGEM_XY = {
    "na pequena área": (0.5, 0.10),
    "de pênalti": (0.5, 0.21),
    "do meio da área": (0.5, 0.25),
    "do lado esquerdo da área": (0.30, 0.22),
    "do lado direito da área": (0.70, 0.22),
    "sem ângulo, pela esquerda": (0.22, 0.07),
    "sem ângulo, pela direita": (0.78, 0.07),
    "de fora da área": (0.5, 0.45),
    "de muito longe": (0.5, 0.70),
}
# onde entrou: (x, y) dentro da boca do gol (0,0 = ângulo esquerdo)
_DESTINO_XY = {
    "no ângulo esquerdo": (0.10, 0.18),
    "no ângulo direito": (0.90, 0.18),
    "no canto esquerdo, rasteiro": (0.10, 0.82),
    "no canto direito, rasteiro": (0.90, 0.82),
    "no alto, no meio do gol": (0.5, 0.18),
    "no meio do gol": (0.5, 0.55),
}

# o desenho mostra só o terço de ataque (essa fração da meia-quadra, a
# partir da linha de fundo) — a meia-quadra inteira ficava miúda no vertical
DEPTH = 0.55

_BG = (14, 16, 15)
_PITCH = (30, 52, 38)
_LINE = (225, 230, 226)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(WATERMARK_FONT, size)


def _size(font) -> int:
    return int(getattr(font, "size", 40))


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: float, loader=_thumb_font):
    font = loader(size)
    while draw.textlength(text, font=font) > max_w and size > 24:
        size = int(size * 0.92)
        font = loader(size)
    return font


def _draw_pitch(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], gol: dict) -> None:
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = (y1 - y0) / DEPTH  # altura da meia-quadra inteira; o desenho mostra só o topo
    lw = max(w // 160, 3)
    draw.rectangle(box, fill=_PITCH, outline=_LINE, width=lw)
    draw.line((x0, y1, x1, y1), fill=_PITCH, width=lw)  # sem linha embaixo: o campo continua
    # grande área, pequena área, marca do pênalti, meia-lua, gol
    area_w, area_h = w * 0.62, h * 0.30
    draw.rectangle((x0 + (w - area_w) / 2, y0, x0 + (w + area_w) / 2, y0 + area_h), outline=_LINE, width=lw)
    small_w, small_h = w * 0.28, h * 0.10
    draw.rectangle((x0 + (w - small_w) / 2, y0, x0 + (w + small_w) / 2, y0 + small_h), outline=_LINE, width=lw)
    r = w * 0.012
    draw.ellipse((x0 + w / 2 - r, y0 + h * 0.21 - r, x0 + w / 2 + r, y0 + h * 0.21 + r), fill=_LINE)
    arc_r = w * 0.15
    draw.arc((x0 + w / 2 - arc_r, y0 + h * 0.21 - arc_r, x0 + w / 2 + arc_r, y0 + h * 0.21 + arc_r), 20, 160, fill=_LINE, width=lw)
    goal_w = w * 0.14
    draw.rectangle((x0 + (w - goal_w) / 2, y0 - h * 0.035, x0 + (w + goal_w) / 2, y0), outline=_LINE, width=lw)

    origem = _ORIGEM_XY.get(gol.get("de_onde") or "")
    if origem is None:
        return
    sx, sy = x0 + origem[0] * w, y0 + min(origem[1], DEPTH * 0.93) * h
    dest = _DESTINO_XY.get(gol.get("onde_entrou") or "", (0.5, 0.5))
    tx, ty = x0 + (w - goal_w) / 2 + dest[0] * goal_w, y0 - h * 0.015
    # trajetória tracejada até o gol
    steps = 14
    for k in range(0, steps, 2):
        a, b = k / steps, (k + 1) / steps
        draw.line((sx + (tx - sx) * a, sy + (ty - sy) * a, sx + (tx - sx) * b, sy + (ty - sy) * b),
                  fill=HIGHLIGHT_YELLOW, width=lw * 2)
    br = w * 0.035
    draw.ellipse((sx - br, sy - br, sx + br, sy + br), fill=(255, 255, 255), outline=(0, 0, 0), width=lw)


def _draw_goal_mouth(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], gol: dict) -> None:
    """A boca do gol de frente, com a bola onde entrou."""
    dest = _DESTINO_XY.get(gol.get("onde_entrou") or "")
    if dest is None:
        return
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    lw = max(w // 60, 4)
    net = (70, 76, 72)
    for k in range(1, 12):
        draw.line((x0 + w * k / 12, y0, x0 + w * k / 12, y1), fill=net, width=2)
    for k in range(1, 5):
        draw.line((x0, y0 + h * k / 5, x1, y0 + h * k / 5), fill=net, width=2)
    draw.line((x0, y1, x0, y0, x1, y0, x1, y1), fill=_LINE, width=lw, joint="curve")
    bx, by = x0 + dest[0] * w, y0 + dest[1] * h
    br = h * 0.14
    draw.ellipse((bx - br, by - br, bx + br, by + br), fill=HIGHLIGHT_YELLOW, outline=(0, 0, 0), width=3)


def goal_card(gol: dict, width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    vertical = height > width
    # vertical: texto em cima e desenho no meio, longe da legenda (terço de
    # baixo, ver assemble._caption_margin); horizontal: texto à esquerda
    text_x, text_w = (width * 0.07, width * 0.86) if vertical else (width * 0.06, width * 0.44)
    sem_desenho = bool(gol.get("gol_contra") or not gol.get("de_onde"))
    # sem desenho do lance (gol contra, lance sem descrição): texto mais
    # pro meio, sem buraco vazio embaixo
    y = height * ((0.22 if sem_desenho else 0.05) if vertical else (0.28 if sem_desenho else 0.12))

    if gol.get("gol_contra"):
        titulo = "GOL CONTRA"
    elif gol.get("penalti"):
        titulo = "GOL DE PÊNALTI"
    else:
        titulo = "GOOOL!"
    minuto = (gol.get("minuto") or "").strip()
    font = _fit(draw, titulo, int(min(width, height) * 0.16), text_w * (0.68 if minuto else 1))
    draw.text((text_x, y), titulo, font=font, fill=HIGHLIGHT_YELLOW)
    if minuto:
        mf = _thumb_font(int(_size(font) * 0.55))
        tw = draw.textlength(titulo, font=font)
        draw.text((text_x + tw + width * 0.025, y + _size(font) * 0.38), minuto, font=mf, fill=(255, 255, 255))
    y += _size(font) * 1.35

    autor = (gol.get("autor") or "").upper()
    if autor:
        font = _fit(draw, autor, int(min(width, height) * 0.10), text_w)
        draw.text((text_x, y), autor, font=font, fill=(255, 255, 255))
        y += _size(font) * 1.3

    detalhes = []
    if gol.get("assistencia"):
        detalhes.append(f"Assistência: {gol['assistencia']}")
    lance = ", ".join(v for v in (gol.get("jeito"), gol.get("de_onde")) if v and v != "de pênalti")
    if lance:
        detalhes.append(lance[0].upper() + lance[1:])
    if gol.get("onde_entrou"):
        detalhes.append(f"Entrou {gol['onde_entrou']}")
    if gol.get("placar_depois"):
        detalhes.append(gol["placar_depois"])
    for linha in detalhes:
        font = _fit(draw, linha, int(min(width, height) * 0.045), text_w, loader=_font)
        cor = HIGHLIGHT_YELLOW if linha == gol.get("placar_depois") else (210, 214, 211)
        draw.text((text_x, y), linha, font=font, fill=cor)
        y += _size(font) * 1.45

    if sem_desenho:
        pass
    elif vertical:
        # terço de ataque na proporção real do campo, acima da legenda
        # (terço de baixo); o "Entrou no..." do texto diz onde a bola entrou
        pw = int(width * 0.84)
        top = int(y + height * 0.035)
        bottom = min(int(height * 0.60), top + int(pw * 0.45))
        px = (width - pw) // 2
        _draw_pitch(draw, (px, top, px + pw, bottom), gol)
    else:
        top, bottom = int(height * 0.08), int(height * 0.50)
        pw = int(width * 0.40)
        px = int(width * 0.75 - pw / 2)
        _draw_pitch(draw, (px, top, px + pw, bottom), gol)
        mw = int(pw * 0.5)
        _draw_goal_mouth(draw, (int(width * 0.75 - mw / 2), int(height * 0.55), int(width * 0.75 + mw / 2), int(height * 0.55 + mw * 0.33)), gol)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
