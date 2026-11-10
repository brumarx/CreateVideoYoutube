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

import json
import logging
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from .config import LLMKeys

log = logging.getLogger("providers")

# Alguns modelos grátis são "reasoning models" e devolvem um bloco
# <think>...</think> com o raciocínio antes da resposta de verdade, mesmo
# quando o prompt não pede isso — se isso vazar sem tratamento pro chamador
# (script_gen, topics), vira roteiro/tema quebrado (já aconteceu: tema virou
# literalmente "<think>" e roteiro saiu com o raciocínio bruto em vez de
# JSON). Também acontece de a resposta cortar (max_tokens insuficiente) no
# MEIO do bloco de raciocínio — nesse caso não sobra nenhuma resposta real,
# então trata como se o modelo não tivesse respondido nada.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(content: str) -> str | None:
    if "<think>" in content.lower() and "</think>" not in content.lower():
        return None
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    return cleaned or None

# Substrings que indicam modelo não-chat (voz, imagem, moderação, código
# especializado, etc.) — filtradas da lista de candidatos.
_NON_CHAT_HINTS = (
    "whisper", "tts", "voice", "guard", "safeguard", "moderation", "embed",
    "image", "vision", "orpheus", "vibe-cli", "-fim", "codestral", "voxtral",
    "content-safety", "omni", "safety", "compound",
    # modelo de código escreve roteiro ruim: o mistral-code-latest era o 1º
    # da lista do Mistral e virou o escritor de reserva — copiava a fonte e
    # inventava fato, e a revisão reprovou o dia inteiro (07/10)
    "code", "leanstral",
)

# Modelos que vão PRIMEIRO quando o provedor tem (a ordem do /models é
# alfabética/aleatória, e os 4 primeiros nem sempre são os bons pra texto)
_PREFERRED_MODELS = {
    "xai": ["grok-3-mini", "grok-3"],  # /models dá 403 nesta conta: lista fixa pra não sumir do rodízio
    "mistral": ["mistral-medium-latest", "mistral-small-latest", "magistral-medium-latest"],
}

_GEMINI_TEXT_IN_CHAIN = False  # ver complete(): Gemini só na visão

# Modelo mais inteligente de cada provedor: tentado em TODAS as chaves do
# rodízio antes de qualquer outro modelo (ver complete)
_STRONG_MODELS = {
    "nvidia": ["nvidia/nemotron-3-ultra-550b-a55b"],
    "groq": ["openai/gpt-oss-120b"],
    "cerebras": ["gpt-oss-120b"],
    # 09/11: a lista grátis do OpenRouter começava por modelos pequenos e o Nemotron 550B
    # (contexto de 1 milhão) nunca era tentado; agora é o primeiro
    "openrouter": ["nvidia/nemotron-3-ultra-550b-a55b:free"],
}

# (nome, atributo em LLMKeys, endpoint de chat, endpoint de listagem de modelos)
# xAI (403 nas contas) e Cerebras (402, virou pago) saíram em 09/11: só geravam atraso e silêncio
OPENAI_COMPAT_PROVIDERS = [
    # 1º: o maior modelo grátis que temos (Nemotron 550B). A cota diária do
    # gpt-oss-120b no Groq acabava à tarde (04/11) e sobrava modelo fraco
    # pra escrever e revisar o roteiro
    ("nvidia", "nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", "https://integrate.api.nvidia.com/v1/models"),
    ("groq", "groq", "https://api.groq.com/openai/v1/chat/completions", "https://api.groq.com/openai/v1/models"),
    ("openrouter", "openrouter", "https://openrouter.ai/api/v1/chat/completions", "https://openrouter.ai/api/v1/models"),
    ("mistral", "mistral", "https://api.mistral.ai/v1/chat/completions", "https://api.mistral.ai/v1/models"),
]

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Cloudflare Workers AI (grátis, 10 mil neurons/dia por conta): endpoint
# compatível com OpenAI, mas um por conta e sem /models útil — lista fixa,
# do mais forte pro mais leve
CLOUDFLARE_CHAT_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1/chat/completions"
CLOUDFLARE_MODELS = [
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/qwen/qwen2.5-coder-32b-instruct",
]


def _cloudflare_complete(entries, messages, max_tokens, ok, model_filter, validate, temperature=0.8):
    rotator = _rotator_for(entries)
    for entry in rotator.order():
        account, _, token = entry.partition(":")
        if not token:
            log.warning("CLOUDFLARE_AI_KEYS precisa ser ACCOUNT_ID:API_TOKEN")
            rotator.ban(entry)
            continue
        for model in CLOUDFLARE_MODELS:
            if model_filter is not None and not model_filter(model):
                continue
            result, status, cooldown = _call_openai_compat(
                CLOUDFLARE_CHAT_URL.format(account=account), token, model, messages, max_tokens, temperature,
            )
            if ok(result):
                log.info("resposta via cloudflare/%s", model)
                return result
            if result and validate is not None:
                log.warning("resposta de cloudflare/%s não passou na validação, tentando próximo modelo", model)
            if status in (401, 403):
                rotator.ban(entry)
                break
            if cooldown:
                rotator.rest(entry, cooldown)
                break
    return None

_MAX_MODEL_CANDIDATES = 4
_model_cache: dict[str, list[str]] = {}


def _is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    return not any(hint in lower for hint in _NON_CHAT_HINTS)


def _fetch_models(provider: str, list_url: str, api_key: str) -> list[str]:
    if provider == "nvidia":
        # o /models lista dezenas que dão 404 "Function not found" nesta
        # conta (testado 02/11 e 04/11): só o Nemotron 550B responde
        return list(_STRONG_MODELS["nvidia"])
    cache_key = f"{provider}:{api_key[-6:]}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    # Falha do /models NÃO pode tirar a chave do rodízio: antes, o resultado
    # vazio ficava no cache do processo e a chave (ex.: xAI com 403 em /models)
    # nunca mais era tentada. Agora cai nos modelos conhecidos do provedor; o
    # erro de verdade (chave inválida, cota) aparece no chat e é tratado lá.
    known = list(dict.fromkeys(_STRONG_MODELS.get(provider, []) + _PREFERRED_MODELS.get(provider, [])))[:_MAX_MODEL_CANDIDATES]
    try:
        resp = httpx.get(list_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            all_ids = [m["id"] for m in data]
            if provider == "openrouter":
                all_ids = [m for m in all_ids if m.endswith(":free")]
            chat = [m for m in all_ids if _is_chat_model(m)]
            preferred = [m for m in _STRONG_MODELS.get(provider, []) + _PREFERRED_MODELS.get(provider, []) if m in chat]
            models = (preferred + [m for m in chat if m not in preferred])[:_MAX_MODEL_CANDIDATES] or known
            _model_cache[cache_key] = models  # só sucesso entra no cache
            return models
        log.warning("%s /models -> HTTP %s (usando modelos conhecidos)", provider, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s /models falhou: %s (usando modelos conhecidos)", provider, exc)
    return known


def _fetch_gemini_models(api_key: str) -> list[str]:
    cache_key = f"gemini:{api_key[-6:]}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    models: list[str] = []
    try:
        resp = httpx.get(GEMINI_MODELS_URL, headers={"x-goog-api-key": api_key}, timeout=20)
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


HEALTH_FILE = Path(__file__).resolve().parent.parent / "data" / "provider_health.json"


def _key_id(key: str) -> str:
    """Identificador da chave pro arquivo de saúde: hash, nunca a chave em si."""
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _health_load() -> dict:
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _health_update(key: str, **fields) -> None:
    """Grava o estado de uma chave: arquivo temporário + troca atômica, pra
    nenhum processo ler JSON pela metade."""
    import fcntl
    import os

    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = HEALTH_FILE.with_suffix(".lock")
    with lock.open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = _health_load()
        data.setdefault(_key_id(key), {}).update(fields)
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HEALTH_FILE)


def health_reset(key_id: str = "all") -> int:
    """Zera o estado de saúde (um provedor/chave pelo id, ou todos). Devolve quantos."""
    data = _health_load()
    if key_id == "all":
        n = len(data)
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text("{}", encoding="utf-8")
        import os

        os.replace(tmp, HEALTH_FILE)
        return n
    if key_id in data:
        del data[key_id]
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        import os

        os.replace(tmp, HEALTH_FILE)
        return 1
    return 0


class KeyRotator:
    """Rodízio de chaves por provedor: cada chamada começa por uma chave
    diferente (round-robin). Chave com erro de auth/permissão (401/402/403)
    fica banida, e chave com 429 de cota descansa até o prazo. O estado vai
    pra data/provider_health.json: um job novo não repete a chave morta que o
    anterior já descobriu (antes, cada execução começava do zero)."""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self._next = 0
        saved = _health_load()
        self._banned = {k for k in self._keys if saved.get(_key_id(k), {}).get("banned")}
        self._resting = {}
        now = time.time()
        for k in self._keys:
            until = float(saved.get(_key_id(k), {}).get("until", 0) or 0)
            if until > now:
                self._resting[k] = until

    def order(self) -> list[str]:
        now = time.time()
        active = [
            k for k in self._keys
            if k not in self._banned and self._resting.get(k, 0.0) <= now
        ]
        if not active:
            return []
        start = self._next % len(active)
        self._next += 1
        return active[start:] + active[:start]

    def ban(self, key: str) -> None:
        self._banned.add(key)
        _health_update(key, banned=True, reason="auth/permissão ou pagamento")
        log.warning("chave banida por erro de auth/permissão (…%s)", key[-4:])

    def rest(self, key: str, seconds: float) -> None:
        if seconds > 0:
            until = time.time() + seconds
            self._resting[key] = until
            _health_update(key, until=until)
            log.warning("chave em descanso por %.0fs (cota, …%s)", seconds, key[-4:])


# O rodízio PRECISA persistir entre chamadas. Antes era `id(keys)`, mas cada
# complete() cria um LLMKeys() novo: o id mudava, o rotador nascia do zero e
# a 1ª chave era sempre a primeira da lista. Na prática o Groq queimava a
# cota da chave 1 antes de encostar nas outras 7 (06/11). A chave do cache é
# o conteúdo da lista (tupla), que é estável entre instâncias.
_rotators: dict[tuple[str, ...], KeyRotator] = {}


def _rotator_for(keys: list[str]) -> KeyRotator:
    cache_id = tuple(keys)
    if cache_id not in _rotators:
        _rotators[cache_id] = KeyRotator(keys)
    return _rotators[cache_id]


# Groq grátis soma entrada + max_tokens no limite por minuto (8 mil no
# gpt-oss): roteiro de 6,5 mil + 3 mil de max_tokens dava 413 em todas as
# contas, sem gastar a cota do dia — encolhe max_tokens pra caber
_TOO_LARGE_RE = re.compile(r"Request too large.*?\(TPM\): Limit (\d+), Requested (\d+)")
# menos que isso não cabe nem um roteiro curto em JSON
_MIN_FIT_TOKENS = 1024


def _cooldown_seconds(status: int | None, body: str) -> int:
    """Quanto tempo a chave deve descansar depois de um 429. Cota por dia
    (TPD) só volta na virada UTC; cota por minuto (TPM) em ~1 min. Sem isso
    a mesma chave esgotada era tentada em toda chamada seguinte — o log de
    06/11 tem 2.089 tentativas no Groq, muitas na chave que já tinha
    estourado o TPD."""
    if status != 429:
        return 0
    low = body.lower()
    if "per day" in low or "per-day" in low or "daily" in low or "tpd" in low:
        now = datetime.now(timezone.utc)
        reset = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return max(60, int((reset - now).total_seconds()))
    return 75


# último instante em que o Groq recebeu pedido: a janela de 60 s do TPM fecha depois disso
_LAST_GROQ_CALL = {"at": 0.0}


def _call_openai_compat(
    endpoint: str, api_key: str, model: str, messages: list[dict], max_tokens: int, temperature: float = 0.8,
    _fitted: bool = False,
) -> tuple[str | None, int | None, int]:
    if "api.groq.com" in endpoint:
        _LAST_GROQ_CALL["at"] = time.monotonic()
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if "gpt-oss" in model:
        # o raciocínio sai do mesmo max_tokens: no padrão o gpt-oss gastava
        # os 3 mil pensando e devolvia o roteiro vazio (teste de 20/10)
        payload["reasoning_effort"] = "low"
    if "nemotron" in model:
        # o Nemotron também pensa dentro do max_tokens: o roteiro voltava
        # vazio ou cortado no meio do JSON e o Groq escrevia no lugar (04/11).
        # Escrita (temperatura alta): sem raciocínio, 3x mais rápido.
        # Julgamento (checagem de fato, temperatura baixa): pensa, com folga
        if temperature >= 0.5:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            payload["max_tokens"] = max_tokens + 4096
    try:
        resp = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=180 if "nemotron" in model else 60,
        )
        m = _TOO_LARGE_RE.search(resp.text) if resp.status_code == 413 else None
        if m and not _fitted:
            # ajusta UMA vez: repetir só descia o max_tokens de 100 em 100
            # (o consumo do minuto sobe a cada pedido) e a resposta cortada
            # reprovava na validação (job 596, 21/10). Abaixo de 3/4 do
            # pedido, o roteiro não cabe: vai pro próximo modelo/provedor
            fitted = max_tokens - (int(m.group(2)) - int(m.group(1))) - 64
            if max(_MIN_FIT_TOKENS, max_tokens * 3 // 4) <= fitted < max_tokens:
                log.info("%s (%s): max_tokens %d -> %d pra caber no limite por minuto", endpoint, model, max_tokens, fitted)
                return _call_openai_compat(endpoint, api_key, model, messages, fitted, temperature, _fitted=True)
        if resp.status_code != 200:
            log.warning("%s (%s) -> HTTP %s: %s", endpoint, model, resp.status_code, resp.text[:200])
            return None, resp.status_code, _cooldown_seconds(resp.status_code, resp.text)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        finish = data["choices"][0].get("finish_reason")
        if finish not in (None, "stop"):
            # 10/11: resposta cortada sem motivo registrado (revisão do job 817 parou no meio
            # de uma string com 3000 de max_tokens); o provedor diz por quê
            log.warning("%s (%s): finish_reason=%s, uso=%s, max_tokens=%d, %d caracteres",
                        endpoint, model, finish, data.get("usage"), max_tokens, len(content or ""))
        if content and _strip_reasoning(content) is None:
            # <think> sem fechamento: resposta cortada no meio do raciocínio (max_tokens)
            log.warning("%s (%s): resposta cortada dentro de <think> (%d caracteres, fim %r)",
                        endpoint, model, len(content), content[-120:])
        return (_strip_reasoning(content) if content else None), 200, 0
    except Exception as exc:  # rede, timeout, JSON malformado, etc.
        log.warning("%s (%s) falhou: %s", endpoint, model, exc)
        return None, None, 0


def _call_gemini(api_key: str, model: str, messages: list[dict], max_tokens: int, temperature: float = 0.8) -> str | None:
    prompt = "\n\n".join(m["content"] for m in messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    config = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if "flash" in model:
        # o 2.5 "pensa" com o mesmo maxOutputTokens: gastava 572 de 600 e o
        # JSON saía cortado (finishReason MAX_TOKENS) — toda resposta dele
        # reprovava na validação (teste de 21/10)
        config["thinkingConfig"] = {"thinkingBudget": 0}
    try:
        resp = httpx.post(
            url,
            headers={"x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
            timeout=60,
        )
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            # modelo que não aceita desligar o raciocínio: vai sem a opção
            config.pop("thinkingConfig", None)
            resp = httpx.post(
                url,
                headers={"x-goog-api-key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
                timeout=60,
            )
        if resp.status_code != 200:
            log.warning("gemini (%s) -> HTTP %s: %s", model, resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _strip_reasoning(text) if text else None
    except Exception as exc:
        log.warning("gemini (%s) falhou: %s", model, exc)
        return None


def is_strong_model(model: str) -> bool:
    """Modelo grande o bastante pra JULGAR fato (prova por citação, premissa
    do tema): o gpt-oss-20b e o qwen aprovavam frase que o trecho não prova."""
    return any(model in ms for ms in _STRONG_MODELS.values())


# limite de tokens por minuto (entrada + saída) por requisição em contas grátis, quando conhecido
_SMALL_TPM = {"groq": 8000}  # limite por requisição (entrada + max_tokens), medido no 413


def _too_big_for(name: str, messages: list[dict], max_tokens: int = 0) -> bool:
    """Medido em 09/11: ~3,7 caracteres por token em pt-BR. O pedido inteiro (entrada + saída
    reservada em max_tokens) não pode passar do limite do provedor, com 200 de folga."""
    limit = _SMALL_TPM.get(name)
    if limit is None:
        return False
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars / 3.7 + max_tokens > limit - 200

def complete(
    messages: list[dict],
    keys: LLMKeys | None = None,
    max_tokens: int = 4096,
    validate: Callable[[str], bool] | None = None,
    model_filter: Callable[[str], bool] | None = None,
    temperature: float = 0.8,
) -> str:
    """Roda a cascata de provedores (com rodízio de chaves e descoberta
    dinâmica de modelo dentro de cada um) e devolve a primeira resposta não
    vazia (e, se `validate` for passado, que também passe nele).

    `validate` existe pra nunca aceitar resposta estruturalmente quebrada
    (ex.: roteiro sem JSON válido, tema vazado com raciocínio) só porque foi
    a primeira a vir — sem isso, um modelo grátis ruim/instável que sempre
    "responde" algo (só que garbage) fazia a cascata parar nele igual toda
    vez, e quem chama (script_gen, topics) gastava as próprias tentativas
    de retry inteiras batendo nesse mesmo modelo quebrado em vez de mudar
    de provedor/modelo (bug real: 3 tentativas seguidas do roteiro vieram
    do mesmo modelo repetindo "{" sem fechar o JSON). Com `validate`, um
    resultado que falha nele é tratado como se o modelo não tivesse
    respondido — a cascata segue pro próximo modelo/chave/provedor na hora,
    dentro da MESMA chamada.

    `max_tokens` importa MUITO pra roteiros longos: sem limite explícito
    cada provedor usa seu próprio default (baixo), e a resposta trunca no
    meio do JSON — foi exatamente o que quebrou o roteiro de formato longo
    (31 cenas) antes desse parâmetro existir.

    `model_filter` pula modelo pelo nome antes de chamar — pra julgamento
    que modelo pequeno erra (checagem de tema: allam-2-7b aprovou "Salmos"
    no canal de política).

    Levanta RuntimeError se nenhum provedor configurado conseguir responder
    (ou, com `validate`, se nenhum devolver algo que passe na validação).
    """
    if keys is None:
        keys = LLMKeys()

    def _ok(result: str | None) -> bool:
        return bool(result) and (validate is None or validate(result))

    for name, key_attr, chat_endpoint, models_endpoint in OPENAI_COMPAT_PROVIDERS:
        provider_keys = getattr(keys, key_attr)
        if not provider_keys:
            continue
        if _too_big_for(name, messages, max_tokens):
            # 09/11: o pedido do roteiro passou de 8 mil tokens e o Groq recusava com 413 em todas
            # as contas; pular o provedor poupa a tentativa e deixa o pedido chegar aos de contexto grande
            log.info("pulando %s: pedido maior que o limite por minuto dele", name)
            continue
        rotator = _rotator_for(provider_keys)
        order = rotator.order()
        # 1ª passada: o modelo forte (_STRONG_MODELS) em TODAS as chaves; só
        # depois os outros modelos, chave por chave. Antes cada chave tentava
        # os modelos na ordem do /models e o qwen3.8-27b (que inventou
        # "camisa 9" no teste de 22/10) respondia 70% de tudo — o gpt-oss-120b
        # só via a vez quando o qwen falhava (pedido do dono, 22/10)
        passes = [(m, order) for m in _STRONG_MODELS.get(name, [])] + [(None, order)]
        # 413 é por chave/conta (TPM daquela chave), não do provedor inteiro:
        # com 8 contas no Groq, marcar o modelo globalmente impedia as outras
        # chaves de tentarem o modelo forte (06/11).
        too_large: set[tuple[str, str]] = set()
        for only, pass_keys in passes:
            for api_key in pass_keys:
                if api_key in rotator._banned or rotator._resting.get(api_key, 0.0) > time.monotonic():
                    # 10/11: pulada em silêncio (o juiz do job 815 não deixou rastro no log)
                    log.info("%s: chave %s pulada (%s) no passe %s", name, _key_id(api_key),
                             "banida" if api_key in rotator._banned else "em descanso", only or "forte")
                    continue
                models = _fetch_models(name, models_endpoint, api_key)
                if not models:
                    log.warning("%s: chave %s sem modelos em /models (passe %s)", name, _key_id(api_key), only or "forte")
                for model in models:
                    if (only is not None and model != only) or (only is None and model in _STRONG_MODELS.get(name, [])):
                        continue
                    if (api_key, model) in too_large or (model_filter is not None and not model_filter(model)):
                        continue
                    result, status, cooldown = _call_openai_compat(
                        chat_endpoint, api_key, model, messages, max_tokens, temperature,
                    )
                    if _ok(result):
                        log.info("resposta via %s/%s", name, model)
                        return result  # type: ignore[return-value]
                    if result and validate is not None:
                        log.warning("resposta de %s/%s não passou na validação, tentando próximo modelo", name, model)
                    if status == 413:
                        too_large.add((api_key, model))
                    if status in (401, 402, 403):
                        # 401 = chave inválida (permanente). 402 "pagamento" e 403
                        # podem voltar (crédito, região, modelo): descansam por
                        # prazo, não banidos pra sempre
                        if status == 401:
                            rotator.ban(api_key)
                        else:
                            rotator.rest(api_key, 86400 if status == 402 else 21600)
                        break  # próxima chave, não adianta repetir modelos com a mesma
                    if cooldown:
                        # cota estourada: descansa a chave e não insiste nos
                        # outros modelos dela nesta mesma chamada
                        rotator.rest(api_key, cooldown)
                        break

    if keys.cloudflare:
        result = _cloudflare_complete(keys.cloudflare, messages, max_tokens, _ok, model_filter, validate, temperature)
        if result is not None:
            return result

    # Gemini fora da cadeia de texto (09/11): gemini-2.5-flash retirado para contas novas (404) e
    # o flash-latest dava timeout de 60 s em cada chamada. A visão usa visual_check.py.
    if _GEMINI_TEXT_IN_CHAIN and keys.gemini:
        rotator = _rotator_for(keys.gemini)
        for api_key in rotator.order():
            models = _fetch_gemini_models(api_key)
            for model in models:
                if model_filter is not None and not model_filter(model):
                    continue
                result = _call_gemini(api_key, model, messages, max_tokens, temperature)
                if _ok(result):
                    log.info("resposta via gemini/%s", model)
                    return result  # type: ignore[return-value]
                if result and validate is not None:
                    log.warning("resposta de gemini/%s não passou na validação, tentando próximo modelo", model)

    raise RuntimeError(
        "Nenhum provedor LLM respondeu (ou nenhuma resposta passou na validação). "
        "Confira as chaves em ~/.secrets/keys.env (XAI_API_KEYS, GROQ_API_KEYS, CEREBRAS_API_KEYS, "
        "OPENROUTER_API_KEYS, MISTRAL_API_KEYS, GEMINI_API_KEYS)."
    )
