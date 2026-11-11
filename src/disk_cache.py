"""Cache em disco (JSON) para o que não muda entre rodadas do mesmo tema: busca na
web (com a leitura das páginas) e a lista de fatos extraída das fontes. Antes cada
rodada refazia tudo do zero (11/11: 19 rodadas do Túnel Seikan buscando as mesmas
fontes). Só resultado não vazio entra; o prazo padrão é de 7 dias."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("disk_cache")

BASE = Path(__file__).resolve().parent.parent / "data" / "cache"
TTL_DAYS = 7


def versao_do_codigo(*arquivos: str) -> str:
    """Impressão digital dos arquivos de código que produzem o valor guardado: mudou o código, a
    chave muda e o cache antigo deixa de valer sozinho (11/11: duas vezes o cache devolveu o
    resultado da versão anterior porque a versão manual não foi trocada)."""
    base = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for nome in arquivos:
        try:
            h.update((base / nome).read_bytes())
        except OSError:
            h.update(nome.encode())
    return h.hexdigest()[:12]


def _path(namespace: str, chave) -> Path:
    raw = json.dumps(chave, ensure_ascii=False, sort_keys=True)
    return BASE / namespace / f"{hashlib.sha1(raw.encode()).hexdigest()}.json"


def get(namespace: str, chave, ttl_days: float = TTL_DAYS):
    """Valor guardado, ou None se não existe, venceu ou está corrompido."""
    path = _path(namespace, chave)
    try:
        if time.time() - path.stat().st_mtime >= ttl_days * 86400:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put(namespace: str, chave, valor) -> None:
    if not valor:
        return
    path = _path(namespace, chave)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(valor, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("cache em disco: não gravou %s (%s)", namespace, exc)
