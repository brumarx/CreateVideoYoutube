"""Narração via edge-tts — baseado em /var/www/html/reacao/bot/tts_edge.py
do ariaBot (mesma lib, mesma prosódia padrão)."""
from __future__ import annotations

import asyncio
import json
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


async def _synthesize(text: str, output_path: Path, voice: str, metadata_path: Path) -> list[dict]:
    # -15% (valor antigo) saía devagar demais e monótono — feedback direto
    # de quem assistiu vários vídeos publicados: "muito chatos, precisam de
    # mais entonação e vontade". Velocidade normal + tom um pouco mais alto
    # soa bem mais engajado sem virar corrida de fala. Se mudar de novo,
    # recalibrar WORDS_PER_MINUTE em script_gen.py junto (duração do vídeo
    # depende dessa velocidade).
    # boundary="WordBoundary" é obrigatório aqui — o padrão da lib é
    # "SentenceBoundary" (só 1 evento por frase inteira, inútil pra
    # legenda karaokê palavra a palavra); sem isso o metadata sai vazio de
    # WordBoundary (testado ao vivo: virava 0 palavras sempre).
    communicate = edge_tts.Communicate(
        text, voice, pitch="+8Hz", rate="+0%", volume="+15%", boundary="WordBoundary"
    )
    await communicate.save(str(output_path), str(metadata_path))

    # `save()` já grava um WordBoundary por linha (JSON) quando recebe
    # metadata_fname — timestamp REAL de cada palavra, direto do serviço da
    # Microsoft (offset/duration em unidades de 100ns, por isso /10_000_000
    # pra virar segundos). Usado pra legenda karaokê sincronizada de
    # verdade em vez de estimar por proporção de palavras (ver
    # src/assemble.py). Arquivo de metadata é só um artefato intermediário,
    # não precisa sobrar no disco depois de lido.
    word_boundaries = []
    if metadata_path.exists():
        for line in metadata_path.read_text().splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("type") == "WordBoundary":
                word_boundaries.append({
                    "text": msg["text"],
                    "start": msg["offset"] / 10_000_000,
                    "end": (msg["offset"] + msg["duration"]) / 10_000_000,
                })
        metadata_path.unlink()
    return word_boundaries


def narrate(text: str, output_path: Path, voice: str = DEFAULT_VOICE) -> tuple[Path, list[dict]]:
    """Sintetiza `text` em áudio mp3 e devolve (caminho salvo, lista de
    palavras com tempo real `{"text", "start", "end"}` em segundos — vazia
    se o serviço não mandou WordBoundary por algum motivo; quem chamar deve
    cair pra um fallback nesse caso, nunca assumir que sempre vem preenchida)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output_path.with_suffix(".wordtimes.json")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            word_boundaries = asyncio.run(_synthesize(text, output_path, voice, metadata_path))
            return output_path, word_boundaries
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == MAX_ATTEMPTS:
                raise
            log.warning(
                "edge-tts não devolveu áudio (tentativa %d/%d) — tentando de novo",
                attempt, MAX_ATTEMPTS,
            )
            time.sleep(RETRY_DELAY_S)
    return output_path, []
