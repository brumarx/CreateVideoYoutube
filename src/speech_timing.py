"""Timing de narração dinâmico por canal — credibilidade vs energia.

edge-tts com rate="-8%" narra ~140 wpm, mas pode variar:
- Política: -12% (~130 wpm) — devagar, credibilidade, peso
- Motivacional: -4% (~155 wpm) — rápido, energia, emoção
- Aprendervia: -8% (~140 wpm) — padrão, claro
- Curiosidades: -6% (~145 wpm) — ritmo natural
- Botafogo: -8% (~140 wpm) — claro, emoção variável
"""
from __future__ import annotations


def get_tts_rate(channel: str) -> str:
    """Retorna a taxa de fala (edge-tts rate parameter) por canal."""
    rates = {
        "politica": "-12%",        # Devagar, credível
        "motivacional": "-4%",     # Rápido, energético
        "aprendervia": "-8%",      # Padrão
        "curiosidades": "-6%",     # Natural
        "botafogo": "-8%",         # Padrão
    }
    return rates.get(channel, "-8%")  # fallback


def get_pause_rules(channel: str) -> dict:
    """Retorna regras de pausa por canal (em cenas específicas)."""
    rules = {
        "politica": {
            "before_number": 0.3,      # Pausa antes de R$ ou número
            "after_shocking": 0.5,     # Pausa após número chocante
            "before_consequence": 0.2, # Pausa antes de conclusão
        },
        "motivacional": {
            "before_number": 0.2,      # Pausa curta
            "after_shocking": 0.3,     # Pausa média antes de impacto
            "before_consequence": 0.15,
        },
        "aprendervia": {
            "before_number": 0.1,      # Sem pausa, fluxo rápido
            "after_shocking": 0.15,
            "before_consequence": 0.1,
        },
        "curiosidades": {
            "before_number": 0.15,
            "after_shocking": 0.4,     # Pausa longa para impactar
            "before_consequence": 0.2,
        },
        "botafogo": {
            "before_number": 0.1,
            "after_shocking": 0.3,
            "before_consequence": 0.1,
        },
    }
    return rules.get(channel, rules["aprendervia"])


def get_accent_instructions(channel: str) -> str:
    """Instruções de entonação para o LLM (narração)."""
    instructions = {
        "politica": (
            "Entonação: SÉRIA e CREDÍVEL. Números são ditos com peso (não rápido). "
            "Contradições com inflexão de ironia contida. Perguntas são retóricas, "
            "não entusiastas. Dramaticidade baixa, consistência alta."
        ),
        "motivacional": (
            "Entonação: INSPIRADORA e ENERGÉTICA. Obstáculos ditos com gravidade, "
            "soluções com esperança. Perguntas existenciais com sinceridade. "
            "Resultado final com entusiasmo e vitória. Variação de tom entre cenas."
        ),
        "aprendervia": (
            "Entonação: CLARA e PRÁTICA. Passos são ditos direto, sem suspense. "
            "Benefícios com entusiasmo (mas não excessivo). Perguntas são convites, "
            "não dúvidas. Tom de tutorial, não entertainment."
        ),
        "curiosidades": (
            "Entonação: SURPREENDIDA e CURIOSA. Números chocantes com ênfase dramática. "
            "Contradições com incredulidade. Mistérios com suspense. Conclusões com "
            "satisfação de descoberta. Variação alta de tom."
        ),
        "botafogo": (
            "Entonação: APAIXONADA e EMOTIVA. Placares e gols com entusiasmo. "
            "Análise técnica com seriedade. Momentos históricos com peso. "
            "Torcida vive cada lance, narração acompanha."
        ),
    }
    return instructions.get(channel, instructions["aprendervia"])
