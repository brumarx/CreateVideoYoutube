"""Busca de fatos reais na internet (Tavily, grátis — free tier voltado
pra grounding de LLM, com fonte/URL em cada resultado) pra ancorar o
roteiro quando o TEMA foi digitado por uma pessoa em vez de vir da fila
curada ou do banco de dados interno.

Motivação: um tema digitado à mão pode ser sobre qualquer coisa — inclusive
um evento real específico ou pessoa real nomeada, onde "conhecimento geral
do LLM" não é confiável o bastante (pode estar desatualizado ou errado) e
inventar fato é o pior erro possível no canal de política. Em vez de
recusar o tema, busca fato real com fonte antes de escrever o roteiro.

Sem TAVILY_API_KEYS configurada (ou sem resultado), devolve None e quem
chamou cai pro comportamento antigo (roteiro só com conhecimento geral do
LLM) — nunca bloqueia nem quebra o pipeline por causa disso.
"""
from __future__ import annotations

import logging

import httpx

from .config import TAVILY_API_KEYS
from .providers import _rotator_for

log = logging.getLogger("web_search")

SEARCH_URL = "https://api.tavily.com/search"


def search_topic_facts(query: str, max_results: int = 5) -> list[dict] | None:
    """Busca `query` na internet e devolve uma lista de
    {"titulo", "url", "trecho"} com fatos/trechos reais e a fonte de cada
    um — ou None (sem chave configurada, sem resultado, ou erro de rede).
    Nunca levanta exceção; quem chamar decide o fallback."""
    if not TAVILY_API_KEYS or not query:
        return None

    rotator = _rotator_for(TAVILY_API_KEYS)
    for api_key in rotator.order():
        try:
            resp = httpx.post(
                SEARCH_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                },
                timeout=20,
            )
            if resp.status_code in (401, 403):
                rotator.ban(api_key)
                continue
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            log.warning("busca tavily falhou (%r): %s", query, exc)
            continue

        if not results:
            log.info("tavily sem resultado pra: %r", query)
            return None

        facts = [
            {"titulo": r.get("title", ""), "url": r.get("url", ""), "trecho": (r.get("content") or "")[:500]}
            for r in results
            if r.get("content")
        ]
        if not facts:
            return None
        log.info("busca real encontrada pra %r (%d resultados)", query, len(facts))
        return facts

    return None
