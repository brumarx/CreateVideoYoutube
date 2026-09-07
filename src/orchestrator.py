"""Fila de vídeos em SQLite: cada linha é 1 vídeo passando pelos estados
pending -> scripted -> narrated -> rendered -> uploaded (ou failed)."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    video_path TEXT,
    thumbnail_path TEXT,
    youtube_video_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    channel: str
    topic: str
    status: str
    video_path: str | None
    thumbnail_path: str | None
    youtube_video_id: str | None
    error: str | None


def enqueue(channel: str, topic: str) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs (channel, topic, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
            (channel, topic, _now(), _now()),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid


def update(job_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    with closing(_connect()) as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))
        conn.commit()


def recent_jobs(channel: str | None = None, limit: int = 10) -> list[Job]:
    query = "SELECT id, channel, topic, status, video_path, thumbnail_path, youtube_video_id, error FROM jobs"
    params: tuple = ()
    if channel:
        query += " WHERE channel = ?"
        params = (channel,)
    query += " ORDER BY id DESC LIMIT ?"
    params = (*params, limit)

    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
        return [Job(*row) for row in rows]


def next_pending(channel: str | None = None) -> Job | None:
    query = "SELECT id, channel, topic, status, video_path, thumbnail_path, youtube_video_id, error FROM jobs WHERE status = 'pending'"
    params: tuple = ()
    if channel:
        query += " AND channel = ?"
        params = (channel,)
    query += " ORDER BY created_at LIMIT 1"

    with closing(_connect()) as conn:
        row = conn.execute(query, params).fetchone()
        return Job(*row) if row else None
