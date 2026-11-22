"""Custo operacional de um job (20/11): chamadas de IA, tempo e modelo por etapa.

Uso: `with metricas.etapa("auditoria"): ...` em volta de cada etapa; providers.complete registra cada
resposta (modelo e segundos) na etapa corrente. `resumo()` devolve o relatório do job e `zerar()` começa
outro. Só conta o que aconteceu — nada aqui decide ou bloqueia."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_local = threading.local()
_dados: dict = {}
_trava = threading.RLock()  # _registro chama zerar() com a trava já pega


def zerar() -> None:
    with _trava:
        _dados.clear()
        _dados.update({"inicio": time.time(), "etapas": {}, "ordem": [], "extras": {}})


def _etapa_atual() -> str:
    pilha = getattr(_local, "pilha", None)
    return pilha[-1] if pilha else "sem_etapa"


def _registro(nome: str) -> dict:
    if not _dados:
        zerar()
    if nome not in _dados["etapas"]:
        _dados["etapas"][nome] = {"chamadas": 0, "falhas": 0, "segundos": 0.0, "segundos_ia": 0.0, "modelos": {}}
        _dados["ordem"].append(nome)
    return _dados["etapas"][nome]


@contextmanager
def etapa(nome: str):
    """Etapa aninhada conta só nela (o tempo da de fora inclui o da de dentro)."""
    pilha = getattr(_local, "pilha", None)
    if pilha is None:
        pilha = _local.pilha = []
    pilha.append(nome)
    t0 = time.time()
    try:
        yield
    finally:
        pilha.pop()
        with _trava:
            _registro(nome)["segundos"] += time.time() - t0


def chamada(modelo: str, segundos: float, ok: bool = True) -> None:
    with _trava:
        r = _registro(_etapa_atual())
        if ok:
            r["chamadas"] += 1
            r["segundos_ia"] += segundos
            r["modelos"][modelo] = r["modelos"].get(modelo, 0) + 1
        else:
            r["falhas"] += 1


def anotar(chave: str, valor) -> None:
    """Fatos do job que não são chamada: reaproveitamento de pesquisa, cenas editadas etc."""
    with _trava:
        if not _dados:
            zerar()
        _dados["extras"][chave] = valor


def somar(chave: str, n: int = 1) -> None:
    with _trava:
        if not _dados:
            zerar()
        _dados["extras"][chave] = _dados["extras"].get(chave, 0) + n


def resumo() -> dict:
    with _trava:
        if not _dados:
            zerar()
        etapas = {k: {**_dados["etapas"][k], "segundos": round(_dados["etapas"][k]["segundos"], 1),
                      "segundos_ia": round(_dados["etapas"][k]["segundos_ia"], 1)} for k in _dados["ordem"]}
        return {"total_segundos": round(time.time() - _dados["inicio"], 1),
                "total_chamadas": sum(e["chamadas"] for e in etapas.values()),
                "etapas": etapas, **dict(_dados["extras"])}


def texto(r: dict | None = None) -> str:
    r = r or resumo()
    linhas = [f"job: {r['total_segundos']:.0f} s, {r['total_chamadas']} chamada(s) de IA"]
    for nome, e in r["etapas"].items():
        mod = ", ".join(f"{m} x{n}" for m, n in e["modelos"].items())
        linhas.append(f"  {nome}: {e['chamadas']} chamada(s), {e['segundos']:.0f} s"
                      + (f" ({e['falhas']} falha(s))" if e["falhas"] else "") + (f" — {mod}" if mod else ""))
    for k, v in r.items():
        if k not in ("total_segundos", "total_chamadas", "etapas"):
            linhas.append(f"  {k}: {v}")
    return "\n".join(linhas)
