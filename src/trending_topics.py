"""Busca trending topics para gerar temas de vídeos dinâmicos.

Fontes (em ordem de prioridade):
1. YouTube Trends (youtube.trends24.in) ← MELHOR para YT (precisa scraping JS)
2. Google Trends (pytrends) ← genérico, requer: pip install pytrends
3. X/Twitter API ← precisa credenciais
4. Temas pré-definidos (fallback)

STATUS: Infraestrutura pronta, sem API ativa (usa fallback config)

PRÓXIMOS PASSOS:
- Instalar pytrends: pip install pytrends
- Ou: Configurar Selenium para YouTube Trends (JS rendering)
- Ou: X/Twitter API keys em ~/.secrets/keys.env

Objetivo: evitar séries estáticas e mortas; usar temas que estão
em alta no momento (mais engagement, melhor SEO no YouTube).
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
    # 1. Google Trends RSS (ariaBot method) — grátis, sem chave, sem autenticação!
    geo = "BR" if country.upper() in ("BR", "PT") else "US"
    try:
        url = f"https://trends.google.com/trending/rss?geo={geo}"
        resp = requests.get(url, timeout=TRENDS_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        # Parse RSS (mesmo método do ariaBot)
        trends = []
        for m in re.finditer(r'<item>([\s\S]*?)</item>', resp.text):
            item = m.group(1)
            title_match = re.search(r'<title>([\s\S]*?)</title>', item)
            if not title_match:
                continue
            title = title_match.group(1)
            # Remove CDATA
            title = re.sub(r'<!\[CDATA\[([\s\S]*?)\]\]>', r'\1', title)
            # Descodifica HTML entities
            title = title.replace('&amp;', '&').replace('&quot;', '"')
            title = title.replace('&#39;', "'").replace('&lt;', '<').replace('&gt;', '>')
            title = title.strip()
            if title:
                trends.append(title)

        if trends:
            log.info("Google Trends RSS (%s): %d tópicos", geo, len(trends))
            return trends[:20]
    except Exception as exc:
        log.debug("Google Trends RSS: %s", exc)

    # 2. Google Trends pytrends (alternativa se RSS falhar)
    try:
        from pytrends.request import TrendReq
        gt = TrendReq(hl="pt-BR", timeout=(5, 10))
        trends_df = gt.trending_searches(pn=country)
        if trends_df is not None and len(trends_df) > 0:
            trends = trends_df[0].tolist()[:20]
            log.info("Google Trends pytrends (%s): %d tópicos", country, len(trends))
            return trends
    except ImportError:
        pass
    except Exception as exc:
        log.debug("Google Trends pytrends: %s", exc)

    # 3. Fallback: config viral_queries
    log.debug("Trending topics: usando config fallback")
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


def record_trend_use(channel: str, topic: str, source: str, success: bool = True) -> None:
    """Registra uso de trending topic para monitoramento (painel)."""
    try:
        from pathlib import Path
        from datetime import datetime, timezone

        monitor_file = Path(__file__).resolve().parent.parent / "data" / "trending_monitor.json"

        # Carrega dados
        data = {}
        if monitor_file.exists():
            data = json.loads(monitor_file.read_text())

        # Inicializa estruturas
        if "sources" not in data:
            data["sources"] = {
                "x_twitter": {"success": 0, "failed": 0, "last_success": None},
                "google_trends": {"success": 0, "failed": 0, "last_success": None},
                "youtube_trends": {"success": 0, "failed": 0, "last_success": None},
                "config_fallback": {"success": 0, "failed": 0, "last_success": None},
            }
        if "channels" not in data:
            data["channels"] = {}
        if "history" not in data:
            data["history"] = []

        # Atualiza contadores
        if source in data["sources"]:
            if success:
                data["sources"][source]["success"] += 1
                data["sources"][source]["last_success"] = datetime.now(timezone.utc).isoformat()
            else:
                data["sources"][source]["failed"] += 1

        # Atualiza canal
        if channel not in data["channels"]:
            data["channels"][channel] = {"count": 0, "last_topic": None, "source": None}
        data["channels"][channel]["count"] += 1
        data["channels"][channel]["last_topic"] = topic[:60]
        data["channels"][channel]["source"] = source

        # Histórico
        data["history"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "topic": topic[:60],
            "source": source,
            "success": success,
        })
        data["history"] = data["history"][-100:]

        # Salva
        monitor_file.parent.mkdir(parents=True, exist_ok=True)
        monitor_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

        if success:
            log.debug("[%s] trend logged: %s (%s)", channel, topic[:40], source)
    except Exception as exc:
        log.debug("Monitor logging failed: %s", exc)


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
