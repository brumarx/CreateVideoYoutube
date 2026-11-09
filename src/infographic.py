"""Cenas desenhadas por código, no estilo dos canais de mapa/história que
viralizam (ex.: "Por que a Coreia do Norte se apaga à noite?", 530 mil views
em 4 dias, 03/11): satélite real da NASA, país em relevo de papel com
sombra, ícones de um só estilo, cidade com brilho, ano em serifa com régua.

Custo zero e sem chave:
- base: NASA GIBS (WMS, domínio público) — Blue Marble (dia) e VIIRS Black
  Marble (noite);
- fronteiras e cidades: Natural Earth 1:50m (domínio público);
- ícones: OpenMoji (CC BY-SA 4.0 — crédito na descrição, ver CREDIT);
- fontes: Fira Sans e Cormorant Garamond (OFL, em assets/fonts).

Tudo baixado sob demanda e guardado em data/cache/ (fora do git).

O roteiro pede o gráfico por cena no campo "grafico" (ver
src/script_gen.py): um dict ou uma lista de até 3 "batidas", cada uma com
"palavra" (a palavra da narração em que ela entra). Tipos:

  {"tipo": "mapa", "lugares": ["North Korea", "South Korea"], "noite": false,
   "destaques": [{"pais": "North Korea", "cor": "vermelho", "rotulo": "Coreia do Norte",
                  "icone": "🏭", "quantidade": 4}],
   "cidade": "Pyongyang", "ano": "1950", "ano_rotulo": "o plano"}
  {"tipo": "comparacao", "a": 1, "a_rotulo": "sul-coreano", "b": 14,
   "b_rotulo": "norte-coreanos", "icone": "🧍"}
  {"tipo": "numero", "valor": "90%", "rotulo": "da eletricidade da Coreia"}
  {"tipo": "ano", "de": "1950", "ate": "1964", "rotulo": "o plano"}
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

log = logging.getLogger("infographic")

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "infographic"
FONTS = ROOT / "assets" / "fonts"
LABEL_FONT = FONTS / "FiraSans-Bold.ttf"
SERIF_FONT = FONTS / "CormorantGaramond.ttf"

CREDIT = ("Mapas: NASA Blue Marble / Black Marble (GIBS) e Natural Earth. "
          "Ícones: OpenMoji (CC BY-SA 4.0).")

GIBS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
LAYER_DAY = "BlueMarble_ShadedRelief_Bathymetry"
LAYER_NIGHT = "VIIRS_Black_Marble"
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
OPENMOJI = "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/color/618x618/{}.png"
FLAG = "https://flagcdn.com/w640/{}.png"  # bandeiras nacionais: domínio público

# paleta curta e fixa: cada cor tem um papel e nunca muda (o que dá
# unidade ao vídeo do começo ao fim)
COLORS = {
    "vermelho": (200, 49, 47), "azul": (58, 86, 181), "cinza": (88, 93, 102),
    "dourado": (217, 164, 65), "verde": (63, 143, 79), "roxo": (122, 72, 170),
    "laranja": (222, 112, 40),
}
HIGHLIGHT = (255, 212, 0)  # mesmo amarelo da palavra de destaque da legenda
SEA_TINT = np.array([0.42, 0.46, 0.92])  # mar puxado pro azul-marinho

# supersampling: o quadro sai 1,5x o vídeo, pro zoom lento não borrar
SCALE = 1.5
TYPES = {"mapa", "comparacao", "numero", "ano"}
MAX_BEATS = 3


def _get(url: str, dest: Path, timeout: int = 60) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "faceless-video-bot/1.0"})
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("download falhou (%s): %s", url, exc)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# ---------------------------------------------------------------- geografia

def _parts(geometry: dict) -> list[list[tuple[float, float]]]:
    """Anel externo de cada polígono (buracos não importam pro desenho)."""
    if geometry["type"] == "Polygon":
        return [[tuple(p) for p in geometry["coordinates"][0]]]
    if geometry["type"] == "MultiPolygon":
        return [[tuple(p) for p in poly[0]] for poly in geometry["coordinates"]]
    return []


def _area_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs, ys = zip(*ring)
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


@lru_cache(maxsize=1)
def _countries() -> dict[str, dict]:
    path = _get(NE + "ne_50m_admin_0_countries.geojson", CACHE / "ne_50m_admin_0_countries.geojson")
    if path is None:
        return {}
    index: dict[str, dict] = {}
    by_feature = []
    for feat in json.loads(path.read_text())["features"]:
        props = feat["properties"]
        parts = []
        for ring in _parts(feat["geometry"]):
            area, cx, cy = _area_centroid(ring)
            parts.append({"ring": ring, "area": area, "c": (cx, cy)})
        if not parts:
            continue
        main = max(parts, key=lambda p: p["area"])
        # território ultramarino (Guiana Francesa, Alasca do jeito que vier)
        # não entra no enquadramento nem no desenho do país
        kept = [p for p in parts if p["area"] >= main["area"] * 0.004
                and math.dist(p["c"], main["c"]) < 25]
        a2 = next((props.get(k) for k in ("ISO_A2_EH", "ISO_A2", "WB_A2") if props.get(k) and props.get(k) != "-99"), "")
        country = {"name": props.get("NAME_PT") or props.get("NAME"), "parts": kept, "main": main, "a2": a2.lower()}
        by_feature.append((props, country))
    # nome próprio do país primeiro; "soberano" por último — senão "United
    # Kingdom" caía nas Malvinas (território britânico) e o rótulo ia pro sul
    for keys in (("ISO_A3", "ADM0_A3", "NAME", "NAME_LONG", "NAME_EN", "NAME_PT", "FORMAL_EN"), ("SOVEREIGNT",)):
        for props, country in by_feature:
            for key in keys:
                value = props.get(key)
                if value and value != "-99":
                    index.setdefault(_fold(value), country)
    for alias, target in _ALIASES.items():
        if _fold(target) in index:
            index.setdefault(_fold(alias), index[_fold(target)])
    return index


# como o roteiro/narração costuma chamar (e o Natural Earth não)
_ALIASES = {
    "Inglaterra": "United Kingdom", "England": "United Kingdom", "Reino Unido": "United Kingdom",
    "Grã-Bretanha": "United Kingdom", "Great Britain": "United Kingdom", "UK": "United Kingdom",
    "Estados Unidos": "United States of America", "EUA": "United States of America", "USA": "United States of America",
    "United States": "United States of America", "Holanda": "Netherlands", "Países Baixos": "Netherlands",
    "Coreia do Norte": "North Korea", "Coreia do Sul": "South Korea", "União Soviética": "Russia",
    "URSS": "Russia", "Soviet Union": "Russia", "Rússia": "Russia", "Tchéquia": "Czechia",
}
_LANGUAGE_STEMS = {"portugues", "ingles", "espanhol", "frances", "alema", "alemao", "alemaes", "italian", "chines",
                   "japones", "russ", "holandes", "grego", "grega", "turc", "sueco", "sueca", "polones",
                   "dinamarques", "finlandes", "norueg", "coreano", "indonesi"}
# adjetivo pátrio na narração ("tropas chilenas") também é o país — senão o
# mapa automático pegava o outro país da frase (job 703 mostrou o Peru)
_DEMONYMS = {
    "chilen": "Chile", "bolivian": "Bolivia", "peruan": "Peru", "argentin": "Argentina", "brasileir": "Brazil",
    "paraguai": "Paraguay", "uruguai": "Uruguay", "colombian": "Colombia", "venezuelan": "Venezuela",
    "equatorian": "Ecuador", "mexican": "Mexico", "norte-american": "United States of America",
    "estadunidense": "United States of America", "canadense": "Canada", "cuban": "Cuba",
    "portugues": "Portugal", "espanhol": "Spain", "frances": "France", "ingles": "United Kingdom",
    "britanic": "United Kingdom", "alema": "Germany", "alemao": "Germany", "alemaes": "Germany",
    "italian": "Italy", "holandes": "Netherlands", "russ": "Russia", "sovietic": "Russia", "ucranian": "Ukraine",
    "polones": "Poland", "sueco": "Sweden", "sueca": "Sweden", "norueg": "Norway", "dinamarques": "Denmark",
    "finlandes": "Finland", "island": "Iceland", "suic": "Switzerland", "grego": "Greece", "grega": "Greece",
    "turc": "Turkey", "egipci": "Egypt", "israelense": "Israel", "iranian": "Iran", "iraquian": "Iraq",
    "saudita": "Saudi Arabia", "chines": "China", "japones": "Japan", "coreano": "South Korea",
    "norte-coreano": "North Korea", "norte coreano": "North Korea", "sul-coreano": "South Korea",
    "sul coreano": "South Korea", "indian": "India", "australian": "Australia", "sul-african": "South Africa",
    "nigerian": "Nigeria", "marroquin": "Morocco", "vietnamit": "Vietnam", "tailandes": "Thailand",
    "indonesi": "Indonesia", "filipin": "Philippines", "singapurian": "Singapore", "catarian": "Qatar",
}


def find_country(name: str) -> dict | None:
    if not name:
        return None
    return _countries().get(_fold(name))


@lru_cache(maxsize=1)
def _cities() -> dict[str, tuple[float, float, str, int]]:
    path = _get(NE + "ne_50m_populated_places.geojson", CACHE / "ne_50m_populated_places.geojson")
    if path is None:
        return {}
    out: dict[str, tuple[float, float, str, int]] = {}
    feats = json.loads(path.read_text())["features"]
    for f in feats:
        f["properties"] = {k.lower(): v for k, v in f["properties"].items()}
    feats.sort(key=lambda f: -(f["properties"].get("pop_max") or 0))
    for feat in feats:
        lon, lat = feat["geometry"]["coordinates"][:2]
        props = feat["properties"]
        shown = props.get("name_pt") or props.get("name") or ""
        for key in ("name", "nameascii", "name_en", "name_pt"):
            value = props.get(key)
            if value:
                out.setdefault(_fold(value), (lon, lat, shown, int(props.get("pop_max") or 0)))
    return out


def find_city(name: str) -> tuple[float, float] | None:
    hit = _cities().get(_fold(name)) if name else None
    return (hit[0], hit[1]) if hit else None


def city_label(name: str) -> str:
    """Nome da cidade em português ("Lisboa", não "Lisbon")."""
    hit = _cities().get(_fold(name)) if name else None
    return hit[2] if hit and hit[2] else name


class View:
    """Enquadramento equirretangular corrigido pela latitude: lon/lat -> px."""

    def __init__(self, lon0: float, lat_top: float, wdeg: float, hdeg: float, w: int, h: int):
        self.lon0, self.lat_top, self.wdeg, self.hdeg, self.w, self.h = lon0, lat_top, wdeg, hdeg, w, h

    def px(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon0) / self.wdeg * self.w, (self.lat_top - lat) / self.hdeg * self.h

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.lat_top - self.hdeg, self.lon0, self.lat_top, self.lon0 + self.wdeg


def frame_view(countries: list[dict], w: int, h: int, focus: float = 0.42, pad: float = 1.45,
               points: list[tuple[float, float]] | None = None) -> View:
    """Enquadra os países pedidos no quadro vertical, com o assunto no terço
    de cima (a legenda fica embaixo)."""
    pts = [p for c in countries for part in c["parts"] for p in part["ring"]] + list(points or [])
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    lon_min, lon_max, lat_min, lat_max = min(lons), max(lons), min(lats), max(lats)
    clon, clat = (lon_min + lon_max) / 2, (lat_min + lat_max) / 2
    k = max(math.cos(math.radians(clat)), 0.2)
    aspect = w / h
    hdeg = max((lat_max - lat_min) * pad, (lon_max - lon_min) * k * pad / aspect, 3.0)
    hdeg = min(hdeg, 150.0)
    wdeg = hdeg * aspect / k
    lat_top = min(clat + focus * hdeg, 85.0)
    if lat_top - hdeg < -85.0:
        lat_top = -85.0 + hdeg
    return View(clon - wdeg / 2, lat_top, wdeg, hdeg, w, h)


# ---------------------------------------------------------------- base

def _gibs(view: View, night: bool) -> Image.Image | None:
    lat0, lon0, lat1, lon1 = view.bbox
    layer = LAYER_NIGHT if night else LAYER_DAY
    key = f"{layer}_{lat0:.3f}_{lon0:.3f}_{lat1:.3f}_{lon1:.3f}_{view.w}x{view.h}.jpg"
    url = (f"{GIBS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS=EPSG:4326"
           f"&BBOX={lat0},{lon0},{lat1},{lon1}&WIDTH={view.w}&HEIGHT={view.h}&FORMAT=image/jpeg")
    path = _get(url, CACHE / "gibs" / key, timeout=90)
    if path is None:
        return None
    try:
        img = Image.open(path).convert("RGB")
    except OSError:
        path.unlink(missing_ok=True)
        return None
    if img.size != (view.w, view.h):
        path.unlink(missing_ok=True)
        return None
    return img


def _vignette(w: int, h: int, strength: float) -> np.ndarray:
    y, x = np.ogrid[:h, :w]
    d = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h * 0.45) / (h / 2)) ** 2)
    return (1 - strength * np.clip(d - 0.35, 0, 1) ** 1.5)[..., None]


def grade(img: Image.Image, night: bool) -> Image.Image:
    """Cor de cinema: mar azul-marinho, terra mais escura e saturada,
    vinheta. À noite, luzes quentes e o escuro puxado pro azul."""
    a = np.asarray(img).astype(np.float32) / 255
    if night:
        lum = a.mean(axis=2, keepdims=True)
        warm = np.array([1.0, 0.86, 0.58])
        a = a * 0.6 + np.clip(lum * 1.9, 0, 1) ** 1.25 * warm * 1.0
        a = a + np.array([0.035, 0.045, 0.11]) * (1 - np.clip(lum * 4, 0, 1))
    else:
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        water = np.clip((b - np.maximum(r, g) - 0.03) * 8, 0, 1)[..., None]
        land = a * 0.88
        gray = land.mean(axis=2, keepdims=True)
        land = np.clip(gray + (land - gray) * 1.15, 0, 1)
        sea = a * SEA_TINT * 0.8 + np.array([0.05, 0.04, 0.12])
        a = land * (1 - water) + sea * water
    a = a * _vignette(img.width, img.height, 0.55)
    return Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))


def _fallback_base(w: int, h: int) -> Image.Image:
    """Sem satélite (GIBS fora): degradê azul-noite com granulação."""
    y = np.linspace(0, 1, h)[:, None, None]
    top, bottom = np.array([20, 26, 58]), np.array([6, 8, 22])
    a = top * (1 - y) + bottom * y + np.random.default_rng(7).normal(0, 3, (h, w, 1))
    a = np.broadcast_to(a, (h, w, 3))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


# ---------------------------------------------------------------- desenho

def _font(path: Path, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(path), size)
    if variation:
        try:
            font.set_variation_by_name(variation)
        except (OSError, ValueError):
            pass
    return font


# algarismo "alinhado" (1950, não o antigo com perninha da Cormorant) — precisa do libraqm
_FEATURES = ["lnum"] if features.check("raqm") else None


def _clean(text: str) -> str:
    """Hífen/espaço "especiais" que o LLM manda (U+2011 em "pós‑guerra") não
    existem na fonte e saíam como quadradinho."""
    return re.sub(r"[\u2010\u2011\u2012\u2013\u2212]", "-", str(text)).replace("\u00a0", " ").replace("\u202f", " ")


def _shadowed_text(img: Image.Image, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont,
                   fill=(255, 255, 255), anchor: str = "mm", shadow: int = 10) -> None:
    text = _clean(text)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0] + 3, xy[1] + 5), text, font=font, fill=(0, 0, 0, 200), anchor=anchor,
                               features=_FEATURES)
    layer = layer.filter(ImageFilter.GaussianBlur(shadow / 2))
    img.alpha_composite(layer)
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill, anchor=anchor, features=_FEATURES,
                             stroke_width=max(font.size // 22, 2), stroke_fill=(0, 0, 0, 160))


def _glow(img: Image.Image, center: tuple[float, float], radius: float, color, alpha: int = 170) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    x, y = center
    ImageDraw.Draw(layer).ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius / 2.2)))


def flag(a2: str) -> Image.Image | None:
    if not re.fullmatch(r"[a-z]{2}", a2 or ""):
        return None
    path = _get(FLAG.format(a2), CACHE / "flags" / f"{a2}.png", timeout=30)
    if path is None:
        return None
    try:
        return Image.open(path).convert("RGB")
    except OSError:
        path.unlink(missing_ok=True)
        return None


def _paper_country(img: Image.Image, view: View, country: dict, color, lift: float,
                   pattern: Image.Image | None = None) -> None:
    """País como recorte de papel: sombra projetada, preenchimento com leve
    degradê e granulação, borda clara."""
    w, h = img.size
    rings = [[view.px(*p) for p in part["ring"]] for part in country["parts"]]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    for r in rings:
        md.polygon(r, fill=255)
    off = int(lift)
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(ImageChops.offset(mask, off // 2, off).point(lambda v: v * 0.62))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(lift * 0.9)))

    ys = [p[1] for r in rings for p in r]
    top, bottom = max(min(ys), 0), min(max(ys), h)
    grad = np.linspace(1.12, 0.88, max(int(bottom - top), 1))
    fill = np.empty((h, w, 3), np.float32)
    if pattern is not None:
        # bandeira esticada por cima do país inteiro (França tricolor,
        # Reino Unido com a Union Jack) — como no Short de Dom João
        xs = [p[0] for r in rings for p in r]
        left, right = max(min(xs), 0), min(max(xs), w)
        bw, bh = max(int(right - left), 1), max(int(bottom - top), 1)
        pat = pattern.resize((bw, bh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (w, h), color)
        canvas.paste(pat, (int(left), int(top)))
        fill[:] = np.asarray(canvas, np.float32) * 0.92
    else:
        fill[:] = color
    band = np.ones(h, np.float32)
    band[int(top):int(top) + len(grad)] = grad[: h - int(top)]
    fill *= band[:, None, None]
    fill += np.random.default_rng(len(rings)).normal(0, 5, (h, w, 1))
    fill_img = Image.fromarray(np.clip(fill, 0, 255).astype(np.uint8)).convert("RGBA")
    fill_img.putalpha(mask.point(lambda v: int(v * 0.93)))
    img.alpha_composite(fill_img)

    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ed = ImageDraw.Draw(edge)
    width = max(int(min(w, h) / 360), 3)
    for r in rings:
        ed.line([*r, r[0]], fill=(255, 245, 230, 215), width=width, joint="curve")
    img.alpha_composite(edge)


def _outline_country(img: Image.Image, view: View, country: dict) -> None:
    edge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ed = ImageDraw.Draw(edge)
    for part in country["parts"]:
        r = [view.px(*p) for p in part["ring"]]
        if len(r) >= 3:
            ed.line([*r, r[0]], fill=(255, 255, 255, 120), width=3, joint="curve")
    img.alpha_composite(edge)


def _emoji_code(emoji: str) -> str:
    return "-".join(f"{ord(c):X}" for c in emoji if ord(c) != 0xFE0F)


def icon(emoji: str, size: int) -> Image.Image | None:
    if not emoji or not any(ord(c) > 0x2000 for c in emoji):
        return None
    code = _emoji_code(emoji.strip())
    path = _get(OPENMOJI.format(code), CACHE / "openmoji" / f"{code}.png", timeout=30)
    if path is None:
        return None
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        path.unlink(missing_ok=True)
        return None
    img = img.crop(img.getbbox() or (0, 0, img.width, img.height))
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return img


def _paste_icon(canvas: Image.Image, ic: Image.Image, center: tuple[float, float]) -> None:
    x, y = int(center[0] - ic.width / 2), int(center[1] - ic.height / 2)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sil = Image.new("RGBA", ic.size, (0, 0, 0, 255))
    sil.putalpha(ic.getchannel("A").point(lambda v: v * 0.55))
    shadow.paste(sil, (x + 6, y + 12), sil)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)))
    canvas.alpha_composite(ic, (x, y))


def _icon_positions(n: int, center: tuple[float, float], spacing: float) -> list[tuple[float, float]]:
    cols = min(n, 3)
    rows = math.ceil(n / cols)
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        x = center[0] + (c - (in_row - 1) / 2) * spacing + (spacing * 0.25 if r % 2 else 0)
        y = center[1] + (r - (rows - 1) / 2) * spacing * 0.85
        out.append((x, y))
    return out


def _year_ruler(img: Image.Image, year: str, label: str = "") -> None:
    w, h = img.size
    y = int(h * 0.085)
    _shadowed_text(img, (w / 2, y), str(year), _font(SERIF_FONT, int(w * 0.11), "SemiBold"))
    d = ImageDraw.Draw(img)
    ry = y + int(w * 0.075)
    half = int(w * 0.25)
    d.line((w / 2 - half, ry, w / 2 + half, ry), fill=(255, 255, 255, 170), width=2)
    for i in range(-20, 21):
        x = w / 2 + i * half / 20
        tick = 14 if i % 5 == 0 else 7
        d.line((x, ry - tick, x, ry), fill=(255, 255, 255, 150), width=2)
    d.polygon([(w / 2 - 9, ry + 14), (w / 2 + 9, ry + 14), (w / 2, ry + 2)], fill=(*HIGHLIGHT, 255))
    if label:
        _shadowed_text(img, (w / 2, ry + int(w * 0.035)), label.upper(), _font(LABEL_FONT, int(w * 0.024)),
                       fill=(235, 235, 235))


def _place(name: str) -> tuple[float, float] | None:
    """Cidade, senão o centro do país."""
    city = find_city(name)
    if city is not None:
        return city
    c = find_country(name)
    return c["main"]["c"] if c else None


def _route_points(rota) -> tuple[tuple[float, float], tuple[float, float], str, str, str] | None:
    if not isinstance(rota, dict):
        return None
    a, b = _place(str(rota.get("de") or "")), _place(str(rota.get("para") or ""))
    if a is None or b is None or a == b:
        return None
    return a, b, str(rota.get("icone") or "⛵"), str(rota.get("de") or ""), str(rota.get("para") or "")


def _draw_route(img: Image.Image, view: View, a, b, emoji: str, name_a: str, name_b: str) -> None:
    """Linha tracejada amarela em arco de A até B, seta e ícone no caminho."""
    w, h = img.size
    (x0, y0), (x1, y1) = view.px(*a), view.px(*b)
    dist = math.dist((x0, y0), (x1, y1))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    nx, ny = -(y1 - y0) / max(dist, 1), (x1 - x0) / max(dist, 1)
    cx, cy = mx + nx * dist * 0.18, my + ny * dist * 0.18

    def at(t: float) -> tuple[float, float]:
        return ((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1,
                (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = 60
    width = max(w // 220, 5)
    for i in range(0, n, 2):
        d.line([at(i / n), at((i + 1) / n)], fill=(*HIGHLIGHT, 255), width=width)
    ex, ey = at(1.0)
    px, py = at(0.96)
    ang = math.atan2(ey - py, ex - px)
    size = w * 0.03
    d.polygon([(ex, ey), (ex - size * math.cos(ang - 0.45), ey - size * math.sin(ang - 0.45)),
               (ex - size * math.cos(ang + 0.45), ey - size * math.sin(ang + 0.45))], fill=(*HIGHLIGHT, 255))
    for x, y in ((x0, y0),):
        d.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(255, 255, 255, 255))
    glow = layer.filter(ImageFilter.GaussianBlur(8))
    img.alpha_composite(glow)
    img.alpha_composite(layer)
    ic = icon(emoji, int(w * 0.1))
    if ic is not None:
        _paste_icon(img, ic, at(0.6))
    lf = _font(LABEL_FONT, int(w * 0.03))
    for (x, y), name in (((x0, y0), name_a), ((ex, ey), name_b)):
        if find_city(name) and 0 <= x <= w and 0 <= y <= h:
            _shadowed_text(img, (x, y + w * 0.035), city_label(name), lf)


def _paper_note(img: Image.Image, text: str) -> None:
    """Bilhete de papel com o documento/prova da cena ("SECRETO · LONDRES ·
    OUT/1807"), levemente torto, com sombra."""
    w, h = img.size
    lines = [l.strip() for l in re.split(r"\s*[·|/\n]\s*(?=\D)|\n", text) if l.strip()][:3] or [text]
    title = _font(SERIF_FONT, int(w * 0.042), "Bold")
    small = _font(SERIF_FONT, int(w * 0.032), "SemiBold")
    tw = max([title.getlength(lines[0].upper()), *(small.getlength(l) for l in lines[1:])])
    cw, ch = int(min(tw + w * 0.09, w * 0.7)), int(title.size * 1.6 + len(lines[1:]) * small.size * 1.35 + w * 0.05)
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rectangle((0, 0, cw - 1, ch - 1), fill=(244, 236, 214, 255), outline=(150, 130, 95, 255), width=3)
    noise = np.random.default_rng(3).normal(0, 7, (ch, cw, 1))
    paper = np.clip(np.asarray(card, np.float32)[..., :3] + noise, 0, 255).astype(np.uint8)
    card = Image.fromarray(paper).convert("RGBA")
    cd = ImageDraw.Draw(card)
    cd.rectangle((0, 0, cw - 1, ch - 1), outline=(150, 130, 95, 255), width=3)
    y = w * 0.03
    cd.text((cw / 2, y + title.size / 2), lines[0].upper(), font=title, fill=(40, 32, 25), anchor="mm", features=_FEATURES)
    y += title.size * 1.15
    cd.line((cw * 0.15, y, cw * 0.85, y), fill=(120, 100, 70), width=2)
    y += small.size * 0.5
    for l in lines[1:]:
        cd.text((cw / 2, y + small.size / 2), l, font=small, fill=(70, 58, 45), anchor="mm", features=_FEATURES)
        y += small.size * 1.35
    card = card.rotate(-3, expand=True, resample=Image.Resampling.BICUBIC)
    x, yy = int(w * 0.06), int(h * 0.2)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sil = Image.new("RGBA", card.size, (0, 0, 0, 170))
    sil.putalpha(card.getchannel("A").point(lambda v: int(v * 0.6)))
    shadow.paste(sil, (x + 10, yy + 16), sil)
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
    img.alpha_composite(card, (x, yy))


def _era_badge(img: Image.Image, year: str) -> None:
    """Ano fixo no canto (1807 → 1808 → 1815): linha do tempo sem narrar."""
    w, _ = img.size
    font = _font(SERIF_FONT, int(w * 0.07), "Bold")
    # longe da borda: o zoom lento corta ~5% de cada lado
    x0, y0 = w * 0.09, w * 0.13
    _shadowed_text(img, (x0, y0), year, font, fill=(232, 190, 90), anchor="lm")
    d = ImageDraw.Draw(img)
    tl = font.getlength(year)
    d.line((x0, y0 + font.size * 0.55, x0 + tl, y0 + font.size * 0.55),
           fill=(232, 190, 90, 220), width=3)


# ---------------------------------------------------------------- cenas

def _map_scene(spec: dict, w: int, h: int) -> tuple[Image.Image, Image.Image] | None:
    night = bool(spec.get("noite"))
    destaques = [d for d in spec.get("destaques") or [] if isinstance(d, dict)]
    names = [str(n) for n in spec.get("lugares") or []] + [str(d.get("pais", "")) for d in destaques]
    found: list[dict] = []
    for n in names:
        c = find_country(n)
        if c is not None and all(c is not f for f in found):
            found.append(c)
    city_name = str(spec.get("cidade") or "")
    city = find_city(city_name)
    route = _route_points(spec.get("rota"))
    if not found and city is None and route is None:
        return None
    countries = found
    # enquadra o ASSUNTO: os países destacados; sem destaque, ignora o
    # vizinho gigante de contexto (China numa cena sobre a Coreia)
    focus = [c for c in countries if any(find_country(str(d.get("pais", ""))) is c for d in destaques)]
    if not focus and countries:
        smallest = min(c["main"]["area"] for c in countries)
        focus = [c for c in countries if c["main"]["area"] <= smallest * 6]
    if spec.get("_zoom") and focus:
        focus = focus[:1]  # variação: fecha no país principal
    if focus or route:
        view = frame_view(focus, w, h, pad=float(spec.get("_pad") or 1.45), points=list(route[:2]) if route else None)
    else:
        assert city is not None
        lon, lat = city
        hdeg = 8.0
        k = max(math.cos(math.radians(lat)), 0.2)
        wdeg = hdeg * (w / h) / k
        view = View(lon - wdeg / 2, lat + 0.42 * hdeg, wdeg, hdeg, w, h)

    raw = _gibs(view, night)
    base = grade(raw, night) if raw is not None else _fallback_base(w, h)
    img = base.convert("RGBA")

    painted = []
    for d in destaques:
        c = find_country(str(d.get("pais", "")))
        if c is None:
            continue
        color = COLORS.get(_fold(d.get("cor", "")), COLORS["vermelho"])
        pattern = flag(c.get("a2", "")) if (d.get("bandeira") or spec.get("bandeira")) else None
        _paper_country(img, view, c, color, lift=min(w, h) / 70, pattern=pattern)
        painted.append((c, d))
    if not painted and not night:
        for c in countries[:2]:
            _outline_country(img, view, c)
    if night and not painted:
        for c in countries[:2]:
            _outline_country(img, view, c)

    label_font = _font(LABEL_FONT, int(w * 0.034))
    icon_size = int(w * 0.11)
    for c, d in painted:
        cx, cy = view.px(*c["main"]["c"])
        emoji = str(d.get("icone") or "")
        qty = max(1, min(int(d.get("quantidade") or 3), 6)) if emoji else 0
        ic = icon(emoji, icon_size) if emoji else None
        if ic is not None:
            for pos in _icon_positions(qty, (cx, cy), icon_size * 1.15):
                _paste_icon(img, ic, pos)
        rotulo = str(d.get("rotulo") or "").strip()
        if rotulo:
            rows = math.ceil(qty / min(qty, 3)) if ic is not None else 0
            ly = cy + (rows * icon_size * 0.5 + icon_size * 0.45 if ic is not None else 0)
            _shadowed_text(img, (min(max(cx, w * 0.2), w * 0.8), ly), rotulo, label_font)

    if city is not None:
        x, y = view.px(*city)
        if 0 <= x <= w and 0 <= y <= h:
            r = w * 0.03
            _glow(img, (x, y), r * 2.2, HIGHLIGHT, 120)
            d = ImageDraw.Draw(img)
            d.ellipse((x - r, y - r, x + r, y + r), outline=(*HIGHLIGHT, 255), width=5)
            d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(255, 255, 255, 255))
            _shadowed_text(img, (x, y + r + w * 0.03), city_label(city_name), label_font)

    # enfeite que quebra não derruba o mapa (bilhete de 1 linha quebrou o
    # max() e a cena inteira perdeu o gráfico, job 705)
    for name, draw in (("rota", lambda: _draw_route(img, view, *route) if route is not None else None),
                       ("nota", lambda: _paper_note(img, str(spec["nota"])) if spec.get("nota") else None),
                       ("personagens", lambda: _place_characters(img, view, spec["personagens"])
                        if spec.get("personagens") else None)):
        try:
            draw()
        except Exception as exc:  # noqa: BLE001
            log.warning("enfeite %s do mapa falhou: %s", name, exc)
    if spec.get("ano"):
        _year_ruler(img, str(spec["ano"]), str(spec.get("ano_rotulo") or ""))
    elif spec.get("_era"):
        _era_badge(img, str(spec["_era"]))
    return img.convert("RGB"), base


def _backdrop(background: Image.Image | None, w: int, h: int) -> Image.Image:
    """Fundo das cenas sem mapa: o último mapa do vídeo, desfocado e
    escurecido — mantém o mesmo mundo visual do começo ao fim."""
    if background is None:
        base = _fallback_base(w, h)
    else:
        base = background.convert("RGB").resize((w, h)).filter(ImageFilter.GaussianBlur(w / 90))
        a = np.asarray(base).astype(np.float32) * 0.42
        base = Image.fromarray(a.astype(np.uint8))
    return base.convert("RGBA")


def _num(value) -> int | None:
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _comparison_scene(spec: dict, w: int, h: int, background: Image.Image | None) -> Image.Image | None:
    a, b = _num(spec.get("a")), _num(spec.get("b"))
    if a is None or b is None or not (1 <= a <= 20 and 1 <= b <= 20):
        return None
    size = int(w * (0.12 if max(a, b) <= 7 else 0.1))
    ic = icon(str(spec.get("icone") or "🧍"), size) or icon("🧍", size)
    if ic is None:
        return None
    img = _backdrop(background, w, h)
    big = _font(SERIF_FONT, int(w * 0.09), "Bold")
    label = _font(LABEL_FONT, int(w * 0.034))

    def block(n: int, rotulo: str, top: float, color) -> float:
        """número em cima, ícones no meio, rótulo embaixo; devolve onde acabou."""
        cols = min(n, 7 if n <= 14 else 5)
        rows = math.ceil(n / cols)
        sp = min(ic.width * 1.25, w * 0.85 / cols)
        _shadowed_text(img, (w / 2, top + big.size * 0.5), str(n), big, fill=color)
        y0 = top + big.size * 1.05 + ic.height / 2
        for i in range(n):
            r, c = divmod(i, cols)
            in_row = min(cols, n - r * cols)
            _paste_icon(img, ic, (w / 2 + (c - (in_row - 1) / 2) * sp, y0 + r * ic.height * 1.1))
        ly = y0 + (rows - 1) * ic.height * 1.1 + ic.height / 2 + label.size
        _shadowed_text(img, (w / 2, ly), rotulo, label)
        return ly + label.size

    end = block(a, str(spec.get("a_rotulo") or ""), h * 0.06, HIGHLIGHT)
    mid = end + w * 0.07
    bulb = icon("💡", int(w * 0.09))
    if bulb is not None and spec.get("energia"):
        _glow(img, (w * 0.5, mid), w * 0.06, HIGHLIGHT, 140)
        _paste_icon(img, bulb, (w * 0.5, mid))
    else:
        _shadowed_text(img, (w / 2, mid), "=", big, fill=(230, 230, 230))
    block(b, str(spec.get("b_rotulo") or ""), mid + w * 0.06, (235, 235, 235))
    return img.convert("RGB")


def _number_scene(spec: dict, w: int, h: int, background: Image.Image | None) -> Image.Image | None:
    value = str(spec.get("valor") or "").strip()
    if not value or len(value) > 12:
        return None
    img = _backdrop(background, w, h)
    size = int(w * (0.32 if len(value) <= 4 else 0.2))
    font = _font(SERIF_FONT, size, "Bold")
    _glow(img, (w / 2, h * 0.33), w * 0.25, HIGHLIGHT, 60)
    _shadowed_text(img, (w / 2, h * 0.33), value, font, fill=HIGHLIGHT, shadow=18)
    rotulo = str(spec.get("rotulo") or "").strip()
    if rotulo:
        lf = _font(LABEL_FONT, int(w * 0.045))
        lines, cur = [], ""
        for word in rotulo.split():
            cand = f"{cur} {word}".strip()
            if cur and lf.getlength(cand) > w * 0.8:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        lines.append(cur)
        for i, line in enumerate(lines[:3]):
            _shadowed_text(img, (w / 2, h * 0.33 + size * 0.62 + i * lf.size * 1.25), line, lf)
    return img.convert("RGB")


def _year_scene(spec: dict, w: int, h: int, background: Image.Image | None) -> Image.Image | None:
    year = str(spec.get("de") or spec.get("ano") or "").strip()
    if not re.fullmatch(r"-?\d{1,4}", year):
        return None
    img = _backdrop(background, w, h)
    font = _font(SERIF_FONT, int(w * 0.3), "SemiBold")
    _shadowed_text(img, (w / 2, h * 0.3), year, font, shadow=20)
    d = ImageDraw.Draw(img)
    ry = int(h * 0.3 + w * 0.2)
    half = int(w * 0.32)
    d.line((w / 2 - half, ry, w / 2 + half, ry), fill=(255, 255, 255, 180), width=3)
    for i in range(-24, 25):
        x = w / 2 + i * half / 24
        tick = 22 if i % 6 == 0 else 10
        d.line((x, ry - tick, x, ry), fill=(255, 255, 255, 160), width=3)
    d.polygon([(w / 2 - 14, ry + 22), (w / 2 + 14, ry + 22), (w / 2, ry + 4)], fill=(*HIGHLIGHT, 255))
    rotulo = str(spec.get("rotulo") or "").strip()
    if rotulo:
        _shadowed_text(img, (w / 2, ry + w * 0.07), rotulo.upper(), _font(LABEL_FONT, int(w * 0.04)))
    return img.convert("RGB")


def _expand(spec: dict) -> list[dict]:
    """"ano" com de/até vira 2 batidas (o contador anda)."""
    if spec.get("tipo") == "ano" and spec.get("ate") and str(spec.get("ate")) != str(spec.get("de")):
        return [spec, {**spec, "de": spec["ate"]}]
    return [spec]


def beats_of(grafico) -> list[dict]:
    if isinstance(grafico, dict):
        grafico = [grafico]
    if not isinstance(grafico, list):
        return []
    out = []
    for g in grafico:
        if isinstance(g, dict) and _fold(g.get("tipo", "")) in TYPES:
            out.extend(_expand({**g, "tipo": _fold(g["tipo"])}))
    return out[:MAX_BEATS + 1]


_PALETTE_ORDER = ["vermelho", "azul", "dourado", "verde", "roxo", "laranja"]


@lru_cache(maxsize=1)
def _country_name_patterns() -> list[tuple[re.Pattern, str]]:
    """Nomes em português que a narração usa ("Bolívia", "Chile") -> país."""
    path = CACHE / "ne_50m_admin_0_countries.geojson"
    if not _countries() or not path.exists():
        return []
    names: dict[str, str] = {}
    for feat in json.loads(path.read_text())["features"]:
        props = feat["properties"]
        if props.get("TYPE") not in ("Sovereign country", "Country", "Sovereignty"):
            continue
        for key in ("NAME_PT", "NAME"):
            v = props.get(key)
            if v and len(v) >= 4:
                names[v] = props.get("NAME")
    for alias in _ALIASES:
        if len(alias) >= 4:
            names[alias] = alias
    pats = []
    for shown, key in sorted(names.items(), key=lambda kv: -len(kv[0])):
        pats.append((re.compile(rf"(?<!\w){re.escape(_fold(shown))}(?!\w)"), key))
    for stem, key in sorted(_DEMONYMS.items(), key=lambda kv: -len(kv[0])):
        # radical + terminação (chilenas, bolivianos, francesa, alemã). Quem
        # também é IDIOMA ("traduzir para português" mostrou Portugal, job
        # 707) só vale no plural ou no feminino: "os ingleses", "a corte portuguesa"
        ends = "(?:as|os|es|a)" if stem in _LANGUAGE_STEMS else "(?:a|o|as|os|es|e|ano|ana)?"
        pats.append((re.compile(rf"(?<!\w){re.escape(_fold(stem))}{ends}(?!\w)"), key))
    return pats


def cities_in(text: str) -> list[str]:
    """Cidade citada pelo nome na narração ("Antofagasta", "La Paz") — só
    com maiúscula e 5+ letras, e não país (senão "Natal", "Vitória" no meio
    da frase viravam cidade)."""
    out = []
    for m in re.finditer(r"\b[A-ZÀ-Ý][a-zà-ÿ]+(?: (?:de |da |do )?[A-ZÀ-Ý][a-zà-ÿ]+)?", text):
        sentence_start = m.start() == 0 or text[:m.start()].rstrip()[-1:] in ".!?:"
        for cand in (m[0], m[0].split()[0]):
            if sentence_start and " " not in cand:
                continue  # "Vitória do Botafogo..." no começo da frase não é a cidade
            hit = _cities().get(_fold(cand))
            if hit and len(cand) >= 5 and hit[3] >= 50_000 and find_country(cand) is None and cand not in out:
                out.append(cand)
                break
    return out


def countries_in(text: str) -> list[str]:
    folded = _fold(text)
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for pat, key in _country_name_patterns():
        for m in pat.finditer(folded):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((m.start(), key))
    out: list[str] = []
    for _, key in sorted(found):
        c = find_country(key)
        if c is not None and all(find_country(o) is not c for o in out):
            out.append(key)
    return out


# número que vale cartão: quantidade com unidade ("5 mil militares", "861
# quilômetros", "90%") — nunca ano solto, nunca número pequeno sem unidade
_STRONG_NUMBER = re.compile(
    r"(R\$|US\$|€|£)?\s?(?<![\w.,])(\d{1,3}(?:[.,]\d{1,3})*(?:,\d+)?)\s*(%|por cento|mil|milh(?:ão|ões)|bilh(?:ão|ões)|trilh(?:ão|ões))?"
    r"\s+(?:(?:de|da|do|dos|das|em|ao|à)\s+|(por)\s+)?([a-zà-ÿ][a-zà-ÿ-]{2,}(?:\s+[a-zà-ÿ][a-zà-ÿ-]{2,})?)", re.I)
_MEASURE_UNITS = {"metros": "m", "metro": "m", "quilometros": "km", "quilometro": "km", "km": "km",
                  "centimetros": "cm", "toneladas": "t", "litros": "L", "hectares": "ha", "graus": "°"}
_NUMBER_STOP = {"anos", "dias", "horas", "minutos", "segundos", "vezes", "outubro", "novembro", "dezembro", "janeiro",
                "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro"}
_LABEL_STOP = {"e", "de", "da", "do", "que", "em", "no", "na", "com", "para", "pra", "por", "a", "o"}


def strong_number(narration: str) -> dict | None:
    best = None
    for m in _STRONG_NUMBER.finditer(narration):
        cur, num, mult, label = (m[1] or ""), m[2], (m[3] or ""), (f"{m[4]} {m[5]}" if m[4] else m[5])
        digits = re.sub(r"\D", "", num)
        if re.fullmatch(r"1\d{3}|20\d{2}", digits) and not mult:
            continue  # ano
        first = label.split()[0].lower()
        if first in _NUMBER_STOP or (not mult and int(digits or 0) < 10):
            continue
        words = [w for i, w in enumerate(label.split()) if i == 0 or w.lower() not in _LABEL_STOP][:2]
        unit = "%" if mult.lower() in ("%", "por cento") else (f" {mult}" if mult else "")
        if words and _fold(words[0]) in _MEASURE_UNITS:
            # medida: a unidade vai junto do número e não pega a palavra
            # seguinte ("4,3 / metros arremessa", job 708)
            base = _MEASURE_UNITS[_fold(words[0])]
            # "metros cúbicos" é m³ e "por dia/ano" vira /dia: sem isso o cartão
            # saía "190 mil m" (09/11, Fractal, job 793)
            if base == "m" and len(words) > 1 and _fold(words[1]).startswith("cubic"):
                base = "m³"
            per = re.match(r"\s+por\s+(dia|ano|mês|mes|hora|semana)\b", narration[m.end():], re.I)
            if per:
                base += f"/{per[1].lower()}"
            unit, words = f"{unit} {base}", []
        cand = {"tipo": "numero", "valor": f"{cur + ' ' if cur else ''}{num}{unit}".strip(), "rotulo": " ".join(words)}
        score = (2 if mult or cur else 0) + len(digits)
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


class Renderer:
    """Um por vídeo: guarda o último mapa pra servir de fundo das cenas
    sem mapa (número, comparação, ano)."""

    def __init__(self, width: int, height: int):
        self.w, self.h = int(width * SCALE), int(height * SCALE)
        self.out_w, self.out_h = width, height
        self.last_base: Image.Image | None = None
        self.era = ""  # último ano mostrado: fica no canto dos mapas seguintes
        self.colors: dict[int, str] = {}  # país -> cor, fixa no vídeo inteiro
        self.cast: list[str] = []  # países da história, na ordem em que apareceram
        self.last_sig: tuple | None = None  # mapa da cena anterior
        self.repeat = 0  # quantas cenas seguidas com o mesmo mapa

    def new_story(self) -> None:
        """Outra história no mesmo vídeo (compilação da semana): o ano e o
        mapa da anterior não passam pra ela — o Japão do Senna saiu com o
        1897 do Machado no canto (job 742)."""
        self.era = ""
        self.last_sig = None
        self.repeat = 0

    def color_for(self, country_name: str) -> str:
        c = find_country(country_name)
        key = id(c)
        if key not in self.colors:
            self.colors[key] = _PALETTE_ORDER[len(self.colors) % len(_PALETTE_ORDER)]
        return self.colors[key]

    def auto(self, narration: str, skip: set[str] | None = None) -> list[dict] | None:
        """Cena sem gráfico que cita país pelo nome: mapa dele destacado, na
        cor que ele já tem no vídeo (o roteiro da Bolívia, 03/11, pediu
        gráfico em só 2 de 8 cenas)."""
        skipped = {id(find_country(s)) for s in skip or set() if find_country(s)}
        names = [n for n in countries_in(narration) if id(find_country(n)) not in skipped][:3]
        number = strong_number(narration)
        years = re.findall(r"(?<!\d)(1[0-9]\d\d|20[0-2]\d)(?!\d)", narration)
        if not names:
            # sem país: o número forte da cena vira cartão; sem número, o ano
            if number:
                return [number]
            if years and self.last_base is not None:
                return [{"tipo": "ano", "de": years[0], "rotulo": ""}]
            return None
        for n in names:
            if all(find_country(c) is not find_country(n) for c in self.cast):
                self.cast.append(n)
        # só o que a CENA cita: o "rival da história" entrava no mapa sem a
        # frase falar dele, contra a regra do gráfico (revisão de 04/11)
        shown = names[:3]
        destaques = [{"pais": n, "cor": self.color_for(n), "rotulo": find_country(n)["name"]} for n in shown]
        years = re.findall(r"(?<!\d)(1[0-9]\d\d|20[0-2]\d)(?!\d)", narration)
        spec = {"tipo": "mapa", "lugares": shown, "destaques": destaques}
        cities = cities_in(narration)
        if cities:
            spec["cidade"] = cities[0]
        if years:
            spec["ano"] = years[0]
        # país E número forte: mapa e, quando a voz fala o número, o cartão dele
        return [spec, {**number, "palavra": re.sub(r"^\D+", "", number["valor"]).split()[0]}] if number else [spec]

    def still(self, spec: dict) -> Image.Image | None:
        """Quadro pronto é reaproveitado: o mesmo gráfico (mesmo spec, mesmo
        ano no canto, mesmo tamanho) sai do disco em vez de ser desenhado
        de novo — neste vídeo e nos próximos."""
        import hashlib

        key = hashlib.sha1(json.dumps([spec, self.era, self.w, self.h, self.repeat if spec.get("tipo") == "mapa" else 0,
                                       sorted(self.colors.values())], sort_keys=True, ensure_ascii=False,
                                      default=str).encode()).hexdigest()
        path = CACHE / "quadros" / f"{key}.jpg"
        if path.exists() and spec.get("tipo") != "mapa":
            return Image.open(path).convert("RGB")
        img = self._draw(spec)
        if img is not None and spec.get("tipo") != "mapa":
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path, quality=92)
        return img

    def _draw(self, spec: dict) -> Image.Image | None:
        try:
            tipo = spec.get("tipo")
            year = str(spec.get("ano") or (spec.get("de") if tipo == "ano" else "") or "").strip()
            if re.fullmatch(r"\d{3,4}", year):
                self.era = year
            if tipo == "mapa":
                # cor do país fixa no vídeo inteiro, mesmo se o roteiro trocar
                for d in spec.get("destaques") or []:
                    if isinstance(d, dict) and d.get("pais") and find_country(str(d["pais"])):
                        if d.get("cor"):
                            self.colors.setdefault(id(find_country(str(d["pais"]))), _fold(d["cor"]))
                        d["cor"] = self.color_for(str(d["pais"]))
                spec = {**spec, "_era": self.era}
                # o mesmo mapa cena após cena (o Chile vermelho em 15 quadros
                # seguidos, job 702): fecha, vira noite, abre — nunca igual
                sig = (tuple(sorted(_fold(n) for n in spec.get("lugares") or [])),
                       tuple(sorted(_fold(d.get("pais", "")) for d in spec.get("destaques") or [] if isinstance(d, dict))))
                self.repeat = self.repeat + 1 if sig == self.last_sig else 0
                self.last_sig = sig
                variation = self.repeat % 4
                if variation == 1:
                    spec.update(_zoom=True, _pad=1.12)
                elif variation == 2:
                    spec.update(noite=not spec.get("noite"))
                elif variation == 3:
                    spec.update(_pad=2.4)
                res = _map_scene(spec, self.w, self.h)
                if res is None:
                    return None
                img, self.last_base = res
                return img
            if tipo == "comparacao":
                return _comparison_scene(spec, self.w, self.h, self.last_base)
            if tipo == "numero":
                return _number_scene(spec, self.w, self.h, self.last_base)
            if tipo == "ano":
                return _year_scene(spec, self.w, self.h, self.last_base)
        except Exception as exc:  # noqa: BLE001 — gráfico é bônus: cai pra cascata normal
            log.warning("gráfico %s falhou: %s", spec.get("tipo"), exc)
        return None

    def render(self, grafico, narration: str, word_boundaries: list[dict] | None,
               duration: float, work: Path) -> tuple[Path, bool] | None:
        """(arquivo, é_clipe). 1 batida = PNG (o render_scene dá o zoom);
        2+ = MP4 mudo com as batidas entrando na palavra certa."""
        beats = beats_of(grafico)
        stills = []
        for b in beats:
            img = self.still(b)
            if img is not None:
                stills.append((b, img))
        if not stills:
            return None
        work.mkdir(parents=True, exist_ok=True)
        if len(stills) == 1 or duration < 2.5:
            path = work.with_suffix(".png")
            stills[0][1].save(path)
            return path, False

        starts = _beat_starts([b for b, _ in stills], word_boundaries or [], duration)
        paths = []
        for i, (_, img) in enumerate(stills):
            p = work / f"beat_{i}.png"
            img.save(p)
            paths.append(p)
        out = work / "grafico.mp4"
        _beats_clip(paths, starts, duration, self.out_w, self.out_h, out)
        return out, True


def _beat_starts(beats: list[dict], words: list[dict], duration: float) -> list[float]:
    """Instante de cada batida: a palavra pedida, na ordem; sem ela,
    distribui igual. A 1ª sempre no 0."""
    n = len(beats)
    even = [duration * i / n for i in range(n)]
    starts = [0.0]
    for i, b in enumerate(beats[1:], start=1):
        target = _fold(b.get("palavra", ""))
        t = None
        if target:
            first = target.split()[0]
            for w in words:
                if w["start"] > starts[-1] + 0.8 and _fold(w["text"]).startswith(first):
                    t = w["start"]
                    break
        if t is None or t > duration - 0.8:
            t = max(even[i], starts[-1] + 0.8)
        starts.append(min(t, duration - 0.6))
    return starts


FPS = 24
XFADE = 0.35


def _beats_clip(paths: list[Path], starts: list[float], duration: float, w: int, h: int, out: Path) -> None:
    durs = [b - a for a, b in zip(starts, starts[1:] + [duration])]
    inputs, chains = [], []
    for i, (p, d) in enumerate(zip(paths, durs)):
        seg = d + (XFADE if i < len(paths) - 1 else 0)
        frames = max(int(seg * FPS), 2)
        inputs += ["-loop", "1", "-t", f"{seg:.3f}", "-i", str(p)]
        chains.append(
            f"[{i}:v]zoompan=z='min(zoom+{0.06 / frames:.6f},1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={w}x{h}:fps={FPS},setsar=1,format=yuv420p[v{i}]"
        )
    last = "v0"
    offset = 0.0
    for i in range(1, len(paths)):
        offset += durs[i - 1]
        chains.append(f"[{last}][v{i}]xfade=transition=fade:duration={XFADE}:offset={offset - 0.001:.3f}[x{i}]")
        last = f"x{i}"
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chains), "-map", f"[{last}]",
         "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
         "-pix_fmt", "yuv420p", "-an", str(out)],
        check=True, capture_output=True, text=True,
    )


def spoken_numbers_ok(spec: dict, narration: str) -> bool:
    """Número na tela que a narração não diz = dado inventado: fora."""
    nums = {re.sub(r"\D", "", n) for n in re.findall(r"\d[\d.,]*", narration)}
    for key in ("a", "b", "valor", "de", "ate", "ano"):
        if key in spec and spec[key] not in (None, ""):
            digits = re.sub(r"\D", "", str(spec[key]))
            if digits and digits not in nums and not (key == "a" and digits == "1"):
                return False
    return True


def _said(text: str, narration_folded: str) -> bool:
    """A narração fala isso? (sem acento; nome composto basta 1 palavra de 4+ letras)"""
    words = [w for w in _fold(text).split() if len(w) >= 4]
    return bool(words) and any(re.search(rf"(?<!\w){re.escape(w)}", narration_folded) for w in words)


def _label_ok(label: str, narration_folded: str) -> bool:
    """Rótulo só com palavras que a narração diz (pelo menos metade das de 4+ letras)."""
    words = [w for w in _fold(label).split() if len(w) >= 4]
    if not words:
        return True
    return sum(1 for w in words if w[:5] in narration_folded) * 2 >= len(words)


def _ground_beat(b: dict, narration: str) -> dict | None:
    """O gráfico só mostra o que a CENA fala: país destacado citado na
    narração (nome ou adjetivo), cidade/rota/bilhete/personagem idem,
    rótulo com palavras da narração. Sem isso, o diretor de arte podia
    pintar um país que a frase nem cita."""
    nf = _fold(narration)
    tipo = b.get("tipo")
    if tipo == "mapa":
        cited = {id(find_country(n)) for n in countries_in(narration)}
        destaques = []
        for d in b.get("destaques") or []:
            c = find_country(str(d.get("pais", ""))) if isinstance(d, dict) else None
            if c is not None and id(c) in cited:
                # nome oficial em português — nunca "Litoral perdido"/"Chile pós-guerra"
                destaques.append({**d, "rotulo": c["name"]})
        lugares = [n for n in b.get("lugares") or [] if find_country(str(n)) and id(find_country(str(n))) in cited]
        b = {**b, "destaques": destaques, "lugares": lugares}
        if b.get("cidade") and not (_said(str(b["cidade"]), nf) or _said(city_label(str(b["cidade"])), nf)):
            b.pop("cidade")
        rota = b.get("rota")
        if isinstance(rota, dict):
            def end_ok(name) -> bool:
                name = str(name or "")
                c = find_country(name)
                return _said(name, nf) or _said(city_label(name), nf) or (c is not None and id(c) in cited)
            if not (end_ok(rota.get("de")) and end_ok(rota.get("para"))):
                b.pop("rota")
        if b.get("nota") and not (spoken_numbers_ok({"valor": re.sub(r"\D", "", str(b["nota"]))[:4] or ""}, narration)
                                  and _label_ok(str(b["nota"]), nf)):
            b.pop("nota")
        if b.get("ano_rotulo") and not _label_ok(str(b["ano_rotulo"]), nf):
            b.pop("ano_rotulo")
        pers = []
        for p in b.get("personagens") or []:
            if isinstance(p, dict) and _said(str(p.get("fala") or p.get("nome") or ""), nf):
                pers.append(p)
        b["personagens"] = pers
        if not (destaques or lugares or b.get("cidade") or b.get("rota")):
            return None
        return b
    if tipo in ("numero", "ano"):
        if b.get("rotulo") and not _label_ok(str(b["rotulo"]), nf):
            b = {**b, "rotulo": ""}
        return b
    if tipo == "comparacao":
        for k in ("a_rotulo", "b_rotulo"):
            if b.get(k) and not _label_ok(str(b[k]), nf):
                return None  # comparação sem saber o que se compara não vale
        return b
    return b


def sanitize(grafico, narration: str):
    """Tira do roteiro a batida que não passa nas travas (número que a
    narração não fala, país/cidade/personagem/rótulo que ela não cita, tipo
    desconhecido). None = cena sem gráfico."""
    beats = [b for b in beats_of(grafico) if spoken_numbers_ok(b, narration)]
    beats = [g for b in beats if (g := _ground_beat(b, narration)) is not None]
    if not beats:
        return None
    # "ano" expandido volta a ser 1 spec só (o render expande de novo)
    out, skip = [], False
    for b in beats:
        if skip:
            skip = False
            continue
        out.append(b)
        if b["tipo"] == "ano" and b.get("ate"):
            skip = True
    return out[:MAX_BEATS]


# ---------------------------------------------------------------- personagens

# personagem caricato gerado por IA (FLUX grátis no Cloudflare, ~50 de 10 mil
# neurons/dia por imagem), UMA vez por personagem+expressão e guardado pra
# sempre: o mesmo Napoleão em todas as cenas e em todos os vídeos
CHAR_STYLE = (
    "cute chibi cartoon caricature sticker, full body, big head small body, flat vector illustration, "
    "thick dark outline, soft cel shading, simple shapes, isolated on plain pure white background, centered"
)
# figura histórica só se morreu antes disso: pessoa viva/recente a IA
# desenha com rosto errado (e político/jogador é o pior caso)
CHAR_MAX_DEATH_YEAR = 1950


def _historical_ok(name: str) -> bool:
    """Nome próprio só passa se a Wikipédia em inglês mostrar que a pessoa
    morreu antes de CHAR_MAX_DEATH_YEAR; papel genérico ("soldado francês",
    sem maiúscula) passa sempre."""
    if not re.search(r"\b[A-ZÀ-Ý]", name):
        return True
    try:
        from .wiki_cache import get_json  # cache em disco: nada de 429/bloqueio da Wikimedia

        data = get_json("https://en.wikipedia.org/w/api.php", {
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
            "titles": name, "format": "json", "formatversion": 2,
        }, timeout=15)
        intro = (data["query"]["pages"][0].get("extract") or "")[:600]
    except Exception:  # noqa: BLE001 — sem confirmar, não desenha
        return False
    m = re.search(r"\(([^()]*?\b\d{3,4})\s*[–-]\s*[^()]*?\b(\d{3,4})\)", intro)
    return bool(m and int(m.group(2)) < CHAR_MAX_DEATH_YEAR)


def _cutout(data: bytes, size: int) -> Image.Image | None:
    """Tira o fundo branco (o que é branco E encosta na borda — a peruca
    branca de dentro fica) e põe borda branca de figurinha."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    white = (np.asarray(img).min(axis=2) > 225).astype(np.uint8) * 255
    filled = Image.fromarray(white).copy()  # sem copy o floodfill não altera (memória do numpy)
    for xy in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
        if filled.getpixel(xy) == 255:
            ImageDraw.floodfill(filled, xy, 128)
    bg = np.asarray(filled) == 128
    if bg.mean() < 0.25:  # fundo não era branco: recorte daria lixo
        return None
    alpha = Image.fromarray(((~bg) * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1))
    rgba = img.convert("RGBA")
    rgba.putalpha(alpha)
    rgba = rgba.crop(rgba.getbbox())
    pad = 12
    out = Image.new("RGBA", (rgba.width + 2 * pad, rgba.height + 2 * pad), (0, 0, 0, 0))
    a2 = Image.new("L", out.size, 0)
    a2.paste(rgba.getchannel("A"), (pad, pad))
    white_layer = Image.new("RGBA", out.size, (255, 255, 255, 255))
    white_layer.putalpha(a2.filter(ImageFilter.MaxFilter(13)))
    out.alpha_composite(white_layer)
    out.alpha_composite(rgba, (pad, pad))
    out.thumbnail((size, size), Image.Resampling.LANCZOS)
    return out


def character(name: str, look: str, mood: str, size: int) -> Image.Image | None:
    key = _fold(f"{name} {mood}").replace(" ", "_")[:80]
    path = CACHE / "personagens" / f"{key}.png"
    if not path.exists():
        if not _historical_ok(name):
            log.info("personagem %r recusado (pessoa real recente ou não confirmada)", name)
            return None
        from .visuals import _try_cloudflare

        prompt = f"{name if re.search(r'[A-ZÀ-Ý]', name) else ''} {look}, {mood} face expression, {CHAR_STYLE}".strip()
        data = _try_cloudflare(prompt, 1)
        sticker = _cutout(data, 900) if data else None
        if sticker is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        sticker.save(path)
    img = Image.open(path).convert("RGBA")
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return img


def _place_characters(img: Image.Image, view: "View | None", specs: list) -> None:
    """Personagem AO LADO do país dele (nunca em cima: o país é a
    informação), do lado com mais espaço, e longe do outro personagem."""
    w, h = img.size
    size = int(w * 0.3)
    placed: list[tuple[float, float]] = []
    for i, p in enumerate([p for p in specs if isinstance(p, dict)][:2]):
        ch = character(str(p.get("nome") or ""), str(p.get("visual") or ""), str(p.get("expressao") or "neutral"), size)
        if ch is None:
            continue
        pos = (w * (0.27 if i == 0 else 0.73), h * 0.6)
        c = find_country(str(p.get("pais") or "")) if view is not None and p.get("pais") else None
        if c is not None:
            x, _ = view.px(*c["main"]["c"])
            bottom = max(view.px(*pt)[1] for pt in c["main"]["ring"])
            if 0 <= x <= w and 0 <= bottom <= h:
                pos = (x, bottom + size * 0.55)  # logo abaixo do próprio país
        for q in placed:
            if math.dist(pos, q) < size * 0.95:
                side = 1 if pos[0] >= q[0] else -1
                pos = (q[0] + side * size * 1.05, pos[1])
        pos = (min(max(pos[0], size * 0.55), w - size * 0.55), min(max(pos[1], h * 0.22), h * 0.62))
        placed.append(pos)
        _paste_icon(img, ch, pos)
