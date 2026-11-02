"""Busca trending topics para gerar temas de vídeos dinâmicos.

Fontes (em ordem de prioridade):
1. Google Trends (via pytrends, se disponível)
2. Scraping direto do X/Twitter (fallback)
3. Temas pré-definidos por canal

Objetivo: evitar séries estáticas e mortas; usar temas que estão
em alta no momento (mais engagement, melhor SEO).
"""
import json
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger("trending_topics")

CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "trending_topics_cache.json"
TRENDS_TIMEOUT = 10

# Por país — Brasil usa "br" ou "BR"
COUNTRY_CODES = {
    "brasil": "BR",
    "eua": "US",
    "portugal": "PT",
}


def _fetch_trends(country: str = "BR") -> list[str] | None:
    """Busca trending topics usando Google Trends ou fallback.

    Retorna lista de strings (ex: ["reforma tributária", "STF", "dólar"])
    ou None se todas as fontes falharem.
    """
    # TODO: Implementar integração com Google Trends (pytrends)
    # ou X/Twitter API quando dependências estiverem disponíveis
    log.debug("Trending topics: aguardando integração com API externa")
    return None


def _save_cache(trends: list[str], country: str = "BR") -> None:
    """Salva trending topics em cache local (TTL ~1h)."""
    from datetime import datetime, timezone

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    cache[country] = {
        "trends": trends,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def _load_cache(country: str = "BR") -> list[str] | None:
    """Carrega trending topics do cache se estiver fresco (<1h)."""
    from datetime import datetime, timedelta, timezone

    if not CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(CACHE_FILE.read_text())
        entry = cache.get(country)
        if not entry:
            return None
        cached_at = datetime.fromisoformat(entry["cached_at"])
        age = datetime.now(timezone.utc) - cached_at
        if age < timedelta(hours=1):
            return entry.get("trends", [])
    except Exception as exc:
        log.warning("Cache de trends corrompido: %s", exc)
    return None


def get_trending_topics(
    country: str = "BR",
    fallback_topics: list[str] | None = None,
    force_refresh: bool = False,
) -> list[str]:
    """Retorna trending topics do X para um país.

    Estratégia:
    1. Tenta carregar do cache (se < 1h)
    2. Se force_refresh ou cache expirado, busca na API
    3. Se API falhar, retorna fallback_topics
    4. Se fallback vazio, retorna cache velho ou []

    Args:
        country: código do país ("BR", "US", "PT")
        fallback_topics: temas pré-definidos (se API/cache falhar)
        force_refresh: ignora cache e busca direto da API

    Returns:
        Lista de trending topics (strings em português pro Brasil)
    """
    fallback = fallback_topics or []

    if not force_refresh:
        cached = _load_cache(country)
        if cached:
            return cached

    trends = _fetch_trends(country)
    if trends:
        _save_cache(trends, country)
        return trends

    log.warning("trending topics falhou para %s; usando fallback (%d temas)", country, len(fallback))
    return fallback


def filter_topics(
    topics: list[str],
    exclude_patterns: list[str] | None = None,
    min_length: int = 3,
) -> list[str]:
    """Filtra trending topics por critérios.

    Remove tópicos que:
    - Correspondem a padrões de exclusão (regex)
    - Têm menos de `min_length` caracteres
    - São URLs, hashtags de promocão ou vídeos
    """
    exclude = exclude_patterns or []
    patterns = [
        r"https?://",  # URLs
        r"^#\w+$",     # hashtags puras
        r"promoção|cupom|desconto|grátis|free|compre",
        r"(live|stream|transmiss)",
    ]
    patterns.extend(exclude)

    filtered = []
    for topic in topics:
        if len(topic) < min_length:
            continue
        if any(re.search(p, topic, re.IGNORECASE) for p in patterns):
            continue
        filtered.append(topic)

    return filtered


def topic_for_channel(
    channel_name: str,
    trending: list[str] | None = None,
) -> str | None:
    """Escolhe um trending topic adequado para um canal específico.

    Estratégia por canal:
    - aprendervia: temas com palavras-chave tech/IA/carreira
    - curiosidades: qualquer trending serve
    - motivacional: evita política/polêmica; procura temas inspiradores
    - politica: prioriza política/economia; evita celebridade/memes
    """
    if not trending:
        return None

    channel = channel_name.lower()

    if channel == "aprendervia":
        # Procura temas tech, IA, carreira, educação
        keywords = [
            "ia", "inteligencia", "artificial", "python", "javascript",
            "tech", "programação", "carreira", "emprego", "trabalho",
            "algoritmo", "dados", "código", "startup", "desenvolvedor",
        ]
        for topic in trending:
            if any(kw in topic.lower() for kw in keywords):
                return topic
        # Se não achar palavra-chave, pega o 1º trending (pode virar "IA grátis para X")
        return trending[0] if trending else None

    elif channel == "curiosidades":
        # Qualquer trending topic pode virar curiosidade
        return trending[0] if trending else None

    elif channel == "motivacional":
        # Evita política, crime, tragédia; procura temas que dão "gancho"
        exclude_bad = [
            "crime", "morte", "acidente", "política", "eleição", "voto",
            "imposto", "direita", "esquerda", "deputado", "senador",
        ]
        for topic in trending:
            if not any(bad in topic.lower() for bad in exclude_bad):
                return topic
        return None  # Sem tema adequado, volta pro fallback

    elif channel == "politica":
        # Prioriza política, economia, social
        keywords = [
            "política", "governo", "congresso", "deputado", "senador",
            "economia", "dólar", "moeda", "imposto", "reforma",
            "STF", "tribunal", "justiça", "lei", "votação",
        ]
        for topic in trending:
            if any(kw in topic.lower() for kw in keywords):
                return topic
        # Fallback: pega 1º trending mesmo que não seja política (notícia quente)
        return trending[0] if trending else None

    return trending[0] if trending else None
