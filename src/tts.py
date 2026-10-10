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
from typing import Callable

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
    "ge": "Gê-É",  # site ge (Globo Esporte): minúsculo a voz lê "jê", "GE" também sai errado
    # verbo recuar: a voz lia "récua" (o substantivo) — dry-run de 06/10
    "recua": "recúa",
    "recuam": "recúam",
}
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


def _is_mixed_case(word: str) -> bool:
    return any(c.isupper() for c in word[1:]) and not word.isupper()


def _apply_pronunciations(text: str, extra: dict[str, str] | None) -> str:
    """Fixas (PRONUNCIATIONS) + as do roteiro: nome estrangeiro que o LLM
    soletrou à portuguesa com a tônica marcada ("Pavlopetri" ->
    "Pavlopétri")."""
    table = {**(extra or {}), **PRONUNCIATIONS}
    # maiúscula no meio da palavra ("vIA", marca do canal) é grafia própria:
    # troca só escrita exatamente assim — sem diferenciar, todo "via" comum
    # da narração viraria "via I-A"
    exact = {k: v for k, v in table.items() if _is_mixed_case(k)}
    if exact:
        text = re.sub(
            r"\b(" + "|".join(map(re.escape, sorted(exact, key=len, reverse=True))) + r")\b",
            lambda m: exact[m[1]], text,
        )
        table = {k: v for k, v in table.items() if k not in exact}
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(table, key=len, reverse=True))) + r")\b", re.IGNORECASE)
    lower = {k.lower(): v for k, v in table.items()}

    def swap(m: re.Match) -> str:
        new = lower[m[1].lower()]
        if m[1][:1].isupper() and new[:1].islower():
            new = new[:1].upper() + new[1:]  # "Recua" no começo da frase
        return new

    return pattern.sub(swap, text)


def _for_speech(text: str, extra: dict[str, str] | None = None) -> tuple[str, list[tuple[str, str]]]:
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

    from .spellcheck import clean_markup

    text = clean_markup(text)  # última barreira: nada de "asterisco" falado
    text = _apply_pronunciations(text, extra)
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


_TRAILING_PUNCT = re.compile(r"[.,;:!?…]+$")


def _restore_punctuation(word_boundaries: list[dict], text: str) -> list[dict]:
    """WordBoundary vem sem pontuação ("E aí curiosos existe") e a legenda
    karaokê sai igual — pergunta sem "?" (Short ZEEU6fhq0KY). Devolve a
    pontuação final de cada palavra casando, em ordem, com o texto falado."""
    tokens = text.split()
    cores = [t.strip("\"'“”‘’()[]«».,;:!?…").lower() for t in tokens]
    pos = 0
    for w in word_boundaries:
        word = " ".join(w["text"].split()).lower()
        if not word:
            continue
        n = len(word.split())  # "5.000 anos" chega como 1 palavra só
        for j in range(pos, min(pos + 3, len(tokens) - n + 1)):  # palavra pode faltar/sobrar no meio
            if " ".join(cores[j:j + n]) == word:
                last = j + n - 1
                m = _TRAILING_PUNCT.search(tokens[last].rstrip("\"'“”’)]»"))
                if m and not _TRAILING_PUNCT.search(w["text"]):
                    w["text"] += m.group(0)
                pos = last + 1
                break
    return word_boundaries


def _join_multiword(word_boundaries: list[dict], extra: dict[str, str] | None) -> list[dict]:
    """Pronúncia de várias palavras ("vIA" -> "via I-A") volta pra 1 palavra
    só na legenda, com o tempo do trecho inteiro."""
    multi = {
        tuple(re.findall(r"\w+", v.lower())): k for k, v in (extra or {}).items() if len(re.findall(r"\w+", v)) > 1
    }
    if not multi:
        return word_boundaries
    out: list[dict] = []
    i = 0
    while i < len(word_boundaries):
        for spoken, original in multi.items():
            # a voz pode devolver "I-A" como 1 palavra ou 2: junta palavras
            # até completar as do trecho falado
            tokens: list[str] = []
            j = i
            while j < len(word_boundaries) and len(tokens) < len(spoken):
                tokens += re.findall(r"\w+", word_boundaries[j]["text"].lower())
                j += 1
            if tuple(tokens) == spoken:
                window = word_boundaries[i:j]
                tail = re.search(r"[.,;:!?]*$", window[-1]["text"])[0]
                out.append({**window[0], "text": original + tail, "end": window[-1].get("end", window[0].get("end"))})
                i = j
                break
        else:
            out.append(word_boundaries[i])
            i += 1
    return out


def _restore_spelling(
    word_boundaries: list[dict], trocas: list[tuple[str, str]] | None = None, extra: dict[str, str] | None = None,
) -> list[dict]:
    word_boundaries = _join_multiword(_join_domains(word_boundaries), extra)
    pendentes = list(trocas or [])
    back = {**{v.lower(): k for k, v in (extra or {}).items()}, **_SPELLING_BACK}
    for w in word_boundaries:
        core = w["text"].strip(".,;:!?")
        original = back.get(core.lower())
        if original and core[:1].isupper() and original[:1].islower():
            original = original[:1].upper() + original[1:]  # "Recúa," no começo da frase
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


def narrate(
    text: str, output_path: Path, voice: str = DEFAULT_VOICE, pronunciations: dict[str, str] | None = None,
) -> tuple[Path, list[dict]]:
    """Sintetiza `text` em áudio mp3 e devolve (caminho salvo, lista de
    palavras com tempo real `{"text", "start", "end"}` em segundos — vazia
    se o serviço não mandou WordBoundary por algum motivo; quem chamar deve
    cair pra um fallback nesse caso, nunca assumir que sempre vem preenchida)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text, spelling_back = _for_speech(text, pronunciations)
    if voice not in EDGE_VOICES:
        # voz só-Azure
        if AZURE_SPEECH_KEYS:
            try:
                words = _restore_punctuation(_synthesize_azure(text, output_path, voice), text)
                return output_path, _restore_spelling(words, spelling_back, pronunciations)
            except Exception as exc:  # noqa: BLE001 — qualquer falha da Azure cai pro grátis
                log.warning("azure tts indisponível (%s) — usando edge-tts", exc)
        else:
            log.warning("voz %s é da Azure mas não há AZURE_SPEECH_KEYS no .env — usando edge-tts", voice)
        voice = EDGE_FALLBACK_VOICE
    metadata_path = output_path.with_suffix(".wordtimes.json")
    global _EDGE_BLOCKED
    if not _EDGE_BLOCKED:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                word_boundaries = asyncio.run(_synthesize(text, output_path, voice, metadata_path))
                return output_path, _restore_spelling(_restore_punctuation(word_boundaries, text), spelling_back, pronunciations)
            except edge_tts.exceptions.NoAudioReceived:
                if attempt == MAX_ATTEMPTS:
                    break
                log.warning(
                    "edge-tts não devolveu áudio (tentativa %d/%d) — tentando de novo",
                    attempt, MAX_ATTEMPTS,
                )
                time.sleep(RETRY_DELAY_S)
            except Exception as exc:  # noqa: BLE001
                # 403 no handshake = Microsoft mudou o protocolo (10/10/2026,
                # rany2/edge-tts#490) — não adianta insistir nas outras cenas
                if "403" in str(exc):
                    _EDGE_BLOCKED = True
                    log.warning("edge-tts bloqueado (403) — usando motor alternativo pro resto do job")
                else:
                    log.warning("edge-tts falhou (%s) — usando motor alternativo", exc)
                break
    words = _synthesize_fallback(text, output_path, voice)
    return output_path, _restore_spelling(_restore_punctuation(words, text), spelling_back, pronunciations)


# edge-tts fora do ar: Google Cloud TTS e depois ElevenLabs (cota grátis
# pequena, rodízio de chaves). Google: vozes WaveNet (4 mi caracteres/mês
# grátis; Neural2 só 1 mi). O projeto tem faturamento ligado, então o TETO
# abaixo é a garantia de custo ZERO (exigência do dono: nem 1 centavo):
# conta o SSML inteiro em bytes (com as <mark>, bem mais que o cobrado) por
# mês UTC e para de usar o Google bem antes dos 4 mi.
_EDGE_BLOCKED = False
GOOGLE_VOICES = {"male": "pt-BR-Wavenet-B", "female": "pt-BR-Wavenet-A"}
GOOGLE_MONTHLY_CAP_BYTES = 2_500_000
GOOGLE_USAGE_FILE = Path(__file__).resolve().parent.parent / "data" / "google_tts_usage.json"
# só vozes "premade": voz da biblioteca (as brasileiras) exige plano pago
ELEVEN_VOICES = {"male": "TX3LPaxmHKxFdv7VOQHJ", "female": "EXAVITQu4vr4xnSDxMaL"}  # Liam / Sarah
ELEVEN_MODEL = "eleven_turbo_v2_5"  # meio crédito por caractere, fala pt-BR
_dead_eleven_keys: set[str] = set()
_google_disabled = False


def _gender(voice: str) -> str:
    return "male" if "Antonio" in voice else "female"


_job_engine: "Callable[[str, Path, str], list[dict]] | None" = None  # motor que narrou a 1ª cena: o resto do job fica nele


def _synthesize_fallback(text: str, output_path: Path, voice: str) -> list[dict]:
    """Job 398: cenas 0-4 no Google e 5-26 na ElevenLabs — a voz trocava no
    meio do vídeo e as taxas diferentes (24 kHz x 44,1 kHz) quebravam a
    junção das cenas (áudio de 561 s num vídeo de 305 s). Agora o job fica
    no motor da 1ª cena e todo áudio sai em 24 kHz mono, igual ao edge-tts."""
    global _job_engine
    engines = [_synthesize_google, _synthesize_eleven]
    if _job_engine is not None and _job_engine in engines:
        engines.remove(_job_engine)
        engines.insert(0, _job_engine)
    errors = []
    for engine in engines:
        try:
            words = engine(text, output_path, _gender(voice))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s falhou: %s", engine.__name__, exc)
            errors.append(f"{engine.__name__}: {exc}")
            continue
        if _job_engine and engine is not _job_engine:
            log.warning("voz trocou de motor no meio do job (%s -> %s)", _job_engine.__name__, engine.__name__)
        _job_engine = engine
        _normalize_audio(output_path)
        return words
    raise RuntimeError("nenhum motor de voz disponível — " + " | ".join(errors))


def _normalize_audio(path: Path) -> None:
    import subprocess

    tmp = path.with_suffix(".norm.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ar", "24000", "-ac", "1", "-b:a", "96k", str(tmp)],
        check=True,
    )
    tmp.replace(path)


def _reserve_google_quota(n_bytes: int) -> None:
    """Soma ANTES de chamar (falha também conta); estourou o teto, recusa."""
    from datetime import datetime, timezone

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        usage = json.loads(GOOGLE_USAGE_FILE.read_text())
    except (OSError, ValueError):
        usage = {}
    used = usage.get(month, 0)
    if used + n_bytes > GOOGLE_MONTHLY_CAP_BYTES:
        raise RuntimeError(f"teto mensal grátis do google tts atingido ({used}/{GOOGLE_MONTHLY_CAP_BYTES} bytes)")
    usage[month] = used + n_bytes
    GOOGLE_USAGE_FILE.write_text(json.dumps(usage))


def _synthesize_google(text: str, output_path: Path, gender: str) -> list[dict]:
    """Google Cloud TTS v1beta1 com <mark> antes de cada palavra: o
    timepoint de cada marca é o início real da palavra (karaokê)."""
    global _google_disabled
    from xml.sax.saxutils import escape

    from .config import GCP_TTS_KEY_FILE

    if _google_disabled or not GCP_TTS_KEY_FILE:
        raise RuntimeError("google tts sem credencial/desativado")
    import base64

    import google.auth.transport.requests
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        GCP_TTS_KEY_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    tokens = text.split()
    ssml = "<speak>" + " ".join(f'<mark name="{i}"/>{escape(t)}' for i, t in enumerate(tokens)) + "</speak>"
    _reserve_google_quota(len(ssml.encode()))
    resp = httpx.post(
        "https://texttospeech.googleapis.com/v1beta1/text:synthesize",
        headers={"Authorization": f"Bearer {creds.token}", "x-goog-user-project": str(creds.project_id)},
        json={
            "input": {"ssml": ssml},
            "voice": {"languageCode": "pt-BR", "name": GOOGLE_VOICES[gender]},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": 1.0, "pitch": 1.0},
            "enableTimePointing": ["SSML_MARK"],
        },
        timeout=120,
    )
    if resp.status_code == 403:
        _google_disabled = True  # API não ativada no projeto: não tenta de novo neste job
    resp.raise_for_status()
    data = resp.json()
    output_path.write_bytes(base64.b64decode(data["audioContent"]))
    starts = [tp["timeSeconds"] for tp in data.get("timepoints", [])]
    words = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else start + 0.4
        words.append({"text": tokens[int(data["timepoints"][i]["markName"])].strip(".,;:!?…\"“”"), "start": start, "end": end})
    return words


def _synthesize_eleven(text: str, output_path: Path, gender: str) -> list[dict]:
    """ElevenLabs /with-timestamps: alinhamento por caractere -> palavras."""
    import base64

    from .config import ELEVENLABS_API_KEYS

    last = "sem ELEVENLABS_API_KEYS"
    for key in [k for k in ELEVENLABS_API_KEYS if k not in _dead_eleven_keys]:
        resp = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICES[gender]}/with-timestamps",
            headers={"xi-api-key": key},
            json={"text": text, "model_id": ELEVEN_MODEL, "language_code": "pt"},
            timeout=180,
        )
        if resp.status_code != 200:
            last = f"{resp.status_code} {resp.text[:200]}"
            if resp.status_code in (401, 402, 429) or "quota" in resp.text:
                _dead_eleven_keys.add(key)  # cota da chave acabou: próxima
            log.warning("elevenlabs falhou com uma chave (%s)", last)
            continue
        data = resp.json()
        output_path.write_bytes(base64.b64decode(data["audio_base64"]))
        al = data.get("alignment") or {}
        words: list[dict] = []
        cur, start, end = "", 0.0, 0.0
        for ch, s, e in zip(al.get("characters", []), al.get("character_start_times_seconds", []), al.get("character_end_times_seconds", [])):
            if ch.isspace():
                if cur:
                    words.append({"text": cur, "start": start, "end": end})
                cur = ""
                continue
            if not cur:
                start = s
            cur += ch
            end = e
        if cur:
            words.append({"text": cur, "start": start, "end": end})
        for w in words:
            w["text"] = w["text"].strip(".,;:!?…\"“”")
        return [w for w in words if w["text"]]
    raise RuntimeError(f"elevenlabs indisponível: {last}")
