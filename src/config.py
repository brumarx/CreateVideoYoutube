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


POLLINATIONS_API_KEYS = _keys("POLLINATIONS_API_KEYS")
YOUTUBE_CLIENT_SECRET_FILE = ROOT / os.getenv(
    "YOUTUBE_CLIENT_SECRET_FILE", "credentials/client_secret.json"
)


@dataclass
class ChannelConfig:
    name: str
    niche: str
    language: str
    tts_voice: str
    prompt_base: str
    scenes_per_video: int
    upload_privacy: str
    token_file: Path
    # Formato longo (documentário, 16:9) — lugares/fenômenos reais
    # específicos pra evitar tema genérico demais pra 15-20min.
    long_form_scenes: int
    long_form_topics: list[str]
    # Duração alvo em minutos — usada pra calcular quantas palavras cada
    # cena precisa ter (contar cena não bastava: o LLM batia a contagem de
    # cenas mas escrevia frases curtas demais, saindo um vídeo bem mais
    # curto que o pedido).
    short_min_minutes: float
    short_max_minutes: float
    long_min_minutes: float
    long_max_minutes: float

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
            tts_voice=data.get("tts_voice", "pt-BR-FranciscaNeural"),
            prompt_base=data["prompt_base"],
            scenes_per_video=data.get("scenes_per_video", 8),
            upload_privacy=data.get("upload_privacy", "private"),
            token_file=ROOT / "credentials" / f"token_{name}.json",
            long_form_scenes=data.get("long_form_scenes", 30),
            long_form_topics=data.get("long_form_topics", []),
            short_min_minutes=data.get("short_min_minutes", 3),
            short_max_minutes=data.get("short_max_minutes", 6),
            long_min_minutes=data.get("long_min_minutes", 15),
            long_max_minutes=data.get("long_max_minutes", 20),
        )
