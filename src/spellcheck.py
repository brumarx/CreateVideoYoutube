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
    typos: set[str] = set()
    for _ in range(MAX_PASSES):
        fixed, typos = _fix_once(text, language)
        if fixed == text:
            break
        text = fixed
    return _fix_typos(text, typos) if typos else text


def _fix_typos(text: str, typos: set[str]) -> str:
    """Palavra comum (minúscula) que o LanguageTool não conhece: a sugestão
    dele costuma ser ruim ("quemoprático" -> "quemo prático"; o certo é
    "quiroprático"), então quem corrige é o LLM, vendo a frase. Nome próprio
    (maiúscula) nunca chega aqui."""
    from .topics import _SMALL_MODEL_RE  # modelo pequeno "corrige" palavra certa

    prompt = (
        f"Texto em português do Brasil:\n{text[:3000]}\n\n"
        f"Palavras suspeitas de erro de digitação: {sorted(typos)}\n"
        "Para cada uma, devolva a palavra que o autor QUIS escrever, corrigindo "
        "só letras trocadas, faltando ou sobrando (\"antigoss\" -> \"antigos\") — "
        "nunca troque por outra palavra de sentido diferente. Se ela já está "
        "certa (termo técnico, estrangeirismo, gíria), devolva igual. Responda "
        'SÓ JSON {"palavra": "correção"}.'
    )

    def parse(raw: str) -> dict | None:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            d = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None
        return d if isinstance(d, dict) and set(d) <= typos else None

    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=300,
            validate=lambda r: parse(r) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
    except RuntimeError as exc:
        log.warning("correção de digitação sem LLM (%s) — fica como está", exc)
        return text
    for wrong, right in parse(raw).items():
        right = str(right).strip()
        # correção tem que ser a MESMA palavra com letras arrumadas:
        # "quemoprático" -> "prático" (0,74) mudava o sentido; o certo
        # "quiroprático" é 0,83
        similar = difflib.SequenceMatcher(None, wrong, right.lower()).ratio() >= 0.8
        if right and right != wrong and len(right.split()) == 1 and similar:
            log.warning("corrigido (digitação): %r -> %r", wrong, right)
            text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text)
    return text


def _fix_once(text: str, language: str) -> tuple[str, set[str]]:
    typos: set[str] = set()
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
    fixed = text
    next_start = len(text)
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
            typos.add(original)
            continue
        if category not in AUTO_FIX_CATEGORIES or not m.get("replacements"):
            log.info("LanguageTool apontou (não corrigido, %s): %r — %s", category, original, m["message"])
            continue
        new = _match_case(original, m["replacements"][0]["value"])
        log.warning("corrigido: %r -> %r (%s)", original, new, m["message"])
        fixed = fixed[:m["offset"]] + new + fixed[m["offset"] + m["length"]:]
        next_start = m["offset"]
    return fixed, typos


def fix_script(script: dict, language: str = "pt-BR") -> dict:
    """Corrige título, texto da thumbnail, descrição e narrações no lugar.
    Narrações vão numa chamada só (limite da API pública: 20 req/min)."""
    for key in ("title", "thumbnail_text", "description"):
        if script.get(key):
            script[key] = fix_text(script[key], language)
    scenes = script.get("scenes") or []
    narrations = [" ".join(s["narration"].split()) for s in scenes]  # sem \n\n interno
    joined = fix_text(SEP.join(narrations), language)
    parts = joined.split(SEP)
    if len(parts) == len(scenes):
        for scene, narration in zip(scenes, parts):
            scene["narration"] = narration
    return script
