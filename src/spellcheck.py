"""Revisão de português (LanguageTool, API pública grátis) do texto que vai
pro ar: título, texto da thumbnail, descrição e narração (vira a legenda
karaokê na tela). O LLM gratuito erra concordância — "O Oceano Esconde Um
Cidade Inteira" foi publicado assim (job 295).

Só corrige sozinho o que é regra de gramática (concordância, crase, verbo):
erro de "digitação" do LanguageTool pega nome próprio estrangeiro
(Anticítera, Huascarán) e trocaria por palavra errada. Título em Title Case
é checado em minúsculas — com cada palavra em maiúscula o LanguageTool acha
que é tudo nome próprio e não aponta nada.

API fora do ar ou estourando limite: devolve o texto como veio, nunca
bloqueia o vídeo por causa disso.
"""
from __future__ import annotations

import difflib
import json
import logging
import re

import httpx

from .providers import complete

log = logging.getLogger("spellcheck")

CHECK_URL = "https://api.languagetool.org/v2/check"
# CONFUSED_WORDS fica de fora: trocava "a relatoria" por "a relatória"
# (varredura dos vídeos publicados, 06/10)
AUTO_FIX_CATEGORIES = {"GRAMMAR", "CRASE"}
SEP = "\n\n"


def _matches(text: str, language: str) -> list[dict] | None:
    try:
        resp = httpx.post(CHECK_URL, data={"text": text, "language": language}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("matches", [])
    except Exception as exc:  # noqa: BLE001 — revisão é barreira extra, não pode derrubar o job
        log.warning("LanguageTool indisponível (%s) — texto segue sem revisão", exc)
        return None


def _match_case(original: str, replacement: str) -> str:
    """'Um Cidade' -> 'Uma Cidade': repete a caixa de cada palavra original."""
    if original.isupper():
        return replacement.upper()
    words = original.split()
    if words and all(w[:1].isupper() for w in words):
        return " ".join(w[:1].upper() + w[1:] for w in replacement.split())
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


MAX_PASSES = 3


def fix_text(text: str, language: str = "pt-BR") -> str:
    """Repete até não sobrar erro corrigível: uma correção pode revelar
    outra ("das cidade perdida" -> "das cidades perdida" -> "...perdidas")."""
    typos: dict[str, list[str]] = {}
    for _ in range(MAX_PASSES):
        fixed, typos = _fix_once(text, language)
        if fixed == text:
            break
        text = fixed
    # 2 opiniões: modelo grátis às vezes acha que "sumergida" (espanhol) é
    # português; só aplica correção que bate com o corretor ou é parecida
    for _ in range(2):
        if not typos:
            break
        text, typos = _fix_typos(text, typos)
    return text


def _fix_typos(text: str, typos: dict[str, list[str]]) -> tuple[str, dict[str, list[str]]]:
    """Devolve o texto e as palavras que continuaram como estavam."""
    """Palavra comum (minúscula) que o LanguageTool não conhece: a sugestão
    dele costuma ser ruim ("quemoprático" -> "quemo prático"; o certo é
    "quiroprático"), então quem corrige é o LLM, vendo a frase. Nome próprio
    (maiúscula) nunca chega aqui."""
    from .topics import _SMALL_MODEL_RE  # modelo pequeno "corrige" palavra certa

    prompt = (
        f"Texto em português do Brasil:\n{text[:3000]}\n\n"
        "Palavras suspeitas de erro de digitação (com a sugestão do corretor): "
        + "; ".join(f"{w} -> {sug or '?'}" for w, sug in sorted(typos.items())) + "\n"
        "Para cada uma: ela existe no dicionário do PORTUGUÊS DO BRASIL (ou é "
        "termo técnico/estrangeirismo de uso comum no Brasil, tipo \"backtest\")? "
        "Palavra de outra língua parecida com o português NÃO conta (espanhol "
        "\"sumergida\" -> \"submergida\"). Se existe, devolva igual. Se não, "
        "devolva a palavra em português que o autor quis escrever, corrigindo só "
        "letras trocadas, faltando ou sobrando (\"antigoss\" -> \"antigos\") — "
        "nunca outra palavra de sentido diferente. Responda "
        'SÓ JSON {"palavra": "correção"}.'
    )

    def parse(raw: str) -> dict | None:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            d = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None
        return d if isinstance(d, dict) and set(d) <= set(typos) else None

    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=300,
            validate=lambda r: parse(r) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
    except RuntimeError as exc:
        log.warning("correção de digitação sem LLM (%s) — fica como está", exc)
        return text, {}
    remaining: dict[str, list[str]] = {}
    for wrong, right in parse(raw).items():
        right = str(right).strip()
        # correção tem que ser a MESMA palavra com letras arrumadas:
        # "quemoprático" -> "prático" (0,74) mudava o sentido; o certo
        # "quiroprático" é 0,83
        # ou uma das sugestões do LanguageTool ("sumergida" -> "submergida")
        similar = difflib.SequenceMatcher(None, wrong, right.lower()).ratio() >= 0.8
        suggested = right.lower() in {x.lower() for x in typos.get(wrong, [])}
        if right == wrong or not right:
            remaining[wrong] = typos[wrong]
            continue
        if len(right.split()) != 1 or not (similar or suggested):
            log.info("digitação: %r -> %r recusado (palavra diferente demais)", wrong, right)
            continue
        log.warning("corrigido (digitação): %r -> %r", wrong, right)
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text)
    return text, remaining


def _fix_once(text: str, language: str) -> tuple[str, dict[str, list[str]]]:
    typos: dict[str, list[str]] = {}
    if not text or not text.strip():
        return text, typos
    words = text.split()
    title_case = len(words) > 2 and sum(w[:1].isupper() for w in words) / len(words) > 0.6
    probe = text.lower() if title_case else text
    if len(probe) != len(text):  # offsets precisam bater 1:1 com o original
        probe = text
    matches = _matches(probe, language)
    if not matches:
        return text, typos
    next_start = len(text)
    candidates: list[tuple[int, int, str, str, str]] = []  # (início, fim, original, novo, motivo)
    logged: set[tuple[str, str]] = set()  # nome repetido no texto: 1 linha de log só
    # de trás pra frente: corrigir um trecho não desloca os offsets anteriores
    for m in sorted(matches, key=lambda m: m["offset"], reverse=True):
        category = m["rule"]["category"]["id"]
        original = text[m["offset"]:m["offset"] + m["length"]]
        if m["offset"] + m["length"] > next_start:
            continue  # sobrepõe trecho já corrigido — fica pra próxima passada
        if "\n" in original:
            # concordância entre linhas/cenas diferentes ("o\n1:06" -> "a
            # 1:06"): o LanguageTool lê como uma frase só
            continue
        if category == "TYPOS" and not title_case and original.isalpha() and original.islower():
            typos[original] = [r["value"] for r in m.get("replacements", [])[:3]]
            continue
        if category not in AUTO_FIX_CATEGORIES or not m.get("replacements"):
            if (category, original) not in logged:
                logged.add((category, original))
                log.info("LanguageTool apontou (não corrigido, %s): %r — %s", category, original, m["message"])
            continue
        new = _match_case(original, m["replacements"][0]["value"])
        candidates.append((m["offset"], m["offset"] + m["length"], original, new, m["message"]))
        next_start = m["offset"]
    fixed = text
    for start, end, original, new, motivo in _confirmed(text, candidates):  # já de trás pra frente
        log.warning("corrigido: %r -> %r (%s)", original, new, motivo)
        fixed = fixed[:start] + new + fixed[end:]
    return fixed, typos


def _confirmed(text: str, candidates: list[tuple[int, int, str, str, str]]) -> list[tuple[int, int, str, str, str]]:
    """Regra de concordância do LanguageTool também erra: "vermelho
    terracota" (nome de cor composto, certo) virou "vermelha terracota" no
    job 304. Um LLM grande confirma cada troca vendo a frase; sem LLM, vale a
    sugestão do LanguageTool (pegou "Um Cidade")."""
    if not candidates:
        return []
    from .topics import _SMALL_MODEL_RE

    itens = []
    for k, (start, end, original, new, _) in enumerate(candidates):
        frase = text[max(0, start - 60):end + 60].replace("\n", " ")
        itens.append(f'{k}. trecho "{original}" -> "{new}" | frase: "...{frase}..."')
    prompt = (
        "Um corretor automático de português do Brasil sugeriu estas trocas:\n"
        + "\n".join(itens)
        + "\n\nPara cada uma: o trecho original está ERRADO e a troca o corrige? "
        "Nome de cor composto (\"vermelho terracota\", \"azul piscina\"), título, "
        "nome próprio e expressão correta não se trocam. Responda SÓ JSON "
        '{"0": true, "1": false, ...} (true = aplicar a troca).'
    )

    def parse(raw: str) -> dict | None:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            d = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None
        return d if isinstance(d, dict) and all(isinstance(v, bool) for v in d.values()) else None

    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=200,
            validate=lambda r: parse(r) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
    except RuntimeError as exc:
        log.warning("confirmação das correções sem LLM (%s) — vale o LanguageTool", exc)
        return candidates
    verdict = parse(raw)
    kept = []
    for k, c in enumerate(candidates):
        if verdict.get(str(k), False):
            kept.append(c)
        else:
            log.info("correção recusada pelo LLM: %r -> %r", c[2], c[3])
    return kept


def _proofread_short(text: str) -> str:
    """Título/thumbnail com palavra certa no lugar errado, que o LanguageTool
    não vê: "Tutorial Passa a Passa" (job 304), "quem quer lançam sua marca"
    na descrição (job 310). LLM grande revisa; só vale se a correção for
    pequena (não reescreve o texto)."""
    from .topics import _SMALL_MODEL_RE

    prompt = (
        f"Texto de título/thumbnail de vídeo do YouTube em português do Brasil: \"{text}\"\n"
        "Corrija SÓ erro de português ou de digitação (expressão errada, concordância, "
        "palavra trocada) mantendo maiúsculas, asteriscos e o resto igual. Sem erro, "
        "devolva exatamente igual (mesmas quebras de linha). Responda SÓ o texto, sem aspas."
    )
    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=max(120, len(text) // 2),
            validate=lambda r: 0 < len(r.strip().strip('"')) <= len(text) * 1.3 + 10,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
    except RuntimeError:
        return text
    new = raw.strip().strip('"“”') if "\n" in text else raw.strip().splitlines()[0].strip().strip('"“”')
    # texto longo: só aceita retoque (0,95), nunca reescrita
    limit = 0.85 if len(text) < 150 else 0.95
    if new != text and difflib.SequenceMatcher(None, text.lower(), new.lower()).ratio() >= limit:
        if _introduces_typo(text, new):
            log.info("revisão recusada (criou palavra inexistente): %r -> %r", text, new)
            return text
        log.warning("revisado: %r -> %r", text, new)
        return new
    return text


def _introduces_typo(old: str, new: str) -> bool:
    """O LLM "corrigiu" "estreia" pra "estrea" (job 309): palavra nova que o
    LanguageTool não reconhece invalida a revisão."""
    old_words = set(re.findall(r"\w+", old.lower()))
    changed = {w for w in re.findall(r"\w+", new.lower()) if w not in old_words}
    if not changed:
        return False
    for m in _matches(new.lower(), "pt-BR") or []:
        frag = new.lower()[m["offset"]:m["offset"] + m["length"]]
        if m["rule"]["category"]["id"] == "TYPOS" and frag in changed:
            return True
    return False


# Marcação de texto que o LLM às vezes põe no roteiro (**negrito**, # título,
# [link](url), emoji) — a voz lia "asterisco asterisco" (botafogo, job 361).
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*_`#~^|<>{}\[\]\\]+")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+"
)
_LIST_BULLET = re.compile(r"(?m)^\s*(?:[-•–]|\d+[.)])\s+")


def clean_markup(text: str) -> str:
    """Só o que pode ser FALADO: tira markdown, emoji e marcador de lista."""
    text = _MD_LINK.sub(r"\1", text)
    text = _LIST_BULLET.sub("", text)
    text = _MD_MARKS.sub(" ", text)
    text = _EMOJI.sub(" ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return " ".join(text.split())


def fix_script(script: dict, language: str = "pt-BR") -> dict:
    """Corrige título, texto da thumbnail, descrição e narrações no lugar.
    Narrações vão numa chamada só (limite da API pública: 20 req/min)."""
    for scene in script.get("scenes") or []:
        scene["narration"] = clean_markup(scene.get("narration", ""))
    for key in ("title", "thumbnail_text"):
        if script.get(key):
            script[key] = clean_markup(script[key])
    for key in ("title", "thumbnail_text", "description"):
        if script.get(key):
            script[key] = fix_text(script[key], language)
    for key in ("title", "thumbnail_text", "description"):
        if script.get(key):
            script[key] = _proofread_short(script[key])
    scenes = script.get("scenes") or []
    narrations = [" ".join(s["narration"].split()) for s in scenes]  # sem \n\n interno
    joined = fix_text(SEP.join(narrations), language)
    parts = joined.split(SEP)
    if len(parts) == len(scenes):
        for scene, narration in zip(scenes, parts):
            scene["narration"] = narration
    # de novo no fim: a correção (também por LLM) pode devolver marcação
    for scene in scenes:
        scene["narration"] = clean_markup(scene["narration"])
    return script
