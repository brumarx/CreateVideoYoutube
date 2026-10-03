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
import json
import logging
import math
import random
import re
import shutil
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import SFX_DIR, _ffprobe_duration, add_background_music, concat_scenes, render_scene
from src.config import ChannelConfig
from src.fact_check import BLOCKING_TYPES, feedback_for_rewrite, review_script
from src.orchestrator import enqueue, update
from src.script_gen import engagement_question, generate_script
from src.stock_media import search_stock_clip, search_stock_photo
from src.thumbnail import make_thumbnail, photo_scene_frame
from src.topics import pick_topic
from src.tts import narrate
from src.visual_check import matches_scene
from src.upload import UPLOAD_META_FILE, after_upload_status, post_comment, top_channel_videos, upload_video
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
# reescritas do roteiro depois de reprovado na revisão de fatos
FACT_CHECK_REWRITES = 2

# Capítulos (timestamps na descrição, "0:00 ..." etc.) só no formato longo —
# no curto (poucos minutos) não faz sentido. YouTube exige pelo menos 3
# marcações, a primeira em 0:00, e no mínimo 10s entre cada uma. Título de
# cada capítulo vem das primeiras palavras da narração daquela cena (gerado
# por código, não pelo LLM — não adiciona campo novo obrigatório no JSON,
# que já teve problema de robustez em roteiros longos).
MIN_CHAPTER_GAP_SECONDS = 10

# Roteiro reprovado na revisão de fatos mesmo depois das reescritas e do
# plano B: sai com este código (e não 1) pro scripts/daily_run.py saber que
# vale tentar o canal de novo — o tema/dado/notícia usado já ficou marcado,
# então a nova rodada pega outro. Falha de render/upload continua exit 1
# (upload falho é do scripts/retry_uploads.py, nunca gerar vídeo duplicado).
EXIT_SCRIPT_REJECTED = 3


class ScriptRejected(RuntimeError):
    pass
MAX_CHAPTERS = 8


def _chapter_label(narration: str, max_words: int = 6) -> str:
    words = narration.strip().split()[:max_words]
    label = " ".join(words).rstrip(",.;:!?-")
    return label[:1].upper() + label[1:] if label else "Continua"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 3}


_FEMININO = {"feminino", "feminina", "femininas", "meninas", "mulheres", "gloriosas"}


def _news_photo_for_scene(narration: str, facts: dict | None, photos: list[dict]) -> dict | None:
    """Foto da notícia que ESTA cena narra — antes era rodízio (cena i = foto
    i), e a foto de uma notícia aparecia enquanto a narração falava de
    outra. Só conta palavra EXCLUSIVA daquela notícia (que não aparece nas
    outras do mesmo vídeo): "copa", "sub", "brasil" em comum já fizeram a
    foto do sub-20 feminino entrar na cena do sub-20 masculino. Notícia de
    time feminino nunca ilustra cena que não fala de feminino. Sem notícia
    reconhecível na fala (abertura genérica, fechamento), devolve None e a
    cena segue a cascata normal de imagem."""
    noticias = (facts or {}).get("noticias") or []
    fala = _words(narration) - {"botafogo", "fogão", "alvinegro", "torcida", "time", "clube"}
    textos = [_words(f"{n.get('titulo', '')} {n.get('resumo', '')}") for n in noticias]
    melhor, melhor_score = None, 1  # pelo menos 2 palavras exclusivas em comum
    for idx, palavras in enumerate(textos):
        if palavras & _FEMININO and not fala & _FEMININO:
            continue
        outras = set().union(*(t for j, t in enumerate(textos) if j != idx))
        score = len(fala & (palavras - outras))
        if score > melhor_score:
            melhor, melhor_score = idx, score
    if melhor is None:
        return None
    return next((f for f in photos if f.get("noticia") == melhor), None)


def _sized_for_facts(facts: dict, scenes: int | None, min_minutes: float, max_minutes: float) -> tuple[int | None, float, float]:
    """Vídeo do tamanho dos dados: o longo do politica pedia 24 cenas e
    4-5 min a partir de 5 linhas do banco — pra encher, o roteirista somava
    valores, comparava com salário mínimo/loteria/médico e chamava de
    "corrupção", e a revisão reprovava (1 vídeo publicado em 5 dias)."""
    n = len(facts.get("dados") or [])
    if not n or scenes is None:
        return scenes, min_minutes, max_minutes
    return min(scenes, 2 * n + 2), min(min_minutes, 1.5), min(max_minutes, 3)


def _clip_still(path: Path) -> bytes | None:
    """Frame limpo (sem legenda) do clipe aprovado — reserva pra repetir se
    uma cena seguinte ficar sem imagem."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(path), "-frames:v", "1", "-f", "image2", "-c:v", "png", "pipe:1"],
        capture_output=True, check=False,
    ).stdout
    return out or None


def _chapter_starts(n: int) -> list[int]:
    """Índice da cena que abre cada capítulo — o mesmo agrupamento serve pros
    timestamps da descrição e pro selo "Parte X de Y" na tela."""
    if n < 3:
        return []
    n_chapters = min(MAX_CHAPTERS, max(3, n // 4))
    group_size = math.ceil(n / n_chapters)
    return list(range(0, n, group_size))


def _build_chapters(scenes: list[dict], durations: list[float]) -> str:
    n = len(scenes)
    if n < 3 or len(durations) != n:
        return ""

    lines = []
    last_ts = -MIN_CHAPTER_GAP_SECONDS
    for idx in _chapter_starts(n):
        cumulative = sum(durations[:idx])
        if cumulative - last_ts >= MIN_CHAPTER_GAP_SECONDS or not lines:
            mm, ss = divmod(int(cumulative), 60)
            lines.append(f"{mm}:{ss:02d} {_chapter_label(scenes[idx]['narration'])}")
            last_ts = cumulative

    return "\n".join(lines) if len(lines) >= 3 else ""


def run(
    channel_name: str,
    topic: str | None,
    dry_run: bool,
    publish_at: str | None,
    long_form: bool | None = None,
    fact_label: str | None = None,
    botafogo_task: str | None = None,
) -> None:
    channel = ChannelConfig.load(channel_name)
    # guardado ANTES de qualquer auto-preenchimento abaixo — só um tema
    # digitado por uma pessoa de verdade deve disparar busca na internet
    # (ver web_facts mais abaixo); tema vindo da fila/banco interno já tem
    # grounding próprio, buscar de novo seria redundante.
    user_provided_topic = topic is not None
    # None = respeita o que está configurado no painel (channels/<nome>.yaml
    # -> daily_format); só quem passar --long/--no-long explícito na linha de
    # comando força um formato pontual diferente do que a UI tem salvo.
    # Guardado ANTES do default abaixo: o canal "botafogo" decide o formato
    # pelo TIPO de tarefa (pré-jogo/pós-jogo), mas um --long/--no-long
    # explícito na linha de comando (teste manual) ainda tem prioridade.
    user_forced_format = long_form is not None
    if long_form is None:
        long_form = channel.daily_format == "long"
    # 1 voz sorteada por vídeo (não por cena — narrador tem que ser
    # consistente do início ao fim), conforme os pesos configurados no
    # painel. Antes era sempre a mesma voz fixa por canal.
    voices, weights = zip(*channel.tts_voice_weights.items())
    tts_voice = random.choices(voices, weights=weights, k=1)[0]

    facts = None
    # vídeo de notícias do Botafogo: fotos reais das próprias matérias (no
    # lugar de filmagem genérica) + links das matérias pra descrição
    news_photos: list[dict] = []
    news_sources: list[dict] = []
    # curto/longo é só formato e duração — não decide sozinho se usa dado
    # real do banco. No curto, SEMPRE usa (random_fact_set/pick_fact_set);
    # no longo, só usa quando fact_label foi passado explicitamente (senão
    # segue o design original: tema genérico de "como o sistema funciona",
    # sem grounding de um fato só pra sustentar 15-20min).
    if channel_name == "politica" and (not long_form or fact_label):
        from src.politica_data import pick_fact_set, random_fact_set

        # --fact-label força um tema específico (teste manual/painel) em vez
        # de sortear entre os fetchers.
        facts = pick_fact_set(fact_label) if fact_label else random_fact_set()
        if topic is None:
            topic = facts["tema"]
    elif channel_name == "botafogo":
        from src.botafogo_data import daily_portal_task, next_pending_task

        # Botafogo não joga todo dia — sem prévia ou pós-jogo pendente,
        # não gera vídeo nenhum hoje (silêncio, não erro; ver
        # src/botafogo_data.py). O `return` acontece ANTES do `enqueue()`
        # abaixo, então não cria job nenhum no banco nesses dias.
        # daily_run chama 2x: "jogo" (prévia/pós-jogo, silêncio sem jogo) e
        # "portal" (1 vídeo/dia com temas do botafogo.win). Sem a opção
        # (execução manual): jogo se houver, senão notícias.
        if botafogo_task == "portal":
            # longo precisa de mais notícia de verdade pra 4-6min — com
            # poucas o LLM estica inventando detalhe e a revisão barra
            task = daily_portal_task(per_video=8 if long_form else 5)
        else:
            task = next_pending_task(news_fallback=botafogo_task != "jogo")
        if task is None:
            log.info("[botafogo] nada a publicar (%s)", botafogo_task or "jogo/notícias")
            return
        facts = task["facts"]
        news_photos = task.get("fotos") or []
        news_sources = task.get("fontes") or []
        if topic is None:
            topic = task["titulo"]
        if not user_forced_format:
            # prévia sempre curta, pós-jogo sempre longo; notícias do dia
            # (botafogo.win) seguem o formato escolhido no painel
            long_form = task["tipo"] == "pos-jogo" or (
                task["tipo"] == "noticias" and channel.daily_format == "long"
            )
    elif topic is None and channel.topics:
        # nunca repete um tema já usado — quando a lista fixa esgota, gera
        # um tema novo via LLM dentro do nicho do canal (ver src/topics.py).
        # Fila única pra curto e longo: só sai 1 vídeo/dia por canal mesmo.
        # Parte dos temas sai inspirada no que está viral no YouTube no mês
        # (channels/<nome>.yaml -> viral_queries/viral_share).
        topic = pick_topic(
            channel_name, channel.topics, channel.niche,
            viral_queries=channel.viral_queries, viral_share=channel.viral_share,
        )
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
    if long_form and channel_name == "politica" and facts:
        scenes, min_minutes, max_minutes = _sized_for_facts(facts, scenes, min_minutes, max_minutes)
    if long_form and facts and facts.get("tipo") == "noticias" and scenes:
        # dia de notícias do Botafogo: ~40s por notícia. Com 3 notícias e
        # meta fixa de 4 min, o roteiro repetia metade do vídeo (job 222).
        n = len(facts.get("noticias") or [])
        if n:
            scenes = min(scenes, 3 * n + 2)
            min_minutes = min(min_minutes, max(1.5, 0.6 * n))

    job_id = enqueue(channel_name, topic)
    work_dir = Path(__file__).resolve().parent.parent / "output" / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:

        # Tema digitado por uma pessoa (não vindo da fila nem do banco interno)
        # pode ser sobre qualquer coisa — inclusive um evento real específico ou
        # pessoa real nomeada, onde "conhecimento geral" do LLM não é confiável
        # o bastante (pode estar desatualizado/errado). Busca fato real com
        # fonte antes de escrever, em vez de recusar o tema ou arriscar
        # inventar (ver src/web_search.py). Sem TAVILY_API_KEYS configurada,
        # cai pro comportamento antigo sem bloquear nada.
        # politica também busca pra tema da fila/viral: são sempre sobre
        # instituição/pessoa real e fato recente (ex.: gastos do STF), onde o
        # LLM sozinho inventava número e a revisão de fatos barrava o
        # roteiro (3 falhas em 5 dias, jobs 139/146/157/161).
        web_facts = None
        if (user_provided_topic or channel_name == "politica") and facts is None:
            from src.web_search import search_topic_facts

            web_facts = search_topic_facts(topic)
            if web_facts:
                log.info("[%s] tema digitado ancorado com %d fonte(s) da internet", job_id, len(web_facts))
            else:
                log.info("[%s] tema digitado sem busca na internet (sem chave configurada ou sem resultado)", job_id)

        log.info("[%s] gerando roteiro (%s) para: %s (voz: %s)", job_id, "longo" if long_form else "curto", topic, tts_voice)
        script = generate_script(
            channel, topic, facts, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes, web_facts=web_facts,
        )
        # revisão de fatos ANTES de gastar TTS/render (ver src/fact_check.py):
        # reprovado, reescreve com o feedback do revisor; se continuar
        # reprovado, o vídeo não sai — melhor um dia sem vídeo do que um
        # tutorial de ferramenta inventada ou acusação sem dado no ar.
        for attempt in range(FACT_CHECK_REWRITES + 1):
            problems = review_script(script, topic, facts, web_facts)
            if problems is None:
                # revisor fora do ar: tenta 1x de novo; sem revisão o vídeo
                # NÃO sai (não existe mais aprovação manual depois)
                problems = review_script(script, topic, facts, web_facts)
                if problems is None:
                    raise ScriptRejected("revisor de fatos indisponível — vídeo não sai sem revisão")
            if not problems:
                break
            log.warning(
                "[%s] revisão de fatos reprovou (tentativa %d): %s", job_id, attempt + 1,
                "; ".join(f"{p.get('tipo')}: {p.get('termo')}" for p in problems),
            )
            if attempt == FACT_CHECK_REWRITES:
                if not any(p.get("tipo") in BLOCKING_TYPES for p in problems):
                    log.warning("[%s] só sobraram ressalvas leves da revisão — segue pra aprovação", job_id)
                    break
                if channel_name == "politica" and facts is None and not user_provided_topic:
                    # tema da fila/viral sem dado que sustente: em vez de
                    # passar o dia sem vídeo, troca por um fato REAL do banco
                    # (mesmo caminho do formato curto, já com fonte).
                    from src.politica_data import random_fact_set

                    facts = random_fact_set()
                    topic = facts["tema"]
                    web_facts = None
                    scenes, min_minutes, max_minutes = _sized_for_facts(facts, scenes, min_minutes, max_minutes)
                    log.warning("[%s] tema reprovado — trocando por dado real do banco: %s", job_id, topic)
                    update(job_id, topic=topic)
                    script = generate_script(
                        channel, topic, facts, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes,
                    )
                    problems = review_script(script, topic, facts, None)
                    if problems == []:  # None = revisor fora do ar: não aprova
                        break
                elif channel.topics and facts is None and not user_provided_topic:
                    # demais canais: tema (quase sempre o viral — jobs
                    # 142/151 do curiosidades) sem base confirmável → troca
                    # por outro da lista fixa, sem viral, e tenta 1 vez. No
                    # curto, só tema sem número (selo de lista já foi
                    # calculado pro tema original).
                    novo = pick_topic(channel_name, channel.topics, channel.niche)
                    if long_form or not LIST_TOPIC_RE.match(novo):
                        topic, web_facts = novo, None
                        log.warning("[%s] tema reprovado — trocando por outro da fila: %s", job_id, topic)
                        update(job_id, topic=topic)
                        script = generate_script(
                            channel, topic, None, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes,
                        )
                        problems = review_script(script, topic, None, None)
                        if problems == []:
                            break
                raise ScriptRejected(
                    "roteiro reprovado na revisão de fatos: "
                    + "; ".join(f"{p.get('termo')} ({p.get('motivo')})" for p in (problems or [{"termo": "revisor de fatos", "motivo": "indisponível"}]))
                )
            script = generate_script(
                channel, topic, facts, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes,
                web_facts=web_facts, revision_feedback=feedback_for_rewrite(problems),
            )
        update(job_id, status="scripted")

        # CTA falado (like + se inscrever) — gerado por CÓDIGO, nunca pelo LLM,
        # pra nunca faltar. Antes só existia um link na descrição, que quase
        # ninguém lê assistindo; pedido em voz alta no fim converte muito mais.
        # Cena extra depois das da LLM, passa pela mesma pipeline de
        # renderização sem precisar de tratamento especial.
        original_scene_count = len(script["scenes"])
        script["scenes"].append({
            "narration": (
                f"As notícias completas, com todos os detalhes, estão no site "
                f"{channel.website.split('//')[-1]}. Deixa o like e se inscreve no canal pra não perder o próximo."
                if channel.website else
                f"Se esse vídeo te ajudou, deixa o like e se inscreve no {channel.channel_title} pra não perder o próximo."
            ),
            "image_prompt": (
                "Close-up of a hand giving a thumbs up gesture, warm natural lighting, "
                "genuine happy mood, no text, no words, no letters, no numbers, no logos, "
                "no UI, no buttons, no watermark, no signs, no signage, no plaques, no "
                "banners, no billboards"
            ),
            "stock_query": "thumbs up hand gesture",
        })

        # selo "Parte X de Y" na tela (só formato longo) no começo de cada
        # capítulo — marco visível de progresso pra segurar a retenção no
        # meio do vídeo. O 1º capítulo não ganha selo (não cobre o gancho de
        # abertura) e a cena de CTA também não.
        chapter_banners: dict[int, dict] = {}
        if long_form:
            starts = [idx for idx in _chapter_starts(len(script["scenes"])) if idx < original_scene_count]
            for n_part, idx in enumerate(starts, start=1):
                if idx > 0:
                    chapter_banners[idx] = {"index": n_part, "total": len(starts)}

        visual_context = f"Canal: {channel.channel_title} — {channel.niche}.\nTema do vídeo: {topic}."
        scene_videos = []
        scene_durations = []
        last_news_photo: dict | None = None
        last_still: bytes | None = None  # último visual aprovado (reserva se a IA de imagem cair)
        for i, scene in enumerate(script["scenes"]):
            log.info("[%s] cena %d/%d", job_id, i + 1, len(script["scenes"]))
            audio_path = work_dir / f"scene_{i}.mp3"
            _, word_boundaries = narrate(scene["narration"], audio_path, voice=tts_voice)

            # toda imagem/clipe real passa por um modelo de visão antes de
            # entrar: tem que combinar com o que ESTA cena narra (vídeos de
            # 03/10: escudo do Barcelona, futebol americano, camisa do
            # Beşiktaş num vídeo do Botafogo). None = checagem fora do ar:
            # aceita (o filtro de outro esporte/clube do stock_media segue).
            def in_context(image: bytes, _narration: str = scene["narration"]) -> bool | None:
                return matches_scene(image, _narration, visual_context)
            scene_durations.append(_ffprobe_duration(audio_path))

            # Cascata de conteúdo visual, do mais vivo/crível pro último recurso:
            # 1) filmagem REAL (Pexels) — muito mais viva que imagem com zoom;
            # 2) foto REAL (Pexels) — pra quando o assunto não tem clipe mas
            #    tem foto (ex.: objeto específico, evento, foto histórica);
            # 3) imagem gerada por IA — só quando nada real foi encontrado.
            # Sem PEXELS_API_KEYS configurada, ou sem resultado, cada nível cai
            # pro próximo automaticamente.
            #
            # stock_query ausente (None/chave faltando) é a cascata de
            # modelos gratuitos caindo num modelo mais fraco que ignora
            # campo extra do schema (visto de verdade: roteiro veio sem
            # stock_query nenhum, mesmo num tema perfeito pra vídeo real) —
            # nesse caso vale tentar buscar mesmo assim, com as primeiras
            # palavras do image_prompt (esse sim sempre vem preenchido) como
            # busca de reserva. Já stock_query == "" (string vazia, não
            # None) é _sanitize_person_images desativando a busca de
            # PROPÓSITO (cena sobre pessoa real — nunca buscar filmagem
            # usando o contexto dela); tratar os dois casos igual reabriria
            # esse buraco de segurança E, na prática, mandava o texto do
            # _SAFE_FALLBACK_IMAGE_PROMPT pro Pexels como se fosse busca de
            # verdade (visto nos logs: "symbolic wide shot related to the
            # story, no" virou clipe repetido dezenas de vezes).
            raw_stock_query = scene.get("stock_query")
            if raw_stock_query is None:
                stock_query = " ".join(scene["image_prompt"].split()[:8])
            else:
                stock_query = raw_stock_query or None

            # notícia do Botafogo: foto real da matéria, em rodízio entre as
            # cenas (a de CTA no fim segue a cascata normal)
            # Cena sem palavra própria da notícia quase sempre CONTINUA a da
            # cena anterior ("O comunicado termina com...") — fica com a
            # foto dela. Antes caía no banco de vídeos, que pra futebol só
            # tem clube estrangeiro (Barcelona, Beşiktaş, Wolfsburg).
            news_frame = None
            if news_photos and i < original_scene_count:
                photo = _news_photo_for_scene(scene["narration"], facts, news_photos) or last_news_photo
                if photo:
                    frame = photo_scene_frame(photo["url"], width, height)
                    if frame is not None and in_context(frame) is not False:
                        last_news_photo = photo
                        news_frame = frame

            stock_clip_path = None
            if news_frame is None and stock_query:
                stock_clip_path = search_stock_clip(stock_query, width, height, check=in_context)

            image_path = None
            if news_frame is not None:
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(news_frame)
                last_still = news_frame
            elif stock_clip_path is None:
                image_bytes = search_stock_photo(stock_query, width, height, check=in_context) if stock_query else None
                if image_bytes is None:
                    # imagem de IA sai do prompt da PRÓPRIA cena — no contexto
                    # por construção. Pede já no formato final do vídeo (pedir
                    # quadrado e esticar no ffmpeg distorcia e borrava tudo).
                    try:
                        image_bytes = generate_image(scene["image_prompt"], width=width, height=height)
                        if in_context(image_bytes) is False:
                            # IA também erra (jogador inventado de uniforme
                            # com escudo falso, job 222): 2ª chance, depois
                            # repete o último visual aprovado
                            image_bytes = generate_image(scene["image_prompt"] + ", no people, no players, no uniforms", width=width, height=height)
                            if in_context(image_bytes) is False:
                                raise ValueError("imagem de IA fora de contexto")
                    except Exception:
                        # IA de imagem fora (Pollinations 402/500): repete o
                        # último visual JÁ APROVADO do vídeo em vez de derrubar
                        # o job ou pôr imagem sem checagem
                        if last_still is None:
                            raise
                        log.warning("[%s] cena %d sem imagem no contexto — repetindo a anterior", job_id, i + 1)
                        image_bytes = last_still
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(image_bytes)
                last_still = image_bytes
            else:
                last_still = _clip_still(stock_clip_path) or last_still

            # i < original_scene_count: a cena de CTA (adicionada por código,
            # depois das da LLM) nunca é uma das cenas numeradas da lista.
            list_number = (list_count - i) if list_count and i < original_scene_count else None
            scene_video_path = work_dir / f"scene_{i}.mp4"
            try:
                render_scene(
                    image_path, audio_path, scene_video_path, width=width, height=height,
                    watermark=channel.watermark, list_number=list_number, accent=channel.accent,
                    caption=scene["narration"] if channel.captions else None,
                    word_boundaries=word_boundaries if channel.captions else None,
                    stat_overlay=scene.get("stat_overlay"),
                    impact_beat=bool(scene.get("impact_beat")),
                    video_path=stock_clip_path,
                    chapter_banner=chapter_banners.get(i),
                )
            finally:
                # sem finally, um clipe baixado (10-20MB) vaza pro /tmp toda vez
                # que o ffmpeg falhar (já visto de verdade nesta sessão — erro de
                # rede, arquivo truncado) — acumula disco silenciosamente rodando
                # todo dia em 5 canais.
                if stock_clip_path is not None:
                    stock_clip_path.unlink(missing_ok=True)
            scene_videos.append(scene_video_path)

        update(job_id, status="narrated")

        raw_video = work_dir / "raw.mp4"
        # crossfade obriga reencodar o vídeo inteiro — caro numa CPU fraca sem
        # encoder de hardware (Pi 5). Vale a pena pro curto (poucos minutos);
        # no longo (15-20min) usa corte seco instantâneo (ver src/assemble.py).
        concat_scenes(
            scene_videos, raw_video, crossfade=not long_form, sfx_path=SFX_DIR / "whoosh.mp3",
        )

        final_video = work_dir / "final.mp4"
        _, music_attribution = add_background_music(raw_video, final_video, mood=script.get("mood"))
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
        if news_photos:
            # foto da notícia que o gancho/título citam — news_photos[0] às
            # vezes era de outra notícia (gancho "PAULINHO VOLTOU" com foto do
            # time feminino). Sem casar com nenhuma, a IA gera a imagem.
            photo = _news_photo_for_scene(f"{script['title']} {thumb_text}", facts, news_photos)
            real_photo_url = photo["url"] if photo else None
        elif facts and facts.get("dados"):
            real_photo_url = facts["dados"][0].get("foto_url") or None
        elif channel_name == "politica" and user_provided_topic:
            # tema digitado à mão (sem `facts` do banco) pode citar alguém que
            # JÁ está cadastrado com foto oficial (deputado/senador/magistrado)
            # mesmo sem ser o fato sorteado — ver politica_data.foto_pessoa_conhecida.
            from src.politica_data import foto_pessoa_conhecida

            real_photo_url = foto_pessoa_conhecida(topic)
            if real_photo_url:
                log.info("[%s] foto oficial encontrada no banco pra pessoa citada no tema", job_id)
        # frame do próprio vídeo como reserva se a IA de imagem estiver fora
        # (cena de impacto, senão a do meio do gancho)
        frame_scene = next((k for k, sc in enumerate(script["scenes"]) if sc.get("impact_beat")), 0)
        fallback_frame = work_dir / "thumb_fallback.jpg"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(scene_videos[frame_scene]),
             "-vf", "crop=iw:ih*0.78:0:0", "-frames:v", "1", str(fallback_frame)],  # sem a faixa da legenda
            check=False,
        )
        make_thumbnail(
            thumb_prompt, thumb_text, thumb_path, accent=channel.accent, real_photo_url=real_photo_url,
            fallback_frame=fallback_frame,
        )
        update(job_id, thumbnail_path=str(thumb_path))
        log.info("[%s] thumbnail pronta: %s", job_id, thumb_path)

        description = script["description"]
        if channel.website:
            # 1ª linha = o que aparece antes do "...mais" — é onde o link
            # pro site próprio do canal realmente é clicado
            description = f"{channel.website_cta}: {channel.website}\n\n{description}"
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

        if web_facts:
            # transparência: tema digitado por pessoa foi ancorado em busca
            # real — lista as fontes usadas (gerado por código, sempre as
            # URLs reais devolvidas pela busca, nunca inventado pelo LLM).
            fontes = "\n".join(f"- {f['titulo']}: {f['url']}" for f in web_facts)
            description += f"\n\nFontes consultadas:\n{fontes}"

        if news_sources:
            fontes = "\n".join(f"- {f['titulo']}: {f['url']}" for f in news_sources)
            creditos = sorted({f["credito"] for f in news_photos if f.get("credito")})
            if channel_name == "botafogo":
                description += f"\n\nFonte: https://botafogo.win\n{fontes}"
            else:
                description += f"\n\nFonte das notícias:\n{fontes}"
            if creditos:
                description += "\nFotos: " + "; ".join(creditos)

        if music_attribution:
            # a licença CC BY (assets/music/ATTRIBUTION.md) exige creditar a
            # faixa na descrição de todo vídeo que a usa.
            description += f"\n\nMúsica: {music_attribution}"

        # hashtags fixas do canal (config, não LLM) — 3-5 é o recomendado hoje
        # em dia (mais que isso o YouTube ignora todas); os 3 primeiros hashtags
        # que aparecem na descrição saem exibidos acima do título. #Shorts só
        # entra no formato curto (é o que classifica o vídeo pra prateleira de
        # Shorts) — no formato longo não faz sentido.
        # "Assista também": os vídeos mais vistos do canal — tela final e
        # cards não dá pra criar pela API, então o caminho pro próximo vídeo
        # (tempo de sessão) vai pela descrição. Sem token/cota, só pula.
        try:
            top = top_channel_videos(channel)
        except Exception as exc:  # noqa: BLE001 — bônus, nunca derruba o vídeo
            log.warning("[%s] sem 'Assista também' (%s)", job_id, exc)
            top = []
        if top:
            links = "\n".join(f"▶ {v['title']}: https://youtu.be/{v['id']}" for v in top)
            description += f"\n\n📺 Assista também:\n{links}"

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

        # guardado antes do upload: se ele falhar (token OAuth expirado,
        # rede), scripts/retry_uploads.py reenvia o vídeo já renderizado sem
        # precisar gerar tudo de novo.
        upload_meta = {
            "title": script["title"],
            "description": description,
            "tags": script["tags"],
            "publish_at": publish_at,
            # comentário com pergunta postado pelo canal logo depois do upload
            "comment": engagement_question(script["title"], script["scenes"][:original_scene_count])
            + (f"\n\n{channel.website_cta}: {channel.website}" if channel.website else ""),
        }
        (work_dir / UPLOAD_META_FILE).write_text(json.dumps(upload_meta, ensure_ascii=False, indent=2))

        video_id = upload_video(
            channel,
            final_video,
            title=script["title"],
            description=description,
            tags=script["tags"],
            thumbnail_path=thumb_path,
            publish_at=publish_at,
        )
        update(job_id, status=after_upload_status(channel), youtube_video_id=video_id)
        log.info("[%s] publicado: https://youtu.be/%s", job_id, video_id)
        try:
            post_comment(channel, video_id, upload_meta["comment"])
        except Exception as exc:  # noqa: BLE001 — engajamento é bônus
            log.warning("[%s] não consegui comentar no vídeo: %s", job_id, exc)

        # já está no YouTube — não precisa mais guardar os arquivos (vídeo longo
        # sozinho passa de 400MB, HD ia encher rápido rodando todo dia)
        shutil.rmtree(work_dir, ignore_errors=True)
        log.info("[%s] arquivos locais removidos (%s)", job_id, work_dir)

    except Exception as exc:
        log.error("[%s] pipeline falhou: %s", job_id, exc)
        update(job_id, status="failed", error=str(exc)[:2000])
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--topic", default=None, help="obrigatório, exceto pro canal 'politica' no curto (usa dado real aleatório) ou quando channels/<nome>.yaml tem topics (escolhe sozinho)")
    parser.add_argument("--dry-run", action="store_true", help="gera tudo mas não publica")
    parser.add_argument("--publish-at", default=None, help="ISO 8601 UTC, ex: 2026-09-08T12:00:00Z")
    parser.add_argument(
        "--long", action=argparse.BooleanOptionalAction, default=None,
        help="formato longo/documentário (16:9). Sem essa flag, usa o que estiver "
             "salvo em channels/<nome>.yaml -> daily_format (editável no painel). "
             "--no-long força o formato curto mesmo que o painel esteja em 'long'.",
    )
    parser.add_argument(
        "--botafogo-task", choices=["jogo", "portal"], default=None,
        help="só canal 'botafogo': 'jogo' = prévia/pós-jogo; 'portal' = vídeo diário do botafogo.win",
    )
    parser.add_argument("--fact-label", default=None, help="só canal 'politica' no curto: força um tema específico de src.politica_data.FACT_FETCHERS em vez de sortear")
    args = parser.parse_args()

    try:
        run(
            args.channel, args.topic, args.dry_run, args.publish_at, long_form=args.long,
            fact_label=args.fact_label, botafogo_task=args.botafogo_task,
        )
    except ScriptRejected:
        sys.exit(EXIT_SCRIPT_REJECTED)


if __name__ == "__main__":
    main()
