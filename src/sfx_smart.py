"""SFX (efeitos sonoros) inteligente — detecção de momentos-chave.

Whoosh em 30% das transições (sorteado), Impact em punchlines/revelações.
"""
from __future__ import annotations

import random
import re


def should_use_whoosh(scene_index: int, random_seed: int = 42) -> bool:
    """Determina se deve usar whoosh nesta transição (~30% das transições)."""
    random.seed(random_seed + scene_index)
    return random.random() < 0.3


def detect_impact_moment(narration: str, scene_type: str = "normal") -> bool:
    """Detecta se uma cena tem um "impact beat" que merece SFX de impacto.

    Heurísticas:
    - Números altos (comparados ao contexto)
    - Palavra de contradição/negação
    - Revelação (estrutura: expectativa + negação + fato)
    """
    narration_lower = narration.lower()

    # Palavras de impacto
    impact_keywords = [
        "mas", "porém", "contrário", "não é", "falso", "mentira",
        "surpreendente", "impossível", "choque", "revela", "descobre",
        "nunca imaginaria", "você não sabe", "pior", "melhor",
        "virou lenda", "impossível", "mudou completamente",
    ]

    has_impact_keyword = any(
        re.search(r"\b" + keyword + r"\b", narration_lower) is not None
        for keyword in impact_keywords
    )

    # Números comparativos (X vezes, X%, X vs Y)
    has_comparison = re.search(r"(\d+(\.\d+)?)\s*(vezes|%|bilhões|milhões)", narration_lower) is not None

    # Estrutura de revelação (pergunta + resposta negativa)
    has_revelation = (
        ("?" in narration and ("não" in narration_lower or "falsso" in narration_lower))
        or ("parece" in narration_lower and "mas" in narration_lower)
    )

    # Scene type specific
    if scene_type == "punchline":
        return has_impact_keyword or has_comparison
    elif scene_type == "finale":
        return has_impact_keyword or has_revelation

    return has_impact_keyword or (has_comparison and has_revelation)


def get_sfx_timing(narration: str, duration: float) -> dict | None:
    """Retorna timing de impacto (onset em segundos) ou None.

    Tenta detectar a palavra de punchline e seu tempo na narração.
    """
    # Palavras que marcam o impacto
    impact_words = ["mas", "não", "falso", "mentira", "nunca", "impossível"]

    narration_lower = narration.lower()
    words = narration_lower.split()

    for i, word in enumerate(words):
        if any(impact_word in word for impact_word in impact_words):
            # Estimar timing: palavra no índice i, ~140 wpm = ~2.4 letras por segundo
            char_position = sum(len(w) + 1 for w in words[:i])
            estimated_seconds = (char_position / 140) * 60 * (duration / duration)  # normalized

            return {
                "word": word,
                "onset_seconds": max(0.5, min(duration - 0.5, estimated_seconds)),
            }

    return None


def get_channel_sfx_rules(channel: str) -> dict:
    """Regras de SFX por canal (subtileza, frequência, etc)."""
    rules = {
        "politica": {
            "whoosh_enabled": True,
            "whoosh_volume": -30,      # Bem baixo (credibilidade)
            "impact_enabled": False,   # Sem impact (muito formal)
            "impact_volume": -25,
        },
        "motivacional": {
            "whoosh_enabled": True,
            "whoosh_volume": -25,
            "impact_enabled": True,
            "impact_volume": -15,      # Mais audível (drama)
        },
        "aprendervia": {
            "whoosh_enabled": False,   # Sem efeitos (foco em conteúdo)
            "whoosh_volume": -30,
            "impact_enabled": False,
            "impact_volume": -30,
        },
        "curiosidades": {
            "whoosh_enabled": True,
            "whoosh_volume": -20,
            "impact_enabled": True,
            "impact_volume": -12,      # Bem audível (diversão)
        },
        "botafogo": {
            "whoosh_enabled": True,
            "whoosh_volume": -22,
            "impact_enabled": True,
            "impact_volume": -14,      # Emoção
        },
    }
    return rules.get(channel, rules["curiosidades"])
