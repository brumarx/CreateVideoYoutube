#!/usr/bin/env python3
"""CLI do pipeline: gera roteiro -> narra -> gera imagens -> monta vídeo ->
thumbnail -> (opcional) upload.

Uso:
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --dry-run
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --publish-at 2026-09-08T12:00:00Z
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import concat_scenes, render_scene
from src.config import ChannelConfig
from src.orchestrator import enqueue, update
from src.script_gen import generate_script
from src.thumbnail import make_thumbnail
from src.tts import narrate
from src.upload import upload_video
from src.visuals import generate_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pipeline")


def run(channel_name: str, topic: str | None, dry_run: bool, publish_at: str | None) -> None:
    channel = ChannelConfig.load(channel_name)

    facts = None
    if channel_name == "politica":
        from src.politica_data import random_fact_set

        facts = random_fact_set()
        if topic is None:
            topic = facts["tema"]
    elif topic is None:
        raise SystemExit("--topic é obrigatório pra esse canal")

    job_id = enqueue(channel_name, topic)
    work_dir = Path(__file__).resolve().parent.parent / "output" / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info("[%s] gerando roteiro para: %s", job_id, topic)
    script = generate_script(channel, topic, facts)
    update(job_id, status="scripted")

    scene_videos = []
    for i, scene in enumerate(script["scenes"]):
        log.info("[%s] cena %d/%d", job_id, i + 1, len(script["scenes"]))
        audio_path = work_dir / f"scene_{i}.mp3"
        narrate(scene["narration"], audio_path, voice=channel.tts_voice)

        # pede a imagem já no formato vertical do vídeo (9:16) — pedir
        # quadrado e esticar depois no ffmpeg distorcia e borrava tudo
        image_bytes = generate_image(scene["image_prompt"], width=1080, height=1920)
        image_path = work_dir / f"scene_{i}.png"
        image_path.write_bytes(image_bytes)

        scene_video_path = work_dir / f"scene_{i}.mp4"
        render_scene(image_path, audio_path, scene_video_path)
        scene_videos.append(scene_video_path)

    update(job_id, status="narrated")

    final_video = work_dir / "final.mp4"
    concat_scenes(scene_videos, final_video)
    update(job_id, status="rendered", video_path=str(final_video))
    log.info("[%s] vídeo pronto: %s", job_id, final_video)

    thumb_prompt = script["scenes"][0]["image_prompt"]
    thumb_path = work_dir / "thumbnail.jpg"
    make_thumbnail(thumb_prompt, script["title"], thumb_path)
    update(job_id, thumbnail_path=str(thumb_path))
    log.info("[%s] thumbnail pronta: %s", job_id, thumb_path)

    if dry_run:
        log.info("[%s] --dry-run: não vou publicar. Revise %s manualmente.", job_id, final_video)
        return

    video_id = upload_video(
        channel,
        final_video,
        title=script["title"],
        description=script["description"],
        tags=script["tags"],
        thumbnail_path=thumb_path,
        publish_at=publish_at,
    )
    update(job_id, status="uploaded", youtube_video_id=video_id)
    log.info("[%s] publicado: https://youtu.be/%s", job_id, video_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--topic", default=None, help="obrigatório, exceto pro canal 'politica' (usa dado real aleatório se omitido)")
    parser.add_argument("--dry-run", action="store_true", help="gera tudo mas não publica")
    parser.add_argument("--publish-at", default=None, help="ISO 8601 UTC, ex: 2026-09-08T12:00:00Z")
    args = parser.parse_args()

    run(args.channel, args.topic, args.dry_run, args.publish_at)


if __name__ == "__main__":
    main()
