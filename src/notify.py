"""Aviso no Telegram (bot do pipeline). Nunca levanta exceção: falha de aviso
não pode derrubar a rotina que está avisando."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger("notify")
load_dotenv(Path("~/.secrets/keys.env").expanduser())


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("Telegram sem TELEGRAM_TOKEN/TELEGRAM_CHAT_ID — aviso não enviado")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000]},
            timeout=20,
        )
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("aviso no Telegram falhou: %s", exc)
        return False
