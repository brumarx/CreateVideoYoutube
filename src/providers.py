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


class ProvedoresIndisponiveis(RuntimeError):
    """19/11: nenhum provedor capaz respondeu porque estão todos em descanso (cota) ou fora do ar, e a
    volta do primeiro está longe demais para esperar dentro da chamada. `disponivel_em` (epoch, UTC) é
    quando o primeiro volta, se conhecido; `resumo` é o diagnóstico (sem chave nenhuma). Quem chama
    decide: guardar o trabalho e retomar depois, ou reprovar com diagnóstico de infraestrutura."""

    def __init__(self, mensagem: str, disponivel_em: float | None = None, resumo: str = ""):
        super().__init__(mensagem)
        self.disponivel_em = disponivel_em
        self.resumo = resumo


# espera DENTRO da chamada quando o primeiro provedor capaz volta logo (descanso de cota por minuto);
# acima disso, ProvedoresIndisponiveis com a hora da volta (o job guarda o trabalho e retoma depois)
ESPERA_CURTA_MAX = 180
ESPERA_TOTAL_MAX = 600
_DUR_PARTE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_TENTE_EM = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.I)
_CHAVE_NO_TEXTO = re.compile(r"(gsk_|sk-or-v1-|sk-|nvapi-|AIza)[A-Za-z0-9_\-]{8,}")


def _duracao(texto: str | None) -> float | None:
    """'1m26.4s', '547ms', '7.5s', '2h3m4s' ou só segundos ('30') -> segundos."""
    t = str(texto or "").strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    partes = _DUR_PARTE.findall(t)
    if not partes:
        return None
    mult = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(v) * mult[u] for v, u in partes)


def _retry_after(headers) -> float | None:
    bruto = (headers or {}).get("retry-after")
    if not bruto:
        return None
    seg = _duracao(bruto)
    if seg is not None:
        return seg
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, (parsedate_to_datetime(bruto) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _mascara(texto: str, api_key: str = "") -> str:
    t = str(texto or "")
    if api_key:
        t = t.replace(api_key, "***")
    return _CHAVE_NO_TEXTO.sub(r"\1***", t)


def classe_do_erro(status: int | None, body: str) -> str:
    """Não confundir: "tamanho" (o pedido sozinho passa do limite por pedido/minuto — esperar não
    resolve), "cota_minuto" (volta em segundos), "cota_diaria" (volta na virada do dia), "config"
    (chave inválida, modelo inexistente, permissão), "pagamento", "transitoria" (rede/5xx), "outro"."""
    low = str(body or "").lower()
    # 20/11: o Groq também responde 400 "Please reduce the length of the messages or completion" (log real
    # de 19/11, allam-2-7b) — é tamanho, não configuração
    if status == 413 or "request too large" in low or "reduce the length" in low \
            or "context length" in low or "maximum context" in low:
        return "tamanho"
    if status == 429:
        if "per day" in low or "per-day" in low or "daily" in low or "(tpd)" in low or "(rpd)" in low:
            return "cota_diaria"
        return "cota_minuto"
    if status == 402:
        return "pagamento"
    if status in (400, 401, 403, 404):
        return "config"
    if status is None or status >= 500:
        return "transitoria"
    return "outro"


# último diagnóstico de cada (chave, modelo): o laço da cascata lê depois da chamada (escopo do descanso)
_ULTIMO_ERRO: dict[tuple[str, str], dict] = {}


def _diagnostico(endpoint: str, model: str, status: int | None, body: str, headers, api_key: str) -> dict:
    """Registro do erro sem chave nenhuma: provedor, modelo, HTTP, código/tipo/mensagem do erro,
    limites e restantes, retry-after e os resets (x-ratelimit-reset-tokens/requests), organização."""
    erro = {}
    try:
        erro = (json.loads(body) or {}).get("error") or {}
        erro = erro if isinstance(erro, dict) else {"message": str(erro)}
    except (ValueError, AttributeError, TypeError):
        erro = {"message": str(body or "")[:300]}
    h = headers or {}
    org = re.search(r"organization `(org_[A-Za-z0-9]+)`", str(body or ""))
    d = {
        "provedor": re.sub(r"^https?://([^/]+).*$", r"\1", endpoint), "modelo": model, "http": status,
        "classe": classe_do_erro(status, body), "codigo": erro.get("code"), "tipo": erro.get("type"),
        "mensagem": _mascara(str(erro.get("message", "")), api_key)[:300],
        "org": (org.group(1)[:8] + "…") if org else None,
        "retry_after": h.get("retry-after"),
        "limite_requisicoes": h.get("x-ratelimit-limit-requests"), "restam_requisicoes": h.get("x-ratelimit-remaining-requests"),
        "reset_requisicoes": h.get("x-ratelimit-reset-requests"),
        "limite_tokens": h.get("x-ratelimit-limit-tokens"), "restam_tokens": h.get("x-ratelimit-remaining-tokens"),
        "reset_tokens": h.get("x-ratelimit-reset-tokens"),
    }
    d = {k: v for k, v in d.items() if v not in (None, "")}
    d["_org_completa"] = org.group(1) if org else None
    return d

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

PAID_PROVIDER = "deepseek"
# 20/11: custo zero. A DeepSeek (paga) fica FORA da cascata operacional: nem dentro do teto antigo
# (US$ 0,15/vídeo, 0,50/dia) é chamada. Credenciais, código e tetos ficam; religar exige trocar isto
# para True de propósito (nenhuma variável de ambiente ou yaml liga sozinha)
PROVEDOR_PAGO_LIGADO = False


class ProvedorPagoDesligado(RuntimeError):
    """Tentativa de chamar a API paga com PROVEDOR_PAGO_LIGADO desligado."""


def _pago_bloqueado(name: str) -> bool:
    """Provedor pago fora da cascata: desligado (custo zero) ou com o teto de gasto atingido."""
    if name != PAID_PROVIDER:
        return False
    if not PROVEDOR_PAGO_LIGADO:
        return True
    from .paid_usage import dentro_do_teto
    return not dentro_do_teto()


def _registra_pago_ok(result: str) -> None:
    from .paid_usage import record_success
    record_success(len(result or ""))


def _registra_pago_uso(model: str, usage: dict) -> None:
    """Tokens cobrados pela DeepSeek contam no teto. Falha aqui nunca derruba a chamada."""
    try:
        from .paid_usage import record_usage
        record_usage(model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("não consegui contar o gasto do provedor pago: %s", exc)


def _registra_pago_sem_saldo(detail: str) -> None:
    from .paid_usage import record_exhausted
    record_exhausted(detail or "402 sem saldo")


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
    "deepseek": ["deepseek-flash"],
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
    # DeepSeek é PAGO e está DESLIGADA (PROVEDOR_PAGO_LIGADO); a linha fica só para religar de propósito
    ("deepseek", "deepseek", "https://api.deepseek.com/chat/completions", "https://api.deepseek.com/models"),
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
                _ULTIMO_MODELO[0] = f"cloudflare/{model}"
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
        self._resting_model: dict[tuple[str, str], float] = {}
        self._org: dict[str, str] = {}
        now = time.time()
        for k in self._keys:
            info = saved.get(_key_id(k), {})
            until = float(info.get("until", 0) or 0)
            if until > now:
                self._resting[k] = until
            for modelo, ate in (info.get("modelos") or {}).items():
                if float(ate or 0) > now:
                    self._resting_model[(k, modelo)] = float(ate)
            if info.get("org"):
                self._org[k] = info["org"]

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

    def ate(self, key: str, model: str | None = None) -> float:
        """Até quando (epoch) a chave — e, com `model`, aquele modelo nela — está em descanso."""
        return max(self._resting.get(key, 0.0), self._resting_model.get((key, model), 0.0) if model else 0.0)

    def set_org(self, key: str, org: str | None) -> None:
        """20/11: a organização só é conhecida quando o PRÓPRIO provedor a escreve numa resposta de erro
        ("... in organization `org_…`"). Sem isso a chave fica sozinha: nenhum agrupamento é suposto.
        No arquivo de saúde vai pelo hash da chave (_key_id), com a hora em que foi vista."""
        if org and self._org.get(key) != org:
            self._org[key] = org
            _health_update(key, org=org, org_visto_em=round(time.time()))
            log.info("chave %s é da organização %s… (lido na resposta do provedor)", _key_id(key)[:8], org[:12])

    def rest_model(self, key: str, model: str, seconds: float) -> None:
        """19/11: cota por modelo (o TPD do gpt-oss-120b não tira o gpt-oss-20b da mesma chave). As
        chaves da MESMA organização dividem o limite: descansam juntas naquele modelo."""
        if seconds <= 0:
            return
        until = time.time() + seconds
        org = self._org.get(key)
        mesmas = [k for k in self._keys if k == key or (org and self._org.get(k) == org)]
        for k in mesmas:
            self._resting_model[(k, model)] = max(until, self._resting_model.get((k, model), 0.0))
            modelos = dict((_health_load().get(_key_id(k), {}).get("modelos") or {}))
            modelos[model] = self._resting_model[(k, model)]
            _health_update(k, modelos=modelos)
        log.warning("%s em descanso por %.0fs em %d chave(s) da mesma organização (…%s)", model, seconds, len(mesmas), key[-4:])


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


def _cooldown_seconds(status: int | None, body: str, headers=None) -> int:
    """Quanto tempo a chave deve descansar depois de um 429. 19/11: o prazo vem do PROVEDOR, nesta
    ordem: cabeçalho Retry-After; "try again in 7m12s" da mensagem; o reset do limite que estourou
    (x-ratelimit-reset-tokens para TPM, x-ratelimit-reset-requests para RPM/RPD). Sem nenhum deles:
    cota diária até a virada UTC, cota por minuto 75 s. Antes era sempre 75 s ou a virada do dia,
    e uma chave que voltava em 2 s ficava 75 s parada."""
    if status != 429:
        return 0
    low = str(body or "").lower()
    diaria = classe_do_erro(status, body) == "cota_diaria"
    espera = _retry_after(headers)
    if espera is None:
        m = _TENTE_EM.search(str(body or ""))
        espera = _duracao(m.group(1)) if m else None
    if espera is None and headers:
        tokens = "tokens per minute" in low or "(tpm)" in low or "(itpm)" in low or "(otpm)" in low
        espera = _duracao(headers.get("x-ratelimit-reset-tokens" if tokens else "x-ratelimit-reset-requests"))
    if espera is not None:
        return max(2, int(espera + 0.999) + 1)
    if diaria:
        now = datetime.now(timezone.utc)
        reset = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return max(60, int((reset - now).total_seconds()))
    return 75


# último instante em que o Groq recebeu pedido: a janela de 60 s do TPM fecha depois disso
_LAST_GROQ_CALL = {"at": 0.0}


def _call_openai_compat(
    endpoint: str, api_key: str, model: str, messages: list[dict], max_tokens: int, temperature: float = 0.8,
    _fitted: bool = False, _ampliado: bool = False, pensar: bool = False,
) -> tuple[str | None, int | None, int]:
    if "api.groq.com" in endpoint:
        _LAST_GROQ_CALL["at"] = time.monotonic()
    if "api.deepseek.com" in endpoint and not PROVEDOR_PAGO_LIGADO:
        # última barreira: nenhum caminho (cascata, teste manual, código novo) chega na API paga
        raise ProvedorPagoDesligado("DeepSeek é paga e está desligada (custo zero)")
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if "api.deepseek.com" in endpoint:
        # sem raciocínio: o Flash gastava os 12 mil tokens de saída pensando e cortava o JSON
        # (rodada 5, 11/11). O roteiro, a revisão e o conserto saem direto, e o raciocínio
        # também é cobrado como saída
        payload["thinking"] = {"type": "disabled"}
    if "gpt-oss" in model:
        # o raciocínio sai do mesmo max_tokens: no padrão o gpt-oss gastava
        # os 3 mil pensando e devolvia o roteiro vazio (teste de 20/10)
        payload["reasoning_effort"] = "low"
    if "nemotron" in model:
        # o Nemotron também pensa dentro do max_tokens: o roteiro voltava
        # vazio ou cortado no meio do JSON e o Groq escrevia no lugar (04/11).
        # 24/11: só o JUIZ fixo pensa (com folga). O resto (banco de evidências, auditoria factual,
        # escrita) sem raciocínio: com ele, 19 de 26 respostas da rodada de Svalbard saíram cortadas
        # (o raciocínio comia o max_tokens) e cada etapa perdia ~5 min antes de cair no Groq; sem
        # ele, a auditoria factual levou 14 s em vez de 52, sem corte
        if pensar:
            payload["max_tokens"] = max_tokens + 4096
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        resp = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            # julgamento (raciocínio ligado, temperatura baixa) passa de 3 min: 21/10 o juiz
            # do job 821 deu timeout aos 180 s e o roteiro morreu sem revisão
            timeout=(420 if pensar else 180) if "nemotron" in model else 60,
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
                return _call_openai_compat(endpoint, api_key, model, messages, fitted, temperature, _fitted=True,
                                           pensar=pensar)
        if resp.status_code != 200:
            log.warning("%s (%s) -> HTTP %s: %s", endpoint, model, resp.status_code, _mascara(resp.text[:200], api_key))
            diag = _diagnostico(endpoint, model, resp.status_code, resp.text, resp.headers, api_key)
            _ULTIMO_ERRO[(_key_id(api_key), model)] = diag
            log.warning("falha de provedor: %s", json.dumps({k: v for k, v in diag.items() if not k.startswith("_")},
                                                             ensure_ascii=False))
            return None, resp.status_code, _cooldown_seconds(resp.status_code, resp.text, resp.headers)
        data = resp.json()
        if "api.deepseek.com" in endpoint:
            _registra_pago_uso(model, data.get("usage") or {})
        content = data["choices"][0]["message"]["content"]
        finish = data["choices"][0].get("finish_reason")
        if finish not in (None, "stop"):
            # 10/11: resposta cortada sem motivo registrado (revisão do job 817 parou no meio
            # de uma string com 3000 de max_tokens); o provedor diz por quê
            log.warning("%s (%s): finish_reason=%s, uso=%s, max_tokens=%d, %d caracteres",
                        endpoint, model, finish, data.get("usage"), max_tokens, len(content or ""))
        if finish == "length":
            # 19/11: resposta TRUNCADA nunca é aceita. Uma vez, com mais espaço de saída se couber no
            # limite do provedor (no Groq, entrada + saída cabe nos tokens por minuto); senão, None e a
            # cascata segue para o próximo modelo/chave
            nome = next((n for n, _, ep, _ in OPENAI_COMPAT_PROVIDERS if ep == endpoint), "")
            teto = _SMALL_TPM.get(nome)
            entrada = int(sum(len(str(m.get("content", ""))) for m in messages) / 3.7)
            maior = min(max_tokens * 2, (teto - entrada - 200) if teto else 16000)
            # no provedor PAGO não repete (dobraria o gasto; as regras financeiras não mudam aqui)
            if not _ampliado and maior >= max_tokens * 1.25 and "api.deepseek.com" not in endpoint:
                log.warning("%s (%s): resposta truncada com max_tokens=%d — de novo com %d", endpoint, model, max_tokens, maior)
                return _call_openai_compat(endpoint, api_key, model, messages, maior, temperature, _fitted, True,
                                           pensar=pensar)
            log.warning("%s (%s): resposta truncada (max_tokens=%d) — descartada", endpoint, model, max_tokens)
            return None, 200, 0
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

def _complete_cascata(
    messages: list[dict],
    keys: LLMKeys | None = None,
    max_tokens: int = 4096,
    validate: Callable[[str], bool] | None = None,
    model_filter: Callable[[str], bool] | None = None,
    temperature: float = 0.8,
    _nova_tentativa: bool = False,
    _esperado: float = 0.0,
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

    # falha transitória = rede caiu ou 5xx (o provedor está fora do ar agora,
    # não sem cota). Se nenhum provedor respondeu e houve dessas, a cascata
    # roda de novo UMA vez depois de 90 s, em vez de o roteiro morrer (job 830)
    transitoria = False
    diagnosticos: list[dict] = []
    grandes: list[str] = []  # provedores pulados porque o pedido sozinho passa do limite deles

    for name, key_attr, chat_endpoint, models_endpoint in OPENAI_COMPAT_PROVIDERS:
        provider_keys = getattr(keys, key_attr)
        if not provider_keys:
            continue
        if _pago_bloqueado(name):
            continue  # pago desligado (custo zero) ou teto atingido: a DeepSeek não é chamada
        if _too_big_for(name, messages, max_tokens):
            # 09/11: o pedido do roteiro passou de 8 mil tokens e o Groq recusava com 413 em todas
            # as contas; pular o provedor poupa a tentativa e deixa o pedido chegar aos de contexto grande
            log.info("pulando %s: o pedido sozinho (entrada + max_tokens) passa do limite por pedido dele "
                     "(tokens por minuto: um pedido maior que o limite é recusado com 413, esperar não resolve)", name)
            grandes.append(name)
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
                # 19/11: o prazo do descanso é relógio de parede (time.time); comparado com time.monotonic
                # dava sempre "em descanso" e a chave que levou um 429 ficava fora pelo resto do processo
                if api_key in rotator._banned or rotator._resting.get(api_key, 0.0) > time.time():
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
                    if rotator.ate(api_key, model) > time.time():
                        log.info("%s: %s em descanso na chave %s até %s", name, model, _key_id(api_key),
                                 datetime.fromtimestamp(rotator.ate(api_key, model), timezone.utc).strftime("%H:%M:%S UTC"))
                        continue
                    chamar = _chamar_com_reconexao if name in PROVEDOR_QUE_RECONECTA else _call_openai_compat
                    result, status, cooldown = chamar(
                        chat_endpoint, api_key, model, messages, max_tokens, temperature,
                    )
                    if _ok(result):
                        log.info("resposta via %s/%s", name, model)
                        _ULTIMO_MODELO[0] = f"{name}/{model}"
                        if name == PAID_PROVIDER:
                            _registra_pago_ok(result)
                        return result  # type: ignore[return-value]
                    if result and validate is not None:
                        # 26/11: sem a amostra não dava para saber que etapa recusou nem por quê
                        log.warning("resposta de %s/%s não passou na validação (%d caracteres; início: %r; "
                                    "fim: %r) — novo pedido ao mesmo modelo com o aviso de formato", name, model,
                                    len(result), result[:160], result[-160:])
                        # 26/11 (Svalbard 1027: a NVIDIA respondeu fora do formato 2 vezes, a cascata caiu no
                        # OpenRouter sem cota e a rodada parou): o mesmo modelo tenta de novo, avisado
                        result, status, cooldown = _call_openai_compat(
                            chat_endpoint, api_key, model, messages + [AVISO_DE_FORMATO], max_tokens, temperature,
                        )
                        if _ok(result):
                            log.info("resposta via %s/%s (2º pedido, com o aviso de formato)", name, model)
                            _ULTIMO_MODELO[0] = f"{name}/{model}"
                            return result  # type: ignore[return-value]
                        log.warning("2º pedido a %s/%s também fora do formato — tentando o próximo modelo", name, model)
                    if result is None and (status is None or status >= 500):
                        transitoria = True
                    if status == 413:
                        too_large.add((api_key, model))
                    if status == 402 and name == PAID_PROVIDER:
                        _registra_pago_sem_saldo(result or "")
                    if status in (401, 402, 403):
                        # 401 = chave inválida (permanente). 402 "pagamento" e 403
                        # podem voltar (crédito, região, modelo): descansam por
                        # prazo, não banidos pra sempre
                        if status == 401:
                            rotator.ban(api_key)
                        else:
                            rotator.rest(api_key, 86400 if status == 402 else 21600)
                        break  # próxima chave, não adianta repetir modelos com a mesma
                    diag = _ULTIMO_ERRO.pop((_key_id(api_key), model), None)
                    if diag:
                        diagnosticos.append(diag)
                        rotator.set_org(api_key, diag.get("_org_completa"))
                    if cooldown:
                        if diag and "for model" in diag.get("mensagem", "") and diag["classe"] in ("cota_minuto", "cota_diaria"):
                            # cota do MODELO (Groq: TPM/TPD por modelo e organização): os outros modelos da
                            # chave seguem disponíveis; as chaves da mesma organização descansam juntas
                            rotator.rest_model(api_key, model, cooldown)
                            continue
                        # cota da chave inteira: descansa a chave e não insiste nos
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
                    _ULTIMO_MODELO[0] = f"gemini/{model}"
                    return result  # type: ignore[return-value]
                if result and validate is not None:
                    log.warning("resposta de gemini/%s não passou na validação, tentando próximo modelo", model)

    if transitoria and not _nova_tentativa:
        log.warning("nenhum provedor respondeu e houve falha de rede/5xx — espera 90 s e tenta a cascata de novo")
        time.sleep(90)
        return _complete_cascata(messages, keys, max_tokens, validate, model_filter, temperature, _nova_tentativa=True,
                        _esperado=_esperado)

    # 19/11: o primeiro provedor capaz que volta logo (descanso de cota por minuto): espera o prazo que
    # o PRÓPRIO provedor deu e tenta de novo — nunca no mesmo segundo, nunca horas parado aqui
    volta = proxima_disponibilidade(keys, model_filter, grandes)
    agora = time.time()
    resumo = _resumo(diagnosticos, grandes, volta)
    if volta is not None and volta > agora:
        espera = volta - agora + 1
        if espera <= ESPERA_CURTA_MAX and _esperado + espera <= ESPERA_TOTAL_MAX:
            log.warning("nenhum provedor capaz agora — o primeiro volta em %.0f s; esperando (%s)", espera, resumo)
            time.sleep(espera)
            return _complete_cascata(messages, keys, max_tokens, validate, model_filter, temperature,
                                     _nova_tentativa=_nova_tentativa, _esperado=_esperado + espera)
        raise ProvedoresIndisponiveis(
            f"Nenhum provedor capaz disponível; o primeiro volta às "
            f"{datetime.fromtimestamp(volta, timezone.utc).strftime('%H:%M UTC')} ({resumo})", disponivel_em=volta, resumo=resumo)
    raise RuntimeError(
        "Nenhum provedor LLM respondeu (ou nenhuma resposta passou na validação) — " + resumo
    )


_ULTIMO_MODELO = ["?"]

# ---------------------------------------------------------------- juiz fixo (23/11)
# Svalbard (job 1005): a leitura de espectador do Nemotron gastou mais de 8 mil tokens raciocinando, veio
# cortada, foi descartada e a cascata respondeu com o gpt-oss-120b — versões do mesmo job julgadas por juízes
# diferentes, sem aviso. Avaliação que decide aprovação usa SEMPRE o mesmo provedor e o mesmo modelo: no
# máximo 2 pedidos (o 2º, se o 1º veio cortado, com o dobro de saída até o teto medido); sem resposta válida,
# JuizSemResposta — nunca outro modelo no lugar.
JUIZ = ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b")
JUIZ_SAIDA_MAX = 32000  # 23/11: a NVIDIA aceitou 60 mil de saída no Nemotron; o juiz nunca pede mais que isto
JUIZ_TENTATIVAS = 2
AVISO_DE_FORMATO = {"role": "user", "content": (
    "ATENÇÃO: a resposta anterior a este pedido não veio no formato pedido. Responda de novo SÓ com o JSON "
    "pedido acima, completo, sem texto fora dele; nomes de campo e valores EXATAMENTE como no pedido (por "
    "exemplo \"decisao\": \"sustentada\", nunca \"factual: sustentada\"); numere os itens com os mesmos números "
    "do pedido.")}


class JuizSemResposta(RuntimeError):
    """O juiz fixo não deu resposta válida em JUIZ_TENTATIVAS pedidos: a avaliação fica NÃO JULGADA."""


# 26/11 (o dono: "se nosso modelo cai com a NVIDIA, ele deve voltar a tentar e não abandonar"): queda do servidor
# (conexão derrubada, tempo esgotado, HTTP 500/502/504) não é resposta do modelo — espera e tenta o MESMO modelo de
# novo, cada vez esperando mais (~15 min no total), antes de desistir ou de ir para outro. Svalbard 1033 v5 (2
# problemas, editorial 7,1) virou "não julgada" por duas quedas seguidas; a 1027 caiu no OpenRouter sem cota.
QUEDA_ESPERAS: tuple[int, ...] = (30, 60, 120, 240, 480)
PROVEDOR_QUE_RECONECTA = ("nvidia",)


def _queda(result, status) -> bool:
    return result is None and (status is None or status in (500, 502, 504))


def _chamar_com_reconexao(endpoint, api_key, model, messages, max_tokens, temperature, **kw):
    result, status, cooldown = _call_openai_compat(endpoint, api_key, model, messages, max_tokens, temperature, **kw)
    for espera in QUEDA_ESPERAS:
        if not _queda(result, status):
            break
        log.warning("%s caiu (%s) — tentando de novo o mesmo modelo em %d s", model,
                    f"HTTP {status}" if status else "sem resposta", espera)
        time.sleep(espera)
        result, status, cooldown = _call_openai_compat(endpoint, api_key, model, messages, max_tokens, temperature, **kw)
    return result, status, cooldown


def _julgar_fixo(messages: list[dict], keys: LLMKeys, max_tokens: int, validate: Callable[[str], bool] | None,
                 temperature: float) -> str:
    nome, modelo = JUIZ
    _, attr, endpoint, _ = next(p for p in OPENAI_COMPAT_PROVIDERS if p[0] == nome)
    chaves = list(getattr(keys, attr) or [])
    if not chaves:
        raise JuizSemResposta(f"juiz {nome}/{modelo} sem chave configurada")
    rotator = _rotator_for(chaves)
    saida = min(max_tokens, JUIZ_SAIDA_MAX)
    motivos = []
    tentativa, esperado = 0, 0.0
    while tentativa < JUIZ_TENTATIVAS:
        ordem = [k for k in rotator.order() if rotator.ate(k, modelo) <= time.time()]
        if not ordem:
            # 24/11: como na cascata (Groq): todas as chaves do juiz descansando → espera a primeira voltar,
            # dentro dos mesmos tetos; só desiste se o descanso for longo
            volta = min(rotator.ate(k, modelo) for k in chaves)
            espera = volta - time.time() + 1
            if espera <= ESPERA_CURTA_MAX and esperado + espera <= ESPERA_TOTAL_MAX:
                log.warning("juiz fixo: todas as %d chave(s) em descanso — esperando %.0f s", len(chaves), espera)
                time.sleep(max(espera, 0))
                esperado += max(espera, 0)
                continue
            motivos.append("todas as chaves do juiz em descanso")
            break
        api_key = ordem[0]
        _ULTIMO_MODELO[0] = f"{nome}/{modelo}"
        result, status, cooldown = _chamar_com_reconexao(endpoint, api_key, modelo, messages, saida, temperature,
                                                         _ampliado=True, pensar=True)  # a 2ª tentativa é decidida aqui
        if result and (validate is None or validate(result)):
            log.info("juiz fixo %s/%s respondeu (tentativa %d, saída %d)", nome, modelo, tentativa + 1, saida)
            return result
        if not result and status in (429, 503):
            # 24/11: limite da chave não é resposta do juiz — a chave descansa e a próxima (ou a mesma, depois
            # do descanso) tenta, sem gastar uma das JUIZ_TENTATIVAS
            rotator.rest_model(api_key, modelo, cooldown or 60)
            motivos.append(f"HTTP {status} numa chave (descansa {cooldown or 60} s)")
            continue
        tentativa += 1
        if result:
            motivos.append(f"resposta inválida (tentativa {tentativa})")
            log.warning("juiz fixo: resposta fora do formato (%d caracteres; início: %r; fim: %r)", len(result),
                        result[:160], result[-160:])
            if AVISO_DE_FORMATO not in messages:
                messages = messages + [AVISO_DE_FORMATO]  # 26/11: a 2ª tentativa vai avisada
        elif status == 200:
            motivos.append(f"resposta cortada com saída {saida} (tentativa {tentativa})")
            saida = min(saida * 2, JUIZ_SAIDA_MAX)
        else:
            motivos.append(f"HTTP {status} (tentativa {tentativa})")
    log.warning("juiz fixo %s/%s sem resposta válida: %s — avaliação NÃO JULGADA (nenhum outro modelo no lugar)",
                nome, modelo, "; ".join(motivos))
    raise JuizSemResposta(f"juiz {nome}/{modelo} sem resposta válida: " + "; ".join(motivos))


def complete(messages: list[dict], keys: LLMKeys | None = None, max_tokens: int = 4096,
             validate: Callable[[str], bool] | None = None, model_filter: Callable[[str], bool] | None = None,
             temperature: float = 0.8, juiz: bool = False) -> str:
    """A cascata (ver _complete_cascata) com o registro de custo do job: modelo que respondeu e tempo de
    cada chamada, na etapa corrente (src/metricas). `juiz=True`: sem cascata — o JUIZ fixo (mesmo provedor e
    modelo sempre, ver _julgar_fixo), ou JuizSemResposta."""
    from . import gravacao, metricas
    t0 = time.time()
    _ULTIMO_MODELO[0] = "?"
    # 26/11: toda chamada fica gravada (data/gravacoes) — aceita ou recusada — e uma rodada gravada pode ser
    # repetida sem chamar ninguém (GRAVACAO_REPETIR); ver src/gravacao.py
    k = gravacao.chave(messages, max_tokens, temperature, juiz)
    gravada = gravacao.repetir(k)
    if gravada is not None:
        metricas.chamada("gravacao", 0.0)
        return gravada
    etapa = metricas._etapa_atual()

    def validar(r: str) -> bool:
        ok = validate is None or validate(r)
        if not ok:
            gravacao.gravar(k, messages, r, _ULTIMO_MODELO[0], False, etapa, juiz)
        return ok
    try:
        r = (_julgar_fixo(messages, keys or LLMKeys(), max_tokens, validar, temperature) if juiz
             else _complete_cascata(messages, keys, max_tokens, validar, model_filter, temperature))
    except Exception:
        metricas.chamada(_ULTIMO_MODELO[0], time.time() - t0, ok=False)
        raise
    metricas.chamada(_ULTIMO_MODELO[0], time.time() - t0)
    gravacao.gravar(k, messages, r, _ULTIMO_MODELO[0], True, etapa, juiz)
    return r


def proxima_disponibilidade(keys: LLMKeys, model_filter: Callable[[str], bool] | None = None,
                            pular: list[str] | None = None) -> float | None:
    """Quando (epoch) a primeira chave de um provedor capaz para este pedido sai do descanso. None se
    nenhuma está em descanso (o problema é outro: validação, configuração, tamanho, provedor fora)."""
    menor: float | None = None
    agora = time.time()
    for name, key_attr, _, _ in OPENAI_COMPAT_PROVIDERS:
        chaves = getattr(keys, key_attr)
        if not chaves or name in (pular or []):
            continue
        if _pago_bloqueado(name):
            continue
        rot = _rotator_for(chaves)
        modelos = [m for m in _STRONG_MODELS.get(name, []) + _PREFERRED_MODELS.get(name, [])
                   if model_filter is None or model_filter(m)] or ([None] if model_filter is None else [])
        for k in chaves:
            if k in rot._banned:
                continue
            for m in modelos:
                ate = rot.ate(k, m)
                if ate > agora and (menor is None or ate < menor):
                    menor = ate
    return menor


def _resumo(diagnosticos: list[dict], grandes: list[str], volta: float | None) -> str:
    """Diagnóstico curto da cascata (sem chave): o que falhou, por classe, e quando volta."""
    from collections import Counter
    classes = Counter(f"{d.get('provedor', '?').split('.')[-2] if '.' in d.get('provedor', '') else d.get('provedor')}"
                      f"/{d.get('modelo')}: {d.get('classe')}" for d in diagnosticos)
    partes = [f"{k} x{n}" if n > 1 else k for k, n in classes.items()]
    if grandes:
        partes.append("pedido grande demais para " + ", ".join(grandes))
    if volta:
        partes.append("primeira volta às " + datetime.fromtimestamp(volta, timezone.utc).strftime("%H:%M:%S UTC"))
    return "; ".join(partes) or "nenhum provedor configurado respondeu"
