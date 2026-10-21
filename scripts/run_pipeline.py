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
import io
import json
import logging
import math
import random
import re
import shutil
import sys
import subprocess
import unicodedata
from functools import partial
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import SFX_DIR, _ffprobe_duration, add_background_music, concat_scenes, render_scene
from src.config import ChannelConfig
from src.goal_card import goal_card
from src.news_card import headline_card
from src.fact_check import BLOCKING_TYPES, feedback_for_rewrite, review_script
from src.orchestrator import enqueue, update
from src.people_photos import names_in, person_photo
from src.real_media import real_clip, real_photos
from src.script_gen import (
    chapter_titles, engagement_question, fix_dangling_hooks, generate_script, refresh_packaging, repair_script,
)
from src.spellcheck import fix_script, fix_text
from src.stock_media import search_stock_clip, search_stock_photo
from src.thumbnail import make_thumbnail, photo_scene_frame, title_card
from src.topics import pick_topic, topic_problem
from src.tts import narrate
from src.video_qa import review_audio, review_video
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

# "Longo" que saiu curto (tema trocado na revisão de fatos, frases cortadas)
# vira Short vertical: 16:9 com menos de 3 min o YouTube trata como vídeo
# comum, fora da aba Shorts (jobs 262/270/278 — 81-197s em 16:9). Folga
# abaixo dos 180s do limite do Shorts.
SHORTS_MAX_SECONDS = 170

MAX_TOPIC_CHECKS = 3
# acima disso a voz acelerada soa apressada: roteiro reprovado e refeito
MAX_SPEEDUP = 1.2


def _fit_short(narrations: list, total_seconds: float, job_id: int) -> list:
    """Short acima de 3 min sai do feed de Shorts: no politica, vídeos de
    3:05-3:34 tiveram 0-1 view e um de 2:56 teve 167 (out/26). Acelera a
    narração (e as marcas das palavras da legenda) até MAX_SPEEDUP pra
    caber em SHORTS_MAX_SECONDS; precisando de mais, reprova o roteiro."""
    factor = total_seconds / SHORTS_MAX_SECONDS
    if factor > MAX_SPEEDUP:
        raise ScriptRejected(
            f"roteiro longo demais pro Short ({total_seconds:.0f}s de narração, máximo {SHORTS_MAX_SECONDS}s)"
        )
    log.warning("[%s] narração com %.0fs passa do Short — acelerando %.2fx", job_id, total_seconds, factor)
    fitted = []
    for audio_path, words, _ in narrations:
        fast = audio_path.with_name(audio_path.stem + "_fast.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_path), "-filter:a", f"atempo={factor:.4f}", str(fast)],
            check=True,
        )
        fast.replace(audio_path)
        words = [{**w, "start": w["start"] / factor, "end": w["end"] / factor} for w in words or []]
        fitted.append((audio_path, words, _ffprobe_duration(audio_path)))
    return fitted

# Vídeo de lista (ex.: "10 fatos sobre..."), só no formato curto: 1 cena =
# 1 item, com selo de contagem regressiva gravado na cena (ver
# src/assemble.py). Nunca mais que isso — vídeo de "30 fatos" vira maçante
# e o selo de card fica com número gigante demais pra manter elegante.
LIST_TOPIC_RE = re.compile(r"^(\d+)\s")
MAX_LIST_ITEMS = 10
# reescritas do roteiro depois de reprovado na revisão de fatos
FACT_CHECK_REWRITES = 2
# rodadas do laço conserta → revisa por reprovação (ver o loop da revisão)
MAX_REPAIR_ROUNDS = 5
# rodadas de troca de imagem da cena reprovada na revisão do vídeo pronto
VISUAL_FIX_ROUNDS = 2

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


# reescrita à portuguesa que a voz não consegue ler: 3+ consoantes seguidas
# fora dos encontros do português ("Procmt" pra "Prompt", "Jigsfild")
_CONSONANTS = re.compile(r"[bcdfgjklmnpqstvwxz]{3,}", re.IGNORECASE)
_OK_CLUSTER = re.compile(r"^[nmrls]?(?:[bcdfgptv][rl]|s[ptc])$|^(?:ns|rs)[bcdfgptv][rl]?$", re.IGNORECASE)


def _speakable(respelling: str) -> bool:
    plain = unicodedata.normalize("NFKD", respelling).encode("ascii", "ignore").decode()
    plain = re.sub(r"nh|lh|ch|sh|qu|gu", "a", plain, flags=re.IGNORECASE)
    return all(_OK_CLUSTER.match(c) for c in _CONSONANTS.findall(plain))


def _foreign_terms(script: dict, pronunciations: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Termos do campo "idioma" do roteiro (voz nativa, src/tts.py) e as
    reescritas de "pronuncia" que sobram: nada de termo que está nos dois,
    de grafia impronunciável ("Procmt") ou de palavra que já é português
    ("Havana" virava "Aavána") — 20/10."""
    from src.spellcheck import portuguese_words
    from src.tts import FOREIGN_VOICES

    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", [])).lower()
    raw = script.get("idioma") if isinstance(script.get("idioma"), dict) else {}
    foreign = {
        k.strip(): v.strip().lower()[:2] for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and 1 < len(k.strip()) <= 40 and len(k.split()) <= 4
        and v.strip().lower()[:2] in FOREIGN_VOICES and k.strip().lower() in narration
    }
    pt = portuguese_words(sorted(set(foreign) | set(pronunciations)))
    foreign = {k: v for k, v in foreign.items() if k not in pt}
    folded = {k.lower() for k in foreign}
    kept = {}
    for k, v in pronunciations.items():
        if k.lower() in folded or k in pt or not _speakable(v):
            log.info("pronúncia descartada: %r -> %r", k, v)
            continue
        kept[k] = v
    return foreign, kept


def _gray(png: bytes) -> bytes:
    """Visual que não é do próprio assunto em preto e branco
    (channel.fallback_grayscale): no futebol, some a cor de outro clube."""
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("L").convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 3}


_FEMININO = {"feminino", "feminina", "femininas", "meninas", "mulheres", "gloriosas"}


def _goal_for_scene(narration: str, gols: list[dict], usados: set[int]) -> int | None:
    """Gol que ESTA cena narra (pós-jogo): cita quem fez E o minuto do gol ou
    um verbo de gol — vira o cartão do gol (src/goal_card.py) em vez de
    foto/banco. Só "gol" não basta: "Gabriel Batista no gol, Fernando
    Marçal na zaga" (escalação) puxou o cartão do gol contra (job 452)."""
    texto = narration.lower()
    fala = _words(narration)
    lance = re.search(r"\bgol (de|do|da|contra)\b|golaço|marc(ou|a)\b|empat(ou|a)\b|abriu o placar|balanç|\bvir(ou|ada)\b|descont", texto)
    for idx, gol in enumerate(gols):
        nome = _words(gol.get("autor") or "")
        minuto = re.match(r"\d+", gol.get("minuto") or "")
        no_minuto = bool(minuto and re.search(rf"\b{minuto.group(0)}\b", texto))
        if idx not in usados and nome and nome & fala and (lance or no_minuto):
            return idx
    return None
    for idx, gol in enumerate(gols):
        nome = _words(gol.get("autor") or "")
        if idx not in usados and nome and nome & fala:
            return idx
    return None


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


# teto do texto das fontes no pedido do roteiro: 8 matérias inteiras deram
# ~11 mil tokens e o Groq (limite de 8 mil/min, um dos poucos modelos fortes
# com cota sobrando) recusava — a cascata caía em modelo fraco que copiava a
# fonte palavra por palavra (03/10, 8 tentativas reprovadas)
FACTS_TEXT_BUDGET = 5000


def _fit_facts(facts: dict, budget: int = FACTS_TEXT_BUDGET) -> dict:
    """Corta os campos de texto longo das fontes (matéria inteira) pra somar
    no máximo `budget` caracteres, dividido igualmente — título e resumo
    ficam inteiros. Vale pra qualquer canal com fontes em texto."""
    items = facts.get("noticias") or []
    textos = [n for n in items if isinstance(n, dict) and len(str(n.get("texto") or "")) > 0]
    total = sum(len(n["texto"]) for n in textos)
    if total <= budget:
        return facts
    cap = max(budget // max(len(textos), 1), 300)
    trimmed = [
        {**n, "texto": (n["texto"][:cap].rsplit(" ", 1)[0] + "…") if len(str(n.get("texto") or "")) > cap else n.get("texto")}
        if isinstance(n, dict) else n
        for n in items
    ]
    return {**facts, "noticias": trimmed}


def _sized_for_facts(facts: dict, scenes: int | None, min_minutes: float, max_minutes: float) -> tuple[int | None, float, float]:
    """Vídeo do tamanho dos dados, em qualquer canal com dados reais (lista
    do banco, notícias): 24 cenas e 4-5 min a partir de 5 linhas obrigavam
    o roteirista a inventar (somas, comparações de fora, "corrupção") ou a
    repetir metade do vídeo (job 222) — e a revisão reprovava."""
    n = len(facts.get("dados") or facts.get("noticias") or [])
    if not n or scenes is None:
        return scenes, min_minutes, max_minutes
    return min(scenes, 2 * n + 2), min(min_minutes, max(1.5, 0.35 * n)), min(max_minutes, max(3.0, 0.6 * n))


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

_ORDINAIS = ["Primeira", "Segunda", "Terceira", "Quarta", "Quinta", "Sexta", "Sétima", "Oitava", "Nona", "Décima"]


def _mark_news_blocks(script: dict, facts: dict | None) -> None:
    """Vídeo de várias notícias: a 1ª cena de cada bloco ("noticia" do
    roteiro, ver script_gen.NEWS_STRUCTURE) ganha a manchete pra tela
    ("_manchete") e, se o roteiro não anunciou, "Segunda notícia:" na fala —
    sem isso o vídeo de 15/10 emendava o Zorro nas outras notícias como se
    fosse uma história só. Depois das revisões: o corte de frase pode ter
    levado o anúncio junto."""
    noticias = [n for n in (facts or {}).get("noticias") or [] if isinstance(n, dict)]
    if len(noticias) < 2:
        return
    inicios, prev = [], None
    for i, scene in enumerate(script["scenes"]):
        k = scene.get("noticia")
        if isinstance(k, int) and 1 <= k <= len(noticias) and k != prev:
            inicios.append((i, k))
        if isinstance(k, int) and k >= 1:
            prev = k
    for ordem, (i, k) in enumerate(inicios):
        scene = script["scenes"][i]
        scene["_manchete"] = {"ordem": ordem + 1, "total": len(inicios), "titulo": noticias[k - 1].get("titulo") or ""}
        if "notícia" not in _SENT_SPLIT.split(scene["narration"], 1)[0].lower():
            ordinal = "Última" if ordem and ordem == len(inicios) - 1 else _ORDINAIS[min(ordem, len(_ORDINAIS) - 1)]
            scene["narration"] = f"{ordinal} notícia: {scene['narration']}"


def _cut_flagged_sentences(
    script: dict, problems: list[dict], max_cuts: int | None = None, lenient: bool = False, min_score: float = 0.6,
) -> dict | None:
    """Roteiro sem as frases que a revisão apontou (cada `termo` é a
    afirmação curta do revisor — acha a frase que tem a maioria das palavras
    dele). None se não achou alguma, ou se precisaria cortar mais que
    `max_cuts` frases (aí o roteiro está ruim mesmo, não é uma frase solta).
    lenient (última tentativa do dia): pula o termo que não achou e corta
    quantas frases precisar, desde que sobrem pelo menos 2 cenas."""
    def words(text: str) -> set[str]:
        # sem acento e só o radical (5 letras): o revisor parafraseia a frase
        # ("altera mando" x "alterar o mando", "judicial" x "Justiça" não, mas
        # "decisão"/"decidiu" sim) — job 458 escapou do corte assim
        text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
        return {w[:5] for w in re.findall(r"\w+", text) if len(w) > 3}

    scenes = [_SENT_SPLIT.split(sc.get("narration", "")) for sc in script["scenes"]]
    if max_cuts is None:
        # até 1/4 das frases (mínimo 3): roteiro de 20 frases com 4 detalhes
        # sem fonte ainda é um bom roteiro sem eles
        max_cuts = max(3, sum(len(x) for x in scenes) // 4)
    to_cut: set[tuple[int, int]] = set()
    for p in problems:
        term = words(str(p.get("termo", "")))
        if not term:
            if lenient:
                continue
            return None
        best, best_score = None, 0.0
        for si, sentences in enumerate(scenes):
            for ji, sentence in enumerate(sentences):
                score = len(term & words(sentence)) / len(term)
                if score > best_score:
                    best, best_score = (si, ji), score
        if best is None or best_score < min_score:
            if lenient:
                continue
            return None
        to_cut.add(best)
    if len(to_cut) > max_cuts and not lenient:
        return None
    new_scenes = []
    for si, sentences in enumerate(scenes):
        kept = " ".join(x for ji, x in enumerate(sentences) if (si, ji) not in to_cut).strip()
        if kept:
            new_scenes.append({**script["scenes"][si], "narration": kept})
    if lenient and (not to_cut or len(new_scenes) < 2):
        return None
    return {**script, "scenes": new_scenes}


def _repair_loop(script: dict, problems: list[dict], topic: str, facts, web_facts, job_id) -> dict | None:
    """Laço conserta → revisa até a revisão vir limpa. O roteiro reescrito do
    zero trocava 1 detalhe inventado por outros (21/10: curiosidades
    555-557, 3 reescritas cada, sempre um detalhe novo sem fonte). O
    conserto não pode trazer número/nome novo (trava em
    script_gen.repair_script), então cada rodada só tira ou generaliza e o
    laço converge; teto de MAX_REPAIR_ROUNDS, e sai antes se parou de mudar.
    None se não chegou numa versão aprovada."""
    fixed, fix_problems = script, problems
    for _ in range(MAX_REPAIR_ROUNDS):
        before = fixed
        fixed = repair_script(fixed, fix_problems, facts, web_facts, topic)
        if fixed is None or fixed["scenes"] == before["scenes"] and fixed.get("title") == before.get("title"):
            return None
        fix_problems = review_script(fixed, topic, facts, web_facts)
        if fix_problems == []:
            log.warning("[%s] frases reprovadas consertadas com base nas fontes — aprovado na revisão seguinte", job_id)
            return {**fixed, "_edited": True}
        if fix_problems is None:
            return None
        log.info("[%s] roteiro consertado ainda reprovado: %s", job_id,
                 "; ".join(f"{p.get('tipo')}: {p.get('termo')}" for p in fix_problems))
    return None


def _forced_cut(script: dict, problems: list[dict], topic: str, facts, web_facts, job_id) -> dict:
    """Última tentativa do dia: corta as frases reprovadas e revisa de novo,
    até 3 rodadas. Não reprova nunca — sobra o roteiro com o máximo de
    frases apontadas removidas."""
    for _ in range(3):
        if any(p.get("tipo") == "titulo_enganoso" for p in problems):
            # título não é frase da narração (job 362: "4 nomes que podem
            # mudar o jogo"): vira o próprio tema/manchete, que a fonte sustenta
            safe = re.sub(r"^Notícias do [^:]+:\s*", "", topic).strip()[:100]
            script = {**script, "title": safe, "thumbnail_text": None}  # o da thumb repetia a promessa
            problems = [p for p in problems if p.get("tipo") != "titulo_enganoso"]
            log.warning("[%s] última tentativa do dia: título reprovado trocado pelo tema: %s", job_id, safe)
        if not problems:
            break
        cut = _cut_flagged_sentences(script, problems, lenient=True)
        if cut is None:
            break
        script = cut
        log.warning("[%s] última tentativa do dia: frases reprovadas cortadas (%d cenas)", job_id, len(script["scenes"]))
        problems = review_script(script, topic, facts, web_facts) or []
    if problems:
        # o que sobrou ainda vai pro ar: corta a frase mais parecida com cada
        # ressalva mesmo com casamento fraco — melhor perder uma frase boa do
        # que publicar a errada (job 458: jogo do NBB "na Tijuca" virou futebol)
        cut = _cut_flagged_sentences(script, problems, lenient=True, min_score=0.34)
        if cut is not None:
            script = cut
            log.warning("[%s] última tentativa do dia: ressalvas restantes cortadas no casamento fraco", job_id)
            problems = review_script(script, topic, facts, web_facts) or []
    if problems:
        log.warning(
            "[%s] última tentativa do dia — publica com ressalvas da revisão: %s", job_id,
            "; ".join(str(p.get("termo")) for p in problems),
        )
    return script


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
    timestamps da descrição. O selo "Parte X de Y" na tela saiu (06/10): sem
    o título do capítulo do lado, não dizia nada a quem assiste."""
    if n < 3:
        return []
    n_chapters = min(MAX_CHAPTERS, max(3, n // 4))
    group_size = math.ceil(n / n_chapters)
    return list(range(0, n, group_size))


def _build_chapters(scenes: list[dict], durations: list[float]) -> str:
    n = len(scenes)
    if n < 3 or len(durations) != n:
        return ""

    starts = []
    last_ts = -MIN_CHAPTER_GAP_SECONDS
    for idx in _chapter_starts(n):
        cumulative = sum(durations[:idx])
        if cumulative - last_ts >= MIN_CHAPTER_GAP_SECONDS or not starts:
            starts.append((idx, cumulative))
            last_ts = cumulative
    if len(starts) < 3:
        return ""

    bounds = [idx for idx, _ in starts] + [n]
    texts = [" ".join(s["narration"] for s in scenes[a:b]) for a, b in zip(bounds, bounds[1:])]
    titles = chapter_titles(texts) or [_chapter_label(scenes[idx]["narration"]) for idx, _ in starts]
    lines = []
    for (_, cumulative), title in zip(starts, titles):
        mm, ss = divmod(int(cumulative), 60)
        lines.append(f"{mm}:{ss:02d} {title}")
    return "\n".join(lines)


def run(
    channel_name: str,
    topic: str | None,
    dry_run: bool,
    publish_at: str | None,
    long_form: bool | None = None,
    fact_label: str | None = None,
    botafogo_task: str | None = None,
    script_file: str | None = None,
    facts_file: str | None = None,
    script_only: bool = False,
    no_upload: bool = False,
    must_publish: bool = False,
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
    if topic is None and not facts_file and not fact_label and not botafogo_task and channel.active_priority_topics:
        # série/tema agendado (bloco `prioridade` do yaml) vale pra todo
        # canal, inclusive os que sorteiam dado do banco (politica: Nikolas
        # agendado pra 22/10) — pick_topic marca como usado
        from src.topics import _load as _used_topics

        if any(t not in _used_topics().get(channel_name, []) for t in channel.active_priority_topics):
            topic = pick_topic(channel_name, channel.topics, channel.niche, priority=channel.active_priority_topics)
            user_provided_topic = True  # tema escolhido à mão: busca na internet e não é trocado
    if facts_file:
        # dados reais prontos em arquivo (mesmo formato do banco: {"tema",
        # "dados"} ou {"noticias"}) — o roteiro e a revisão usam exatamente
        # estes dados (ex.: roteiro escrito à mão com --script-file)
        facts = json.loads(Path(facts_file).read_text())
        # gravado pelo --script-only: fotos e links das matérias do Botafogo
        news_photos = facts.pop("_fotos", None) or []
        news_sources = facts.pop("_fontes", None) or []
        # gravados em todo job com dados reais: a nova tentativa do daily_run
        # refaz o mesmo vídeo (mesmo tema e formato) com os mesmos dados
        saved_topic = facts.pop("_tema", None)
        saved_long = facts.pop("_long", None)
        if topic is None:
            topic = saved_topic or facts.get("tema") or facts.get("titulo")
        if saved_long is not None and not user_forced_format:
            long_form = saved_long
    elif channel_name == "politica" and topic is not None and not fact_label and (
        perfil := __import__("src.politica_data", fromlist=["perfil_parlamentar"]).perfil_parlamentar(topic)
    ):
        # tema sobre um parlamentar: dados dele no banco + internet (o banco
        # não é de hoje) — ver politica_data.perfil_parlamentar
        facts = perfil
    elif channel_name == "politica" and (fact_label or (not long_form and topic is None)):
        # tema digitado (ex.: um político específico) segue o tema com busca
        # na internet — antes sorteava dado do banco e o roteiro saía sobre
        # ele, ignorando o tema (job 594, 21/10)
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
            # prévia sempre curta; pós-jogo e notícias seguem o formato do
            # painel — pós-jogo longo tinha 1-3 views contra 100+ do curto
            # (out/26), o canal todo passou pra curto
            long_form = task["tipo"] != "pre-jogo" and channel.daily_format == "long"
    elif topic is None and channel.topics:
        # nunca repete um tema já usado — quando a lista fixa esgota, gera
        # um tema novo via LLM dentro do nicho do canal (ver src/topics.py).
        # Fila única pra curto e longo: só sai 1 vídeo/dia por canal mesmo.
        # Parte dos temas sai inspirada no que está viral no YouTube no mês
        # (channels/<nome>.yaml -> viral_queries/viral_share).
        topic = pick_topic(
            channel_name, channel.topics, channel.niche,
            viral_queries=channel.viral_queries, viral_share=channel.viral_share,
            priority=channel.active_priority_topics,
        )
    elif topic is None:
        raise SystemExit(f"channels/{channel_name}.yaml não tem topics configurado e --topic não foi passado")

    # tema sem base em dado real (fila, viral, digitado): sem sentido ou fora
    # do nicho não vira vídeo. Da fila, troca por outro; digitado, recusa.
    # tema da série (escolhido à mão) e tema que já saiu da fila (refeito
    # pelo daily_run com --topic, já checado quando foi escolhido) não
    # passam de novo pela checagem
    from src.topics import _load as _used_topics

    already_picked = topic in channel.active_priority_topics or topic in _used_topics().get(channel_name, [])
    if facts is None and not already_picked:
        for _ in range(MAX_TOPIC_CHECKS):
            problem = topic_problem(topic, channel.niche, strict=user_provided_topic)
            if problem is None:
                break
            log.warning("[%s] tema recusado (%s): %s", channel_name, problem, topic)
            if user_provided_topic or not channel.topics:
                raise ScriptRejected(f"tema recusado: {problem}")
            topic = pick_topic(channel_name, channel.topics, channel.niche, priority=channel.active_priority_topics)
        else:
            raise ScriptRejected(f"{MAX_TOPIC_CHECKS} temas seguidos recusados")

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
    source_facts = facts
    if long_form and facts:
        scenes, min_minutes, max_minutes = _sized_for_facts(facts, scenes, min_minutes, max_minutes)
    if facts:
        facts = _fit_facts(facts)
        if channel.editorial_line:
            facts = {**facts, "linha_editorial": channel.editorial_line}

    job_id = enqueue(channel_name, topic)
    work_dir = Path(__file__).resolve().parent.parent / "output" / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    if source_facts is not None:
        # a seleção (notícias do Botafogo, fato do politica) já marcou estes
        # dados como usados: roteiro reprovado na revisão e a 2ª tentativa
        # não achava mais nada (Botafogo sem vídeo em 19 e 20/10). Daqui o
        # daily_run reaproveita os mesmos dados (--facts-file)
        (work_dir / "facts.json").write_text(json.dumps(
            {**source_facts, "_fotos": news_photos, "_fontes": news_sources, "_tema": topic, "_long": long_form},
            ensure_ascii=False, indent=2,
        ))
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
        # todo tema sem dado real busca fonte (não só digitado/politica): tema
        # da fila do curiosidades sem fonte saiu "Pavlopetri do século 12 que
        # reaparece na maré baixa" (tem ~5 mil anos e fica sempre submersa)
        # e a revisão aprovou — dry-run de 06/10
        from src.web_search import search_topic_facts

        web_facts = None
        # perfil do banco (politica_data.perfil_parlamentar) busca também na
        # internet: o banco não tem o que é recente
        if facts is None or facts.get("tipo") == "perfil_parlamentar":
            web_facts = search_topic_facts(topic)
            if web_facts and facts:
                # junto com o perfil, 2 fontes curtas: banco + 4 páginas
                # passava do limite por minuto do Groq (8,4 mil tokens, job 595)
                web_facts = [{**f, "trecho": f["trecho"][:500]} for f in web_facts[:2]]
            if web_facts:
                log.info("[%s] tema ancorado com %d fonte(s) da internet", job_id, len(web_facts))
            else:
                log.info("[%s] tema sem busca na internet (sem chave configurada ou sem resultado)", job_id)

        log.info("[%s] gerando roteiro (%s) para: %s (voz: %s)", job_id, "longo" if long_form else "curto", topic, tts_voice)
        if script_file:
            # roteiro escrito à mão (ex.: quando só sobra modelo fraco que copia
            # a fonte) — passa pelas MESMAS revisões; reprovado não é reescrito
            from src.script_gen import _drop_repeated_scenes

            script = _drop_repeated_scenes(json.loads(Path(script_file).read_text()))
            log.info("[%s] roteiro do arquivo %s (%d cenas)", job_id, script_file, len(script["scenes"]))
        else:
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
                    if must_publish:
                        log.warning("[%s] revisor de fatos indisponível — última tentativa do dia, segue sem revisão", job_id)
                        break
                    raise ScriptRejected("revisor de fatos indisponível — vídeo não sai sem revisão")
            if not problems:
                break
            log.warning(
                "[%s] revisão de fatos reprovou (tentativa %d): %s", job_id, attempt + 1,
                "; ".join(f"{p.get('tipo')}: {p.get('termo')}" for p in problems),
            )
            if script_file:
                fixed = _repair_loop(script, problems, topic, facts, web_facts, job_id)
                if fixed is not None:
                    script = fixed
                    break
                cut = _cut_flagged_sentences(script, problems)
                if cut is not None and review_script(cut, topic, facts, web_facts) == []:
                    script = {**cut, "_edited": True}
                    break
                raise ScriptRejected(
                    "roteiro do arquivo reprovado na revisão de fatos: "
                    + "; ".join(f"{p.get('termo')} ({p.get('motivo')})" for p in problems)
                )
            # conserta as frases apontadas com base nas fontes ANTES de
            # reescrever (ver _repair_loop)
            fixed = _repair_loop(script, problems, topic, facts, web_facts, job_id)
            if fixed is not None:
                script = fixed
                break
            cut = _cut_flagged_sentences(script, problems)
            if cut is not None:
                recheck = review_script(cut, topic, facts, web_facts)
                if recheck == []:
                    log.warning("[%s] frases reprovadas cortadas do roteiro — aprovado na revisão seguinte", job_id)
                    script = {**cut, "_edited": True}
                    break
                log.info("[%s] roteiro cortado ainda reprovado: %s", job_id,
                         "; ".join(f"{p.get('tipo')}: {p.get('termo')}" for p in recheck or []) or "revisor fora")
            else:
                log.info("[%s] não deu pra cortar só as frases apontadas — reescrevendo", job_id)
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
                    if channel.editorial_line:
                        facts = {**facts, "linha_editorial": channel.editorial_line}
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
                if must_publish:
                    # última tentativa do dia: o vídeo SEMPRE sai — corta as
                    # frases apontadas (até 3 rodadas de revisão), em vez de
                    # descartar o roteiro e passar o dia sem vídeo (botafogo
                    # 09/10: 4 notícias seguidas reprovadas, jobs 350-353)
                    script = {**_forced_cut(script, problems or [], topic, facts, web_facts, job_id), "_edited": True}
                    break
                raise ScriptRejected(
                    "roteiro reprovado na revisão de fatos: "
                    + "; ".join(f"{p.get('termo')} ({p.get('motivo')})" for p in (problems or [{"termo": "revisor de fatos", "motivo": "indisponível"}]))
                )
            script = generate_script(
                channel, topic, facts, scenes=scenes, min_minutes=min_minutes, max_minutes=max_minutes,
                web_facts=web_facts, revision_feedback=feedback_for_rewrite(problems),
            )
        if script.pop("_edited", False):
            # conserto/corte mudou o que o vídeo narra: a descrição gerada
            # junto com o roteiro prometia o que saiu (job 558)
            script = refresh_packaging(script, channel, topic)
            log.info("[%s] descrição refeita a partir do roteiro final", job_id)
        update(job_id, status="scripted")
        # pergunta de transição que a cena seguinte não responde (job 363)
        script = fix_dangling_hooks(script)
        # português revisado antes de narrar: a narração vira legenda na tela
        script = fix_script(script, channel.language)
        # roteiro aprovado: se o vídeo reprovar depois (imagem, revisão
        # final), o daily_run refaz só o vídeo com ele (--script-file)
        (work_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2))

        if script_only:
            # roteiro aprovado nas revisões, salvo pra uma pessoa (ou um assistente)
            # ler ANTES de gastar 20-30 min renderizando; o comando de render
            # reusa exatamente este roteiro e estes dados
            (work_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2))
            render = [
                ".venv/bin/python", "scripts/run_pipeline.py", "--channel", channel_name,
                "--topic", topic, "--script-file", str(work_dir / "script.json"), "--no-upload",
            ]
            if long_form != (channel.daily_format == "long"):
                # só força quando foge do padrão do canal: forçado, o longo
                # que sai curto não vira Short (Botafogo de 1:20 em 16:9)
                render.append("--long" if long_form else "--no-long")
            if facts is not None:
                render += ["--facts-file", str(work_dir / "facts.json")]
            (work_dir / "render_cmd.json").write_text(json.dumps(render, ensure_ascii=False))
            update(job_id, status="script_ready")
            log.info("[%s] --script-only: roteiro em %s", job_id, work_dir / "script.json")
            return

        _mark_news_blocks(script, facts)

        # bordão de entrada do canal — por código, igual ao CTA abaixo, pra
        # sair sempre igual; o gancho do LLM vem logo depois.
        if channel.greeting and script["scenes"]:
            first = script["scenes"][0]
            first["narration"] = f"{channel.greeting} {first['narration']}"

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

        # narra tudo antes de renderizar: o formato (vertical x 16:9) sai da
        # duração REAL da narração, não do que foi pedido ao roteiro
        # nome estrangeiro com a tônica marcada só pra voz ("Pavlopétri");
        # a legenda volta pra grafia original (src/tts.py)
        pronunciations = {
            k: v for k, v in (script.get("pronuncia") or {}).items()
            if isinstance(k, str) and isinstance(v, str) and 2 < len(k) <= 40 and 0 < len(v) <= 50
            and k.lower() != v.lower()
        } if isinstance(script.get("pronuncia"), dict) else {}
        foreign: dict[str, str] = {}
        if channel.native_foreign:
            foreign, pronunciations = _foreign_terms(script, pronunciations)
            if foreign:
                log.info("[%s] voz nativa: %s", job_id, foreign)
        pronunciations.update(channel.pronunciations)  # nome do canal ("Aprender vIA")
        if pronunciations:
            log.info("[%s] pronúncia: %s", job_id, pronunciations)
        narrations = []
        for i, scene in enumerate(script["scenes"]):
            audio_path = work_dir / f"scene_{i}.mp3"
            _, word_boundaries = narrate(
                scene["narration"], audio_path, voice=tts_voice, pronunciations=pronunciations, foreign=foreign,
            )
            narrations.append((audio_path, word_boundaries, _ffprobe_duration(audio_path)))
        total_seconds = sum(d for _, _, d in narrations)
        if not long_form and total_seconds > SHORTS_MAX_SECONDS:
            narrations = _fit_short(narrations, total_seconds, job_id)
            total_seconds = sum(d for _, _, d in narrations)
        if long_form and not user_forced_format and total_seconds <= SHORTS_MAX_SECONDS:
            log.warning("[%s] roteiro longo saiu com %.0fs — publicando como Short vertical", job_id, total_seconds)
            long_form = False
            width, height = SHORT_WIDTH, SHORT_HEIGHT

        visual_context = f"Canal: {channel.channel_title} — {channel.niche}.\nTema do vídeo: {topic}."
        if channel.country:
            visual_context += (
                f"\nO vídeo é sobre o {channel.country}: bandeira, prédio de governo, parlamento, placa de rua, "
                f"viatura, dinheiro ou monumento identificável de OUTRO país é SEMPRE fora de contexto "
                f"(ex.: bandeira do Canadá ou dos EUA, Capitólio americano, Parlamento de Londres)."
            )
        scene_videos = []
        # remonta UMA cena com outra imagem (ver o conserto de visual depois
        # da revisão do vídeo pronto)
        scene_rerender: list = []
        scene_durations = []
        last_news_photo: dict | None = None
        photo_streak = 0  # cenas seguidas herdando a foto da notícia anterior
        same_photo_run = 0  # cenas seguidas com a MESMA foto de notícia
        last_still: bytes | None = None  # último visual aprovado (reserva se a IA de imagem cair)
        scene_sources: list[str] = []  # de onde veio o visual de cada cena (revisão final usa)
        squad: list[str] = []
        if channel.player_photos:
            from src.botafogo_data import elenco

            squad = elenco()
        player_credits: set[str] = set()
        real_credits: list[str] = []  # Commons (src/real_media.py): crédito na descrição
        real_used: set[str] = set()  # arquivo do Commons não repete no vídeo
        real_found: list[str] = []  # assuntos que já deram foto (reserva de cena sem foto própria)
        gols = [g for g in ((facts or {}).get("gols") or []) if isinstance(g, dict)]
        gols_usados: set[int] = set()
        # fotos REAIS do jogo (pós-jogo, ver botafogo_data.match_photos): em
        # rodízio nas cenas sem foto própria, antes do banco de vídeos
        match_photos = [f for f in news_photos if f.get("jogo")]
        match_photo_uses = {f["url"]: 0 for f in match_photos}
        # a visão reprovou foto real da partida pela camisa rosa (Outubro Rosa)
        match_context = visual_context + (
            "\nA imagem é uma FOTO REAL desta partida, da matéria de um jornal: uniforme "
            "especial ou de cor diferente (ex.: camisa rosa do Outubro Rosa) é normal."
        )
        for i, scene in enumerate(script["scenes"]):
            log.info("[%s] cena %d/%d", job_id, i + 1, len(script["scenes"]))
            audio_path, word_boundaries, scene_duration = narrations[i]

            # toda imagem/clipe real passa por um modelo de visão antes de
            # entrar: tem que combinar com o que ESTA cena narra (vídeos de
            # 03/10: escudo do Barcelona, futebol americano, camisa do
            # Beşiktaş num vídeo do Botafogo). None = checagem fora do ar:
            # aceita (o filtro de outro esporte/clube do stock_media segue).
            def in_context(image: bytes, _narration: str = scene["narration"]) -> bool | None:
                # sem visão (None): o banco já barra busca de ESPORTE sozinho
                # (src/stock_media.py) e deixa passar imagem neutra (contrato,
                # calendário, tribunal) — sem isso, canal estrito sem visão
                # repetia a mesma foto em 12 de 15 cenas (job 249)
                return matches_scene(image, _narration, visual_context)

            def ai_in_context(image: bytes, _narration: str = scene["narration"]) -> bool | None:
                verdict = matches_scene(image, _narration, visual_context)
                if verdict is None and channel.strict_visual:
                    return False  # IA sem checagem pode inventar jogador/escudo
                return verdict
            scene_durations.append(scene_duration)

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
            # quem a cena cita (elenco): foto do verbete na Wikipédia — entra
            # quando a cena não casa com a foto de nenhuma notícia, antes de
            # herdar a foto da cena anterior ou cair no banco de vídeos
            player = None
            if squad and i < original_scene_count:
                for name in names_in(scene["narration"], squad):
                    player = person_photo(name, "Botafogo")
                    if player:
                        break

            news_frame = None
            gol_idx = _goal_for_scene(scene["narration"], gols, gols_usados) if i < original_scene_count else None
            if gol_idx is not None:
                gols_usados.add(gol_idx)
                news_frame = goal_card(gols[gol_idx], width, height)
                log.info("[%s] cena %d: cartão do gol de %s", job_id, i + 1, gols[gol_idx].get("autor"))
            elif i >= original_scene_count:
                # cena de CTA (like/inscrição): cartão fixo do canal — a busca
                # "thumbs up" no banco trouxe um polegar PRA BAIXO (job 259)
                news_frame = title_card(channel.channel_title, width, height, channel.accent)
            elif news_photos:
                # o roteiro diz qual notícia a cena narra ("noticia"): foto
                # DELA ou nenhuma — nunca a de outra notícia por palavra em comum
                k = scene.get("noticia")
                own = isinstance(k, int) and k >= 1
                if own:
                    photo = next((f for f in news_photos if f.get("noticia") == k - 1), None)
                else:
                    photo = _news_photo_for_scene(scene["narration"], facts, news_photos)
                if photo is not None:
                    photo_streak = 0
                elif player is not None:
                    pass  # cena sem foto própria de notícia que cita jogador: a foto dele
                elif match_photos:
                    # pós-jogo: fotos do jogo em rodízio (menos usada primeiro),
                    # até 3 tentativas na visão antes de cair no banco
                    for cand in sorted(match_photos, key=lambda f: match_photo_uses[f["url"]])[:3]:
                        match_photo_uses[cand["url"]] += 1
                        frame = photo_scene_frame(cand["url"], width, height)
                        if frame is not None and matches_scene(frame, scene["narration"], match_context) is not False:
                            news_frame = frame
                            last_news_photo = None
                            same_photo_run = 0
                            break
                elif last_news_photo is not None and photo_streak < 2 and not own:
                    # cena que continua a notícia anterior: no máximo 2 cenas
                    # seguidas — a camisa de Flamengo/Flu da notícia das bets
                    # ficou em 9 cenas, inclusive na agenda do Botafogo (job 239)
                    photo = last_news_photo
                    photo_streak += 1
                if news_frame is None and photo is None and not stock_query and not own:
                    # abertura antes de qualquer notícia casar (ou continuidade
                    # esgotada), sem busca própria da cena: foto da 1ª
                    # notícia (o gancho é ela) em vez de clipe de banco, que
                    # pra futebol só tem clube estrangeiro
                    photo = news_photos[0]
                if photo is not None and photo is last_news_photo and same_photo_run >= 2 and stock_query:
                    # mesma foto já em 2 cenas seguidas e a cena tem busca
                    # própria: alterna com o banco (job 251: a foto da matéria
                    # principal casava com 11 de 15 cenas e a revisão reprovou)
                    photo = None
                if photo:
                    frame = photo_scene_frame(photo["url"], width, height)
                    # foto da PRÓPRIA matéria: só sai se a visão disser que não
                    # combina (indisponível não bloqueia — é do assunto por construção)
                    if frame is not None and matches_scene(frame, scene["narration"], visual_context) is not False:
                        same_photo_run = same_photo_run + 1 if photo is last_news_photo else 1
                        last_news_photo = photo
                        news_frame = frame

            player_frame = None
            if news_frame is None and player is not None:
                frame = photo_scene_frame(player["url"], width, height)
                if frame is not None:
                    frame = _gray(frame) if channel.fallback_grayscale else frame
                    if matches_scene(frame, scene["narration"], visual_context) is not False:
                        player_frame = frame
                        player_credits.add(player["credito"])
                        log.info("[%s] cena %d: foto de %s", job_id, i + 1, player["pessoa"])

            # começo de notícia: manchete na tela, por cima da foto (ou num
            # cartão, se a notícia não tem foto — em vez de clipe de banco)
            manchete = scene.get("_manchete") if i < original_scene_count else None
            plain_frame = news_frame
            if manchete:
                card = partial(
                    headline_card, kicker=f"NOTÍCIA {manchete['ordem']} DE {manchete['total']}",
                    headline=manchete["titulo"], site=channel.website.split("//")[-1] if channel.website else "",
                    width=width, height=height, accent=channel.accent,
                )
                if player_frame is not None and news_frame is None:
                    player_frame = card(player_frame)
                else:
                    news_frame = card(news_frame)
                log.info("[%s] cena %d: manchete da notícia %d", job_id, i + 1, manchete["ordem"])

            # o que a cena cita pelo nome (Antikythera, Passo Dyatlov): vídeo
            # e foto DE ARQUIVO do próprio assunto antes do banco genérico —
            # o vídeo do Antikythera (job 527) não mostrou o mecanismo nenhuma vez
            stock_clip_path = None
            real_frame = None
            real_clip_found = False
            real_subject = (scene.get("real_subject") or "").strip() if channel.real_media else ""
            if news_frame is None and player_frame is None and real_subject and i < original_scene_count:
                real_context = visual_context + (
                    f"\nA imagem é FOTO/VÍDEO REAL de arquivo (Wikimedia Commons) de '{real_subject}': "
                    "foto antiga, preto e branco, granulada, de museu ou reconstrução do objeto é normal, "
                    "e também o que é do MESMO caso (o local, outro achado do mesmo sítio, o museu onde está) "
                    "— no job 533 as estátuas do naufrágio de Anticítera foram recusadas. Reprove se for "
                    "quase só texto moderno (placa de museu, painel, legenda impressa)."
                )

                def real_check(image: bytes, _narration: str = scene["narration"], _ctx: str = real_context) -> bool | None:
                    return matches_scene(image, _narration, _ctx)

                clip = real_clip(real_subject, real_used, check=real_check)
                if clip is not None:
                    stock_clip_path = clip["path"]
                    real_clip_found = True
                    real_credits.append(clip["credit"])
                else:
                    # sem foto do assunto da cena (Arquimedes, job 533): outra
                    # foto inédita de um assunto que já rendeu foto no vídeo,
                    # em vez de repetir a anterior
                    for subject in dict.fromkeys([real_subject, *list(dict.fromkeys(reversed(real_found)))[:2]]):
                        for cand in real_photos(subject, real_used):
                            frame = photo_scene_frame(cand["url"], width, height)
                            if frame is not None and real_check(frame) is not False:
                                real_used.add(cand["title"])
                                real_credits.append(cand["credit"])
                                real_found.append(subject)
                                real_frame = frame
                                log.info("[%s] cena %d: foto real de %r (%s)", job_id, i + 1, subject, cand["title"])
                                break
                        if real_frame is not None:
                            break
            if stock_clip_path is None and real_frame is None and news_frame is None and player_frame is None and stock_query:
                stock_clip_path = search_stock_clip(stock_query, width, height, check=in_context, region=channel.country)

            image_path = None
            if news_frame is not None:
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(news_frame)
                # reserva sem a manchete: repetida em outra cena, mentiria o assunto
                last_still = plain_frame if manchete else news_frame
                scene_sources.append("noticia")
            elif player_frame is not None:
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(player_frame)
                last_still = player_frame
                same_photo_run = 0
                scene_sources.append("jogador")
            elif real_frame is not None:
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(real_frame)
                last_still = real_frame
                same_photo_run = 0
                scene_sources.append("arquivo")
            elif stock_clip_path is None:
                image_bytes = search_stock_photo(stock_query, width, height, check=in_context, region=channel.country) if stock_query else None
                if image_bytes is None:
                    # imagem de IA sai do prompt da PRÓPRIA cena — no contexto
                    # por construção. Pede já no formato final do vídeo (pedir
                    # quadrado e esticar no ffmpeg distorcia e borrava tudo).
                    try:
                        image_bytes = generate_image(scene["image_prompt"], width=width, height=height)
                        if ai_in_context(image_bytes) is False:
                            # IA também erra (jogador inventado de uniforme
                            # com escudo falso, job 222): 2ª chance, depois
                            # repete o último visual aprovado
                            image_bytes = generate_image(scene["image_prompt"] + ", no people, no players, no uniforms", width=width, height=height)
                            if ai_in_context(image_bytes) is False:
                                raise ValueError("imagem de IA fora de contexto")
                    except Exception:
                        # IA de imagem fora (Pollinations 402/500): repete o
                        # último visual JÁ APROVADO do vídeo em vez de derrubar
                        # o job ou pôr imagem sem checagem
                        if last_still is None:
                            log.warning("[%s] cena %d sem nenhum visual aprovado — cartão do canal", job_id, i + 1)
                            image_bytes = title_card(channel.channel_title, width, height, channel.accent)
                        else:
                            log.warning("[%s] cena %d sem imagem no contexto — repetindo a anterior", job_id, i + 1)
                            image_bytes = last_still
                        scene_sources.append("repetida")
                    else:
                        scene_sources.append("ia")
                else:
                    scene_sources.append("banco")
                if channel.fallback_grayscale and scene_sources[-1] in ("ia", "banco"):
                    image_bytes = _gray(image_bytes)
                image_path = work_dir / f"scene_{i}.png"
                image_path.write_bytes(image_bytes)
                last_still = image_bytes
                same_photo_run = 0
            else:
                still = _clip_still(stock_clip_path)
                if still and channel.fallback_grayscale:
                    still = _gray(still)  # repetido numa cena seguinte, não pode voltar colorido (job 424)
                last_still = still or last_still
                same_photo_run = 0
                scene_sources.append("arquivo" if real_clip_found else "banco")

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
                    grayscale=channel.fallback_grayscale and stock_clip_path is not None,
                    # cartão (gol, manchete, canal) e foto encaixada: zoom leve
                    composed=scene_sources[-1] in ("noticia", "jogador", "repetida") or real_frame is not None,
                )
            finally:
                # sem finally, um clipe baixado (10-20MB) vaza pro /tmp toda vez
                # que o ffmpeg falhar (já visto de verdade nesta sessão — erro de
                # rede, arquivo truncado) — acumula disco silenciosamente rodando
                # todo dia em 5 canais.
                if stock_clip_path is not None:
                    stock_clip_path.unlink(missing_ok=True)
            scene_videos.append(scene_video_path)

            def _rerender(new_image: Path, _audio=audio_path, _out=scene_video_path, _num=list_number,
                          _scene=scene, _words=word_boundaries) -> None:
                render_scene(
                    new_image, _audio, _out, width=width, height=height,
                    watermark=channel.watermark, list_number=_num, accent=channel.accent,
                    caption=_scene["narration"] if channel.captions else None,
                    word_boundaries=_words if channel.captions else None,
                    stat_overlay=_scene.get("stat_overlay"), impact_beat=bool(_scene.get("impact_beat")),
                )
            scene_rerender.append(_rerender)

        update(job_id, status="narrated")

        raw_video = work_dir / "raw.mp4"
        final_video = work_dir / "final.mp4"
        # revisão do vídeo MONTADO antes de subir (src/video_qa.py): frase
        # repetida, a mesma imagem no vídeo todo, cena fora de contexto. Em
        # 03/10 foram 16 vídeos apagados do YouTube depois de publicados.
        # Cena fora de contexto é CONSERTADA: imagem nova só nela (conferida
        # contra a narração), remonta e revisa de novo — antes o vídeo todo
        # era descartado por uma imagem (job 600, 21/10: cena 6)
        for visual_round in range(VISUAL_FIX_ROUNDS + 1):
            # crossfade obriga reencodar o vídeo inteiro — caro numa CPU fraca
            # sem encoder de hardware (Pi 5). Vale a pena pro curto; no longo
            # usa corte seco instantâneo (ver src/assemble.py).
            concat_scenes(
                scene_videos, raw_video, crossfade=not long_form, sfx_path=SFX_DIR / "whoosh.mp3",
            )
            _, music_attribution = add_background_music(raw_video, final_video, mood=script.get("mood"))
            log.info("[%s] vídeo pronto: %s — revisando antes do upload", job_id, final_video)
            qa_problems = review_video(
                [s["narration"] for s in script["scenes"]], scene_videos, visual_context,
                # futebol com clipe de banco/IA precisa de olho; vídeo só com
                # foto das matérias (do próprio assunto) dispensa
                strict_visual=channel.strict_visual and "ia" in scene_sources,
                trusted_scenes={i + 1 for i, src in enumerate(scene_sources) if src in ("noticia", "repetida", "jogador", "arquivo")},
            )
            bad = [int(m.group(1)) - 1 for p in qa_problems if (m := re.match(r"cena (\d+) fora de contexto", p))]
            if not bad or len(bad) != len(qa_problems) or visual_round == VISUAL_FIX_ROUNDS:
                break
            for i in bad:
                if not 0 <= i < len(scene_rerender):
                    continue
                narration = script["scenes"][i]["narration"]
                image_bytes = None
                for extra in ("", ", no people"):
                    try:
                        candidate = generate_image(script["scenes"][i]["image_prompt"] + extra, width=width, height=height)
                    except Exception as exc:  # noqa: BLE001 — IA de imagem fora: tenta a próxima
                        log.warning("[%s] cena %d: imagem nova falhou (%s)", job_id, i + 1, exc)
                        continue
                    if matches_scene(candidate, narration, visual_context) is not False:
                        image_bytes = candidate
                        break
                if image_bytes is None:
                    log.warning("[%s] cena %d: nenhuma imagem nova no contexto", job_id, i + 1)
                    continue
                if channel.fallback_grayscale:
                    image_bytes = _gray(image_bytes)
                image_path = work_dir / f"scene_{i}_fix.png"
                image_path.write_bytes(image_bytes)
                scene_rerender[i](image_path)
                scene_sources[i] = "ia"
                log.warning("[%s] cena %d fora de contexto — imagem trocada, remontando o vídeo", job_id, i + 1)
        # voz falando besteira não passa nem na última tentativa do dia —
        # não publicar é melhor que "Fala a é acento circunflexo" (job 414)
        audio_problems = review_audio([s["narration"] for s in script["scenes"]], [n[0] for n in narrations])
        if audio_problems:
            raise ScriptRejected("áudio reprovado antes do upload: " + "; ".join(audio_problems))
        if qa_problems:
            for p in qa_problems:
                log.warning("[%s] revisão do vídeo: %s", job_id, p)
        if qa_problems and must_publish:
            log.warning("[%s] última tentativa do dia — publica mesmo com ressalvas da revisão do vídeo", job_id)
        elif qa_problems:
            raise ScriptRejected("vídeo reprovado na revisão antes do upload: " + "; ".join(qa_problems))
        update(job_id, status="rendered", video_path=str(final_video))
        log.info("[%s] revisão do vídeo aprovada", job_id)

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
            # sem a cena de CTA: "7:46 Se esse vídeo te ajudou, deixa" virava
            # o último capítulo
            chapters = _build_chapters(
                script["scenes"][:original_scene_count], scene_durations[:original_scene_count],
            )
            if chapters:
                description = chapters + "\n\n" + description

        if channel_name == "politica":
            # link fixo pro site fonte dos dados — sempre gerado por código
            # (nunca pelo LLM), pra garantir que aponta pro lugar certo sempre.
            description += "\n\nFonte dos dados: https://brmx.org/politica/"

        if web_facts:
            # transparência: tema ancorado em busca real — lista as fontes
            # usadas (gerado por código, sempre as
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

        if player_credits:
            # licença livre do Commons (CC BY/BY-SA) exige crédito do autor
            description += "\nFotos dos jogadores: " + "; ".join(sorted(player_credits))

        if real_credits:
            description += "\nImagens de arquivo: " + "; ".join(dict.fromkeys(real_credits))

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
            update(job_id, status="dry_run")
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
            "comment": fix_text(engagement_question(script["title"], script["scenes"][:original_scene_count]), channel.language)
            + (f"\n\n{channel.website_cta}: {channel.website}" if channel.website else ""),
        }
        (work_dir / UPLOAD_META_FILE).write_text(json.dumps(upload_meta, ensure_ascii=False, indent=2))

        if no_upload:
            # pronto e revisado, sobe depois: o daily_run publica o "ready" do
            # canal no lugar de gerar outro (cota de ~6 uploads/dia)
            update(job_id, status="ready", video_path=str(final_video), thumbnail_path=str(thumb_path))
            log.info("[%s] --no-upload: vídeo pronto em %s, sobe no próximo daily_run", job_id, final_video)
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
    parser.add_argument("--facts-file", default=None, help="JSON com os dados reais do vídeo ({tema, dados} ou {noticias}) em vez de buscar/sortear")
    parser.add_argument("--script-file", default=None, help="JSON de roteiro pronto (mesmo formato do gerado) — pula a geração, mantém todas as revisões")
    parser.add_argument("--script-only", action="store_true", help="para depois do roteiro aprovado nas revisões; salva script.json/facts.json e o comando de render")
    parser.add_argument("--no-upload", action="store_true", help="renderiza e deixa 'ready' (com upload_meta) pro daily_run publicar")
    parser.add_argument("--must-publish", action="store_true", help="última tentativa do dia: revisões viram aviso (corta frases reprovadas) em vez de descartar o vídeo")
    parser.add_argument("--fact-label", default=None, help="só canal 'politica' no curto: força um tema específico de src.politica_data.FACT_FETCHERS em vez de sortear")
    args = parser.parse_args()

    try:
        run(
            args.channel, args.topic, args.dry_run, args.publish_at, long_form=args.long,
            fact_label=args.fact_label, botafogo_task=args.botafogo_task, script_file=args.script_file,
            facts_file=args.facts_file, script_only=args.script_only, no_upload=args.no_upload,
            must_publish=args.must_publish,
        )
    except ScriptRejected:
        sys.exit(EXIT_SCRIPT_REJECTED)


if __name__ == "__main__":
    main()
