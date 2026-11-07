"""Razão diário da cota da YouTube Data API (10.000 unidades por projeto, zera
à meia-noite do Pacífico). Cada escrita reserva o custo ANTES de chamar a API:
perto do limite, a escrita não acontece e o job espera o próximo dia, em vez
de estourar a cota no meio da fila."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "yt_quota.json"
DAILY_LIMIT = 9_500  # folga de 500 sobre o limite de 10.000
COST = {"upload": 1600, "thumbnail": 50, "playlist_item": 50, "playlist_create": 50, "comment": 50}


class QuotaExceeded(RuntimeError):
    pass


def _day() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _read() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def used_today() -> int:
    return int(_read().get(_day(), 0))


def spend(kind: str) -> None:
    """Reserva o custo de `kind` no dia. Levanta QuotaExceeded se passar do limite."""
    cost = COST[kind]
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        data = json.loads(raw) if raw.strip() else {}
        today = _day()
        used = int(data.get(today, 0))
        if used + cost > DAILY_LIMIT:
            raise QuotaExceeded(f"cota do YouTube no limite hoje ({used}/{DAILY_LIMIT}); {kind} espera amanhã")
        data = {k: v for k, v in data.items() if k >= today[:7]}  # mantém só o mês corrente
        data[today] = used + cost
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data))
        fh.flush()
