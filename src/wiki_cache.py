"""Cache em disco das consultas às APIs da Wikimedia (Wikipédia pt/en e
Commons) enquanto os vídeos do dia são feitos: o conserto em laço refaz o
mesmo tema várias vezes e a consulta não volta ao servidor — em 03/11 os
testes seguidos levaram 429, e insistir dá bloqueio do IP (política de
robôs da Wikimedia). Só resposta 200 entra. O daily_run apaga tudo no fim
da rodada (clear); o prazo de 30 dias é só a rede de segurança."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("wiki_cache")

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "wikimedia"
TTL_DAYS = 30
UA = {"User-Agent": "FractalCurioso-video-bot/1.0 (https://www.youtube.com/@fractalcurioso)"}
# intervalo mínimo entre pedidos reais (os do cache não contam)
_MIN_INTERVAL = 1.0
_last_call = 0.0


def _key(url: str, params: dict) -> Path:
    raw = json.dumps([url, sorted((str(k), str(v)) for k, v in params.items())], ensure_ascii=False)
    return CACHE / f"{hashlib.sha1(raw.encode()).hexdigest()}.json"


def get_json(url: str, params: dict, timeout: float = 20, ttl_days: float = TTL_DAYS) -> dict:
    """GET com cache; 429 espera (Retry-After) e tenta de novo até 3 vezes.
    Levanta httpx.HTTPError se não conseguir — quem chama decide o fallback."""
    global _last_call
    path = _key(url, params)
    if path.exists() and time.time() - path.stat().st_mtime < ttl_days * 86400:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
    for attempt in range(4):
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        resp = httpx.get(url, params=params, headers=UA, timeout=timeout, follow_redirects=True)
        if resp.status_code == 429 and attempt < 3:
            delay = float(resp.headers.get("retry-after") or 0) or 10 * (attempt + 1)
            log.warning("wikimedia 429 — esperando %.0fs", delay)
            time.sleep(delay)
            continue
        resp.raise_for_status()
        data = resp.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))
        return data
    return {}


def clear() -> None:
    """Apaga as consultas guardadas (fim da rodada diária)."""
    import shutil

    shutil.rmtree(CACHE, ignore_errors=True)
