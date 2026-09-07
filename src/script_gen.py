"""Geração de roteiro: título, descrição, tags e cenas (texto de narração +
prompt de imagem por cena), via cascata de LLMs (providers.complete)."""
from __future__ import annotations

import json
import logging
import re

from .config import ChannelConfig
from .providers import complete

log = logging.getLogger("script_gen")

SYSTEM_PROMPT = (
    "Você é um roteirista de vídeos do YouTube no estilo 'faceless' "
    "(sem apresentador, só narração + imagens). Responda SEMPRE em JSON "
    "válido, sem markdown, sem texto fora do JSON. "
    "REGRA CRÍTICA para todo 'image_prompt': a IA de imagem SEMPRE erra "
    "letras/palavras e não deve imitar logos de marcas (YouTube, etc.) — "
    "por isso NUNCA peça texto, palavras, letras, números escritos, botões, "
    "ícones de interface, logos ou telas de 'inscreva-se'/'curta' na "
    "imagem. Descreva só cenário, objetos, pessoas e atmosfera visual."
)


def _prompt_for(channel: ChannelConfig, topic: str, facts: dict | None = None) -> str:
    facts_block = ""
    if facts:
        facts_block = f"""
DADOS REAIS (use SOMENTE estes números e nomes — não invente, não
extrapole, não cite nenhum nome/valor que não esteja aqui; atribua sempre à
fonte, ex.: "segundo declaração ao TSE" / "segundo dados abertos da
Câmara"):
{json.dumps(facts, ensure_ascii=False, indent=2)}
"""

    return f"""{channel.prompt_base}

Tópico do vídeo: {topic}
Idioma: {channel.language}
Número de cenas: {channel.scenes_per_video}
{facts_block}
Gere um JSON com exatamente este formato:
{{
  "title": "título chamativo, até 100 caracteres",
  "description": "descrição para o YouTube, 2-3 parágrafos, com contexto e call-to-action",
  "tags": ["tag1", "tag2", "..."],
  "scenes": [
    {{"narration": "texto que o narrador vai falar nesta cena", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera — SEM texto, palavras, logos, botões ou UI"}}
  ]
}}

O array "scenes" deve ter exatamente {channel.scenes_per_video} itens. A soma das
narrações deve formar um vídeo coeso de 3 a 6 minutos quando narrado."""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # Alguns modelos ainda envolvem em ```json ... ``` mesmo pedindo pra não fazer isso.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta do LLM não contém JSON: {raw[:300]}")
    return json.loads(match.group(0))


def generate_script(channel: ChannelConfig, topic: str, facts: dict | None = None) -> dict:
    """Gera o roteiro completo do vídeo. Se `facts` for passado (ex.: saída
    de politica_data.random_fact_set()), o roteiro é obrigado a usar só
    esses dados reais em vez de a IA inventar números/nomes.

    Levanta ValueError se o LLM não devolver um JSON parseável (raro, mas
    acontece)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _prompt_for(channel, topic, facts)},
    ]

    required = {"title", "description", "tags", "scenes"}
    last_error: Exception | None = None
    for attempt in range(3):
        raw = complete(messages)
        try:
            script = _extract_json(raw)
            missing = required - script.keys()
            if missing:
                raise ValueError(f"JSON do roteiro sem campos {missing}: {script}")
            return script
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            log.warning("tentativa %d/3: roteiro malformado (%s), tentando de novo", attempt + 1, exc)

    raise ValueError(f"LLM não devolveu roteiro válido após 3 tentativas: {last_error}")
