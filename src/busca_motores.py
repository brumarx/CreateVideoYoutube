"""Busca na web pelos motores grátis da biblioteca ddgs, com tentativas e rodízio de motores (22/11).

Antes: uma chamada `DDGS().text()` no modo automático; qualquer falha (motor bloqueado, desafio anti-robô,
limite por minuto) virava "No results found" e a consulta era perdida — em Svalbard as 4 consultas de fonte
oficial caíram assim (~40% das buscas falhavam desde 13/11) e o banco ficou só com a Wikipédia.

Agora cada consulta tem até TENTATIVAS rodadas. Em cada rodada os motores disponíveis são consultados um a um
(os ainda não tentados nesta consulta primeiro), até juntar resultados suficientes:
- 403 ou desafio anti-robô (202 do DuckDuckGo) → o motor fica de fora por ESPERA_BLOQUEIO (não se insiste);
- 429/503 → fica de fora pelo Retry-After (ou ESPERA_LIMITE se o motor não disser);
- erro de rede/tempo → fica de fora por ESPERA_ERRO;
- 200 sem resultado → sem castigo (a consulta é que não achou nada nele).
O castigo vale para o processo inteiro (as consultas seguintes não batem de novo no motor bloqueado). Entre
rodadas, espera curta (ou até o primeiro motor voltar, se todos estiverem de fora), sempre dentro de
ORCAMENTO_S por consulta — o tempo de processamento nunca cresce sem limite. Resultado repetido (mesma URL,
de qualquer motor ou rodada) entra uma vez só; cada um guarda o motor de origem, a posição (relevância) e a
data da busca. Consulta sem resultado depois das tentativas fica registrada com o motivo de cada motor.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("busca_motores")

TENTATIVAS = 3
ESPERA_BLOQUEIO = 900.0  # 403 / desafio anti-robô: 15 min fora
ESPERA_LIMITE = 120.0  # 429/503 sem Retry-After
ESPERA_ERRO = 60.0  # rede, tempo esgotado
ESPERA_MAX_RODADA = 20.0  # nunca dorme mais que isso entre duas rodadas
ORCAMENTO_S = 75.0  # tempo total máximo de uma consulta (todas as rodadas)
TIMEOUT_MOTOR = 10  # segundos por pedido

# nome do motor -> instante (time.monotonic) até quando fica de fora
_fora_ate: dict[str, float] = {}
# consultas sem resultado depois das tentativas (diagnóstico do job)
falhas_definitivas: list[dict] = []

_relogio = time.monotonic
_dormir = time.sleep


def _motores() -> dict[str, type]:
    """Motores de texto que a ddgs instalada oferece (os desativados por ela já ficam de fora)."""
    try:
        from ddgs.engines import ENGINES
    except Exception:  # noqa: BLE001 — sem ddgs: sem busca
        return {}
    return dict(ENGINES.get("text", {}))


def _segundos_retry_after(valor: str | None) -> float | None:
    if not valor:
        return None
    valor = valor.strip()
    try:
        return max(0.0, float(valor))
    except ValueError:
        pass
    try:
        quando = parsedate_to_datetime(valor)
        return max(0.0, (quando - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def consultar_motor(nome: str, cls: type, query: str, region: str) -> tuple[str, list[dict], float | None]:
    """Uma consulta num motor. Devolve (situação, resultados, retry_after): situação é 'ok', 'vazio',
    'bloqueado', 'limite' ou 'erro'."""
    estado: dict = {}
    try:
        motor = cls(timeout=TIMEOUT_MOTOR)
        pedir = motor.http_client.request

        def pedido(*a, **k):
            resp = pedir(*a, **k)
            estado["status"] = resp.status_code
            try:
                estado["retry_after"] = (resp._resp.headers or {}).get("retry-after")  # noqa: SLF001
            except Exception:  # noqa: BLE001
                estado["retry_after"] = None
            return resp

        motor.http_client.request = pedido
        brutos = motor.search(query, region=region) or []
    except Exception as exc:  # noqa: BLE001 — motor fora do ar não derruba a busca
        status = estado.get("status")
        if status in (403, 202):
            return "bloqueado", [], None
        if status in (429, 503):
            return "limite", [], _segundos_retry_after(estado.get("retry_after"))
        log.info("busca: motor %s com erro (%s)", nome, str(exc)[:120])
        return "erro", [], None
    status = estado.get("status")
    resultados = [{"titulo": getattr(r, "title", "") or "", "url": getattr(r, "href", "") or "",
                   "trecho": getattr(r, "body", "") or ""} for r in brutos]
    resultados = [r for r in resultados if r["url"]]
    if resultados:
        return "ok", resultados, None
    if status in (403, 202):
        return "bloqueado", [], None
    if status in (429, 503):
        return "limite", [], _segundos_retry_after(estado.get("retry_after"))
    if status not in (None, 200):
        return "erro", [], None
    return "vazio", [], None


def _chave_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return urlunsplit(("", host, p.path.rstrip("/"), p.query, ""))


def _castigar(nome: str, situacao: str, retry_after: float | None, agora: float) -> None:
    if situacao == "bloqueado":
        _fora_ate[nome] = agora + ESPERA_BLOQUEIO
    elif situacao == "limite":
        _fora_ate[nome] = agora + (retry_after if retry_after is not None else ESPERA_LIMITE)
    elif situacao == "erro":
        _fora_ate[nome] = agora + ESPERA_ERRO


def buscar(query: str, max_results: int = 10, region: str = "br-pt") -> list[dict]:
    """Resultados da web para `query` (sem repetição de URL), ou [] depois das tentativas. Cada resultado:
    {titulo, url, trecho, origem (motor), relevancia (1 = primeiro), buscado_em (data UTC)}."""
    motores = _motores()
    inicio = _relogio()
    achados: list[dict] = []
    vistos: set[str] = set()
    tentados: set[str] = set()
    situacoes: dict[str, str] = {}
    rodada = 0
    for rodada in range(1, TENTATIVAS + 1):
        agora = _relogio()
        disponiveis = [n for n in motores if _fora_ate.get(n, 0) <= agora]
        # os ainda não tentados nesta consulta primeiro; depois por prioridade da própria ddgs
        disponiveis.sort(key=lambda n: (n in tentados, -float(getattr(motores[n], "priority", 1))))
        novos = 0
        for nome in disponiveis:
            if _relogio() - inicio >= ORCAMENTO_S:
                break
            situacao, resultados, retry = consultar_motor(nome, motores[nome], query, region)
            tentados.add(nome)
            sit = situacao + (f" (Retry-After {retry:.0f} s)" if retry is not None else "")
            situacoes[nome] = f"{situacoes[nome]}→{sit}" if nome in situacoes else sit
            _castigar(nome, situacao, retry, _relogio())
            for r in resultados:
                k = _chave_url(r["url"])
                if k in vistos:
                    continue
                vistos.add(k)
                achados.append({**r, "origem": nome, "relevancia": len(achados) + 1,
                                "buscado_em": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
                novos += 1
            if len(achados) >= max_results:
                break
        if len(achados) >= max_results or (achados and novos == 0):
            break
        if achados and all(n in tentados for n in motores if _fora_ate.get(n, 0) <= _relogio()):
            # já tem resultado e todo motor disponível já respondeu esta consulta: repetir não traz nada novo
            break
        if rodada == TENTATIVAS:
            break
        resta = ORCAMENTO_S - (_relogio() - inicio)
        if resta <= 0:
            break
        agora = _relogio()
        if all(_fora_ate.get(n, 0) > agora for n in motores):
            # todos de fora: espera o primeiro voltar, se couber no orçamento
            espera = min(_fora_ate[n] for n in motores) - agora
            if espera > min(resta, ESPERA_MAX_RODADA):
                break
        else:
            espera = 2.0 * rodada
        if espera > 0:
            _dormir(min(espera, resta, ESPERA_MAX_RODADA))
    if achados:
        log.info("busca %r: %d resultado(s) em %d motor(es) — %s", query[:60], len(achados), len(tentados),
                 ", ".join(f"{n}={s}" for n, s in situacoes.items()))
        return achados[:max_results]
    motivo = ", ".join(f"{n}={s}" for n, s in situacoes.items()) or "nenhum motor disponível (todos em espera)"
    falhas_definitivas.append({"consulta": query, "motores": dict(situacoes),
                               "em": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")})
    log.warning("busca sem resultado depois de %d rodada(s) (%r): %s", min(TENTATIVAS, rodada), query[:80], motivo)
    try:
        from . import metricas
        metricas.somar("busca_sem_resultado")
    except Exception:  # noqa: BLE001
        pass
    return []
