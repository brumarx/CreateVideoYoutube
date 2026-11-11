"""Uso e saldo do provedor PAGO (DeepSeek): quantas chamadas deram certo hoje,
quando acabou o saldo (402) e o saldo atual, para o painel mostrar.

Só é usado como ÚLTIMO recurso da cascata de texto (ver providers.complete):
se algum grátis respondeu, a DeepSeek nem é chamada."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

PROVIDER = "deepseek"
BALANCE_URL = "https://api.deepseek.com/user/balance"
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "paid_usage.json"
_BALANCE_TTL = 600  # s: o saldo não precisa ser consultado a cada carregamento do painel
_balance_cache: dict = {"at": 0.0, "value": None, "error": None}

# preço por 1M tokens (entrada, saída) — tabela oficial, PICO (o mais caro, então o teto é conservador)
PRECOS_POR_MILHAO = {"deepseek-flash": (0.30, 1.20), "deepseek-v4-pro": (1.32, 3.96)}
PRECO_PADRAO = (1.32, 3.96)  # modelo desconhecido: assume o mais caro
JOB_CAP_USD = 0.15  # teto por vídeo (job), definido pelo dono em 11/11
DAY_CAP_USD = 0.50  # teto por dia
_job_atual: str | None = None


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("não consegui gravar o uso do provedor pago: %s", exc)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record_success(chars: int) -> None:
    state = _load()
    day = state.setdefault("dias", {}).setdefault(_today(), {"chamadas": 0, "caracteres": 0})
    day["chamadas"] += 1
    day["caracteres"] += int(chars)
    state["ultimo_sucesso"] = datetime.now(timezone.utc).isoformat()
    state["sem_saldo_desde"] = None  # respondeu: saldo voltou
    _save(state)


def set_job(job_id) -> None:
    """Job em geração: o gasto das próximas chamadas é contado nele."""
    global _job_atual
    _job_atual = str(job_id) if job_id is not None else None


def custo_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    entrada, saida = PRECOS_POR_MILHAO.get(model, PRECO_PADRAO)
    return (prompt_tokens * entrada + completion_tokens * saida) / 1_000_000


def record_usage(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Conta tokens e custo do dia e do job. Vale também para resposta cortada:
    o provedor cobra os tokens de saída mesmo quando a resposta não serve."""
    custo = custo_usd(model, prompt_tokens, completion_tokens)
    tokens = prompt_tokens + completion_tokens
    state = _load()
    dia = state.setdefault("dias", {}).setdefault(_today(), {"chamadas": 0, "caracteres": 0})
    dia["custo_usd"] = round(dia.get("custo_usd", 0.0) + custo, 6)
    dia["tokens"] = dia.get("tokens", 0) + tokens
    if _job_atual:
        jobs = state.setdefault("jobs", {})
        j = jobs.setdefault(_job_atual, {"custo_usd": 0.0, "tokens": 0})
        j["custo_usd"] = round(j.get("custo_usd", 0.0) + custo, 6)
        j["tokens"] = j.get("tokens", 0) + tokens
        while len(jobs) > 60:  # guarda só os 60 jobs mais recentes
            jobs.pop(next(iter(jobs)))
    _save(state)
    return custo


def dentro_do_teto() -> bool:
    """False quando o dia ou o job atual já passaram do teto: a DeepSeek não é chamada."""
    state = _load()
    gasto_dia = ((state.get("dias") or {}).get(_today()) or {}).get("custo_usd", 0.0)
    if gasto_dia >= DAY_CAP_USD:
        log.warning("provedor pago: teto do dia atingido (US$ %.4f de US$ %.2f)", gasto_dia, DAY_CAP_USD)
        return False
    if _job_atual:
        gasto_job = ((state.get("jobs") or {}).get(_job_atual) or {}).get("custo_usd", 0.0)
        if gasto_job >= JOB_CAP_USD:
            log.warning("provedor pago: teto do job %s atingido (US$ %.4f de US$ %.2f)", _job_atual, gasto_job, JOB_CAP_USD)
            return False
    return True


def record_exhausted(detail: str) -> None:
    state = _load()
    if not state.get("sem_saldo_desde"):
        state["sem_saldo_desde"] = datetime.now(timezone.utc).isoformat()
    state["ultimo_erro"] = detail[:300]
    _save(state)
    log.warning("provedor pago (DeepSeek) sem saldo: %s", detail[:120])


def _fetch_balance(api_key: str) -> dict:
    now = time.monotonic()
    if _balance_cache["at"] and now - _balance_cache["at"] < _BALANCE_TTL:
        return _balance_cache
    try:
        resp = httpx.get(BALANCE_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            infos = data.get("balance_infos") or []
            value = ", ".join(f"{i.get('total_balance', '?')} {i.get('currency', '')}".strip() for i in infos) or "?"
            _balance_cache.update(at=now, value=value, error=None)
        else:
            _balance_cache.update(at=now, value=None, error=f"HTTP {resp.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        _balance_cache.update(at=now, value=None, error=str(exc)[:120])
    return _balance_cache


def summary() -> dict:
    """Estado para o painel. Nunca levanta exceção: falta de chave ou de rede só aparece como aviso."""
    from .config import LLMKeys

    keys = LLMKeys().deepseek
    state = _load()
    hoje = (state.get("dias") or {}).get(_today(), {"chamadas": 0, "caracteres": 0})
    total = sum(d.get("chamadas", 0) for d in (state.get("dias") or {}).values())
    out = {
        "configurada": bool(keys),
        "custo_hoje_usd": round(float(hoje.get("custo_usd", 0.0)), 4),
        "teto_dia_usd": DAY_CAP_USD,
        "teto_job_usd": JOB_CAP_USD,
        "chamadas_hoje": hoje.get("chamadas", 0),
        "chamadas_total": total,
        "sem_saldo_desde": state.get("sem_saldo_desde"),
        "ultimo_sucesso": state.get("ultimo_sucesso"),
        "ultimo_erro": state.get("ultimo_erro"),
        "saldo": None,
        "saldo_erro": None,
    }
    if keys:
        bal = _fetch_balance(keys[0])
        out["saldo"], out["saldo_erro"] = bal.get("value"), bal.get("error")
    return out
