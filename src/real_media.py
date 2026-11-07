"""Foto e vídeo REAIS do assunto da cena, do Wikimedia Commons.

Pedido de 20/10: o Fractal Curioso parecia aleatório — o vídeo do
Mecanismo de Antikythera (job 527) não mostrou o mecanismo nenhuma vez, só
clipe de banco ("bronze gears close up", "old library books") e a visão
ainda barrou relógio moderno e barra de chocolate. O Commons tem foto (e às
vezes vídeo) de quase todo caso com nome próprio: o mecanismo no museu, a
barraca do Passo Dyatlov, as fendas de Centralia, páginas do Voynich.

O roteiro diz o que a cena mostra em `real_subject` (nome próprio em
inglês, ver script_gen.py). Primeiro a foto principal do verbete da
Wikipédia em inglês (a mais representativa), depois a busca de arquivos do
Commons. Só arquivo hospedado no Commons entra — a imagem local da
Wikipédia em inglês pode ser uso justo (logo, capa de filme) e não serve.
Licença NC/ND fica de fora; o crédito do autor vai na descrição (CC BY/
BY-SA exige). Grátis, sem chave.

Vídeo real (pedido de 20/10: "o ideal são vídeos mais reais"), nessa
ordem: Commons, NASA (domínio público — espaço, Terra vista de cima,
fenômenos naturais), Europeana (cinejornal europeu, só reuso aberto: PD,
CC0, CC BY/BY-SA) e Internet Archive SÓ em acervos de domínio público de
verdade (Prelinger, Universal Newsreels, FedFlix/governo dos EUA): lá a
licença é declarada por quem sobe, e desenho do Titanic aparecia como
"domínio público" — Content ID derruba. Filme longo (cinejornal de 10
min): corta um trecho de ~20s direto da URL (ffmpeg com -ss, sem baixar
tudo) e a visão olha cada trecho.
"""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import EUROPEANA_API_KEY
from .visual_check import frames_from_clip

log = logging.getLogger("real_media")

_UA = {"User-Agent": "FractalCurioso-video-bot/1.0 (https://www.youtube.com/@fractalcurioso)"}
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_ENWIKI_API = "https://en.wikipedia.org/w/api.php"
MIN_PHOTO_WIDTH = 800  # menor que isso fica borrado esticado em 1080p
MIN_CLIP_SECONDS = 4
MAX_TRIES = 4  # candidatos que a checagem visual olha antes de desistir
SEGMENT_SECONDS = 20
# tempo máximo de busca de vídeo real por cena: o servidor da Europeana
# (Sound & Vision) levou mais de 2 min pra entregar um trecho de 20s
CLIP_BUDGET_SECONDS = 150
_NASA_API = "https://images-api.nasa.gov/search"
_ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
_EUROPEANA_API = "https://api.europeana.eu/record/v2/search.json"
# acervos do Internet Archive que são domínio público de fato
_ARCHIVE_COLLECTIONS = "prelinger OR universal_newsreels OR FedFlix OR nasa OR national_archives OR usnationalarchives"
# cinejornal com dono que reivindica no Content ID (Fox Movietone, British
# Pathé) — aparece como "domínio público" no Commons e dentro do Prelinger
_OWNED = re.compile(r"movietone|path[eé]|\bfox\b|hearst|paramount", re.IGNORECASE)
# licença que não permite uso comercial/derivado não entra
_BAD_LICENSE = re.compile(r"\bNC\b|\bND\b|non-?commercial|no ?deriv|fair use", re.IGNORECASE)
# mapa de localização, bandeira, brasão, ícone: não é foto do assunto
_BAD_TITLE = re.compile(
    r"\b(locator|location map|draft map|flag of|coat of arms|logo|icon|signature|seal of|subgraph|wikidata|"
    r"lego|minecraft|wikimedia|wikipedia|tutorial)\b",
    re.IGNORECASE,
)
_STOP = {"the", "and", "of", "in", "on", "at", "for", "with", "from", "incident", "case", "event", "mystery"}


def _relevant(title: str, subject: str) -> bool:
    """Busca do Commons casa por descrição/categoria: "Mary Celeste" trouxe
    vídeo da Alexa da Amazon (o navio se chamava Amazon). Exige o nome do
    assunto no nome do arquivo."""
    words = {w for w in re.findall(r"[a-z0-9]+", subject.lower()) if len(w) >= 3 and w not in _STOP}
    # palavra inteira: "oak" casava com "Oakland" (aeroporto em Oak Island)
    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    if not words:
        return False
    # todas as palavras, ou a mais longa (o nome raro: "Antikythera") —
    # metade deixava "Oak Island" casar com "Oakland ... (OAK)"
    longest = max(words, key=len)
    return words <= title_words or (len(longest) >= 7 and longest in title_words)

_search_cache: dict[tuple[str, str], list[dict]] = {}


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or ""))).strip()


def _meta(info: dict) -> tuple[str, str]:
    meta = info.get("extmetadata") or {}
    artist = _strip_html(meta.get("Artist", {}).get("value", ""))[:80]
    lic = _strip_html(meta.get("LicenseShortName", {}).get("value", ""))
    return artist, lic


def _candidate(page: dict, kind: str) -> dict | None:
    info = (page.get("imageinfo") or page.get("videoinfo") or [None])[0]
    title = page.get("title", "")
    if not info or _BAD_TITLE.search(title):
        return None
    artist, lic = _meta(info)
    if _BAD_LICENSE.search(lic):
        return None
    name = title.removeprefix("File:")
    credit = f"{artist or 'autor desconhecido'} ({lic or 'licença livre'}, Wikimedia Commons: {name})"
    if kind == "photo":
        if (info.get("width") or 0) < MIN_PHOTO_WIDTH or not info.get("thumburl"):
            return None
        return {"title": title, "url": info["thumburl"], "credit": credit}
    if float(info.get("duration") or 0) < MIN_CLIP_SECONDS:
        return None
    # transcodificação do próprio Commons (webm/mp4 ~720p): o original
    # pode ter centenas de MB
    derivs = [d for d in info.get("derivatives") or [] if d.get("src") and (d.get("height") or 0) >= 360]
    derivs.sort(key=lambda d: abs((d.get("height") or 0) - 720))
    url = derivs[0]["src"] if derivs else info.get("url")
    if not url:
        return None
    return {"title": title, "url": url, "credit": credit}


def _commons_files(params: dict, kind: str) -> list[dict]:
    prop = {"photo": {"prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920},
            "clip": {"prop": "videoinfo", "viprop": "url|size|extmetadata|derivatives"}}[kind]
    try:
        from .wiki_cache import get_json  # cache em disco: nada de 429/bloqueio da Wikimedia

        data = get_json(_COMMONS_API, {"action": "query", "format": "json", **params, **prop})
    except Exception as exc:  # noqa: BLE001
        log.warning("commons falhou (%s): %s", kind, exc)
        return []
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    return [c for p in pages if (c := _candidate(p, kind))]


def _fold_name(text: str) -> str:
    import unicodedata

    return " ".join("".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)).split())


def _lead_image(subject: str) -> list[dict]:
    """Foto principal do verbete em inglês — se estiver no Commons."""
    try:
        from .wiki_cache import get_json

        data = get_json(_ENWIKI_API, {
            "action": "query", "format": "json", "redirects": 1, "generator": "search",
            "gsrsearch": subject, "gsrlimit": 1, "prop": "pageimages", "piprop": "name",
        })
        page = next(iter(data["query"]["pages"].values()))
        name = page.get("pageimage")
        title = page.get("title", "")
    except Exception:  # noqa: BLE001
        return []
    # identidade: a busca pode trazer o verbete de OUTRA pessoa de mesmo nome
    # (ou um verbete que só cita o assunto). Só vale se o título é o próprio
    # assunto (sem acento, sem maiúscula) — "Simone Biles" sim, "Gleisi (desambig.)" não
    if _fold_name(title) != _fold_name(subject):
        return []
    if not name or not name.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")):
        return []
    return _commons_files({"titles": f"File:{name}"}, "photo")


def _search(subject: str, kind: str) -> list[dict]:
    key = (subject.lower(), kind)
    if key not in _search_cache:
        filetype = "bitmap" if kind == "photo" else "video"
        found = _lead_image(subject) if kind == "photo" else []
        found += _commons_files({
            "generator": "search", "gsrnamespace": 6, "gsrlimit": 12,
            "gsrsearch": f"{subject} filetype:{filetype}",
        }, kind)
        # foto principal do verbete é do assunto por construção
        lead = {c["title"] for c in found[:1]} if kind == "photo" else set()
        found = [c for c in found if c["title"] in lead or _relevant(c["title"], subject)]
        seen: set[str] = set()
        _search_cache[key] = [c for c in found if not (c["title"] in seen or seen.add(c["title"]))]
    return _search_cache[key]


_PERSON_STOP = {
    "Fala", "Você", "Hoje", "Mas", "Quando", "Como", "Qual", "Quanto", "Ainda", "Agora", "Então", "Porque",
    "Brasil", "Primeira", "Segunda", "Terceira", "Última", "Veja", "Imagine", "Esse", "Essa", "Isso", "Aqui",
    "Pois", "Além", "Assim", "Depois", "Antes", "Também", "Nesse", "Nessa", "Neste", "Nesta", "Ele", "Ela",
    "Eles", "Elas", "Em", "No", "Na", "Os", "As", "Um", "Uma", "Mais", "Menos", "Todo", "Toda", "Cada",
    "Governo", "Congresso", "Senado", "Câmara", "Supremo", "Tribunal", "Federal", "Estado", "Portal",
    "Transparência", "Reais", "Real", "Ano", "Mês", "Dia", "Segundo", "Nosso", "Nossa", "Seu", "Sua",
}


def topic_person(topic: str) -> str | None:
    """Pessoa do tema, quando ele vem no formato "Nome: resto do tema"
    ("Neymar: da periferia de Mogi Cruzes..."). É o nome que o canal já escolheu."""
    head = (topic or "").split(":", 1)[0].strip() if ":" in (topic or "") else ""
    words = head.split()
    if 1 <= len(words) <= 3 and all(w[:1].isupper() and w.isalpha() and len(w) >= 3 for w in words):
        return head
    return None


def person_in_narration(text: str, topic: str | None = None) -> str | None:
    """Pessoa citada na narração. Se o tema já diz quem é a personagem, a cena
    que cita esse nome (ou sobrenome) usa o nome do tema — isso também pega o
    nome que começa a frase ("Neymar começou..."), que a regra de maiúscula não
    pega. Senão: nome e sobrenome no meio da frase, ou nome único no meio."""
    name = topic_person(topic or "")
    if name:
        narracao = _fold_name(text or "")
        for token in name.split():
            if len(token) >= 4 and re.search(r"\b" + re.escape(_fold_name(token)) + r"\b", narracao):
                return name

    """Nome de pessoa citado na narração ("Gleisi Hoffmann", "Simone Biles"):
    a primeira sequência de 2 ou 3 palavras com maiúscula que não começa com
    palavra comum. Só serve de assunto de BUSCA; a foto ainda passa pela
    checagem visual e pela regra de contexto antes de ir ao ar."""
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        run: list[str] = []
        for raw in sentence.split() + [""]:
            core = raw.strip(".,;:!?…\"“”()'")
            if core[:1].isupper() and core.isalpha() and not core.isupper() and len(core) >= 2 and core not in _PERSON_STOP:
                run.append(core)
                continue
            if 2 <= len(run) <= 3:
                return " ".join(run)
            if len(run) == 1 and len(run[0]) >= 5 and not sentence.startswith(run[0]):
                # nome único no meio da frase ("...com Neymar no Santos"): entra
                return run[0]
            run = []
    return None


def real_photos(subject: str, used: set[str]) -> list[dict]:
    """Candidatos a foto real de `subject` ainda não usados neste vídeo,
    na ordem de preferência ({title, url, credit}). Quem chama baixa
    (thumbnail.photo_scene_frame), checa na visão e marca `used`."""
    if not subject:
        return []
    return [c for c in _search(subject, "photo") if c["title"] not in used][:MAX_TRIES]


def _nasa_clips(subject: str) -> list[dict]:
    try:
        items = httpx.get(_NASA_API, headers=_UA, timeout=30, params={"q": subject, "media_type": "video"}).json()
        items = items["collection"]["items"][:20]
    except Exception as exc:  # noqa: BLE001
        log.warning("nasa falhou: %s", exc)
        return []
    out = []
    for item in items:
        data = (item.get("data") or [{}])[0]
        title = data.get("title", "")
        if not _relevant(f"{title} {' '.join(data.get('keywords') or [])}", subject) or not item.get("href"):
            continue
        out.append({"title": f"NASA:{data.get('nasa_id')}", "manifest": item["href"],
                    "credit": f"NASA ({title}, domínio público)"})
    return out


def _nasa_url(cand: dict) -> str | None:
    try:
        files = httpx.get(cand["manifest"], headers=_UA, timeout=30, follow_redirects=True).json()
    except Exception:  # noqa: BLE001
        return None
    for size in ("~medium.mp4", "~small.mp4", "~large.mp4", "~orig.mp4"):
        url = next((f for f in files if f.endswith(size)), None)
        if url:
            return url.replace("http://", "https://")
    return None


def _europeana_clips(subject: str) -> list[dict]:
    try:
        items = httpx.get(_EUROPEANA_API, headers=_UA, timeout=40, params={
            "wskey": EUROPEANA_API_KEY, "query": subject, "media": "true", "qf": "TYPE:VIDEO",
            "reusability": "open", "rows": 12, "profile": "rich",
        }).json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("europeana falhou: %s", exc)
        return []
    out = []
    for item in items:
        title = (item.get("title") or [""])[0]
        url = (item.get("edmIsShownBy") or [""])[0]
        rights = (item.get("rights") or [""])[0]
        if not url or _BAD_LICENSE.search(rights) or "/nc" in rights or "/nd" in rights:
            continue
        if not _relevant(title, subject) or not url.lower().split("?")[0].endswith((".mp4", ".webm", ".ogv", ".mov")):
            continue
        provider = (item.get("dataProvider") or item.get("provider") or ["Europeana"])[0]
        out.append({"title": f"EU:{item.get('id')}", "url": url,
                    "credit": f"{provider} via Europeana ({title}, {rights.rstrip('/').split('/')[-2] if '/' in rights else rights})"})
    return out


def _archive_clips(subject: str) -> list[dict]:
    words = " ".join(re.findall(r"[A-Za-z0-9]+", subject))
    try:
        docs = httpx.get(_ARCHIVE_SEARCH, headers=_UA, timeout=40, params={
            "q": f"title:({words}) AND mediatype:movies AND collection:({_ARCHIVE_COLLECTIONS})",
            "fl[]": ["identifier", "title"], "rows": 15, "output": "json",
        }).json()["response"]["docs"]
    except Exception as exc:  # noqa: BLE001
        log.warning("internet archive falhou: %s", exc)
        return []
    return [
        {"title": f"IA:{d['identifier']}", "identifier": d["identifier"],
         "credit": f"Internet Archive ({d.get('title')}, domínio público)"}
        for d in docs
        if _relevant(str(d.get("title", "")), subject) and not _OWNED.search(str(d.get("title", "")))
    ]


def _archive_url(cand: dict) -> str | None:
    try:
        files = httpx.get(f"https://archive.org/metadata/{cand['identifier']}", headers=_UA, timeout=30).json()["files"]
    except Exception:  # noqa: BLE001
        return None
    for fmt in ("h.264", "512Kb MPEG4", "MPEG4", "h.264 IA"):
        f = next((f for f in files if f.get("format") == fmt and f.get("name", "").endswith(".mp4")), None)
        if f:
            return f"https://archive.org/download/{cand['identifier']}/{f['name']}"
    return None


def _duration(url: str, timeout: float = 60) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=max(timeout, 5),
        ).stdout.strip()
        return float(out or 0)
    except Exception:  # noqa: BLE001
        return 0.0


# A Wikimedia pede um User-Agent identificável; o padrão do ffmpeg é recusado
# em rajada (74 falhas "exit status 8" entre 20/10 e 07/11, sem o motivo no log)
_FFMPEG_UA = "youtube-pipeline/1.0 (video de fundo; contato pelo repositorio do canal)"


# Vídeo de origem: baixado UMA vez e cortado do disco. Antes o ffmpeg cortava
# direto da URL, e cada -ss virava dezenas de pedidos de faixa à Wikimedia
# (429 em rajada). O cache de consultas (wiki_cache) não cobria isso.
MEDIA_CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "media"
MEDIA_MAX_MB = 100
_media_last = 0.0


MEDIA_BLOCK_FILE = MEDIA_CACHE.parent / "media_block.json"


def _block_until() -> float:
    try:
        return float(json.loads(MEDIA_BLOCK_FILE.read_text())["until"])
    except (OSError, ValueError, KeyError):
        return 0.0


def _set_block(seconds: int) -> None:
    MEDIA_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEDIA_BLOCK_FILE.write_text(json.dumps({"until": time.time() + seconds}))


def _fetch_media(url: str) -> Path | None:
    """Caminho local do arquivo de vídeo (baixa só se não estiver no cache)."""
    global _media_last
    import hashlib

    import httpx

    MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
    path = MEDIA_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if path.exists() and path.stat().st_size > 0:
        path.touch()  # uso recente: a limpeza não apaga
        return path
    if time.time() < _block_until():
        log.info("Wikimedia em espera (429): vídeo %s não baixado agora", url[-50:])
        return None
    for attempt in range(3):
        wait = 1.0 - (time.monotonic() - _media_last)
        if wait > 0:
            time.sleep(wait)  # pausa mínima entre pedidos à Wikimedia
        _media_last = time.monotonic()
        try:
            with httpx.stream("GET", url, headers={"User-Agent": _FFMPEG_UA}, follow_redirects=True, timeout=180) as r:
                if r.status_code == 429:
                    # respeita o Retry-After da Wikimedia (ex.: 600s): insistir só piora o bloqueio
                    wait = int(r.headers.get("retry-after") or 600)
                    _set_block(wait)
                    log.warning("Wikimedia 429: pausa de vídeos por %ds", wait)
                    return None
                r.raise_for_status()
                size = int(r.headers.get("content-length") or 0)
                if size > MEDIA_MAX_MB * 1024 * 1024:
                    log.info("vídeo grande demais pro cache (%d MB): %s", size // 1048576, url[-60:])
                    return None
                tmp = path.with_suffix(".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                tmp.replace(path)
                return path
        except Exception as exc:  # noqa: BLE001
            log.warning("download do vídeo falhou (%s): %s", url[-60:], str(exc)[:160])
    return None


def _segment(url: str, start: float, timeout: float = 180) -> Path | None:
    """~20s a partir de `start`, sem áudio, direto da URL (o filme inteiro
    pode ter centenas de MB). mkv aceita qualquer codec (webm/ogv/mp4).
    Até 3 tentativas com espera (o erro real era HTTP 429 da Wikimedia) e o stderr
    do ffmpeg vai pro log, pra saber o motivo da próxima falha."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-user_agent", _FFMPEG_UA, "-ss", f"{start:.1f}", "-i", url,
           "-t", str(SEGMENT_SECONDS), "-an", "-c:v", "copy"]
    last_err = ""
    for attempt in range(3):
        tmp = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            proc = subprocess.run(cmd + [str(path)], capture_output=True, timeout=max(timeout, 5), text=True)
            if proc.returncode == 0 and path.stat().st_size >= 50_000:
                return path
            last_err = (proc.stderr or f"exit {proc.returncode}").strip()[-300:]
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[-300:]
        path.unlink(missing_ok=True)
        if attempt < 2:
            # 429 da Wikimedia (muitas requisições em rajada): espera crescente
            time.sleep(10 * (attempt + 1))
    log.warning("corte do vídeo falhou (%s): %s", url[-60:], last_err)
    return None


def _clip_candidates(subject: str) -> list[dict]:
    key = (subject.lower(), "clips")
    if key not in _search_cache:
        _search_cache[key] = (
            _search(subject, "clip") + _nasa_clips(subject) + _europeana_clips(subject) + _archive_clips(subject)
        )
    return _search_cache[key]


def real_clip(subject: str, used: set[str], check: Callable[[bytes], bool | None] | None = None) -> dict | None:
    """Vídeo real de `subject` (Commons, NASA, Europeana, Internet Archive), trecho
    aprovado pela visão: {title, path, credit} (marca `used`), ou None.
    Nunca levanta exceção."""
    if not subject:
        return None
    deadline = time.monotonic() + CLIP_BUDGET_SECONDS
    cands = [c for c in _clip_candidates(subject) if c["title"] not in used and not _OWNED.search(c["credit"])]
    for cand in cands[:MAX_TRIES]:
        if time.monotonic() >= deadline:
            log.info("vídeo real pra %r: tempo esgotado, segue pra foto", subject)
            return None
        if "manifest" in cand:
            url = _nasa_url(cand)
        elif "identifier" in cand:
            url = _archive_url(cand)
        else:
            url = cand["url"]
        if not url:
            continue
        local = _fetch_media(url)
        if local is None:
            continue
        url = str(local)  # daqui pra frente o corte é local: zero pedido à Wikimedia
        dur = _duration(url, deadline - time.monotonic())
        if dur < MIN_CLIP_SECONDS:
            continue
        # curto: do começo; longo (cinejornal): 2 pontos do meio — a
        # abertura costuma ser cartela de título
        starts = [0.0] if dur <= SEGMENT_SECONDS * 2 else [dur * 0.3, dur * 0.6]
        for start in starts:
            if time.monotonic() >= deadline:
                break
            path = _segment(url, start, deadline - time.monotonic())
            if path is None:
                continue
            if check is not None:
                frames = frames_from_clip(path)
                if frames is None or check(frames) is False:
                    path.unlink(missing_ok=True)
                    continue
            used.add(cand["title"])
            log.info("vídeo real pra %r: %s (a partir de %.0fs)", subject, cand["title"], start)
            return {**cand, "path": path}
    return None
