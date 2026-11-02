"""Visuais dinâmicos por canal — zoom, cores, efeitos.

1. Ken Burns dinâmico (zoom) por tipo de cena
2. Cores de captions customizadas por canal
3. Efeitos especiais em momentos-chave
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KenBurnsSettings:
    """Configuração de zoom (Ken Burns) para uma cena."""
    zoom_factor: float  # 1.02 (mínimo) a 1.2 (máximo)
    duration: float     # segundos
    focus: str          # "center", "top", "bottom", "left", "right"


def get_ken_burns_for_scene(channel: str, scene_type: str) -> KenBurnsSettings:
    """Retorna configuração de zoom baseado no tipo de cena.

    Tipos: "person_close", "landscape", "data_graphic", "action", "emotion"
    """
    # Padrão para todos os canais
    defaults = {
        "person_close": KenBurnsSettings(zoom_factor=1.02, duration=5.0, focus="center"),
        "landscape": KenBurnsSettings(zoom_factor=1.06, duration=3.0, focus="center"),
        "data_graphic": KenBurnsSettings(zoom_factor=1.0, duration=0.0, focus="center"),  # Sem zoom
        "action": KenBurnsSettings(zoom_factor=1.08, duration=2.5, focus="center"),
        "emotion": KenBurnsSettings(zoom_factor=1.04, duration=4.0, focus="center"),
    }

    # Customizações por canal
    overrides = {
        "politica": {
            "data_graphic": KenBurnsSettings(zoom_factor=1.0, duration=0.0, focus="center"),
            "action": KenBurnsSettings(zoom_factor=1.03, duration=3.0, focus="center"),
        },
        "motivacional": {
            "emotion": KenBurnsSettings(zoom_factor=1.06, duration=3.5, focus="center"),
            "action": KenBurnsSettings(zoom_factor=1.1, duration=2.0, focus="center"),
        },
        "curiosidades": {
            "action": KenBurnsSettings(zoom_factor=1.12, duration=2.0, focus="center"),
            "landscape": KenBurnsSettings(zoom_factor=1.08, duration=2.5, focus="center"),
        },
    }

    channel_overrides = overrides.get(channel, {})
    return channel_overrides.get(scene_type, defaults.get(scene_type, defaults["landscape"]))


def get_caption_color(channel: str) -> str:
    """Retorna cor do karaokê (caption highlight) por canal."""
    colors = {
        "politica": "#8c8cff",      # Azul (credibilidade)
        "motivacional": "#00ff99",  # Verde (esperança, vida)
        "aprendervia": "#dd44ff",   # Roxo/Magenta (criatividade)
        "curiosidades": "#ffaa00",  # Laranja (surpresa, energia)
        "botafogo": "#000000",      # Preto (ou use accent do canal)
    }
    return colors.get(channel, "#ffd400")  # fallback amarelo


def get_caption_background(channel: str) -> tuple[int, int, int, int]:
    """Retorna cor de fundo da faixa de caption (RGBA) por canal."""
    # (R, G, B, Alpha 0-255 onde 255 é opaco, 0 é transparente)
    backgrounds = {
        "politica": (20, 20, 60, 200),      # Azul escuro, translúcido
        "motivacional": (0, 40, 20, 200),   # Verde escuro
        "aprendervia": (40, 0, 60, 200),    # Roxo escuro
        "curiosidades": (50, 30, 0, 200),   # Marrom escuro
        "botafogo": (0, 0, 0, 200),         # Preto
    }
    return backgrounds.get(channel, (0, 0, 0, 220))  # fallback preto opaco


def get_color_grade_by_mood(channel: str, mood: str) -> str:
    """Retorna filtro de color grade (ffmpeg) baseado no mood da cena."""
    # Moods: "tenso", "animado", "melancolico", "calmo", "neutro"

    # Filtro base: eq=contrast=1.05:saturation=1.12,vignette=PI/5
    # Adicionar antes:
    mood_grades = {
        "tenso": "eq=contrast=1.15:saturation=0.9:brightness=-0.05",  # Alto contraste, menos saturação
        "animado": "eq=contrast=1.1:saturation=1.25:brightness=0.05", # Saturado, luminoso
        "melancolico": "eq=contrast=0.95:saturation=0.85:brightness=-0.1", # Baixo contraste, dessaturado
        "calmo": "eq=contrast=1.0:saturation=1.0:brightness=0.02",    # Neutro, suave
        "neutro": "eq=contrast=1.05:saturation=1.12",                 # Padrão
    }

    # Customizar por canal se necessário
    if channel == "botafogo":
        # Botafogo ganha saturação maior (pré-jogo = cores vibrantes)
        mood_grades.update({
            "animado": "eq=contrast=1.15:saturation=1.3:brightness=0.08",
            "tenso": "eq=contrast=1.2:saturation=1.0",
        })

    base_grade = mood_grades.get(mood, mood_grades["neutro"])
    # Adicionar vignette sempre
    return f"{base_grade},vignette=PI/5"


def should_grayscale_fade(channel: str, mood: str) -> bool:
    """Se True, aplicar fade para grayscale leve nas cenas tristes/finais."""
    # Apenas em motivacional com mood melancolico
    if channel == "motivacional" and mood == "melancolico":
        return True
    if channel == "curiosidades" and mood == "melancolico":
        return True
    return False


def get_emphasis_effect(channel: str, moment_type: str) -> str | None:
    """Retorna efeito de ênfase (ffmpeg filter) para momentos especiais.

    moment_type: "punchline", "number", "revelation", "tragedy", "victory"
    Retorna None se não deve aplicar nada.
    """
    effects = {
        "politica": {
            "punchline": None,              # Nenhum efeito, confiança na fala
            "number": None,
            "revelation": None,
        },
        "motivacional": {
            "victory": "eq=brightness=0.1",  # Mais luminoso em vitória
            "tragedy": "eq=brightness=-0.1", # Mais escuro em tragédia
            "revelation": None,
        },
        "curiosidades": {
            "revelation": "eq=saturation=1.2",  # Mais saturado na revelação
            "number": None,
        },
    }

    channel_effects = effects.get(channel, {})
    return channel_effects.get(moment_type, None)
