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
import re

import httpx

from .config import TAVILY_API_KEYS
from .providers import _rotator_for

log = logging.getLogger("web_search")

SEARCH_URL = "https://api.tavily.com/search"
# rede social e vídeo não são fonte: tema vago ("cidade engolida pelo mar")
# só trazia reels e vídeos do YouTube
EXCLUDED_DOMAINS = [
    "youtube.com", "instagram.com", "facebook.com", "tiktok.com", "x.com",
    "twitter.com", "pinterest.com", "kwai.com", "threads.net", "reddit.com",
]


def search_topic_facts(query: str, max_results: int = 5) -> list[dict] | None:
    """Busca `query` na internet e devolve uma lista de
    {"titulo", "url", "trecho"} com fatos/trechos reais e a fonte de cada
    um — ou None (sem chave configurada, sem resultado, ou erro de rede).
    Nunca levanta exceção; quem chamar decide o fallback. Tavily (1.000
    buscas/mês por chave) primeiro; sem chave, sem cota ou sem resultado,
    DuckDuckGo (grátis, sem chave)."""
    if not query:
        return None
    facts = _tavily(query, max_results) or _duckduckgo(query, max_results)
    facts = _enrich(facts, query) if facts else []
    wiki = wikipedia_facts(query)
    if wiki:
        # Wikipédia primeiro (o artigo inteiro do assunto, pt + en) e só 2
        # trechos curtos da busca: cabe no limite por minuto do Groq
        seen = {w["url"] for w in wiki}
        facts = wiki + [{**f, "trecho": f["trecho"][:500]} for f in facts if f["url"] not in seen
                        and "wikipedia.org" not in f["url"]][:2]
    return facts or None


# Wikipédia como fonte principal (03/11): a busca trazia 4 trechos de 800
# caracteres de páginas quaisquer e o revisor cortava até fato verdadeiro e
# famoso ("Tratado de Ancón 1884" na Bolívia, job 700) — o roteiro ficava
# sem a virada. O artigo inteiro do assunto, pt e en, rankeado por frases
# com data/número sobre o tema, sustenta a história toda.
_WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
_WIKI_HEADERS = {"User-Agent": "FractalCurioso-video-bot/1.0 (https://www.youtube.com/@fractalcurioso)"}
WIKI_CHARS = {"pt": 5000, "en": 5000}  # 10 mil no total: cabe no teto de 12 mil das fontes do roteiro (antes 1400/1000 deixava o roteiro com um pedaço do artigo)


def _wiki_get(lang: str, params: dict) -> dict:
    """Com cache em disco de 30 dias (src/wiki_cache.py): a mesma consulta
    não volta ao servidor — insistir na Wikipédia dá 429 e bloqueio."""
    from .wiki_cache import get_json

    return get_json(_WIKI_API.format(lang=lang), {**params, "format": "json", "formatversion": 2}, timeout=15)


def _wiki_search(lang: str, query: str) -> str | None:
    """Título do artigo que mais casa com o tema (sem desambiguação)."""
    data = _wiki_get(lang, {"action": "query", "list": "search", "srsearch": query, "srlimit": 5,
                            "srprop": "snippet"})
    keys = _keywords(query)
    best, best_score = None, 0
    for i, hit in enumerate(data.get("query", {}).get("search", [])):
        title = hit["title"]
        if "desambigua" in title.lower() or "disambiguation" in title.lower():
            continue
        score = 2 * len(keys & _keywords(title)) + len(keys & _keywords(re.sub(r"<[^>]+>", "", hit.get("snippet", "")))) - i * 0.5
        if score > best_score:
            best, best_score = title, score
    return best


def _wiki_article(lang: str, title: str) -> tuple[str, str, str | None]:
    """(texto, url, título em inglês) do artigo."""
    data = _wiki_get(lang, {"action": "query", "prop": "extracts|langlinks|info", "titles": title,
                            "explaintext": 1, "exsectionformat": "plain", "redirects": 1, "lllang": "en",
                            "inprop": "url"})
    page = (data.get("query", {}).get("pages") or [{}])[0]
    links = page.get("langlinks") or []
    return page.get("extract") or "", page.get("fullurl") or "", (links[0].get("title") if links else None)


def _dense_passage(text: str, keys: set[str], limit: int) -> str:
    """Abertura do artigo (contexto) + as frases com mais tema E data/número
    (é onde está a virada: tratado, ano, quantidade), na ordem original."""
    text = re.split(r"\n(?:Ver também|Referências|Bibliografia|Ligações externas|See also|References|Notes|"
                    r"Further reading|External links)\n", text)[0]
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text) if 40 < len(x.strip()) < 400]
    if not sentences:
        return ""
    lead = set(range(min(2, len(sentences))))

    def score(i: int) -> float:
        sent = sentences[i]
        return 2 * len(keys & _keywords(sent)) + (1.5 if re.search(r"\d", sent) else 0)

    chosen, size = set(lead), sum(len(sentences[i]) + 1 for i in lead)
    for i in sorted(range(len(sentences)), key=lambda i: -score(i)):
        if i in chosen or score(i) < 2 or size + len(sentences[i]) > limit:
            continue
        chosen.add(i)
        size += len(sentences[i]) + 1
    return " ".join(sentences[i] for i in sorted(chosen))


def _article_titles(query: str) -> dict[str, list[str]]:
    """Títulos de artigos da Wikipédia (pt e en) que explicam o tema — a
    pergunta do tema não serve de busca ("se apaga à noite" achou um mangá;
    "Bolívia não tem mar" achou o artigo genérico do país)."""
    import json

    from .providers import complete

    prompt = (
        "Tema de um vídeo: " + query + "\n\nListe os títulos EXATOS de artigos da Wikipédia que explicam esse "
        "tema por dentro: o evento, a causa, o tratado, a obra, o fenômeno — nunca só o artigo genérico do "
        "país ou da cidade, a não ser que não exista outro. Até 3 em PORTUGUÊS e até 2 em INGLÊS (a "
        "Wikipédia em inglês costuma ter o artigo específico que falta na portuguesa, ex.: 'Energy in North "
        "Korea'). Do mais específico pro mais geral. Responda SÓ com JSON: "
        "{\"pt\": [\"Título\"], \"en\": [\"Title\"]}"
    )
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=300, validate=lambda r: '"en"' in r)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m[0]) if m else {}
    except Exception as exc:  # noqa: BLE001 — sem LLM: busca pelo texto do tema
        log.info("títulos de artigo via LLM falharam: %s", exc)
        data = {}
    return {lang: [str(t).strip() for t in data.get(lang) or [] if str(t).strip()][:3] for lang in ("pt", "en")}


def _proper_keys(text: str) -> set[str]:
    # a 1ª palavra do título sempre tem maiúscula ("Energia na Coreia"): não conta
    words = text.split()
    return _keywords(" ".join(w for w in words[1:] if re.match(r"[A-ZÀ-Ý]", w)))


def _resolve(lang: str, title: str) -> tuple[str, str, str, str | None] | None:
    """(título, texto, url, título en) do artigo pedido; se não existir, o
    da busca — só se ele tiver o nome próprio pedido ("Coreia"): a busca
    por "Apagões na Coreia do Norte" devolvia a Venezuela."""
    text, url, en = _wiki_article(lang, title)
    if text:
        return title, text, url, en
    found = _wiki_search(lang, title)
    if not found:
        return None
    proper = _proper_keys(title)
    subject = _keywords(title) - proper
    got = _keywords(found)
    # nome próprio E assunto ("Crise energética na Coreia do Norte" não vira
    # "Lista de estações de rádio da Coreia do Norte")
    if (proper and not proper & got) or (subject and not subject & got):
        return None
    text, url, en = _wiki_article(lang, found)
    return (found, text, url, en) if text else None


def wikipedia_facts(query: str) -> list[dict]:
    """Artigos do assunto na Wikipédia em português e em inglês (escolhidos
    pelo LLM e conferidos), rankeados por frase com data/número sobre o tema.
    Lista vazia se não achar ou a rede falhar."""
    out: list[dict] = []
    keys = _keywords(query)
    try:
        titles = _article_titles(query)
        if not titles["pt"] and not titles["en"]:
            titles["pt"] = [t for t in [_wiki_search("pt", query)] if t]
        picked: dict[str, list] = {"pt": [], "en": []}
        urls: set[str] = set()
        for lang in ("pt", "en"):
            for t in titles[lang]:
                art = _resolve(lang, t)
                if art is None or art[2] in urls:
                    continue
                # artigo que não fala do tema (palavra-chave nenhuma) não entra;
                # em inglês as palavras do tema não batem ("Korea"), vale o título pedido
                if lang == "pt" and len(keys & _keywords(art[1][:20000])) < max(1, len(keys) // 3):
                    continue
                urls.add(art[2])
                picked[lang].append(art)
        if not picked["en"]:
            en_title = next((a[3] for a in picked["pt"] if a[3]), None)
            art = _resolve("en", en_title) if en_title else None
            if art is not None and art[2] not in urls:
                picked["en"].append(art)
        # o idioma que achou menos cede o espaço pro outro
        total = WIKI_CHARS["pt"] + WIKI_CHARS["en"]
        share = {"pt": WIKI_CHARS["pt"] if picked["en"] else total, "en": WIKI_CHARS["en"] if picked["pt"] else total}
        for lang, label in (("pt", "Wikipédia"), ("en", "Wikipedia (en)")):
            arts = picked[lang][:2]
            for t, text, url, _ in arts:
                lang_keys = keys if lang == "pt" else set()
                passage = _dense_passage(text, lang_keys | _keywords(t) | _keywords(" ".join(titles[lang])),
                                         share[lang] // len(arts))
                if passage:
                    out.append({"titulo": f"{label}: {t}", "url": url, "trecho": passage})
    except Exception as exc:  # noqa: BLE001 — Wikipédia fora: fica a busca normal
        log.warning("wikipédia falhou (%r): %s", query, exc)
    if out:
        log.info("wikipédia: %s", " + ".join(f["titulo"] for f in out))
    return out


# trecho por fonte depois de abrir a página: o resumo da busca (1-2 frases)
# não sustentava quase nada e a revisão reprovava todo detalhe como
# "fora_da_fonte" (21/10: Tavily sem cota desde 17/10, só DuckDuckGo).
# 4 x 800 caracteres cabem no limite por minuto do Groq (prompt ~7 mil tokens)
ENRICHED_SOURCES = 4
ENRICHED_CHARS = 800
_STOPWORDS = {
    "como", "para", "pela", "pelo", "com", "sem", "que", "uma", "dos", "das", "nos", "nas", "por",
    "mais", "seu", "sua", "the", "and", "how", "from", "with", "este", "esta", "isso", "toda", "todo",
}


def _keywords(text: str) -> set[str]:
    import unicodedata

    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w[:6] for w in re.findall(r"\w+", text) if len(w) > 3 and w not in _STOPWORDS}


def _page_text(url: str) -> str:
    try:
        import trafilatura

        resp = httpx.get(url, timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
        })
        resp.raise_for_status()
        return trafilatura.extract(resp.text) or ""
    except Exception as exc:  # noqa: BLE001 — página fora/bloqueando: fica o resumo da busca
        log.info("página da fonte indisponível (%s): %s", url, exc)
        return ""


def _best_passage(text: str, keys: set[str], limit: int = ENRICHED_CHARS) -> str:
    """Frases da página que mais falam do tema, na ordem original, até `limit`."""
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text) if len(x.strip()) > 30]
    ranked = sorted(range(len(sentences)), key=lambda i: -len(keys & _keywords(sentences[i])))
    chosen, size = set(), 0
    for i in ranked:
        if not keys & _keywords(sentences[i]) or size + len(sentences[i]) > limit:
            continue
        chosen.add(i)
        size += len(sentences[i]) + 1
    return " ".join(sentences[i] for i in sorted(chosen))


def _enrich(facts: list[dict], query: str) -> list[dict]:
    """Troca o resumo da busca pelo trecho da página que mais fala do tema."""
    from concurrent.futures import ThreadPoolExecutor

    facts = facts[:ENRICHED_SOURCES]
    keys = _keywords(query)
    with ThreadPoolExecutor(max_workers=len(facts)) as pool:
        pages = list(pool.map(lambda f: _page_text(f["url"]), facts))
    out = []
    for f, page in zip(facts, pages):
        passage = _best_passage(page, keys | _keywords(f["trecho"])) if page else ""
        out.append({**f, "trecho": passage} if len(passage) > len(f["trecho"]) else f)
    log.info("fontes abertas: %d de %d com trecho da página", sum(a is not b for a, b in zip(out, facts)), len(facts))
    return out


def _duckduckgo(query: str, max_results: int) -> list[dict] | None:
    try:
        from ddgs import DDGS

        results = DDGS().text(query, region="br-pt", max_results=max_results * 2) or []
    except Exception as exc:  # noqa: BLE001 — bloqueio/rede: sem fonte, quem chamou decide
        log.warning("busca duckduckgo falhou (%r): %s", query, exc)
        return None
    facts = [
        {"titulo": r.get("title", ""), "url": r.get("href", ""), "trecho": (r.get("body") or "")[:500]}
        for r in results
        if r.get("body") and not any(d in (r.get("href") or "") for d in EXCLUDED_DOMAINS)
    ][:max_results]
    if facts:
        log.info("busca duckduckgo encontrada pra %r (%d resultados)", query, len(facts))
    return facts or None


def _tavily(query: str, max_results: int) -> list[dict] | None:
    if not TAVILY_API_KEYS:
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
                    "exclude_domains": EXCLUDED_DOMAINS,
                },
                timeout=20,
            )
            # 432 = cota do mês da chave acabou (as 2 desde 17/10)
            if resp.status_code in (401, 403, 432):
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
