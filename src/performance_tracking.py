"""Performance tracking — armazena dados de vídeos para análise.

Estende o schema de queue.db com colunas adicionais:
- thumbnail_type (real_photo, ai_generated, text_overlay)
- published_at (timestamp)
- views_24h, views_7d, views_30d
- engagement (likes, comments, shares estimado)
- opening_style, closing_style (para análise A/B)
"""
from __future__ import annotations

import sqlite3
from .db import connect_db
from datetime import datetime
from pathlib import Path

import logging

log = logging.getLogger("performance_tracking")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QUEUE_DB = DATA_DIR / "queue.db"


def init_schema() -> None:
    """Cria tabelas extras se não existirem."""
    with connect_db(QUEUE_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_performance (
                job_id TEXT PRIMARY KEY,
                channel TEXT,
                published_at TIMESTAMP,
                thumbnail_type TEXT DEFAULT 'unknown',  -- real_photo, ai_generated, text_overlay
                opening_style TEXT DEFAULT 'unknown',   -- hook_number, hook_question, etc.
                closing_style TEXT DEFAULT 'unknown',
                duration_seconds INTEGER,
                views_24h INTEGER DEFAULT 0,
                views_7d INTEGER DEFAULT 0,
                views_30d INTEGER DEFAULT 0,
                views_current INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                engagement_rate REAL DEFAULT 0,  -- (likes + comments) / views
                publish_hour INTEGER,  -- 0-23
                publish_day_of_week TEXT,  -- Mon, Tue, etc.
                is_short BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_performance_summary (
                channel TEXT PRIMARY KEY,
                best_publish_hour INTEGER,
                best_publish_day TEXT,
                avg_views_per_video REAL,
                avg_engagement_rate REAL,
                best_opening_style TEXT,
                best_thumbnail_type TEXT,
                total_videos_tracked INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()


def record_video_published(
    job_id: str,
    channel: str,
    thumbnail_type: str = "unknown",
    opening_style: str = "unknown",
    closing_style: str = "unknown",
    duration_seconds: int = 0,
    is_short: bool = True,
) -> None:
    """Registra um vídeo como publicado."""
    now = datetime.now()

    with connect_db(QUEUE_DB) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO video_performance (
                job_id, channel, published_at, thumbnail_type, opening_style,
                closing_style, duration_seconds, publish_hour, is_short,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id, channel, now, thumbnail_type, opening_style, closing_style,
            duration_seconds, now.hour, is_short, now, now
        ))
        conn.commit()


def update_video_views(
    job_id: str,
    views_24h: int = 0,
    views_7d: int = 0,
    views_30d: int = 0,
    views_current: int = 0,
    likes: int = 0,
    comments: int = 0,
) -> None:
    """Atualiza visualizações e engagement de um vídeo."""
    now = datetime.now()
    engagement_rate = 0.0
    if views_current > 0:
        engagement_rate = (likes + comments) / views_current

    with connect_db(QUEUE_DB) as conn:
        conn.execute("""
            UPDATE video_performance SET
                views_24h = ?,
                views_7d = ?,
                views_30d = ?,
                views_current = ?,
                likes = ?,
                comments = ?,
                engagement_rate = ?,
                updated_at = ?
            WHERE job_id = ?
        """, (views_24h, views_7d, views_30d, views_current, likes, comments, engagement_rate, now, job_id))
        conn.commit()


def get_best_publish_time(channel: str) -> dict:
    """Retorna melhor hora e dia para publicar baseado em histórico."""
    with connect_db(QUEUE_DB) as conn:
        conn.row_factory = sqlite3.Row

        # Hora melhor
        result_hour = conn.execute("""
            SELECT publish_hour, AVG(views_30d) as avg_views
            FROM video_performance
            WHERE channel = ? AND views_30d > 0
            GROUP BY publish_hour
            ORDER BY avg_views DESC
            LIMIT 1
        """, (channel,)).fetchone()

        best_hour = result_hour["publish_hour"] if result_hour else 14

        # Dia melhor
        result_day = conn.execute("""
            SELECT publish_day_of_week, AVG(views_30d) as avg_views
            FROM video_performance
            WHERE channel = ? AND views_30d > 0
            GROUP BY publish_day_of_week
            ORDER BY avg_views DESC
            LIMIT 1
        """, (channel,)).fetchone()

        best_day = result_day["publish_day_of_week"] if result_day else "Friday"

    return {"best_hour": best_hour, "best_day": best_day}


def get_best_format_by_metric(
    channel: str,
    metric: str = "opening_style",
) -> dict:
    """Retorna melhor formato (abertura, thumbnail, etc.) por engajamento.

    metric: "opening_style", "closing_style", "thumbnail_type"
    """
    with connect_db(QUEUE_DB) as conn:
        conn.row_factory = sqlite3.Row

        result = conn.execute(f"""
            SELECT {metric}, AVG(views_30d) as avg_views,
                   AVG(engagement_rate) as avg_engagement,
                   COUNT(*) as count
            FROM video_performance
            WHERE channel = ? AND {metric} != 'unknown' AND views_30d > 0
            GROUP BY {metric}
            ORDER BY avg_engagement DESC
            LIMIT 1
        """, (channel,)).fetchone()

    if result:
        return {
            "best": result[metric],
            "avg_views": result["avg_views"],
            "avg_engagement": result["avg_engagement"],
            "sample_size": result["count"],
        }

    return {"best": "unknown", "avg_views": 0, "avg_engagement": 0, "sample_size": 0}


def update_channel_summary(channel: str) -> None:
    """Atualiza resumo de performance do canal."""
    best_time = get_best_publish_time(channel)
    best_opening = get_best_format_by_metric(channel, "opening_style")
    best_thumbnail = get_best_format_by_metric(channel, "thumbnail_type")

    with connect_db(QUEUE_DB) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO channel_performance_summary (
                channel, best_publish_hour, best_publish_day,
                best_opening_style, best_thumbnail_type,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            channel,
            best_time["best_hour"],
            best_time["best_day"],
            best_opening["best"],
            best_thumbnail["best"],
            datetime.now()
        ))
        conn.commit()
