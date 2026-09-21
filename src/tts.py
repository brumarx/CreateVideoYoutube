"""Narração via edge-tts — baseado em /var/www/html/reacao/bot/tts_edge.py
do ariaBot (mesma lib, mesma prosódia padrão)."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import edge_tts

log = logging.getLogger("tts")

DEFAULT_VOICE = "pt-BR-FranciscaNeural"

# edge-tts fala com um endpoint não-oficial da Microsoft que ocasionalmente
# devolve NoAudioReceived sem motivo aparente (falha transitória documentada
# na lib, nada a ver com o texto/voz) — sem retry, isso derrubava o job
# inteiro no meio da narração de uma cena.
MAX_ATTEMPTS = 4
RETRY_DELAY_S = 3


async def _synthesize(text: str, output_path: Path, voice: str) -> None:
    # -15% (valor antigo) saía devagar demais e monótono — feedback direto
    # de quem assistiu vários vídeos publicados: "muito chatos, precisam de
    # mais entonação e vontade". Velocidade normal + tom um pouco mais alto
    # soa bem mais engajado sem virar corrida de fala. Se mudar de novo,
    # recalibrar WORDS_PER_MINUTE em script_gen.py junto (duração do vídeo
    # depende dessa velocidade).
    communicate = edge_tts.Communicate(
        text, voice, pitch="+8Hz", rate="+0%", volume="+15%"
    )
    await communicate.save(str(output_path))


def narrate(text: str, output_path: Path, voice: str = DEFAULT_VOICE) -> Path:
    """Sintetiza `text` em áudio mp3 e devolve o caminho salvo."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            asyncio.run(_synthesize(text, output_path, voice))
            return output_path
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == MAX_ATTEMPTS:
                raise
            log.warning(
                "edge-tts não devolveu áudio (tentativa %d/%d) — tentando de novo",
                attempt, MAX_ATTEMPTS,
            )
            time.sleep(RETRY_DELAY_S)
    return output_path
