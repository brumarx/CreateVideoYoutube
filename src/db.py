"""Conexão única com o banco da fila: WAL, espera de 30 s por bloqueio e chaves
estrangeiras ligadas. Todo código novo abre o banco por aqui."""
from __future__ import annotations

import sqlite3
from pathlib import Path

QUEUE_DB = Path(__file__).resolve().parent.parent / "data" / "queue.db"


def connect_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or QUEUE_DB), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
