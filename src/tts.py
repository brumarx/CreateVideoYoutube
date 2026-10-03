"""Narração via edge-tts — baseado em /var/www/html/reacao/bot/tts_edge.py
do ariaBot (mesma lib, mesma prosódia padrão) — ou via Azure AI Speech
(API oficial da Microsoft) pras vozes pt-BR que o edge-tts não tem. O
motor é decidido pela própria voz sorteada: as 3 do edge seguem grátis pelo
edge-tts; qualquer outra vai pela Azure. Azure sem chave, sem cota ou fora
do ar cai sozinho pro edge-tts: o vídeo nunca deixa de sair por causa da voz."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import edge_tts
import httpx

from .config import AZURE_SPEECH_KEYS, AZURE_SPEECH_REGION

log = logging.getLogger("tts")

# mesma prosódia nos dois motores (ver comentário em _synthesize)
PITCH, RATE, VOLUME = "+8Hz", "-8%", "+15%"
# vozes que o edge-tts grátis tem — voz só-Azure usa este fallback
EDGE_VOICES = {"pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural"}
EDGE_FALLBACK_VOICE = "pt-BR-AntonioNeural"

AZURE_VOICES_CACHE = Path(__file__).resolve().parent.parent / "data" / "azure_voices.json"

# Pronúncia: nome que a voz pt-BR lê errado -> grafia fonética que ela lê
# certo. Troca só no texto que vai pra síntese; a legenda karaokê volta pra
# grafia original (ver _restore_spelling). "x" vira "cs" de propósito — em
# português o "x" pode sair "ch" ("Téchtor").
PRONUNCIATIONS = {
    "Textor": "Técstor",  # John Textor (SAF do Botafogo): TÉX-tor, não tex-TÔR
    "FogãoNET": "Fogãonéti",  # site FogãoNET: a voz soletrava "Fogão N-E-T"
    "Botafogo.WIN": "botafogo.win",  # maiúsculo a voz soletra "dáblio-i-ene"
    "SAF": "Sáfi",  # "a SAF" se fala como palavra, não "ésse-á-éfe"
    "UOL": "Uól",
    "ge": "GE",  # site ge (Globo Esporte): minúsculo a voz lê "jê"; sigla ela soletra "gê-é"
}
_PRON_RE = re.compile(r"\b(" + "|".join(map(re.escape, PRONUNCIATIONS)) + r")\b", re.IGNORECASE)
_SPELLING_BACK = {v.lower(): k for k, v in PRONUNCIATIONS.items()}

# PALAVRA INTEIRA EM MAIÚSCULAS (5+ letras) a voz costuma soletrar: nome de
# veículo ("O GLOBO", "LANCE!", "NETVASCO") ou ênfase do roteiro ("QUE SE
# VIREM"). Vira Title Case só na síntese. Sigla de 2-4 letras (CBF, STF,
# ESPN) fica como está — soletrar é o certo pra elas; as de 5+ que também
# se soletram entram na exceção.
_CAPS_WORD = re.compile(r"\b[A-ZÀ-Ý]{2,}\b")
_SPELLED_ACRONYMS = {"BNDES", "HTTPS", "CNBB", "OCDE"}
# palavra comum curta em frase de ênfase ("QUE SE VIREM", "ISSO É REAL")
_SHORT_WORDS = {
    "QUE", "SE", "NÃO", "VAI", "ELE", "ELA", "ISSO", "ESSE", "ESSA", "ESTÁ", "COMO", "MAIS", "TUDO",
    "NADA", "FOI", "SÓ", "PRA", "UM", "UMA", "DE", "DO", "DA", "NO", "NA", "EM", "OS", "AS", "SIM",
    "JÁ", "AGORA", "BOM", "MAU", "FIM", "VEM", "SEM", "COM", "POR", "QUEM", "ONDE", "REAL", "VIU",
}


def _for_speech(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Texto pra síntese + a lista, em ordem, de (palavra falada, original)
    das palavras em maiúsculas trocadas — a legenda karaokê devolve a
    grafia original na mesma ordem, sem tocar num "lance" comum só porque
    o roteiro também citou o jornal "LANCE!"."""
    trocas: list[tuple[str, str]] = []

    def caps(m: re.Match) -> str:
        word = m[0]
        if word in _SPELLED_ACRONYMS or (len(word) < 5 and word not in _SHORT_WORDS):
            return word  # sigla (CBF, STF, ESPN): soletrar é o certo
        trocas.append((word.capitalize().lower(), word))
        return word.capitalize()

    text = _PRON_RE.sub(lambda m: PRONUNCIATIONS[next(k for k in PRONUNCIATIONS if k.lower() == m[1].lower())], text)
    text = _versus(text)
    return _CAPS_WORD.sub(caps, text), trocas


# "Botafogo x Vasco" a voz lia "Botafogo xis Vasco"; placar "2 x 1" vira
# "2 a 1" (como o narrador fala), confronto vira "contra".
_PLACAR_X = re.compile(r"\b(\d+)\s*[xX×]\s*(\d+)\b")
# só entre nomes próprios ("Botafogo x Vasco"), não "eixo x do gráfico"
_CONFRONTO_X = re.compile(r"\b([A-ZÀ-Ý][\w-]*)\s+[xX×]\s+(?=[A-ZÀ-Ý])")


def _versus(text: str) -> str:
    return _CONFRONTO_X.sub(r"\1 contra ", _PLACAR_X.sub(r"\1 a \2", text))


def _join_domains(word_boundaries: list[dict]) -> list[dict]:
    """A voz lê "botafogo.win" como 3 palavras (botafogo / . / win) — a
    legenda mostrava o ponto solto; junta de volta numa palavra só."""
    out: list[dict] = []
    i = 0
    while i < len(word_boundaries):
        w = word_boundaries[i]
        if i + 2 < len(word_boundaries) and word_boundaries[i + 1]["text"] == "." and word_boundaries[i + 2]["text"].lower() in {"win", "com", "br", "org"}:
            nxt = word_boundaries[i + 2]
            out.append({**w, "text": f"{w['text']}.{nxt['text']}", "end": nxt.get("end", w.get("end"))})
            i += 3
            continue
        out.append(w)
        i += 1
    return out


def _restore_spelling(word_boundaries: list[dict], trocas: list[tuple[str, str]] | None = None) -> list[dict]:
    word_boundaries = _join_domains(word_boundaries)
    pendentes = list(trocas or [])
    for w in word_boundaries:
        core = w["text"].strip(".,;:!?")
        original = _SPELLING_BACK.get(core.lower())
        if not original and pendentes and core.lower() == pendentes[0][0]:
            original = pendentes.pop(0)[1]
        if original:
            w["text"] = w["text"].replace(core, original)
    return word_boundaries


def azure_voices(locale: str = "pt-BR") -> list[dict]:
    """Vozes da Azure pro idioma (sem as 3 que o edge já tem), cada uma
    {"id", "nome", "genero"}. Lista vem da API e fica em cache no disco
    (muda raramente); sem chave devolve [] — painel só mostra as do edge."""
    if not AZURE_SPEECH_KEYS:
        return []
    voices = None
    if AZURE_VOICES_CACHE.exists() and time.time() - AZURE_VOICES_CACHE.stat().st_mtime < 7 * 86400:
        voices = json.loads(AZURE_VOICES_CACHE.read_text())
    if voices is None:
        try:
            resp = httpx.get(
                f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEYS[0]}, timeout=20,
            )
            resp.raise_for_status()
            voices = resp.json()
            AZURE_VOICES_CACHE.write_text(json.dumps(voices, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            log.warning("não consegui listar vozes da Azure: %s", exc)
            return []
    return [
        {"id": v["ShortName"], "nome": v.get("LocalName") or v["ShortName"], "genero": v.get("Gender", "")}
        for v in voices
        if v.get("Locale") == locale and v["ShortName"] not in EDGE_VOICES
    ]

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


def narrate(text: str, output_path: Path, voice: str = DEFAULT_VOICE) -> tuple[Path, list[dict]]:
    """Sintetiza `text` em áudio mp3 e devolve (caminho salvo, lista de
    palavras com tempo real `{"text", "start", "end"}` em segundos — vazia
    se o serviço não mandou WordBoundary por algum motivo; quem chamar deve
    cair pra um fallback nesse caso, nunca assumir que sempre vem preenchida)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text, spelling_back = _for_speech(text)
    if voice not in EDGE_VOICES:
        # voz só-Azure
        if AZURE_SPEECH_KEYS:
            try:
                return output_path, _restore_spelling(_synthesize_azure(text, output_path, voice), spelling_back)
            except Exception as exc:  # noqa: BLE001 — qualquer falha da Azure cai pro grátis
                log.warning("azure tts indisponível (%s) — usando edge-tts", exc)
        else:
            log.warning("voz %s é da Azure mas não há AZURE_SPEECH_KEYS no .env — usando edge-tts", voice)
        voice = EDGE_FALLBACK_VOICE
    metadata_path = output_path.with_suffix(".wordtimes.json")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            word_boundaries = asyncio.run(_synthesize(text, output_path, voice, metadata_path))
            return output_path, _restore_spelling(word_boundaries, spelling_back)
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == MAX_ATTEMPTS:
                raise
            log.warning(
                "edge-tts não devolveu áudio (tentativa %d/%d) — tentando de novo",
                attempt, MAX_ATTEMPTS,
            )
            time.sleep(RETRY_DELAY_S)
    return output_path, []
