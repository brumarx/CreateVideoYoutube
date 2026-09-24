#!/usr/bin/env python3
"""Reenvia vídeos que já foram renderizados mas falharam no upload (token
OAuth expirado, rede, cota) — sem gerar nada de novo.

Pega todo job com status "failed" que ainda tem final.mp4 e upload.json na
pasta (salvo por run_pipeline.py logo antes do upload). Roda sozinho no fim
de scripts/daily_run.py; dá pra rodar manual também, ex. logo depois de
reautorizar um canal com auth_youtube.py:

  .venv/bin/python3 scripts/retry_uploads.py [--channel politica]

Se o token do canal continuar inválido, pula o canal e tenta de novo na
próxima rodada.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google.auth.exceptions import RefreshError  # noqa: E402

from src.config import ChannelConfig  # noqa: E402
from src.orchestrator import DB_PATH, update  # noqa: E402
from src.upload import UPLOAD_META_FILE, after_upload_status, upload_video  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("retry_uploads")


def _failed_jobs(channel: str | None) -> list[tuple[int, str, Path]]:
    query = "SELECT id, channel, video_path FROM jobs WHERE status = 'failed' AND video_path IS NOT NULL"
    params: tuple = ()
    if channel:
        query += " AND channel = ?"
        params = (channel,)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    jobs = []
    for job_id, ch, video_path in rows:
        video = Path(video_path)
        if video.exists() and (video.parent / UPLOAD_META_FILE).exists():
            jobs.append((job_id, ch, video))
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()

    jobs = _failed_jobs(args.channel)
    if not jobs:
        log.info("nenhum vídeo pendente de reenvio")
        return

    dead_tokens: set[str] = set()
    for job_id, channel_name, video in jobs:
        if channel_name in dead_tokens:
            continue
        work_dir = video.parent
        meta = json.loads((work_dir / UPLOAD_META_FILE).read_text())
        thumb = work_dir / "thumbnail.jpg"
        channel = ChannelConfig.load(channel_name)
        log.info("[%s] reenviando vídeo de %s: %s", job_id, channel_name, meta["title"])
        try:
            video_id = upload_video(
                channel,
                video,
                title=meta["title"],
                description=meta["description"],
                tags=meta["tags"],
                thumbnail_path=thumb if thumb.exists() else None,
                publish_at=meta.get("publish_at"),
            )
        except RefreshError as exc:
            dead_tokens.add(channel_name)
            log.error(
                "[%s] token do canal %s continua inválido (%s) — rode "
                "scripts/auth_youtube.py --channel %s", job_id, channel_name, exc, channel_name,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — cota/rede: tenta na próxima rodada
            log.error("[%s] reenvio falhou: %s", job_id, exc)
            continue
        update(job_id, status=after_upload_status(channel), youtube_video_id=video_id, error=None)
        log.info("[%s] publicado no reenvio: https://youtu.be/%s", job_id, video_id)
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
