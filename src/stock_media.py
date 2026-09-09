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

import logging
import tempfile
from pathlib import Path

import httpx

from .config import PEXELS_API_KEYS
from .providers import _rotator_for

log = logging.getLogger("stock_media")

VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
MIN_CLIP_HEIGHT = 480  # não usa clipe abaixo disso — qualidade mínima aceitável


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


def search_stock_clip(query: str, width: int, height: int) -> Path | None:
    """Busca um clipe real que combine com `query` (2-4 palavras em
    inglês, ver script_gen.py -> stock_query) e devolve o caminho local do
    arquivo baixado — ou None (sem chave configurada, sem resultado, ou
    qualquer erro de rede/API). Quem chamar decide o fallback; esta função
    nunca levanta exceção."""
    if not PEXELS_API_KEYS or not query:
        return None

    orientation = "portrait" if height > width else "landscape"
    rotator = _rotator_for(PEXELS_API_KEYS)

    for api_key in rotator.order():
        try:
            resp = httpx.get(
                VIDEO_SEARCH_URL,
                headers={"Authorization": api_key},
                params={"query": query, "orientation": orientation, "per_page": 5, "size": "medium"},
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

        picked = _pick_best_file(videos[0], width, height)
        if not picked or not picked.get("link"):
            return None

        try:
            video_resp = httpx.get(picked["link"], timeout=60, follow_redirects=True)
            video_resp.raise_for_status()
        except Exception as exc:
            log.warning("download do clipe pexels falhou (%r): %s", query, exc)
            return None

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(video_resp.content)
        tmp.close()
        log.info("clipe real encontrado pra %r (%sx%s)", query, picked.get("width"), picked.get("height"))
        return Path(tmp.name)

    return None


def search_stock_photo(query: str, width: int, height: int) -> bytes | None:
    """Busca uma FOTO real que combine com `query` e devolve os bytes da
    imagem — mesma interface de src.visuals.generate_image, pra ser
    intercambiável no lugar dela. Meio-termo da cascata: mais crível que
    imagem gerada por IA pra quando não existe CLIPE de vídeo do assunto
    mas existe FOTO real (ex.: um objeto específico, um evento, uma foto
    histórica) — devolve None nos mesmos casos de search_stock_clip."""
    if not PEXELS_API_KEYS or not query:
        return None

    orientation = "portrait" if height > width else "landscape"
    rotator = _rotator_for(PEXELS_API_KEYS)

    for api_key in rotator.order():
        try:
            resp = httpx.get(
                PHOTO_SEARCH_URL,
                headers={"Authorization": api_key},
                params={"query": query, "orientation": orientation, "per_page": 5},
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

        src = photos[0].get("src", {})
        url = src.get("large2x") or src.get("original") or src.get("large")
        if not url:
            return None

        try:
            photo_resp = httpx.get(url, timeout=30, follow_redirects=True)
            photo_resp.raise_for_status()
        except Exception as exc:
            log.warning("download da foto pexels falhou (%r): %s", query, exc)
            return None

        log.info("foto real encontrada pra %r", query)
        return photo_resp.content

    return None
