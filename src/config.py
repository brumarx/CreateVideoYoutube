from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _keys(env_var: str) -> list[str]:
    """Lê uma lista de chaves separadas por vírgula ou quebra de linha.

    Suporta múltiplas chaves por provedor (rotação/failover), igual ao
    ariaBot — mais chaves = mais cota grátis somada.
    """
    raw = os.getenv(env_var, "")
    parts = [p.strip() for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


@dataclass
class LLMKeys:
    xai: list[str] = field(default_factory=lambda: _keys("XAI_API_KEYS"))
    groq: list[str] = field(default_factory=lambda: _keys("GROQ_API_KEYS"))
    cerebras: list[str] = field(default_factory=lambda: _keys("CEREBRAS_API_KEYS"))
    openrouter: list[str] = field(default_factory=lambda: _keys("OPENROUTER_API_KEYS"))
    mistral: list[str] = field(default_factory=lambda: _keys("MISTRAL_API_KEYS"))
    gemini: list[str] = field(default_factory=lambda: _keys("GEMINI_API_KEYS"))


# Únicas 3 vozes pt-BR grátis do edge-tts (checado ao vivo via
# edge_tts.list_voices()) — 1 masculina, 2 femininas.
FREE_TTS_VOICES = ("pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural")

POLLINATIONS_API_KEYS = _keys("POLLINATIONS_API_KEYS")
YOUTUBE_CLIENT_SECRET_FILE = ROOT / os.getenv(
    "YOUTUBE_CLIENT_SECRET_FILE", "credentials/client_secret.json"
)


@dataclass
class ChannelConfig:
    name: str
    niche: str
    language: str
    # sorteio ponderado entre as 3 vozes grátis (1 vídeo = 1 voz, sorteada
    # na hora) — antes era fixa por canal, sempre a mesma narradora todo
    # dia. {"pt-BR-AntonioNeural": 50, "pt-BR-FranciscaNeural": 30, ...}
    tts_voice_weights: dict[str, int]
    prompt_base: str
    scenes_per_video: int
    upload_privacy: str
    token_file: Path
    # Formato longo (documentário, 16:9) — lugares/fenômenos reais
    # específicos pra evitar tema genérico demais pra 15-20min.
    long_form_scenes: int
    # Fila única de temas — só sai 1 vídeo/dia por canal (curto OU longo,
    # conforme daily_format), então não faz sentido ter lista separada por
    # formato: era confuso e deixava metade da fila sempre parada.
    topics: list[str]
    # Duração alvo em minutos — usada pra calcular quantas palavras cada
    # cena precisa ter (contar cena não bastava: o LLM batia a contagem de
    # cenas mas escrevia frases curtas demais, saindo um vídeo bem mais
    # curto que o pedido).
    short_min_minutes: float
    short_max_minutes: float
    long_min_minutes: float
    long_max_minutes: float
    # marca d'água (@handle do canal) gravada em toda cena — dificulta
    # repostagem sem crédito e ajuda a provar autoria se alguém roubar.
    watermark: str
    # cor de destaque do canal (hex) — usada no número de contagem regressiva
    # dos vídeos de lista ("10 fatos sobre...", ver run_pipeline.py).
    accent: str
    # hashtags fixas do canal (sem "#Shorts" — isso é adicionado por código
    # só no formato curto, ver run_pipeline.py). 3-5 é o recomendado hoje em
    # dia: uma ampla, uma de nicho, uma de marca — mais que isso o YouTube
    # ignora todas. Geradas por código (não pelo LLM) pra nunca sair errado
    # ou faltando.
    hashtags: list[str]
    # @handle real do canal no YouTube (ex.: "@fractalcurioso", sem acento —
    # é diferente da watermark, que é só texto decorativo gravado na cena).
    # Usado pra montar o link de inscrição no fim da descrição. Vazio =
    # canal ainda não confirmou o handle (não gera link quebrado).
    youtube_handle: str
    # nome de exibição real do canal (ex.: "Fractal Curioso") — usado só
    # pra nomear a playlist automática (ver src/upload.py), não afeta nada
    # do vídeo em si.
    channel_title: str
    # legenda embutida estilo TikTok (2-3 palavras por vez) — maior fator
    # de retenção pra canal sem apresentador (maioria assiste mudo), mas
    # dá pra desligar por canal se algum estilo não combinar.
    captions: bool
    # "short" (vertical, Shorts) ou "long" (16:9, documentário) — fonte
    # única de verdade pro formato do dia, editável no painel. run_pipeline.py
    # só usa outra coisa se --long/--no-long for passado explicitamente na
    # linha de comando (teste manual pontual).
    daily_format: str

    @staticmethod
    def load(name: str) -> "ChannelConfig":
        path = ROOT / "channels" / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Config de canal não encontrada: {path}")
        data = yaml.safe_load(path.read_text())
        return ChannelConfig(
            name=name,
            niche=data["niche"],
            language=data.get("language", "pt-BR"),
            tts_voice_weights=data.get(
                "tts_voice_weights",
                {"pt-BR-AntonioNeural": 50, "pt-BR-FranciscaNeural": 30, "pt-BR-ThalitaMultilingualNeural": 20},
            ),
            prompt_base=data["prompt_base"],
            scenes_per_video=data.get("scenes_per_video", 8),
            upload_privacy=data.get("upload_privacy", "private"),
            token_file=ROOT / "credentials" / f"token_{name}.json",
            long_form_scenes=data.get("long_form_scenes", 30),
            topics=data.get("topics", []),
            short_min_minutes=data.get("short_min_minutes", 3),
            short_max_minutes=data.get("short_max_minutes", 6),
            long_min_minutes=data.get("long_min_minutes", 15),
            long_max_minutes=data.get("long_max_minutes", 20),
            watermark=data.get("watermark", f"@{name}"),
            accent=data.get("accent", "#ffffff"),
            hashtags=data.get("hashtags", []),
            youtube_handle=data.get("youtube_handle", ""),
            channel_title=data.get("channel_title", name),
            captions=data.get("captions", True),
            daily_format=data.get("daily_format", "short"),
        )
