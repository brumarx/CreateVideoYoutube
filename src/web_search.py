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
# versão da lógica de busca na chave do cache: mudou a busca, o cache antigo não é mais usado
# (11/11: a busca em outros idiomas não rodava porque o cache devolvia o resultado da versão anterior)
BUSCA_VERSAO = 5
# rede social e vídeo não são fonte: tema vago ("cidade engolida pelo mar")
# só trazia reels e vídeos do YouTube
EXCLUDED_DOMAINS = [
    "youtube.com", "instagram.com", "facebook.com", "tiktok.com", "x.com",
    "twitter.com", "pinterest.com", "kwai.com", "threads.net", "reddit.com",
]


def search_topic_facts(query: str, max_results: int = 5, oficiais: bool = False) -> list[dict] | None:
    """Busca `query` na internet e devolve uma lista de
    {"titulo", "url", "trecho"} com fatos/trechos reais e a fonte de cada
    um — ou None (sem chave configurada, sem resultado, ou erro de rede).
    Nunca levanta exceção; quem chamar decide o fallback. Tavily (1.000
    buscas/mês por chave) primeiro; sem chave, sem cota ou sem resultado,
    DuckDuckGo (grátis, sem chave)."""
    if not query:
        return None
    from . import disk_cache
    versao = disk_cache.versao_do_codigo("web_search.py", "fatos.py")
    guardado = disk_cache.get("busca_web", [BUSCA_VERSAO, versao, query, max_results, oficiais])
    if guardado:
        log.info("busca em cache: %r (%d fontes)", query[:60], len(guardado))
        return guardado
    facts = _tavily(query, max_results) or _duckduckgo(query, max_results)
    facts = _enrich(facts, query, TRECHO_FATOS if oficiais else ENRICHED_CHARS) if facts else []
    wiki = wikipedia_facts(query)
    if wiki:
        # Wikipédia primeiro (o artigo inteiro do assunto, pt + en) e só 2
        # trechos curtos da busca: cabe no limite por minuto do Groq
        seen = {w["url"] for w in wiki}
        facts = wiki + [{**f, "trecho": f["trecho"][:500]} for f in facts if f["url"] not in seen
                        and "wikipedia.org" not in f["url"]][:2]
    # dados estruturados (Wikidata: coordenadas, datas) e artigos abertos (OpenAlex, só em tema de ciência)
    extras = wikidata_facts(query) + openalex_facts(query)
    have = {f["url"] for f in facts}
    facts = facts + [f for f in extras if f["url"] not in have]
    if oficiais:
        # 11/11: fontes em QUALQUER idioma, além da Wikipédia: busca pelo nome do assunto em inglês e no
        # idioma do país (青函トンネル), e fontes oficiais. Mais publicadores independentes confirmam o
        # fato (sem "estima-se"); o número vale em qualquer idioma (ver fatos.numeros)
        titulos = _article_titles(query)
        outros_idiomas: list[dict] = []
        for nome in ((titulos.get("en") or [""])[0], (titulos.get("local") or [""])[0]):
            if nome:
                achados = _tavily(nome, max_results) or _duckduckgo(nome, max_results) or []
                outros_idiomas += _enrich(achados, nome, TRECHO_FATOS) if achados else []
        oficiais_achadas = official_facts(query)
        vistos: set[str] = set()
        ordenadas = []
        for f in oficiais_achadas + outros_idiomas + facts:  # oficiais primeiro: não são cortadas no bloco
            if f["url"] not in vistos:
                vistos.add(f["url"])
                ordenadas.append(f)
        facts = ordenadas
    disk_cache.put("busca_web", [BUSCA_VERSAO, versao, query, max_results, oficiais], facts)
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
    "Bolívia não tem mar" achou o artigo genérico do país). Guardado em disco."""
    import json

    from . import disk_cache
    guardado = disk_cache.get("titulos_wiki_v2", query)
    if guardado:
        return guardado

    from .providers import complete

    prompt = (
        "Tema de um vídeo: " + query + "\n\nListe os títulos EXATOS de artigos da Wikipédia que explicam esse "
        "tema por dentro: o evento, a causa, o tratado, a obra, o fenômeno — nunca só o artigo genérico do "
        "país ou da cidade, a não ser que não exista outro. Até 3 em PORTUGUÊS e até 2 em INGLÊS (a "
        "Wikipédia em inglês costuma ter o artigo específico que falta na portuguesa, ex.: 'Energy in North "
        "Korea'). Do mais específico pro mais geral. E o título do artigo principal no IDIOMA DO PAÍS "
        "onde fica o assunto, se não for português nem inglês (ex.: Túnel Seikan → ja, 青函トンネル). "
        "Responda SÓ com JSON: "
        "{\"pt\": [\"Título\"], \"en\": [\"Title\"], \"local\": [\"título no idioma do país\"]}"
    )
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=300, validate=lambda r: '"en"' in r)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m[0]) if m else {}
    except Exception as exc:  # noqa: BLE001 — sem LLM: busca pelo texto do tema
        log.info("títulos de artigo via LLM falharam: %s", exc)
        data = {}
    titulos = {lang: [str(t).strip() for t in data.get(lang) or [] if str(t).strip()][:3] for lang in ("pt", "en", "local")}
    if titulos["pt"] or titulos["en"]:
        disk_cache.put("titulos_wiki_v2", query, titulos)
    return titulos


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
TRECHO_FATOS = 3000  # busca do tema no canal de fatos: o trecho alimenta a extração, precisa dos números
_STOPWORDS = {
    "como", "para", "pela", "pelo", "com", "sem", "que", "uma", "dos", "das", "nos", "nas", "por",
    "mais", "seu", "sua", "the", "and", "how", "from", "with", "este", "esta", "isso", "toda", "todo",
}


def _keywords(text: str) -> set[str]:
    import unicodedata

    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w[:6] for w in re.findall(r"\w+", text) if len(w) > 3 and w not in _STOPWORDS}


PDF_PAGINAS = 15


def _pdf_text(conteudo: bytes) -> str:
    """Texto de PDF pelo pdftotext do sistema (poppler), sem biblioteca a mais. Fontes oficiais
    costumam ser PDF (folheto da JRTT sobre o Seikan). Falha devolve vazio."""
    import subprocess
    import tempfile

    if not conteudo.startswith(b"%PDF"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(conteudo)
        tmp.flush()
        try:
            out = subprocess.run(["pdftotext", "-l", str(PDF_PAGINAS), "-enc", "UTF-8", tmp.name, "-"],
                                 capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("pdf ilegível: %s", exc)
            return ""
    return out.stdout.decode("utf-8", "ignore").strip()


def _page_text(url: str) -> str:
    try:
        import trafilatura

        resp = httpx.get(url, timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
        })
        resp.raise_for_status()
        if "pdf" in resp.headers.get("content-type", "").lower() or url.lower().split("?")[0].endswith(".pdf"):
            return _pdf_text(resp.content)
        return trafilatura.extract(resp.text) or ""
    except Exception as exc:  # noqa: BLE001 — página fora/bloqueando: fica o resumo da busca
        log.info("página da fonte indisponível (%s): %s", url, exc)
        return ""


def _best_passage(text: str, keys: set[str], limit: int = ENRICHED_CHARS, com_numeros: bool = False) -> str:
    """Frases da página que mais falam do tema, na ordem original, até `limit`. Com `com_numeros`
    (extração de fatos), frase com número entra mesmo sem palavra-chave em comum e tem prioridade:
    numa página em japonês nenhuma palavra latina bate, mas "53.85km" e "1988年" estão lá."""
    # japonês e chinês dizem mais com menos caracteres: frase a partir de 10
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|(?<=[。！？])|\n+", text)
                 if len(x.strip()) > 30 or (len(x.strip()) >= 10 and re.search(r"[\u3040-\u9fff]", x))]

    def peso(i: int) -> int:
        return len(keys & _keywords(sentences[i])) + (3 if com_numeros and re.search(r"\d", sentences[i]) else 0)

    ranked = sorted(range(len(sentences)), key=lambda i: -peso(i))
    chosen, size = set(), 0
    for i in ranked:
        if not peso(i) or size + len(sentences[i]) > limit:
            continue
        chosen.add(i)
        size += len(sentences[i]) + 1
    return " ".join(sentences[i] for i in sorted(chosen))


def _enrich(facts: list[dict], query: str, limite: int = ENRICHED_CHARS) -> list[dict]:
    """Troca o resumo da busca pelo trecho da página que mais fala do tema. Com limite maior que o
    padrão (busca do canal de fatos), abre todas as fontes e prioriza as frases com número."""
    from concurrent.futures import ThreadPoolExecutor

    para_fatos = limite > ENRICHED_CHARS
    facts = facts if para_fatos else facts[:ENRICHED_SOURCES]
    if not facts:
        return facts
    keys = _keywords(query)
    with ThreadPoolExecutor(max_workers=len(facts)) as pool:
        pages = list(pool.map(lambda f: _page_text(f["url"]), facts))
    out = []
    for f, page in zip(facts, pages):
        passage = _best_passage(page, keys | _keywords(f["trecho"]), limite, para_fatos) if page else ""
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


def official_facts(query: str, max_results: int = 5) -> list[dict]:
    """Só resultados de domínio oficial (ver fatos.classificar_fonte), já marcados com
    oficial=True e com o texto da página. Duas consultas: em inglês e em português."""
    from .fatos import classificar_fonte
    vistos: set[str] = set()
    achados: list[dict] = []
    # o nome em inglês do assunto acha a fonte oficial ("Seikan Tunnel official" → jrtt.go.jp);
    # o tema inteiro em português não acha (rodada 21)
    titulos = _article_titles(query)
    assunto_en = (titulos.get("en") or [""])[0]
    assunto_pt = (titulos.get("pt") or [""])[0] or query.split(":")[0].strip()
    assunto_local = (titulos.get("local") or [""])[0]
    # no idioma do país as fontes oficiais aparecem pelo nome do assunto (青函トンネル → jrtt.go.jp, sangiin.go.jp)
    consultas = [c for c in (f"{assunto_en} official" if assunto_en else "",
                             f"{assunto_en} government report" if assunto_en else "",
                             assunto_local,
                             f"{assunto_pt} site oficial governo") if c]
    for consulta in consultas:
        resultados = _tavily(consulta, max_results) or _duckduckgo(consulta, max_results) or []
        for r in resultados:
            url = r.get("url", "")
            if url in vistos or classificar_fonte({"url": url}) != "oficial":
                continue
            vistos.add(url)
            achados.append({**r, "oficial": True})
    if achados:
        achados = _enrich(achados, query, TRECHO_FATOS)
        log.info("fontes oficiais achadas para %r: %d", query[:60], len(achados))
    else:
        log.info("nenhuma fonte oficial achada para %r", query[:60])
    return [{**f, "oficial": True} for f in achados]


# ---------------------------------------------------------------- Wikidata e OpenAlex
# APIs abertas, sem chave. Pedem um User-Agent com o nome do projeto (sem e-mail
# pessoal, ver regra de dados do usuário).
OPEN_HEADERS = {"User-Agent": "faceless-video-bot/1.0 (+https://github.com/brumarx/CreateVideoYoutube)"}
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OPENALEX_API = "https://api.openalex.org/works"
_SCIENCE_RE = re.compile(
    r"ciênc|cientist|science|vulcão|vulcan|geolog|biolog|química|quimic|física|fisic|clima|climát|"
    r"espécie|species|oceano|ocean|dessaliniz|desaliniz|lago|lake|mineral|fóssil|fossil|astronom|"
    r"planeta|planet|genética|genetic|vacina|vaccine|doença|disease|solo|soil|rocha|rock",
    re.IGNORECASE,
)


def _wd_time(value: str) -> str:
    """"+2016-12-14T00:00:00Z" -> "14/12/2016" (ou só o ano, se o mês/dia são 00)."""
    m = re.match(r"[+-]?(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{d}/{mo}/{y}" if d != "00" and mo != "00" else y


def wikidata_facts(query: str) -> list[dict]:
    """Ficha do assunto no Wikidata: coordenadas, data de fundação/evento e
    descrição. Dados estruturados, então entram com número e data exatos."""
    if not query:
        return []
    try:
        with httpx.Client(headers=OPEN_HEADERS, timeout=20) as c:
            found = c.get(WIKIDATA_API, params={"action": "wbsearchentities", "search": query,
                                                "language": "pt", "format": "json", "limit": 1}).json().get("search", [])
            if not found:
                return []
            qid = found[0]["id"]
            ent = c.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": qid, "format": "json",
                                              "languages": "pt|en", "props": "labels|descriptions|claims"}).json()
            e = ent.get("entities", {}).get(qid, {})
    except Exception as exc:  # noqa: BLE001 — fonte extra: falha não derruba a pesquisa
        log.warning("wikidata falhou (%r): %s", query, exc)
        return []
    label = (e.get("labels", {}).get("pt") or e.get("labels", {}).get("en") or {}).get("value", query)
    desc = (e.get("descriptions", {}).get("pt") or e.get("descriptions", {}).get("en") or {}).get("value", "")
    claims = e.get("claims", {})
    parts = [f"{label}: {desc}." if desc else f"{label}."]
    coord = (claims.get("P625") or [{}])[0].get("mainsnak", {}).get("datavalue", {}).get("value")
    if isinstance(coord, dict) and "latitude" in coord:
        parts.append(f"Coordenadas: {coord['latitude']:.4f}, {coord['longitude']:.4f}.")
    for prop, nome in (("P571", "Fundação/criação"), ("P585", "Data do evento")):
        t = (claims.get(prop) or [{}])[0].get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(t, dict) and t.get("time"):
            when = _wd_time(t["time"])
            if when:
                parts.append(f"{nome}: {when}.")
    trecho = " ".join(parts)
    if len(trecho) <= len(label) + 2:
        return []
    return [{"titulo": f"Wikidata: {label}", "url": f"https://www.wikidata.org/wiki/{qid}", "trecho": trecho[:900]}]


def _openalex_abstract(index: dict | None) -> str:
    """OpenAlex guarda o resumo como índice invertido (palavra -> posições)."""
    if not index:
        return ""
    slots: dict[int, str] = {}
    for word, pos in index.items():
        for p in pos:
            slots[p] = word
    return " ".join(slots[i] for i in sorted(slots))


def openalex_facts(query: str, n: int = 2) -> list[dict]:
    """Artigos científicos de acesso aberto (OpenAlex) com resumo e ano. Só
    quando o tema tem termo de ciência/natureza; senão não chama."""
    if not query or not _SCIENCE_RE.search(query):
        return []
    try:
        with httpx.Client(headers=OPEN_HEADERS, timeout=20) as c:
            works = c.get(OPENALEX_API, params={"search": query[:120], "filter": "is_oa:true",
                                                "per-page": n}).json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("openalex falhou (%r): %s", query, exc)
        return []
    out = []
    for w in works[:n]:
        title = w.get("display_name") or ""
        abstract = _openalex_abstract(w.get("abstract_inverted_index"))
        if not title or not abstract:
            continue
        year = w.get("publication_year") or ""
        url = w.get("doi") or w.get("id") or ""
        out.append({"titulo": f"Artigo ({year}): {title}", "url": url,
                    "trecho": f"{title} ({year}). {abstract}"[:900]})
    return out
