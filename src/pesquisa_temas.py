"""25/11 (Grupo C): escolher tema pelo que o público já provou, sem cota da YouTube Data API e sem IA.

C1. Outliers: busca no YouTube (yt-dlp, página de resultados) os termos de `pesquisa_temas.buscas` do canal;
    para cada canal achado, lê os 30 envios mais recentes da aba certa (/shorts ou /videos) e calcula a
    mediana de views dele. Vídeo com views/mediana >= 5 é outlier (o assunto agradou além do normal daquele
    canal, não só porque o canal é grande); >= 20 é muito forte.
C2. Demanda: autocompletar do YouTube (suggestqueries, grátis) para o termo, termo + a..z e "por que/como/o que"
    + termo; sugestão que aparece em mais expansões = mais procurada.
C3. Candidatos: JSON por canal em data/temas_pesquisados/<canal>.json, ordenado. O título do outlier NUNCA vira
    o nosso título: guardamos o ASSUNTO (título limpo de hashtag/emoji/caixa alta) e o pipeline de sempre
    escreve título e roteiro. `candidatos_do_publico` é o gancho que src/topics.pick_topic usa.
C4. (não ligado) mecanismo com objeto trocado via IA — ver `mecanismo_com_objeto_trocado`.
C5. Comentários do nosso próprio canal (commentThreads.list, 1 unidade) viram ideias anotadas no mesmo JSON.

Detalhe que custou caro em projeto anterior: a página do canal em português mostra "1,3 mi visualizações" e um
parser ingênuo lê 1. Aqui toda leitura do YouTube vai com hl=en/Accept-Language en-US; o yt-dlp devolve
view_count numérico (conferido em 25/11 com yt-dlp 2026.08.19: "46K" → 46000).
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import string
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import disk_cache

log = logging.getLogger("pesquisa_temas")

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "temas_pesquisados"

MIN_VIEWS = 10_000  # abaixo disso não é prova de público, é ruído
VIEWS_POR_INSCRITO_MIN = 0.3
SCORE_FORTE = 5.0  # views / mediana do canal
SCORE_MUITO_FORTE = 20.0
RECENTES_POR_CANAL = 30
MAX_CANAIS = 15  # canais distintos lidos por rodada (cada um = 1 leitura da aba)
TOP_COM_DATA = 15  # views_por_dia só para os melhores (cada um = 1 leitura do vídeo)
VALIDADE_DIAS = 14  # pesquisa mais velha que isso não manda no tema
MEDIANA_TTL_DIAS = 5
SUGESTAO_TTL_DIAS = 7
PAUSA_SUGESTAO = 0.2
PREFIXOS_PERGUNTA = ("por que", "como", "o que")

# filtros da busca (parâmetro sp). 25/11, conferido ao vivo: o filtro de Shorts devolve 1-2 itens (o resto vem
# numa prateleira que o yt-dlp não lê) e "ordenar por views" devolve vídeo de 10 views; o que funciona é
# relevância + só vídeos + enviados no último ano. Shorts entram como reforço quando o canal é de Shorts
_SP_VIDEOS_ANO = "EgQIBRAB"
_SP_SHORTS = "EgIQCQ%3D%3D"
SHORT_MAX_S = 180  # até 3 min o YouTube trata como Short: mediana sai da aba /shorts do canal
_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "extractor_args": {"youtubetab": {"lang": ["en"]}, "youtube": {"lang": ["en"]}},
    "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
}
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64; rv:128.0) Gecko/20100101 Firefox/128.0"}


# ---------------------------------------------------------------- acesso à rede (trocado nos testes)

def _extrair(url: str, limite: int, flat: bool = True) -> dict:
    """Uma leitura do yt-dlp (sem baixar nada). Erro sobe — quem chama decide."""
    import yt_dlp

    opts = dict(_YDL_OPTS, playlistend=limite)
    if not flat:
        opts.pop("extract_flat")
        opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False, process=flat) or {}


def _get_json(url: str, params: dict):
    import httpx

    r = httpx.get(url, params=params, headers=_UA, timeout=15)
    r.raise_for_status()
    try:
        texto = r.content.decode("utf-8")
    except UnicodeDecodeError:  # o autocompletar com hl=pt às vezes responde em latin-1
        texto = r.content.decode("latin-1")
    return json.loads(texto)


# ---------------------------------------------------------------- texto

_STOP = {
    "que", "com", "para", "pra", "por", "uma", "dos", "das", "nos", "nas", "não", "nao", "como", "mais", "muito",
    "the", "and", "with", "from", "that", "this", "você", "voce", "seu", "sua", "isso", "esse", "essa", "este",
    "esta", "foi", "são", "sao", "tem", "the", "how", "why", "what", "shorts", "short", "vídeo", "video", "mundo",
    "porque", "quando", "onde", "qual", "quem", "ele", "ela", "eles", "elas", "sobre", "pelo", "pela", "até",
}


def _norm(texto: str) -> str:
    return unicodedata.normalize("NFKD", (texto or "").lower()).encode("ascii", "ignore").decode()


def palavras(texto: str) -> set[str]:
    """Palavras de conteúdo (sem acento, 4+ letras ou número, sem stopword)."""
    return {w for w in re.findall(r"[a-z]{4,}|\d+", _norm(texto)) if w not in _STOP}


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+")
_HASHTAG_RE = re.compile(r"#\w+")
_COLCHETE_RE = re.compile(r"[\[(][^\])]*(shorts?|parte|part|ep\.?|epis[oó]dio|\d+/\d+)[^\])]*[\])]", re.IGNORECASE)


def assunto_do_titulo(titulo: str, canal: str = "") -> str:
    """O ASSUNTO de um título de outlier: sem hashtag, emoji, marca do canal, "(parte 2)", pontuação de
    gritaria e caixa alta. Continua sendo a frase dele — por isso vai como tema (o pipeline escreve o título
    nosso), nunca como título. A troca de objeto/ângulo fica para o C4 (IA)."""
    t = _HASHTAG_RE.sub(" ", titulo or "")
    t = _EMOJI_RE.sub(" ", t)
    t = _COLCHETE_RE.sub(" ", t)
    partes = [p.strip() for p in re.split(r"\s[|•–-]\s|\s\|\s?", t) if p.strip()]
    if canal:
        partes = [p for p in partes if _norm(p) != _norm(canal)] or partes
    t = max(partes, key=len) if partes else t
    t = re.sub(r"([!?.])\1+", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip(" -|:!.,")
    letras = [c for c in t if c.isalpha()]
    if letras and sum(c.isupper() for c in letras) / len(letras) > 0.6:
        t = t.lower()
        t = t[:1].upper() + t[1:]
    return t


# ---------------------------------------------------------------- C1: outliers

def buscar_videos(termo: str, formato: str = "shorts", limite: int = 30, pais: str = "BR") -> list[dict]:
    """Resultados da busca do YouTube para o termo (sem cota): [{id, url, titulo, canal, canal_url, views,
    duracao}]. Só vídeo com view_count numérico. Com hl=pt o yt-dlp não lê a contagem (view_count None,
    conferido 25/11); com hl=en lê, mas o YouTube TRADUZ o título — o original vem depois (titulo_original)."""
    sps = [_SP_VIDEOS_ANO] + ([_SP_SHORTS] if formato == "shorts" else [])
    entradas: list[dict] = []
    for sp in sps:
        url = f"https://www.youtube.com/results?search_query={quote(termo)}&sp={sp}&hl=en&gl={pais}"
        try:
            entradas += (_extrair(url, limite).get("entries") or [])[:limite]
        except Exception as exc:  # noqa: BLE001 — rede/layout do YouTube: termo sem resultado, a rodada segue
            log.warning("busca %r falhou: %s", termo, exc)
    out, vistos = [], set()
    for e in entradas:
        views = e.get("view_count")
        if not isinstance(views, (int, float)) or not e.get("id") or e["id"] in vistos:
            continue
        vistos.add(e["id"])
        out.append({
            "id": e["id"],
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "titulo": e.get("title") or "",
            "canal": e.get("channel") or e.get("uploader") or "",
            "canal_url": e.get("channel_url") or e.get("uploader_url") or "",
            "views": int(views),
            "duracao": e.get("duration"),
        })
    return out


def _canal_base(canal_url: str) -> str:
    return re.sub(r"/(shorts|videos|streams|featured)/?$", "", (canal_url or "").rstrip("/"))


def estatistica_do_canal(canal_url: str, formato: str = "shorts", n: int = RECENTES_POR_CANAL) -> dict | None:
    """Mediana de views dos `n` envios mais recentes da aba do formato (Shorts → /shorts; sem aba de Shorts,
    /videos), mais inscritos e a lista dos envios (para os canais de referência). Guardada 5 dias. Short e
    vídeo longo do mesmo canal têm públicos de tamanhos diferentes: cada um contra a mediana da sua aba."""
    base = _canal_base(canal_url)
    if not base:
        return None
    chave = {"canal": base, "formato": formato, "n": n}
    guardado = disk_cache.get("pesquisa_mediana_canal", chave, ttl_days=MEDIANA_TTL_DIAS)
    if guardado:
        return guardado
    abas = ["/shorts", "/videos"] if formato == "shorts" else ["/videos"]
    info, aba_lida = None, ""
    for aba in abas:
        try:
            info = _extrair(base + aba, n)
            aba_lida = aba
            break
        except Exception as exc:  # noqa: BLE001 — canal sem a aba (ou fora do ar): tenta a próxima
            log.info("aba %s de %s indisponível: %s", aba, base, str(exc)[:120])
    if not info:
        return None
    envios = [
        {"id": e.get("id"), "titulo": e.get("title") or "", "views": int(e["view_count"]),
         "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"}
        for e in (info.get("entries") or [])[:n]
        if isinstance(e.get("view_count"), (int, float)) and e.get("id")
    ]
    if len(envios) < 5:  # mediana de 2 ou 3 vídeos não diz nada sobre o canal
        return None
    est = {
        "canal": info.get("channel") or info.get("uploader") or "",
        "canal_url": base,
        "aba": aba_lida,
        "mediana": statistics.median(e["views"] for e in envios),
        "inscritos": info.get("channel_follower_count"),
        "envios": envios,
    }
    disk_cache.put("pesquisa_mediana_canal", chave, est)
    return est


def data_de_envio(video_id: str) -> str | None:
    """AAAA-MM-DD do envio (a busca em modo flat não traz data). Uma leitura do vídeo; guardada 1 ano."""
    guardado = disk_cache.get("pesquisa_data_envio", video_id, ttl_days=365)
    if guardado:
        return guardado
    try:
        info = _extrair(f"https://www.youtube.com/watch?v={video_id}", 1, flat=False)
    except Exception as exc:  # noqa: BLE001 — sem data: fica sem views_por_dia
        log.info("data de envio de %s indisponível: %s", video_id, str(exc)[:120])
        return None
    bruto = str(info.get("upload_date") or "")
    if not re.fullmatch(r"\d{8}", bruto):
        return None
    data = f"{bruto[:4]}-{bruto[4:6]}-{bruto[6:]}"
    disk_cache.put("pesquisa_data_envio", video_id, data)
    return data


def titulo_original(video_id: str) -> str | None:
    """Título no idioma em que o dono escreveu (o oEmbed não traduz). Guardado 1 ano; erro → None."""
    guardado = disk_cache.get("pesquisa_titulo_original", video_id, ttl_days=365)
    if guardado:
        return guardado
    try:
        titulo = str(_get_json("https://www.youtube.com/oembed", {
            "url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}).get("title") or "")
    except Exception as exc:  # noqa: BLE001 — fica o título da busca (talvez traduzido)
        log.info("título original de %s indisponível: %s", video_id, exc)
        return None
    disk_cache.put("pesquisa_titulo_original", video_id, titulo)
    return titulo or None


def _id_do_video(url: str) -> str | None:
    m = re.search(r"(?:v=|shorts/)([\w-]+)", url or "")
    return m.group(1) if m else None


def _outlier(video: dict, est: dict, busca: str) -> dict | None:
    mediana = est.get("mediana") or 0
    if video["views"] < MIN_VIEWS or mediana <= 0:
        return None
    score = video["views"] / mediana
    if score < SCORE_FORTE:
        return None
    inscritos = est.get("inscritos")
    # 25/11 (amostra ao vivo: Brasil Paralelo 37x com 0,07 view por inscrito): canal grande de pauta variada infla a
    # nota pela mediana baixa; o vídeo precisa ter alcançado ao menos VIEWS_POR_INSCRITO_MIN da base do canal
    if inscritos and video["views"] / inscritos < VIEWS_POR_INSCRITO_MIN:
        return None
    canal = video.get("canal") or est.get("canal") or ""
    return {
        "assunto": assunto_do_titulo(video["titulo"], canal),
        "titulo_outlier": video["titulo"],
        "url": video["url"],
        "canal": canal,
        "canal_url": est.get("canal_url") or video.get("canal_url") or "",
        "views": video["views"],
        "mediana": mediana,
        "score": round(score, 1),
        "muito_forte": score >= SCORE_MUITO_FORTE,
        "views_por_inscrito": round(video["views"] / inscritos, 2) if inscritos else None,
        "views_por_dia": None,
        "data": None,
        "busca": busca,
    }


def outliers(buscas: list[str], formato: str = "shorts", resultados: int = 30, max_canais: int = MAX_CANAIS,
             canais_referencia: list[str] | None = None, top_com_data: int = TOP_COM_DATA,
             pais: str = "BR") -> list[dict]:
    """C1 inteiro: outliers das buscas + dos canais de referência, do mais forte para o mais fraco."""
    achados: list[tuple[dict, str]] = []
    for termo in buscas:
        for v in buscar_videos(termo, formato, resultados, pais):
            if v["views"] >= MIN_VIEWS and v["canal_url"]:
                achados.append((v, termo))
    # canais na ordem do melhor vídeo achado: o teto de canais corta os de menos alcance
    melhor: dict[str, int] = {}
    for v, _ in achados:
        base = _canal_base(v["canal_url"])
        melhor[base] = max(melhor.get(base, 0), v["views"])
    canais = set(sorted(melhor, key=melhor.get, reverse=True)[:max_canais])
    stats: dict[tuple[str, str], dict | None] = {}

    vistos: set[str] = set()
    out: list[dict] = []
    for v, termo in achados:
        base = _canal_base(v["canal_url"])
        if base not in canais or v["id"] in vistos:
            continue
        aba = "shorts" if (v.get("duracao") or SHORT_MAX_S + 1) <= SHORT_MAX_S else "videos"
        if (base, aba) not in stats:
            stats[(base, aba)] = estatistica_do_canal(base, aba)
        est = stats[(base, aba)]
        if not est:
            continue
        o = _outlier(v, est, termo)
        if o:
            vistos.add(v["id"])
            out.append(o)
    for ref in canais_referencia or []:
        est = estatistica_do_canal(ref, formato)
        for e in (est or {}).get("envios", []):
            if e["id"] in vistos:
                continue
            o = _outlier({**e, "canal": est["canal"], "canal_url": est["canal_url"]}, est, f"canal:{est['canal']}")
            if o:
                vistos.add(e["id"])
                out.append(o)
    out.sort(key=lambda o: o["score"], reverse=True)
    for o in out:
        original = titulo_original(_id_do_video(o["url"]) or "")
        if original and original != o["titulo_outlier"]:
            o["titulo_outlier"] = original
            o["assunto"] = assunto_do_titulo(original, o["canal"])
    hoje = datetime.now(timezone.utc).date()
    for o in out[:top_com_data]:
        vid = _id_do_video(o["url"])
        data = data_de_envio(vid) if vid else None
        if data:
            dias = max(1, (hoje - datetime.strptime(data, "%Y-%m-%d").date()).days)
            o["data"] = data
            o["views_por_dia"] = round(o["views"] / dias)
    return out


# ---------------------------------------------------------------- C2: demanda (autocompletar)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"


def sugestoes(q: str, idioma: str = "pt") -> list[str]:
    """Sugestões do autocompletar do YouTube para `q`. Guardadas 7 dias; erro → []."""
    chave = {"q": q, "hl": idioma}
    guardado = disk_cache.get("pesquisa_autocomplete", chave, ttl_days=SUGESTAO_TTL_DIAS)
    if guardado is not None:
        return guardado
    try:
        dados = _get_json(SUGGEST_URL, {"client": "firefox", "ds": "yt", "hl": idioma, "q": q})
    except Exception as exc:  # noqa: BLE001 — autocompletar fora: sem demanda, não derruba a pesquisa
        log.info("autocompletar %r falhou: %s", q, exc)
        return []
    lista = [str(s) for s in (dados[1] if isinstance(dados, list) and len(dados) > 1 else []) if s]
    disk_cache.put("pesquisa_autocomplete", chave, lista)
    return lista


def demanda(termo: str, idioma: str = "pt", rapido: bool = False, pausa: float = PAUSA_SUGESTAO) -> dict[str, int]:
    """Em quantas expansões do termo cada sugestão aparece (termo, termo+a..z, pergunta+termo). Quanto mais
    expansões levam à mesma busca, mais gente digita aquilo."""
    expansoes = [termo]
    if not rapido:
        expansoes += [f"{termo} {letra}" for letra in string.ascii_lowercase]
    expansoes += [f"{p} {termo}" for p in PREFIXOS_PERGUNTA]
    contagem: dict[str, int] = {}
    for i, q in enumerate(expansoes):
        if i and pausa:
            time.sleep(pausa)
        for s in dict.fromkeys(_norm(x).strip() for x in sugestoes(q, idioma)):
            if s:
                contagem[s] = contagem.get(s, 0) + 1
    return dict(sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0])))


def demanda_do_assunto(assunto: str, termo: str, mapa: dict[str, int]) -> int:
    """Soma das contagens das sugestões que citam alguma palavra do assunto além das do próprio termo."""
    alvo = palavras(assunto) - palavras(termo)
    if not alvo:
        return 0
    return sum(n for s, n in mapa.items() if alvo & palavras(s))


# ---------------------------------------------------------------- Wikipédia (material para o modo redação)

def artigo_da_wikipedia(assunto: str, idiomas: tuple[str, ...] = ("pt", "en")) -> str | None:
    """Título do artigo da Wikipédia que cobre o assunto (busca da própria Wikipédia, sem IA), ou None.
    Só vale se o título+trecho do 1º resultado tem ao menos metade das palavras do assunto — senão a busca
    devolve qualquer coisa. Guardado 30 dias (inclusive o "não tem")."""
    chave = {"assunto": assunto, "idiomas": list(idiomas)}
    guardado = disk_cache.get("pesquisa_wikipedia", chave, ttl_days=30)
    if guardado:
        return guardado.get("titulo")
    alvo = palavras(assunto)
    titulo = None
    if alvo:
        for lang in idiomas:
            try:
                r = _get_json(f"https://{lang}.wikipedia.org/w/api.php", {
                    "action": "query", "list": "search", "srsearch": assunto, "srlimit": 1, "format": "json"})
            except Exception as exc:  # noqa: BLE001 — sem Wikipédia agora: fica sem a marca
                log.info("Wikipédia (%s) indisponível para %r: %s", lang, assunto[:60], exc)
                continue
            hits = (r.get("query") or {}).get("search") or []
            if hits:
                texto = hits[0].get("title", "") + " " + re.sub(r"<[^>]+>", "", hits[0].get("snippet", ""))
                if len(alvo & palavras(texto)) * 2 >= len(alvo):
                    titulo = f"{lang}:{hits[0]['title']}"
                    break
    disk_cache.put("pesquisa_wikipedia", chave, {"titulo": titulo})
    return titulo


# ---------------------------------------------------------------- C3: candidatos

def _rank(c: dict) -> float:
    # o outlier manda; a demanda do autocompletar desempata e empurra (até 2x)
    return round(c["score"] * (1 + 0.25 * min(c.get("demanda") or 0, 4)), 2)


def pesquisar_canal(canal: str, cfg: dict, resultados: int = 30, max_buscas: int | None = None,
                    max_canais: int = MAX_CANAIS, rapido: bool = False, top_com_data: int = TOP_COM_DATA,
                    wiki_top: int = 10, comentarios: dict | None = None) -> dict:
    """Pesquisa completa de um canal (C1+C2+C3) e grava data/temas_pesquisados/<canal>.json."""
    buscas = [str(b) for b in cfg.get("buscas") or []][:max_buscas]
    formato = "shorts" if str(cfg.get("formato") or "shorts").lower().startswith("short") else "videos"
    idioma = str(cfg.get("idioma") or "pt")
    achados = outliers(buscas, formato, resultados, max_canais, cfg.get("canais_referencia") or [], top_com_data,
                       pais="BR" if idioma == "pt" else "US")
    mapas = {termo: demanda(termo, idioma, rapido) for termo in buscas}
    for c in achados:
        mapa = mapas.get(c["busca"]) or {}
        c["demanda"] = demanda_do_assunto(c["assunto"], c["busca"], mapa)
        c["rank"] = _rank(c)
    achados.sort(key=lambda c: c["rank"], reverse=True)
    for c in achados[:wiki_top]:
        c["wikipedia"] = artigo_da_wikipedia(c["assunto"])
    resultado = {
        "canal": canal,
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "formato": formato,
        "buscas": buscas,
        "candidatos": achados,
        "demanda": {t: list(m.items())[:20] for t, m in mapas.items()},
    }
    if comentarios is not None:
        resultado["comentarios"] = comentarios
    salvar(canal, resultado)
    return resultado


def salvar(canal: str, dados: dict) -> Path:
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{canal}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def config_do_canal(canal: str) -> dict:
    try:
        from .config import ChannelConfig
        return ChannelConfig.load(canal).pesquisa_temas or {}
    except Exception:  # noqa: BLE001 — canal sem yaml (testes, canal apagado): sem pesquisa
        return {}


def candidatos_do_publico(canal: str, limite: int = 10) -> list[dict]:
    """Gancho do pick_topic: os melhores candidatos da última pesquisa do canal, se o canal tem
    `pesquisa_temas` no yaml e a pesquisa tem menos de VALIDADE_DIAS. Lista vazia em qualquer outro caso."""
    if not config_do_canal(canal):
        return []
    try:
        dados = json.loads((DIR / f"{canal}.json").read_text(encoding="utf-8"))
        gerado = datetime.fromisoformat(dados["gerado_em"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if datetime.now(timezone.utc) - gerado > timedelta(days=VALIDADE_DIAS):
        log.info("[%s] pesquisa de temas de %s venceu (%d dias) — rode scripts/pesquisar_temas.py",
                 canal, dados["gerado_em"][:10], VALIDADE_DIAS)
        return []
    return [c for c in dados.get("candidatos") or [] if c.get("assunto")][:limite]


def parcela_do_publico(canal: str) -> float:
    """Fração dos temas que sai da pesquisa quando há candidato (yaml `pesquisa_temas.parcela`, padrão 1)."""
    try:
        return max(0.0, min(1.0, float(config_do_canal(canal).get("parcela", 1.0))))
    except (TypeError, ValueError):
        return 1.0


# ---------------------------------------------------------------- C4 (rascunho, NÃO ligado)

PROMPT_MECANISMO = """Você planeja temas de um canal do YouTube sobre: {niche}.

Um vídeo de OUTRO canal fez {score}x a mediana daquele canal: "{titulo_outlier}".
Não copie o título nem o assunto. Descubra o MECANISMO que fez o público clicar (ex.: "país pequeno resolve
problema gigante com solução barata"; "animal comum com habilidade absurda medida em número"; "objeto do dia
a dia com origem trágica") e aplique esse mecanismo a OUTRO objeto real, documentado na Wikipédia, que o
canal ainda não usou.

Temas já usados (não repita nem reformule): {usados}

Responda só com JSON: {{"mecanismo": "...", "assunto": "nome do artigo da Wikipédia", "tema": "pergunta em
português, uma linha"}}"""


def mecanismo_com_objeto_trocado(candidato: dict, niche: str, usados: list[str]) -> dict | None:
    """C4 — TODO (quando as chamadas de IA estiverem liberadas para isso): mandar PROMPT_MECANISMO ao juiz
    forte (providers.complete, gpt-oss-120b primeiro), validar o JSON, confirmar o assunto com
    topics.subject_confirmed e devolver {"mecanismo", "assunto", "tema"}. Assim o outlier vira um tema NOSSO
    (mesmo gatilho, outro objeto) em vez de repetir o assunto dele. Hoje não é chamado por ninguém."""
    log.debug("C4 desligado (sem IA): %s", candidato.get("titulo_outlier"))
    return None


# ---------------------------------------------------------------- C5: comentários do nosso canal

_PEDIDO_RE = re.compile(
    r"\?|faz (um|uma|o|a)?\s*v[ií]deo|fa[çc]a (um|uma)?\s*v[ií]deo|fala sobre|fale sobre|podia|poderia|"
    r"explica|traz (um|uma|mais)|pr[oó]ximo v[ií]deo|parte 2|sobre (o|a|os|as) ",
    re.IGNORECASE,
)


def extrair_pedidos(textos: list[str]) -> dict:
    """Comentários com pergunta/pedido e as palavras que mais se repetem entre eles."""
    pedidos = [t.strip() for t in textos if t and _PEDIDO_RE.search(t)]
    contagem: dict[str, int] = {}
    for t in pedidos:
        for w in palavras(t):
            contagem[w] = contagem.get(w, 0) + 1
    chaves = [[w, n] for w, n in sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2][:30]
    return {"pedidos": pedidos[:50], "palavras": chaves}


def _servico_youtube(canal: str):
    """Cliente da API com o token do canal, ou None (com o motivo no log) se o token não pode ler comentários."""
    from googleapiclient.discovery import build

    from .config import ChannelConfig
    from .upload import COMMENT_SCOPE, _load_credentials

    creds = _load_credentials(ChannelConfig.load(canal).token_file)
    if not creds.has_scopes([COMMENT_SCOPE]):
        log.warning("[%s] token sem o escopo %s — comentários não lidos; reautorize com "
                    "scripts/auth_youtube.py --channel %s", canal, COMMENT_SCOPE, canal)
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def ideias_dos_comentarios(canal: str, paginas: int = 2) -> dict | None:
    """C5: comentários recentes nos vídeos do NOSSO canal (commentThreads.list, 1 unidade por página de 100;
    o id do canal sai de channels.list mine=true, 1 unidade, guardado 1 ano) → perguntas/pedidos e palavras
    repetidas. None se o token não serve ou a API falha (a pesquisa segue sem comentários)."""
    from .yt_quota import spend

    try:
        yt = _servico_youtube(canal)
        if yt is None:
            return None
        canal_id = disk_cache.get("pesquisa_canal_id", canal, ttl_days=365)
        if not canal_id:
            spend("list")
            itens = yt.channels().list(part="id", mine=True).execute().get("items") or []
            if not itens:
                return None
            canal_id = itens[0]["id"]
            disk_cache.put("pesquisa_canal_id", canal, canal_id)
        textos: list[str] = []
        token = None
        for _ in range(paginas):
            spend("list")
            resp = yt.commentThreads().list(
                part="snippet", allThreadsRelatedToChannelId=canal_id, maxResults=100, order="time",
                textFormat="plainText", **({"pageToken": token} if token else {})).execute()
            for item in resp.get("items") or []:
                top = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
                if top.get("authorChannelId", {}).get("value") != canal_id:  # o nosso próprio comentário não conta
                    textos.append(str(top.get("textDisplay") or top.get("textOriginal") or ""))
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 — token vencido (invalid_grant a cada ~7 dias), cota, rede
        log.warning("[%s] comentários não lidos: %s", canal, exc)
        return None
    return {"lidos": len(textos), **extrair_pedidos(textos)}
