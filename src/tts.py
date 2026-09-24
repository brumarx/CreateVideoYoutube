"""Narração via edge-tts — baseado em /var/www/html/reacao/bot/tts_edge.py
do ariaBot (mesma lib, mesma prosódia padrão) — ou via Azure AI Speech
(API oficial paga da Microsoft, mais vozes pt-BR e sem as falhas
aleatórias do endpoint grátis) quando o canal tem `tts_provider: "azure"`.
Azure sem chave, sem cota ou fora do ar cai sozinho pro edge-tts: o vídeo
nunca deixa de sair por causa da voz."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import edge_tts

from .config import AZURE_SPEECH_KEYS, AZURE_SPEECH_REGION

log = logging.getLogger("tts")

# mesma prosódia nos dois motores (ver comentário em _synthesize)
PITCH, RATE, VOLUME = "+8Hz", "-8%", "+15%"
# vozes que o edge-tts grátis tem — voz só-Azure usa este fallback
EDGE_VOICES = {"pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural"}
EDGE_FALLBACK_VOICE = "pt-BR-AntonioNeural"

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
    # soa bem mais engajado. Só que +0% ficou rápido demais na prática
    # (feedback seguinte), então fica no meio-termo: -8%. Se mudar de novo,
    # recalibrar WORDS_PER_MINUTE em script_gen.py junto (duração do vídeo
    # depende dessa velocidade).
    # boundary="WordBoundary" é obrigatório aqui — o padrão da lib é
    # "SentenceBoundary" (só 1 evento por frase inteira, inútil pra
    # legenda karaokê palavra a palavra); sem isso o metadata sai vazio de
    # WordBoundary (testado ao vivo: virava 0 palavras sempre).
    communicate = edge_tts.Communicate(
        text, voice, pitch=PITCH, rate=RATE, volume=VOLUME, boundary="WordBoundary"
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


def _synthesize_azure(text: str, output_path: Path, voice: str) -> list[dict]:
    """Azure AI Speech via SDK oficial. Levanta RuntimeError em qualquer
    falha (chave, cota, rede) — quem chama cai pro edge-tts."""
    import azure.cognitiveservices.speech as speechsdk
    from xml.sax.saxutils import escape

    config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEYS[0], region=AZURE_SPEECH_REGION)
    config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Audio24Khz96KBitRateMonoMp3)
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=config, audio_config=speechsdk.audio.AudioOutputConfig(filename=str(output_path)),
    )

    word_boundaries: list[dict] = []

    def on_word(evt) -> None:
        # só palavra — pontuação também gera evento e bagunçaria o karaokê.
        # audio_offset vem em unidades de 100ns (igual ao edge-tts).
        if evt.boundary_type == speechsdk.SpeechSynthesisBoundaryType.Word:
            start = evt.audio_offset / 10_000_000
            word_boundaries.append({"text": evt.text, "start": start, "end": start + evt.duration.total_seconds()})

    synthesizer.synthesis_word_boundary.connect(on_word)
    lang = voice[:5]
    ssml = (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang}">'
        f'<voice name="{voice}"><prosody pitch="{PITCH}" rate="{RATE}" volume="{VOLUME}">'
        f"{escape(text)}</prosody></voice></speak>"
    )
    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        detail = getattr(result, "cancellation_details", None)
        raise RuntimeError(f"azure tts falhou: {result.reason} {getattr(detail, 'error_details', '')}")
    return word_boundaries


def narrate(
    text: str, output_path: Path, voice: str = DEFAULT_VOICE, provider: str = "edge",
) -> tuple[Path, list[dict]]:
    """Sintetiza `text` em áudio mp3 e devolve (caminho salvo, lista de
    palavras com tempo real `{"text", "start", "end"}` em segundos — vazia
    se o serviço não mandou WordBoundary por algum motivo; quem chamar deve
    cair pra um fallback nesse caso, nunca assumir que sempre vem preenchida)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "azure" and AZURE_SPEECH_KEYS:
        try:
            return output_path, _synthesize_azure(text, output_path, voice)
        except Exception as exc:  # noqa: BLE001 — qualquer falha da Azure cai pro grátis
            log.warning("azure tts indisponível (%s) — usando edge-tts", exc)
    elif provider == "azure":
        log.warning("tts_provider azure sem AZURE_SPEECH_KEYS no .env — usando edge-tts")
    if voice not in EDGE_VOICES:
        voice = EDGE_FALLBACK_VOICE
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
