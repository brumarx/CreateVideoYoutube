"""Narração via edge-tts — baseado em /var/www/html/reacao/bot/tts_edge.py
do ariaBot (mesma lib, mesma prosódia padrão)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

DEFAULT_VOICE = "pt-BR-FranciscaNeural"


async def _synthesize(text: str, output_path: Path, voice: str) -> None:
    communicate = edge_tts.Communicate(
        text, voice, pitch="+5Hz", rate="-15%", volume="+10%"
    )
    await communicate.save(str(output_path))


def narrate(text: str, output_path: Path, voice: str = DEFAULT_VOICE) -> Path:
    """Sintetiza `text` em áudio mp3 e devolve o caminho salvo."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_synthesize(text, output_path, voice))
    return output_path
