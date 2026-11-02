"""Integração de todas as melhorias — ponto central de ativação.

Módulos de melhoria criados:
- opening_variations.py: Aberturas/encerramentos variados por canal
- speech_timing.py: Timing de narração dinâmico (taxa por canal)
- stock_media_diversity.py: Diversidade de stock footage (7 dias)
- visual_dynamics.py: Zoom dinâmico, cores, efeitos por cena/channel
- sfx_smart.py: SFX inteligente (whoosh 30%, impact em punchlines)
- performance_tracking.py: Analytics de performance (A/B testing)
- trending_topics.py: (já existe) Trending Topics ativo via Google Trends RSS
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import ChannelConfig
from .opening_variations import get_opening_rules, select_opening, select_closing
from .speech_timing import get_tts_rate, get_accent_instructions
from .stock_media_diversity import load_recent_media, mark_used, score_candidates
from .visual_dynamics import get_ken_burns_for_scene, get_caption_color, get_color_grade_by_mood
from .sfx_smart import should_use_whoosh, detect_impact_moment, get_channel_sfx_rules
from .performance_tracking import init_schema, record_video_published
from .trending_topics import fetch_trending_for_channel

log = logging.getLogger("improvements_integration")


def enhance_script_prompt(
    channel: ChannelConfig,
    base_prompt: str,
) -> str:
    """Adiciona instruções de abertura/encerramento variados ao prompt do LLM."""

    opening_rule = select_opening(channel.name)
    opening_instruction = opening_rule.instructions

    closing_rule = select_closing(
        channel.name,
        site=channel.website,
    )

    enhancement = f"""
VARIAÇÃO DE ABERTURA (imprevisibilidade, retenção):
O gancho inicial DEVE seguir este estilo:
{opening_instruction}

ENCERRAMENTO (CTA natural):
O último parágrafo do vídeo DEVE terminar com:
{closing_rule}
"""

    return base_prompt + enhancement


def get_channel_tts_config(channel: ChannelConfig) -> dict:
    """Retorna configuração completa de TTS para o canal."""
    return {
        "rate": get_tts_rate(channel.name),
        "accent_instructions": get_accent_instructions(channel.name),
    }


def get_scene_visual_config(
    channel_name: str,
    scene_index: int,
    scene_type: str = "normal",
    mood: str = "neutro",
) -> dict:
    """Retorna configuração visual completa para uma cena."""
    return {
        "ken_burns": get_ken_burns_for_scene(channel_name, scene_type),
        "caption_color": get_caption_color(channel_name),
        "color_grade": get_color_grade_by_mood(channel_name, mood),
        "has_whoosh": should_use_whoosh(scene_index),
        "has_impact_sfx": False,  # será detectado na narração
    }


def initialize_improvements() -> None:
    """Inicializa infraestrutura de melhorias (criar tabelas, cache, etc)."""
    log.info("Inicializando infraestrutura de melhorias...")

    # Criar schema de performance tracking
    try:
        init_schema()
        log.info("✓ Performance tracking schema criado")
    except Exception as e:
        log.warning("Performance tracking: %s", e)

    # Carregar histórico de stock media (valida 7 dias)
    try:
        media_recent = load_recent_media()
        log.info("✓ Stock media recent history carregado (%d clips, %d fotos)",
                 len(media_recent.get("clips", {})),
                 len(media_recent.get("photos", {})))
    except Exception as e:
        log.warning("Stock media diversity: %s", e)


def fetch_trending_topics_for_all_channels() -> dict[str, list[str]]:
    """Busca trending topics para todos os canais (para viral_share dinâmico)."""
    channels = {
        "politica": ["política", "reforma", "eleição", "deputados", "senadores"],
        "motivacional": ["história", "sucesso", "brasileiro"],
        "aprendervia": ["IA", "ferramenta", "grátis"],
        "curiosidades": ["curiosidade", "mundo", "país"],
        "botafogo": ["Botafogo", "futebol", "Brasil"],
    }

    trending = {}
    for channel_name in channels.keys():
        try:
            topics = fetch_trending_for_channel(channel_name)
            if topics:
                trending[channel_name] = topics
                log.debug("Trending para %s: %d tópicos", channel_name, len(topics))
        except Exception as e:
            log.debug("Trending topics (%s): %s", channel_name, e)

    return trending
