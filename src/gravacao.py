"""26/11: gravação e repetição das chamadas ao modelo.

Toda chamada real (cascata ou juiz) fica gravada em data/gravacoes/AAAA-MM-DD.jsonl: o pedido, a resposta,
o modelo, a etapa e se a resposta passou na validação. Serve para duas coisas:

1. **Corpus**: as respostas recusadas (formato que o código não leu) ficam guardadas de verdade. Antes,
   "factual: sustentada" só apareceu por acaso no log, depois de 4 pedidos perdidos e um job parado.
2. **Repetição**: com GRAVACAO_REPETIR=arquivo(s) separados por ":", `providers.complete` devolve a resposta
   gravada para o mesmo pedido, sem chamar ninguém. Uma mudança de código é testada repetindo uma rodada real
   inteira, de graça e em minutos, antes de gastar uma rodada nova. Pedido que não está na gravação (o
   prompt mudou) levanta SemGravacao — ou, com GRAVACAO_MISTA=1, vai ao modelo de verdade (e é gravado).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "data" / "gravacoes"
_trava = threading.Lock()
_repeticao: dict[str, list[str]] | None = None
_usadas: dict[str, int] = {}


class SemGravacao(RuntimeError):
    """Repetição: o pedido não está na gravação (o prompt mudou)."""


from .vocabulario import re_mes

_MESES = re_mes()
_DATA = re.compile(rf"\b\d{{1,2}}/\d{{1,2}}/\d{{4}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}} de ({_MESES})( de \d{{4}})?\b",
                   re.I)


def chave(messages: list[dict], max_tokens: int, temperature: float, juiz: bool) -> str:
    """A data de hoje entra em alguns pedidos (regra de data do escritor): fora da chave, senão a gravação de
    ontem não serve hoje."""
    textos = [_DATA.sub("<data>", str(m.get("content", ""))) + "|" + str(m.get("role", "")) for m in messages]
    bruto = json.dumps([textos, max_tokens, round(float(temperature), 3), bool(juiz)], ensure_ascii=False)
    return hashlib.sha1(bruto.encode()).hexdigest()


def ligada() -> bool:
    return "PYTEST_CURRENT_TEST" not in os.environ and os.environ.get("GRAVACAO_DESLIGADA") != "1"


def gravar(k: str, messages: list[dict], resposta: str | None, modelo: str, aceita: bool, etapa: str,
           juiz: bool) -> None:
    if not ligada() or resposta is None:
        return
    pedido = "\n".join(str(m.get("content", "")) for m in messages)
    linha = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "chave": k, "etapa": etapa,
             "modelo": modelo, "juiz": juiz, "aceita": aceita, "pedido": pedido, "resposta": resposta,
             "job": os.environ.get("JOB_ID", "")}
    try:
        with _trava:
            DIR.mkdir(parents=True, exist_ok=True)
            with open(DIR / f"{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except OSError:
        pass  # gravar nunca derruba a rodada


def _carregar() -> dict[str, list[str]] | None:
    global _repeticao
    arquivos = [a for a in os.environ.get("GRAVACAO_REPETIR", "").split(":") if a]
    if not arquivos:
        return None
    if _repeticao is None:
        _repeticao = {}
        for a in arquivos:
            for linha in open(a, encoding="utf-8"):
                try:
                    d = json.loads(linha)
                except ValueError:
                    continue
                if d.get("aceita"):
                    _repeticao.setdefault(d["chave"], []).append(d["resposta"])
    return _repeticao


def repetir(k: str) -> str | None:
    """Resposta gravada para o pedido (na ordem em que foram gravadas, para pedidos repetidos), ou None se
    a repetição está desligada. SemGravacao se ligada e o pedido não está lá (salvo GRAVACAO_MISTA=1)."""
    tabela = _carregar()
    if tabela is None:
        return None
    respostas = tabela.get(k)
    if respostas:
        n = _usadas.get(k, 0)
        _usadas[k] = n + 1
        return respostas[min(n, len(respostas) - 1)]
    if os.environ.get("GRAVACAO_MISTA") == "1":
        return None
    raise SemGravacao(f"pedido {k[:12]} não está na gravação (o prompt mudou?)")


def recusadas(arquivos: list[Path] | None = None) -> list[dict]:
    """O corpus das respostas que não passaram na validação (para teste e diagnóstico)."""
    out = []
    for a in arquivos or sorted(DIR.glob("*.jsonl")):
        for linha in open(a, encoding="utf-8"):
            try:
                d = json.loads(linha)
            except ValueError:
                continue
            if not d.get("aceita"):
                out.append(d)
    return out


def guardar_partida(job_id, partida: dict) -> None:
    """O que a redação recebe (canal, tema, durações, banco) — para repetir a rodada depois."""
    if not ligada():
        return
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"partida_{job_id}.json").write_text(json.dumps(partida, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
