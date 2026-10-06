"""Conteúdo REAL de banco (Pexels, grátis, licença livre pra uso comercial
e sem exigir atribuição) pra cenas cujo tema existe de verdade — evita a
"imagem estática com zoom" (IA + zoompan) que fica visivelmente menos viva
que conteúdo real: comparação frame a frame contra um canal do mesmo nicho
mostrou filmagem real (zebra correndo, cachorro correndo, foto histórica
genuína) contra nossa imagem gerada por IA com zoom lento — a diferença de
"vida" na tela é grande mesmo com imagem boa.

Cascata usada por scripts/run_pipeline.py, na ordem: vídeo real (mais
vivo) -> foto real (ainda mais crível que IA, útil quando não existe
CLIPE mas existe FOTO do assunto) -> imagem gerada por IA (último
recurso). Sem PEXELS_API_KEYS configurada (ou sem resultado pra busca),
cada função devolve None e quem chamou cai pro próximo nível da cascata —
nunca bloqueia nem quebra o pipeline por causa disso.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from .config import PEXELS_API_KEYS, PIXABAY_API_KEYS
from .providers import _rotator_for
from .visual_check import frames_from_clip

log = logging.getLogger("stock_media")

VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
MIN_CLIP_HEIGHT = 480  # não usa clipe abaixo disso — qualidade mínima aceitável

# Estado "usado recentemente" (vídeos/fotos, por id) — sem isso, uma busca
# genérica sempre devolve o MESMO clipe mais popular do Pexels pra
# qualquer vídeo que usar aquele termo, virando repetição visível entre
# vídeos completamente diferentes (visto de verdade: um clipe de "pilha de
# dinheiro" apareceu em dezenas de vídeos). Guarda só os últimos
# _RECENT_CAP ids por tipo — não precisa durar pra sempre, só evitar
# repetir o que saiu há pouco tempo.
_RECENT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "stock_media_recent.json"
_RECENT_CAP = 300


def _load_recent() -> dict[str, list[str]]:
    if _RECENT_STATE_FILE.exists():
        try:
            return json.loads(_RECENT_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _mark_recent(kind: str, item_id: str) -> None:
    state = _load_recent()
    ids = state.get(kind, [])
    ids = [i for i in ids if i != item_id] + [item_id]
    state[kind] = ids[-_RECENT_CAP:]
    _RECENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _RECENT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


_WORD_RE = re.compile(r"[a-z]+")


def _relevance_score(query: str, descriptive_text: str) -> int:
    """Conta quantas palavras da busca aparecem no texto descritivo do
    candidato (slug da URL pro vídeo, campo `alt` pra foto) — sinal simples
    mas direto de que aquele candidato específico é sobre o que foi pedido,
    em vez de confiar cegamente que o primeiro resultado do Pexels é o
    melhor pra ESTA cena."""
    query_words = set(_WORD_RE.findall(query.lower()))
    text_words = set(_WORD_RE.findall((descriptive_text or "").lower()))
    return len(query_words & text_words)


# "football" no Pexels é futebol AMERICANO (capacete, NFL) — visto de verdade
# num vídeo de futebol. Sem "american" na frente, vira "soccer".
_FOOTBALL = re.compile(r"\b(?<!american )football\b", re.IGNORECASE)

# candidato cujo slug/alt cita outro esporte ou um clube/patrocínio
# identificável — num vídeo do Botafogo apareceu escudo do Barcelona,
# camisa do Beşiktaş (Beko) e o estádio do Wolfsburg.
_OFF_TOPIC = re.compile(
    r"american|nfl|rugby|helmet|quarterback|touchdown|baseball|basketball|hockey|cricket|"
    r"barcelona|barca|real-madrid|atletico-madrid|chelsea|arsenal|liverpool|manchester|juventus|ac-milan|"
    r"inter-milan|psg|paris-saint|bayern|dortmund|wolfsburg|besiktas|galatasaray|fenerbahce|ajax|benfica|"
    r"fc-porto|flamengo|corinthians|palmeiras|santos-fc|gremio|boca-juniors|river-plate|messi|ronaldo|"
    r"neymar|beko|jersey|crest|badge|emblem",
    re.IGNORECASE,
)
# o filtro acima só vale pra busca de esporte — "badge" de polícia ou
# "american flag" num vídeo de política/curiosidade são legítimos
_SPORTS_QUERY = re.compile(r"soccer|football|stadium|goal|ball|fans|team|player|match|jersey|shield|crest|coach|referee", re.IGNORECASE)


def _normalize_query(query: str) -> str:
    return _FOOTBALL.sub("soccer", query)


def _rank_candidates(query: str, items: list[dict], kind: str, describe) -> list[dict]:
    """Candidatos em ordem de preferência (relevância > 0, inédito, mais
    relevante), sem os de outro esporte/clube numa busca de esporte. Lista
    vazia se só sobrou candidato fora do tema."""
    recent = set(_load_recent().get(kind, []))
    if _SPORTS_QUERY.search(query):
        items = [i for i in items if not _OFF_TOPIC.search(describe(i) or "")]

    def score(item: dict) -> tuple[int, int, int]:
        # inédito vem ANTES de relevância: com relevância na frente, a mesma
        # busca devolvia o mesmo clipe 7 vezes no mesmo vídeo (estádio do
        # Wolfsburg). Relevância > 0 ainda é exigida antes de tudo.
        item_id = str(item.get("id"))
        relevance = _relevance_score(query, describe(item))
        return (min(relevance, 1), 0 if item_id not in recent else -1, relevance)

    return sorted(items, key=score, reverse=True)


# quantos candidatos a checagem visual olha antes de desistir da busca
MAX_VISUAL_TRIES = 4


def _pick_best_file(video: dict, width: int, height: int) -> dict | None:
    """Pexels devolve várias renderizações por clipe (SD/HD/4K) — escolhe a
    mais próxima do tamanho alvo e da orientação certa (vertical/horizontal),
    em vez de sempre baixar a maior (gasta banda/disco à toa no Pi)."""
    files = [f for f in video.get("video_files", []) if (f.get("height") or 0) >= MIN_CLIP_HEIGHT]
    if not files:
        files = video.get("video_files", [])
    if not files:
        return None

    want_portrait = height > width

    def score(f: dict) -> tuple[int, int]:
        fw, fh = f.get("width") or 0, f.get("height") or 0
        is_portrait = fh > fw
        return (0 if is_portrait == want_portrait else 1, abs(fh - height))

    return min(files, key=score)


def search_stock_clip(query: str, width: int, height: int, check: Callable[[bytes], bool | None] | None = None) -> Path | None:
    """Pexels e, sem nada no contexto, Pixabay (2º banco grátis)."""
    return _pexels_clip(query, width, height, check) or _pixabay_clip(query, width, height, check)


def search_stock_photo(query: str, width: int, height: int, check: Callable[[bytes], bool | None] | None = None) -> bytes | None:
    """Pexels e, sem nada no contexto, Pixabay (2º banco grátis)."""
    return _pexels_photo(query, width, height, check) or _pixabay_photo(query, width, height, check)


PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_PHOTO_URL = "https://pixabay.com/api/"


def _pixabay_hits(url: str, query: str, params: dict) -> list[dict]:
    if not PIXABAY_API_KEYS or not query:
        return []
    rotator = _rotator_for(PIXABAY_API_KEYS)
    for api_key in rotator.order():
        try:
            resp = httpx.get(url, params={"key": api_key, "q": _normalize_query(query)[:100], "per_page": 20,
                                          "safesearch": "true", **params}, timeout=20)
            if resp.status_code in (400, 401, 403) and "key" in resp.text.lower():
                rotator.ban(api_key)
                continue
            resp.raise_for_status()
            return resp.json().get("hits", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("busca pixabay falhou (%r): %s", query, exc)
    return []


def _passes(check, content: bytes, query: str) -> bool:
    if check is None:
        return True
    verdict = check(content)
    return not (verdict is False or (verdict is None and _SPORTS_QUERY.search(query)))


def _pixabay_clip(query: str, width: int, height: int, check) -> Path | None:
    hits = _pixabay_hits(PIXABAY_VIDEO_URL, query, {})
    want_portrait = height > width
    for hit in _rank_candidates(query, hits, "pixabay_video", lambda h: h.get("tags", ""))[:MAX_VISUAL_TRIES]:
        files = [f for f in (hit.get("videos") or {}).values() if f.get("url") and (f.get("height") or 0) >= MIN_CLIP_HEIGHT]
        files = [f for f in files if ((f.get("height") or 0) > (f.get("width") or 0)) == want_portrait]
        if not files:
            continue
        picked = min(files, key=lambda f: abs((f.get("height") or 0) - height))
        try:
            resp = httpx.get(picked["url"], timeout=60, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("download do clipe pixabay falhou (%r): %s", query, exc)
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(resp.content)
        tmp.close()
        path = Path(tmp.name)
        frames = frames_from_clip(path) if check is not None else b""
        if check is not None and (frames is None or not _passes(check, frames, query)):
            path.unlink(missing_ok=True)
            continue
        _mark_recent("pixabay_video", str(hit.get("id")))
        log.info("clipe real (pixabay) encontrado pra %r (%sx%s)", query, picked.get("width"), picked.get("height"))
        return path
    return None


def _pixabay_photo(query: str, width: int, height: int, check) -> bytes | None:
    orientation = "vertical" if height > width else "horizontal"
    hits = _pixabay_hits(PIXABAY_PHOTO_URL, query, {"image_type": "photo", "orientation": orientation})
    for hit in _rank_candidates(query, hits, "pixabay_photo", lambda h: h.get("tags", ""))[:MAX_VISUAL_TRIES]:
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not url:
            continue
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("download da foto pixabay falhou (%r): %s", query, exc)
            continue
        if not _passes(check, resp.content, query):
            continue
        _mark_recent("pixabay_photo", str(hit.get("id")))
        log.info("foto real (pixabay) encontrada pra %r", query)
        return resp.content
    return None


def _pexels_clip(query: str, width: int, height: int, check: Callable[[bytes], bool | None] | None = None) -> Path | None:
    """Busca um clipe real que combine com `query` (2-4 palavras em
    inglês, ver script_gen.py -> stock_query) e devolve o caminho local do
    arquivo baixado — ou None (sem chave configurada, sem resultado, ou
    qualquer erro de rede/API). Quem chamar decide o fallback; esta função
    nunca levanta exceção."""
    if not PEXELS_API_KEYS or not query:
        return None
    query = _normalize_query(query)

    orientation = "portrait" if height > width else "landscape"
    rotator = _rotator_for(PEXELS_API_KEYS)

    for api_key in rotator.order():
        try:
            resp = httpx.get(
                VIDEO_SEARCH_URL,
                headers={"Authorization": api_key},
                params={"query": query, "orientation": orientation, "per_page": 15, "size": "medium"},
                timeout=20,
            )
            if resp.status_code in (401, 403):
                rotator.ban(api_key)
                continue
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
        except Exception as exc:
            log.warning("busca de vídeo pexels falhou (%r): %s", query, exc)
            continue

        if not videos:
            log.info("pexels sem resultado pra: %r", query)
            return None

        for chosen in _rank_candidates(query, videos, "video", lambda v: v.get("url", ""))[:MAX_VISUAL_TRIES]:
            picked = _pick_best_file(chosen, width, height)
            if not picked or not picked.get("link"):
                continue
            try:
                video_resp = httpx.get(picked["link"], timeout=60, follow_redirects=True)
                video_resp.raise_for_status()
            except Exception as exc:
                log.warning("download do clipe pexels falhou (%r): %s", query, exc)
                continue
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp.write(video_resp.content)
            tmp.close()
            path = Path(tmp.name)
            if check is not None:
                frames = frames_from_clip(path)
                verdict = check(frames) if frames is not None else False
                # sem visão disponível, busca de esporte não entra às cegas
                # (é onde vinha outro clube e futebol americano) — qualquer canal
                if verdict is False or (verdict is None and _SPORTS_QUERY.search(query)):
                    path.unlink(missing_ok=True)
                    continue
            _mark_recent("video", str(chosen.get("id")))
            log.info("clipe real encontrado pra %r (%sx%s)", query, picked.get("width"), picked.get("height"))
            return path
        log.info("pexels: nenhum clipe no contexto da cena pra %r", query)
        return None

    return None


def _pexels_photo(query: str, width: int, height: int, check: Callable[[bytes], bool | None] | None = None) -> bytes | None:
    """Busca uma FOTO real que combine com `query` e devolve os bytes da
    imagem — mesma interface de src.visuals.generate_image, pra ser
    intercambiável no lugar dela. Meio-termo da cascata: mais crível que
    imagem gerada por IA pra quando não existe CLIPE de vídeo do assunto
    mas existe FOTO real (ex.: um objeto específico, um evento, uma foto
    histórica) — devolve None nos mesmos casos de search_stock_clip."""
    if not PEXELS_API_KEYS or not query:
        return None
    query = _normalize_query(query)

    orientation = "portrait" if height > width else "landscape"
    rotator = _rotator_for(PEXELS_API_KEYS)

    for api_key in rotator.order():
        try:
            resp = httpx.get(
                PHOTO_SEARCH_URL,
                headers={"Authorization": api_key},
                params={"query": query, "orientation": orientation, "per_page": 15},
                timeout=20,
            )
            if resp.status_code in (401, 403):
                rotator.ban(api_key)
                continue
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
        except Exception as exc:
            log.warning("busca de foto pexels falhou (%r): %s", query, exc)
            continue

        if not photos:
            log.info("pexels sem foto pra: %r", query)
            return None

        for chosen in _rank_candidates(query, photos, "photo", lambda p: p.get("alt", ""))[:MAX_VISUAL_TRIES]:
            src = chosen.get("src", {})
            url = src.get("large2x") or src.get("original") or src.get("large")
            if not url:
                continue
            try:
                photo_resp = httpx.get(url, timeout=30, follow_redirects=True)
                photo_resp.raise_for_status()
            except Exception as exc:
                log.warning("download da foto pexels falhou (%r): %s", query, exc)
                continue
            if check is not None:
                verdict = check(photo_resp.content)
                if verdict is False or (verdict is None and _SPORTS_QUERY.search(query)):
                    continue
            _mark_recent("photo", str(chosen.get("id")))
            log.info("foto real encontrada pra %r", query)
            return photo_resp.content
        log.info("pexels: nenhuma foto no contexto da cena pra %r", query)
        return None

    return None
