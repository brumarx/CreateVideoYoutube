#!/usr/bin/env python3
"""CLI do pipeline: gera roteiro -> narra -> gera imagens -> monta vídeo ->
thumbnail -> (opcional) upload.

Uso:
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --dry-run
  python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --publish-at 2026-09-08T12:00:00Z

Formato longo (documentário, 16:9, ~15-20min, sobre um lugar/fenômeno real
específico — ver channels/<nome>.yaml -> topics):
  python3 scripts/run_pipeline.py --channel curiosidades --long --dry-run

Tema com número no início (ex.: "10 fatos sobre...") no formato curto vira
vídeo de lista: 1 cena por item, com selo de contagem regressiva.
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import _ffprobe_duration, add_background_music, concat_scenes, render_scene
from src.config import ChannelConfig
from src.orchestrator import enqueue, update
from src.script_gen import generate_script
from src.thumbnail import make_thumbnail
from src.topics import pick_topic
from src.tts import narrate
from src.upload import upload_video
from src.visuals import generate_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pipeline")

# Formato curto (Shorts, vertical) — padrão.
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920

# Formato longo (documentário, horizontal) — muitas cenas, lugar real
# específico. Ver channels/<nome>.yaml -> long_form_scenes/long_form_topics.
LONG_WIDTH, LONG_HEIGHT = 1920, 1080

# Vídeo de lista (ex.: "10 fatos sobre..."), só no formato curto: 1 cena =
# 1 item, com selo de contagem regressiva gravado na cena (ver
# src/assemble.py). Nunca mais que isso — vídeo de "30 fatos" vira maçante
# e o selo de card fica com número gigante demais pra manter elegante.
LIST_TOPIC_RE = re.compile(r"^(\d+)\s")
MAX_LIST_ITEMS = 10

# Capítulos (timestamps na descrição, "0:00 ..." etc.) só no formato longo —
# no curto (poucos minutos) não faz sentido. YouTube exige pelo menos 3
# marcações, a primeira em 0:00, e no mínimo 10s entre cada uma. Título de
# cada capítulo vem das primeiras palavras da narração daquela cena (gerado
# por código, não pelo LLM — não adiciona campo novo obrigatório no JSON,
# que já teve problema de robustez em roteiros longos).
MIN_CHAPTER_GAP_SECONDS = 10
MAX_CHAPTERS = 8


def _chapter_label(narration: str, max_words: int = 6) -> str:
    words = narration.strip().split()[:max_words]
    label = " ".join(words).rstrip(",.;:!?-")
    return label[:1].upper() + label[1:] if label else "Continua"


def _build_chapters(scenes: list[dict], durations: list[float]) -> str:
    n = len(scenes)
    if n < 3 or len(durations) != n:
        return ""

    n_chapters = min(MAX_CHAPTERS, max(3, n // 4))
    group_size = math.ceil(n / n_chapters)

    lines = []
    cumulative = 0.0
    last_ts = -MIN_CHAPTER_GAP_SECONDS
    idx = 0
    while idx < n:
        if cumulative - last_ts >= MIN_CHAPTER_GAP_SECONDS or not lines:
            mm, ss = divmod(int(cumulative), 60)
            lines.append(f"{mm}:{ss:02d} {_chapter_label(scenes[idx]['narration'])}")
            last_ts = cumulative
        group_end = min(idx + group_size, n)
        cumulative += sum(durations[idx:group_end])
        idx = group_end

    return "\n".join(lines) if len(lines) >= 3 else ""


def run(
    channel_name: str,
    topic: str | None,
    dry_run: bool,
    publish_at: str | None,
    long_form: bool = False,
    fact_label: str | None = None,
) -> None:
    channel = ChannelConfig.load(channel_name)
    # 1 voz sorteada por vídeo (não por cena — narrador tem que ser
    # consistente do início ao fim), conforme os pesos configurados no
    # painel. Antes era sempre a mesma voz fixa por canal.
    voices, weights = zip(*channel.tts_voice_weights.items())
    tts_voice = random.choices(voices, weights=weights, k=1)[0]

    facts = None
    if channel_name == "politica" and not long_form:
        from src.politica_data import pick_fact_set, random_fact_set

        # --fact-label força um tema específico (teste manual/painel) em vez
        # de sortear entre os fetchers.
        facts = pick_fact_set(fact_label) if fact_label else random_fact_set()
        if topic is None:
            topic = facts["tema"]
    elif topic is None and channel.topics:
        # nunca repete um tema já usado — quando a lista fixa esgota, gera
        # um tema novo via LLM dentro do nicho do canal (ver src/topics.py).
        # Fila única pra curto e longo: só sai 1 vídeo/dia por canal mesmo.
        topic = pick_topic(channel_name, channel.topics, channel.niche)
    elif topic is None:
        raise SystemExit(f"channels/{channel_name}.yaml não tem topics configurado e --topic não foi passado")

    list_count = None
    if not long_form:
        m = LIST_TOPIC_RE.match(topic or "")
        if m:
            list_count = min(int(m.group(1)), MAX_LIST_ITEMS)
            if list_count != int(m.group(1)):
                # corrige o texto também (título/roteiro têm que bater com
                # a contagem real de cenas/selos, não o número original)
                topic = f"{list_count}{topic[m.end(1):]}"

    width, height = (LONG_WIDTH, LONG_HEIGHT) if long_form else (SHORT_WIDTH, SHORT_HEIGHT)
    scenes = channel.long_form_scenes if long_form else list_count
    min_minutes = channel.long_min_minutes if long_form else channel.short_min_minutes
    max_minutes = channel.long_max_minutes if long_form else channel.short_max_minutes

    job_id = enqueue(channel_name, topic)
    work_dir = Path(__file__).resolve().parent.parent / "output" / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info("[%s] gerando roteiro (%s) para: %s (voz: %s)", job_id, "longo" if long_form else "curto", topic, tts_voice)
    script = generate_script(channel, topic, facts, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes)
    update(job_id, status="scripted")

    scene_videos = []
    scene_durations = []
    for i, scene in enumerate(script["scenes"]):
        log.info("[%s] cena %d/%d", job_id, i + 1, len(script["scenes"]))
        audio_path = work_dir / f"scene_{i}.mp3"
        narrate(scene["narration"], audio_path, voice=tts_voice)
        scene_durations.append(_ffprobe_duration(audio_path))

        # pede a imagem já no formato final do vídeo — pedir quadrado e
        # esticar depois no ffmpeg distorcia e borrava tudo
        image_bytes = generate_image(scene["image_prompt"], width=width, height=height)
        image_path = work_dir / f"scene_{i}.png"
        image_path.write_bytes(image_bytes)

        list_number = (list_count - i) if list_count else None
        scene_video_path = work_dir / f"scene_{i}.mp4"
        render_scene(
            image_path, audio_path, scene_video_path, width=width, height=height,
            watermark=channel.watermark, list_number=list_number, accent=channel.accent,
            caption=scene["narration"] if channel.captions else None,
        )
        scene_videos.append(scene_video_path)

    update(job_id, status="narrated")

    raw_video = work_dir / "raw.mp4"
    # crossfade obriga reencodar o vídeo inteiro — caro numa CPU fraca sem
    # encoder de hardware (Pi 5). Vale a pena pro curto (poucos minutos);
    # no longo (15-20min) usa corte seco instantâneo (ver src/assemble.py).
    concat_scenes(scene_videos, raw_video, crossfade=not long_form)

    final_video = work_dir / "final.mp4"
    add_background_music(raw_video, final_video)
    update(job_id, status="rendered", video_path=str(final_video))
    log.info("[%s] vídeo pronto: %s", job_id, final_video)

    # campos dedicados de thumbnail (LLM às vezes esquece com modelo fraco
    # da cascata) — cai pro título/cena 1 se faltar, nunca quebra o vídeo
    # por causa só da thumbnail.
    thumb_prompt = script.get("thumbnail_image_prompt") or script["scenes"][0]["image_prompt"]
    thumb_text = script.get("thumbnail_text") or script["title"]
    thumb_path = work_dir / "thumbnail.jpg"
    # quando o fato citado tem foto OFICIAL da pessoa (deputado/senador/
    # magistrado — ver src/politica_data.py), usa a foto de verdade em vez
    # de pedir pra IA inventar o rosto: mais preciso e sem risco de gerar
    # cara errada atribuída a alguém real.
    real_photo_url = None
    if facts and facts.get("dados"):
        real_photo_url = facts["dados"][0].get("foto_url") or None
    make_thumbnail(thumb_prompt, thumb_text, thumb_path, accent=channel.accent, real_photo_url=real_photo_url)
    update(job_id, thumbnail_path=str(thumb_path))
    log.info("[%s] thumbnail pronta: %s", job_id, thumb_path)

    description = script["description"]
    if long_form:
        # timestamps calculados a partir da duração real de cada narração
        # (não estimados) — só faz sentido no documentário, o curto é curto
        # demais pra precisar de capítulo.
        chapters = _build_chapters(script["scenes"], scene_durations)
        if chapters:
            description = chapters + "\n\n" + description

    if channel_name == "politica":
        # link fixo pro site fonte dos dados — sempre gerado por código
        # (nunca pelo LLM), pra garantir que aponta pro lugar certo sempre.
        description += "\n\nFonte dos dados: https://brmx.org/politica/"

    # hashtags fixas do canal (config, não LLM) — 3-5 é o recomendado hoje
    # em dia (mais que isso o YouTube ignora todas); os 3 primeiros hashtags
    # que aparecem na descrição saem exibidos acima do título. #Shorts só
    # entra no formato curto (é o que classifica o vídeo pra prateleira de
    # Shorts) — no formato longo não faz sentido.
    if channel.youtube_handle:
        # CTA de inscrição — link direto de "increva-se" (sub_confirmation=1
        # abre o popup de inscrição na hora). Retenção de sessão/inscritos é
        # sinal de recomendação do YouTube; gerado por código pra sempre
        # apontar pro canal certo.
        description += (
            f"\n\n👉 Inscreva-se pra não perder o próximo vídeo: "
            f"https://www.youtube.com/{channel.youtube_handle}?sub_confirmation=1"
        )

    hashtags = (["#Shorts"] if not long_form else []) + list(channel.hashtags)
    if hashtags:
        description += "\n\n" + " ".join(hashtags)

    if dry_run:
        log.info("[%s] --dry-run: não vou publicar. Revise %s manualmente.", job_id, final_video)
        return

    video_id = upload_video(
        channel,
        final_video,
        title=script["title"],
        description=description,
        tags=script["tags"],
        thumbnail_path=thumb_path,
        publish_at=publish_at,
    )
    update(job_id, status="uploaded", youtube_video_id=video_id)
    log.info("[%s] publicado: https://youtu.be/%s", job_id, video_id)

    # já está no YouTube — não precisa mais guardar os arquivos (vídeo longo
    # sozinho passa de 400MB, HD ia encher rápido rodando todo dia)
    shutil.rmtree(work_dir, ignore_errors=True)
    log.info("[%s] arquivos locais removidos (%s)", job_id, work_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--topic", default=None, help="obrigatório, exceto pro canal 'politica' no curto (usa dado real aleatório) ou quando channels/<nome>.yaml tem topics (escolhe sozinho)")
    parser.add_argument("--dry-run", action="store_true", help="gera tudo mas não publica")
    parser.add_argument("--publish-at", default=None, help="ISO 8601 UTC, ex: 2026-09-08T12:00:00Z")
    parser.add_argument("--long", action="store_true", help="formato longo/documentário (16:9, ~15-20min, lugar real específico)")
    parser.add_argument("--fact-label", default=None, help="só canal 'politica' no curto: força um tema específico de src.politica_data.FACT_FETCHERS em vez de sortear")
    args = parser.parse_args()

    run(args.channel, args.topic, args.dry_run, args.publish_at, long_form=args.long, fact_label=args.fact_label)


if __name__ == "__main__":
    main()
