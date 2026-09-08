"""Cascata de provedores LLM grátis/free-tier, mesmo padrão do ariaBot
(src/engine/provider.ts + src/engine/providers/*): tenta cada PROVEDOR na
ordem definida; dentro de cada provedor, roda em rodízio entre todas as
chaves configuradas (mais chaves = mais cota grátis somada), banindo chaves
com erro de autenticação/permissão. Os nomes de modelo grátis mudam com
frequência — em vez de fixar nomes, a lista de modelos de cada provedor é
buscada em tempo real via `GET /models` (mesma ideia do ariaBot) e cacheada
no processo.
"""
from __future__ import annotations

import logging

import httpx

from .config import LLMKeys

log = logging.getLogger("providers")

# Substrings que indicam modelo não-chat (voz, imagem, moderação, código
# especializado, etc.) — filtradas da lista de candidatos.
_NON_CHAT_HINTS = (
    "whisper", "tts", "voice", "guard", "safeguard", "moderation", "embed",
    "image", "vision", "orpheus", "vibe-cli", "-fim", "codestral", "voxtral",
    "content-safety", "omni", "safety", "compound",
)

# (nome, atributo em LLMKeys, endpoint de chat, endpoint de listagem de modelos)
OPENAI_COMPAT_PROVIDERS = [
    ("xai", "xai", "https://api.x.ai/v1/chat/completions", "https://api.x.ai/v1/models"),
    ("groq", "groq", "https://api.groq.com/openai/v1/chat/completions", "https://api.groq.com/openai/v1/models"),
    ("cerebras", "cerebras", "https://api.cerebras.ai/v1/chat/completions", "https://api.cerebras.ai/v1/models"),
    ("openrouter", "openrouter", "https://openrouter.ai/api/v1/chat/completions", "https://openrouter.ai/api/v1/models"),
    ("mistral", "mistral", "https://api.mistral.ai/v1/chat/completions", "https://api.mistral.ai/v1/models"),
]

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

_MAX_MODEL_CANDIDATES = 4
_model_cache: dict[str, list[str]] = {}


def _is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    return not any(hint in lower for hint in _NON_CHAT_HINTS)


def _fetch_models(provider: str, list_url: str, api_key: str) -> list[str]:
    cache_key = f"{provider}:{api_key[-6:]}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    models: list[str] = []
    try:
        resp = httpx.get(list_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            all_ids = [m["id"] for m in data]
            if provider == "openrouter":
                all_ids = [m for m in all_ids if m.endswith(":free")]
            models = [m for m in all_ids if _is_chat_model(m)][:_MAX_MODEL_CANDIDATES]
        else:
            log.warning("%s /models -> HTTP %s", provider, resp.status_code)
    except Exception as exc:
        log.warning("%s /models falhou: %s", provider, exc)

    _model_cache[cache_key] = models
    return models


def _fetch_gemini_models(api_key: str) -> list[str]:
    cache_key = f"gemini:{api_key[-6:]}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    models: list[str] = []
    try:
        resp = httpx.get(GEMINI_MODELS_URL, params={"key": api_key}, timeout=20)
        if resp.status_code == 200:
            data = resp.json().get("models", [])
            candidates = [
                m["name"].removeprefix("models/")
                for m in data
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            # prioriza modelos "flash" (rápidos/cota melhor), sem imagem/tts/preview
            preferred = [m for m in candidates if "flash" in m and _is_chat_model(m) and "preview" not in m]
            rest = [m for m in candidates if m not in preferred and _is_chat_model(m)]
            models = (preferred + rest)[:_MAX_MODEL_CANDIDATES]
        else:
            log.warning("gemini /models -> HTTP %s", resp.status_code)
    except Exception as exc:
        log.warning("gemini /models falhou: %s", exc)

    _model_cache[cache_key] = models
    return models


class KeyRotator:
    """Rodízio de chaves por provedor: cada chamada começa por uma chave
    diferente (round-robin) e bane chaves com erro de auth/permissão
    (401/403) pelo resto do processo — as demais continuam disponíveis.
    """

    def __init__(self, keys: list[str]):
        self._keys = keys
        self._banned: set[str] = set()
        self._next = 0

    def order(self) -> list[str]:
        active = [k for k in self._keys if k not in self._banned]
        if not active:
            return []
        start = self._next % len(active)
        self._next += 1
        return active[start:] + active[:start]

    def ban(self, key: str) -> None:
        self._banned.add(key)
        log.warning("chave banida por erro de auth/permissão (…%s)", key[-4:])


_rotators: dict[int, KeyRotator] = {}


def _rotator_for(keys: list[str]) -> KeyRotator:
    cache_id = id(keys)
    if cache_id not in _rotators:
        _rotators[cache_id] = KeyRotator(keys)
    return _rotators[cache_id]


def _call_openai_compat(
    endpoint: str, api_key: str, model: str, messages: list[dict], max_tokens: int
) -> tuple[str | None, int | None]:
    try:
        resp = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "temperature": 0.8, "max_tokens": max_tokens},
            timeout=60,
        )
        if resp.status_code != 200:
            log.warning("%s (%s) -> HTTP %s: %s", endpoint, model, resp.status_code, resp.text[:200])
            return None, resp.status_code
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return (content.strip() if content else None), 200
    except Exception as exc:  # rede, timeout, JSON malformado, etc.
        log.warning("%s (%s) falhou: %s", endpoint, model, exc)
        return None, None


def _call_gemini(api_key: str, model: str, messages: list[dict], max_tokens: int) -> str | None:
    prompt = "\n\n".join(m["content"] for m in messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    try:
        resp = httpx.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
            timeout=60,
        )
        if resp.status_code != 200:
            log.warning("gemini (%s) -> HTTP %s: %s", model, resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        log.warning("gemini (%s) falhou: %s", model, exc)
        return None


def complete(messages: list[dict], keys: LLMKeys | None = None, max_tokens: int = 4096) -> str:
    """Roda a cascata de provedores (com rodízio de chaves e descoberta
    dinâmica de modelo dentro de cada um) e devolve a primeira resposta não
    vazia.

    `max_tokens` importa MUITO pra roteiros longos: sem limite explícito
    cada provedor usa seu próprio default (baixo), e a resposta trunca no
    meio do JSON — foi exatamente o que quebrou o roteiro de formato longo
    (31 cenas) antes desse parâmetro existir.

    Levanta RuntimeError se nenhum provedor configurado conseguir responder.
    """
    if keys is None:
        keys = LLMKeys()

    for name, key_attr, chat_endpoint, models_endpoint in OPENAI_COMPAT_PROVIDERS:
        provider_keys = getattr(keys, key_attr)
        if not provider_keys:
            continue
        rotator = _rotator_for(provider_keys)
        for api_key in rotator.order():
            models = _fetch_models(name, models_endpoint, api_key)
            if not models:
                continue
            for model in models:
                result, status = _call_openai_compat(chat_endpoint, api_key, model, messages, max_tokens)
                if result:
                    log.info("resposta via %s/%s", name, model)
                    return result
                if status in (401, 403):
                    rotator.ban(api_key)
                    break  # próxima chave, não adianta repetir modelos com a mesma

    if keys.gemini:
        rotator = _rotator_for(keys.gemini)
        for api_key in rotator.order():
            models = _fetch_gemini_models(api_key)
            for model in models:
                result = _call_gemini(api_key, model, messages, max_tokens)
                if result:
                    log.info("resposta via gemini/%s", model)
                    return result

    raise RuntimeError(
        "Nenhum provedor LLM respondeu. Confira as chaves no .env "
        "(XAI_API_KEYS, GROQ_API_KEYS, CEREBRAS_API_KEYS, OPENROUTER_API_KEYS, "
        "MISTRAL_API_KEYS, GEMINI_API_KEYS)."
    )
