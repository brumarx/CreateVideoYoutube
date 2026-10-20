"""Foto e vídeo REAIS do assunto da cena, do Wikimedia Commons.

Pedido de 20/10: o Fractal Curioso parecia aleatório — o vídeo do
Mecanismo de Antikythera (job 527) não mostrou o mecanismo nenhuma vez, só
clipe de banco ("bronze gears close up", "old library books") e a visão
ainda barrou relógio moderno e barra de chocolate. O Commons tem foto (e às
vezes vídeo) de quase todo caso com nome próprio: o mecanismo no museu, a
barraca do Passo Dyatlov, as fendas de Centralia, páginas do Voynich.

O roteiro diz o que a cena mostra em `real_subject` (nome próprio em
inglês, ver script_gen.py). Primeiro a foto principal do verbete da
Wikipédia em inglês (a mais representativa), depois a busca de arquivos do
Commons. Só arquivo hospedado no Commons entra — a imagem local da
Wikipédia em inglês pode ser uso justo (logo, capa de filme) e não serve.
Licença NC/ND fica de fora; o crédito do autor vai na descrição (CC BY/
BY-SA exige). Grátis, sem chave.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from .visual_check import frames_from_clip

log = logging.getLogger("real_media")

_UA = {"User-Agent": "FractalCurioso-video-bot/1.0 (https://www.youtube.com/@fractalcurioso)"}
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_ENWIKI_API = "https://en.wikipedia.org/w/api.php"
MIN_PHOTO_WIDTH = 800  # menor que isso fica borrado esticado em 1080p
MIN_CLIP_SECONDS = 4
MAX_CLIP_BYTES = 80 * 1024 * 1024
MAX_TRIES = 4  # candidatos que a checagem visual olha antes de desistir
# licença que não permite uso comercial/derivado não entra
_BAD_LICENSE = re.compile(r"\bNC\b|\bND\b|non-?commercial|no ?deriv|fair use", re.IGNORECASE)
# mapa de localização, bandeira, brasão, ícone: não é foto do assunto
_BAD_TITLE = re.compile(
    r"\b(locator|location map|draft map|flag of|coat of arms|logo|icon|signature|seal of|subgraph|wikidata|"
    r"lego|minecraft|wikimedia|wikipedia|tutorial)\b",
    re.IGNORECASE,
)
_STOP = {"the", "and", "of", "in", "on", "at", "for", "with", "from", "incident", "case", "event", "mystery"}


def _relevant(title: str, subject: str) -> bool:
    """Busca do Commons casa por descrição/categoria: "Mary Celeste" trouxe
    vídeo da Alexa da Amazon (o navio se chamava Amazon). Exige o nome do
    assunto no nome do arquivo."""
    words = {w for w in re.findall(r"[a-z0-9]+", subject.lower()) if len(w) >= 3 and w not in _STOP}
    # palavra inteira: "oak" casava com "Oakland" (aeroporto em Oak Island)
    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    if not words:
        return False
    # todas as palavras, ou a mais longa (o nome raro: "Antikythera") —
    # metade deixava "Oak Island" casar com "Oakland ... (OAK)"
    longest = max(words, key=len)
    return words <= title_words or (len(longest) >= 7 and longest in title_words)

_search_cache: dict[tuple[str, str], list[dict]] = {}


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def _meta(info: dict) -> tuple[str, str]:
    meta = info.get("extmetadata") or {}
    artist = _strip_html(meta.get("Artist", {}).get("value", ""))[:80]
    lic = _strip_html(meta.get("LicenseShortName", {}).get("value", ""))
    return artist, lic


def _candidate(page: dict, kind: str) -> dict | None:
    info = (page.get("imageinfo") or page.get("videoinfo") or [None])[0]
    title = page.get("title", "")
    if not info or _BAD_TITLE.search(title):
        return None
    artist, lic = _meta(info)
    if _BAD_LICENSE.search(lic):
        return None
    name = title.removeprefix("File:")
    credit = f"{artist or 'autor desconhecido'} ({lic or 'licença livre'}, Wikimedia Commons: {name})"
    if kind == "photo":
        if (info.get("width") or 0) < MIN_PHOTO_WIDTH or not info.get("thumburl"):
            return None
        return {"title": title, "url": info["thumburl"], "credit": credit}
    if float(info.get("duration") or 0) < MIN_CLIP_SECONDS:
        return None
    # transcodificação do próprio Commons (webm/mp4 ~720p): o original
    # pode ter centenas de MB
    derivs = [d for d in info.get("derivatives") or [] if d.get("src") and (d.get("height") or 0) >= 360]
    derivs.sort(key=lambda d: abs((d.get("height") or 0) - 720))
    url = derivs[0]["src"] if derivs else info.get("url")
    if not url:
        return None
    return {"title": title, "url": url, "credit": credit}


def _commons_files(params: dict, kind: str) -> list[dict]:
    prop = {"photo": {"prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920},
            "clip": {"prop": "videoinfo", "viprop": "url|size|extmetadata|derivatives"}}[kind]
    try:
        data = httpx.get(_COMMONS_API, headers=_UA, timeout=20, params={
            "action": "query", "format": "json", **params, **prop,
        }).json()
    except Exception as exc:  # noqa: BLE001
        log.warning("commons falhou (%s): %s", kind, exc)
        return []
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    return [c for p in pages if (c := _candidate(p, kind))]


def _lead_image(subject: str) -> list[dict]:
    """Foto principal do verbete em inglês — se estiver no Commons."""
    try:
        data = httpx.get(_ENWIKI_API, headers=_UA, timeout=20, params={
            "action": "query", "format": "json", "redirects": 1, "generator": "search",
            "gsrsearch": subject, "gsrlimit": 1, "prop": "pageimages", "piprop": "name",
        }).json()
        page = next(iter(data["query"]["pages"].values()))
        name = page.get("pageimage")
    except Exception:  # noqa: BLE001
        return []
    if not name or not name.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")):
        return []
    return _commons_files({"titles": f"File:{name}"}, "photo")


def _search(subject: str, kind: str) -> list[dict]:
    key = (subject.lower(), kind)
    if key not in _search_cache:
        filetype = "bitmap" if kind == "photo" else "video"
        found = _lead_image(subject) if kind == "photo" else []
        found += _commons_files({
            "generator": "search", "gsrnamespace": 6, "gsrlimit": 12,
            "gsrsearch": f"{subject} filetype:{filetype}",
        }, kind)
        # foto principal do verbete é do assunto por construção
        lead = {c["title"] for c in found[:1]} if kind == "photo" else set()
        found = [c for c in found if c["title"] in lead or _relevant(c["title"], subject)]
        seen: set[str] = set()
        _search_cache[key] = [c for c in found if not (c["title"] in seen or seen.add(c["title"]))]
    return _search_cache[key]


def real_photos(subject: str, used: set[str]) -> list[dict]:
    """Candidatos a foto real de `subject` ainda não usados neste vídeo,
    na ordem de preferência ({title, url, credit}). Quem chama baixa
    (thumbnail.photo_scene_frame), checa na visão e marca `used`."""
    if not subject:
        return []
    return [c for c in _search(subject, "photo") if c["title"] not in used][:MAX_TRIES]


def real_clip(subject: str, used: set[str], check: Callable[[bytes], bool | None] | None = None) -> dict | None:
    """Vídeo real de `subject` baixado e aprovado pela visão: {title, path,
    credit} (marca `used`), ou None. Nunca levanta exceção."""
    if not subject:
        return None
    for cand in [c for c in _search(subject, "clip") if c["title"] not in used][:MAX_TRIES]:
        try:
            resp = httpx.get(cand["url"], headers=_UA, timeout=90, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("download do vídeo do commons falhou (%s): %s", cand["title"], exc)
            continue
        if len(resp.content) > MAX_CLIP_BYTES:
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=Path(cand["url"].split("?")[0]).suffix or ".webm", delete=False)
        tmp.write(resp.content)
        tmp.close()
        path = Path(tmp.name)
        if check is not None:
            frames = frames_from_clip(path)
            if frames is None or check(frames) is False:
                path.unlink(missing_ok=True)
                continue
        used.add(cand["title"])
        log.info("vídeo real (commons) pra %r: %s", subject, cand["title"])
        return {**cand, "path": path}
    return None
