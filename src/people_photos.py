"""Foto de quem a cena cita (jogador, técnico), do verbete na Wikipédia.

Pedido de 11/10: mais imagem dos personagens e menos clipe de banco (que
pra futebol só tem clube estrangeiro). A ESPN tem foto de 2 em 50 do
elenco; a Wikipédia tem da maioria, com licença livre (Wikimedia Commons)
— o crédito vai na descrição. A foto principal do verbete é da pessoa
certa, mas muitas vezes com camisa de OUTRO clube (Benfica, Chelsea):
quem usa aplica preto e branco (ver channel.fallback_grayscale).

Só aceita verbete cujo título tem o nome citado e cujo resumo fala do
`required` (ex.: "Botafogo") — "Kadu" sozinho devolvia o verbete do clube,
"Danilo" tem dezenas de homônimos. Grátis, sem chave; resultado (achou ou
não) fica em cache por 7 dias.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import httpx

log = logging.getLogger("people_photos")

_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "people_photos.json"
_CACHE_TTL = 7 * 86400
_UA = {"User-Agent": "BotafogoWIN-video-bot/1.0 (https://botafogo.win)"}
_API = "https://pt.wikipedia.org/w/api.php"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# parte de nome que também é palavra comum na fala ("aos domingos")
_COMMON_WORDS = {"domingos", "santos", "lucas", "victor"}


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _credit(file_name: str) -> str:
    try:
        info = httpx.get(_COMMONS_API, headers=_UA, timeout=20, params={
            "action": "query", "format": "json", "titles": f"File:{file_name}",
            "prop": "imageinfo", "iiprop": "extmetadata",
        }).json()
        page = next(iter(info["query"]["pages"].values()))
        meta = page["imageinfo"][0]["extmetadata"]
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
        lic = meta.get("LicenseShortName", {}).get("value", "")
        return f"{artist or 'autor desconhecido'} ({lic}, Wikimedia Commons)"
    except Exception:  # noqa: BLE001
        return "Wikimedia Commons"


def _page_ok(page: dict, name: str, required: str) -> bool:
    """Título com o nome INTEIRO (só uma parte aceitava "Loucos pelo
    Botafogo", verbete da torcida, como foto do Júnior Santos — job 423),
    resumo citando o `required` e foto em jpg/png."""
    title, extract = _plain(page.get("title", "")), _plain(page.get("extract", ""))
    tokens = [t for t in _plain(name).split() if len(t) >= 3]
    return (
        bool(page.get("original"))
        and page.get("pageimage", "").lower().endswith((".jpg", ".jpeg", ".png"))
        and all(t in title for t in tokens)
        and _plain(required) in extract
    )


_PAGE_PROPS = {
    "action": "query", "format": "json", "redirects": 1,
    "prop": "pageimages|extracts", "piprop": "original|name",
    "exintro": 1, "explaintext": 1, "exchars": 1500,
}


def _lookup(name: str, required: str) -> dict | None:
    # 1) verbete pelo título ("Alex Telles", "Vitinho (futebolista)"); 2) busca
    direct = httpx.get(_API, headers=_UA, timeout=20, params={
        **_PAGE_PROPS, "titles": f"{name}|{name} (futebolista)",
    }).json()
    searched = httpx.get(_API, headers=_UA, timeout=20, params={
        **_PAGE_PROPS, "generator": "search", "gsrsearch": f"{name} futebol {required}", "gsrlimit": 3,
    }).json()
    pages = list((direct.get("query") or {}).get("pages", {}).values())
    pages += sorted((searched.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for page in pages:
        if _page_ok(page, name, required):
            return {"url": page["original"]["source"], "credito": _credit(page["pageimage"]), "pessoa": name}
    return None


def person_photo(name: str, required: str) -> dict | None:
    cache = _load_cache()
    key = f"{_plain(name)}|{_plain(required)}"
    hit = cache.get(key)
    if hit and time.time() - hit.get("ts", 0) < _CACHE_TTL:
        return hit.get("foto")
    try:
        photo = _lookup(name, required)
    except Exception as exc:  # noqa: BLE001
        log.warning("foto de %s indisponível: %s", name, exc)
        return None  # erro de rede não entra no cache
    cache[key] = {"ts": time.time(), "foto": photo}
    try:
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    except OSError:
        pass
    return photo


def _keep_case(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return f" {' '.join(re.findall(r'[A-Za-z0-9]+', plain))} "


def names_in(text: str, names: list[str]) -> list[str]:
    """Quem de `names` a fala cita, na ordem em que aparece. Vale o nome
    completo ou uma parte dele que só uma pessoa da lista tem ("Ziyech",
    "Telles"); parte repetida ("Gabriel", "Santos", "Arthur") não conta.
    Nome só conta com inicial maiúscula ("aos domingos" não é o Domingos), e
    nome de uma palavra que está dentro de outro do elenco ("Danilo" e
    "Danilo Santos") fica de fora — foto da pessoa errada é pior que nenhuma."""
    fala = _keep_case(text)
    parts: dict[str, list[str]] = {}
    for n in names:
        for t in set(_plain(n).split()):
            if len(t) >= 4 and t not in _COMMON_WORDS:
                parts.setdefault(t, []).append(n)
    found: list[tuple[int, str]] = []
    for n in names:
        words = _keep_case(n).split()
        if len(words) == 1 and any(n != o and words[0].lower() in _plain(o).split() for o in names):
            continue
        cap = [w[:1].upper() + w[1:] for w in words]
        pos = fala.find(f" {' '.join(cap)} ")
        if pos < 0:
            hits = [fala.find(f" {w} ") for w in cap if len(parts.get(w.lower(), [])) == 1]
            hits = [h for h in hits if h >= 0]
            pos = min(hits) if hits else -1
        if pos >= 0:
            found.append((pos, n))
    return [n for _, n in sorted(found)]
