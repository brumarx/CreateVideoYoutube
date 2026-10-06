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

import logging

import httpx

log = logging.getLogger("spellcheck")

CHECK_URL = "https://api.languagetool.org/v2/check"
AUTO_FIX_CATEGORIES = {"GRAMMAR", "CONFUSED_WORDS", "CRASE"}
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
    for _ in range(MAX_PASSES):
        fixed = _fix_once(text, language)
        if fixed == text:
            break
        text = fixed
    return text


def _fix_once(text: str, language: str) -> str:
    if not text or not text.strip():
        return text
    words = text.split()
    title_case = len(words) > 2 and sum(w[:1].isupper() for w in words) / len(words) > 0.6
    probe = text.lower() if title_case else text
    if len(probe) != len(text):  # offsets precisam bater 1:1 com o original
        probe = text
    matches = _matches(probe, language)
    if not matches:
        return text
    fixed = text
    next_start = len(text)
    # de trás pra frente: corrigir um trecho não desloca os offsets anteriores
    for m in sorted(matches, key=lambda m: m["offset"], reverse=True):
        category = m["rule"]["category"]["id"]
        original = text[m["offset"]:m["offset"] + m["length"]]
        if m["offset"] + m["length"] > next_start:
            continue  # sobrepõe trecho já corrigido — fica pra próxima passada
        if category not in AUTO_FIX_CATEGORIES or not m.get("replacements"):
            log.info("LanguageTool apontou (não corrigido, %s): %r — %s", category, original, m["message"])
            continue
        new = _match_case(original, m["replacements"][0]["value"])
        log.warning("corrigido: %r -> %r (%s)", original, new, m["message"])
        fixed = fixed[:m["offset"]] + new + fixed[m["offset"] + m["length"]:]
        next_start = m["offset"]
    return fixed


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
