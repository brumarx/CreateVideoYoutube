#!/usr/bin/env python3
"""CLI do pipeline: gera roteiro -> narra -> gera imagens -> monta vídeo ->
thumbnail -> (opcional) upload.

Uso:
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --dry-run
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --publish-at 2026-09-08T12:00:00Z

Formato longo (documentário, 16:9, ~15-20min, sobre um lugar/fenômeno real
específico — ver channels/<nome>.yaml -> long_form_topics):
  python3 scripts/run_pipeline.py --channel curiosidades --long --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import add_background_music, concat_scenes, render_scene
from src.config import ChannelConfig
from src.orchestrator import enqueue, update
from src.script_gen import generate_script
from src.thumbnail import make_thumbnail
from src.tts import narrate
from src.upload import upload_video
from src.visuals import generate_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pipeline")

# Formato curto (Shorts, vertical) — padrão.
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920
SHORT_DURATION_HINT = "3 a 6 minutos"

# Formato longo (documentário, horizontal) — muitas cenas, lugar real
# específico. Ver channels/<nome>.yaml -> long_form_scenes/long_form_topics.
LONG_WIDTH, LONG_HEIGHT = 1920, 1080
LONG_DEFAULT_SCENES = 30
LONG_DURATION_HINT = "15 a 20 minutos"


def run(
    channel_name: str,
    topic: str | None,
    dry_run: bool,
    publish_at: str | None,
    long_form: bool = False,
) -> None:
    channel = ChannelConfig.load(channel_name)

    facts = None
    if channel_name == "politica" and not long_form:
        from src.politica_data import random_fact_set

        facts = random_fact_set()
        if topic is None:
            topic = facts["tema"]
    elif long_form and topic is None:
        topics = channel.long_form_topics
        if not topics:
            raise SystemExit(f"channels/{channel_name}.yaml não tem long_form_topics configurado")
        import random

        topic = random.choice(topics)
    elif topic is None:
        raise SystemExit("--topic é obrigatório pra esse canal")

    width, height = (LONG_WIDTH, LONG_HEIGHT) if long_form else (SHORT_WIDTH, SHORT_HEIGHT)
    scenes = channel.long_form_scenes if long_form else None
    duration_hint = LONG_DURATION_HINT if long_form else SHORT_DURATION_HINT

    job_id = enqueue(channel_name, topic)
    work_dir = Path(__file__).resolve().parent.parent / "output" / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info("[%s] gerando roteiro (%s) para: %s", job_id, "longo" if long_form else "curto", topic)
    script = generate_script(channel, topic, facts, scenes=scenes, duration_hint=duration_hint)
    update(job_id, status="scripted")

    scene_videos = []
    for i, scene in enumerate(script["scenes"]):
        log.info("[%s] cena %d/%d", job_id, i + 1, len(script["scenes"]))
        audio_path = work_dir / f"scene_{i}.mp3"
        narrate(scene["narration"], audio_path, voice=channel.tts_voice)

        # pede a imagem já no formato final do vídeo — pedir quadrado e
        # esticar depois no ffmpeg distorcia e borrava tudo
        image_bytes = generate_image(scene["image_prompt"], width=width, height=height)
        image_path = work_dir / f"scene_{i}.png"
        image_path.write_bytes(image_bytes)

        scene_video_path = work_dir / f"scene_{i}.mp4"
        render_scene(image_path, audio_path, scene_video_path, width=width, height=height)
        scene_videos.append(scene_video_path)

    update(job_id, status="narrated")

    raw_video = work_dir / "raw.mp4"
    concat_scenes(scene_videos, raw_video)

    final_video = work_dir / "final.mp4"
    add_background_music(raw_video, final_video)
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
    parser.add_argument("--topic", default=None, help="obrigatório, exceto pro canal 'politica' (usa dado real aleatório) ou --long (usa long_form_topics)")
    parser.add_argument("--dry-run", action="store_true", help="gera tudo mas não publica")
    parser.add_argument("--publish-at", default=None, help="ISO 8601 UTC, ex: 2026-09-08T12:00:00Z")
    parser.add_argument("--long", action="store_true", help="formato longo/documentário (16:9, ~15-20min, lugar real específico)")
    args = parser.parse_args()

    run(args.channel, args.topic, args.dry_run, args.publish_at, long_form=args.long)


if __name__ == "__main__":
    main()
