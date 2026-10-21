#!/usr/bin/env python3
"""Painel de administração do pipeline — lista canais, jobs recentes e
permite disparar uma rodada manual por canal. Roda local, acessível na rede
local (mesmo esquema do ariaBot: http://192.168.1.70:PORTA).

Uso: .venv/bin/python3 web/app.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import AZURE_SPEECH_KEYS, ChannelConfig  # noqa: E402
from src.tts import EDGE_VOICES, azure_voices  # noqa: E402
from src.topics import topic_problem  # noqa: E402
from src.orchestrator import get_job, jobs_with_status, published_jobs, recent_jobs, update  # noqa: E402
from src.politica_data import FACT_FETCHERS  # noqa: E402
from src.topics import STATE_FILE as USED_TOPICS_FILE  # noqa: E402

app = Flask(__name__)

# status do token por canal, com cache — checar de verdade exige um refresh
# no Google (o arquivo existir não quer dizer nada: token de app OAuth em
# modo "Teste" expira em 7 dias e o painel continuava dizendo "token OK").
_TOKEN_CACHE: dict[str, tuple[float, str]] = {}
_TOKEN_CACHE_TTL = 600


EDGE_VOICE_LABELS = {
    "pt-BR-AntonioNeural": ("Antonio", "M"),
    "pt-BR-FranciscaNeural": ("Francisca", "F"),
    "pt-BR-ThalitaMultilingualNeural": ("Thalita", "F"),
}
_EDGE_DEFAULT_WEIGHTS = {"pt-BR-AntonioNeural": 50, "pt-BR-FranciscaNeural": 30, "pt-BR-ThalitaMultilingualNeural": 20}


def _voice_options(weights: dict) -> list[dict]:
    """3 vozes do edge (grátis) + vozes pt-BR da Azure, se houver chave."""
    weights = weights or _EDGE_DEFAULT_WEIGHTS
    out = [
        {"id": vid, "nome": nome, "genero": gen, "azure": False, "peso": weights.get(vid, 0)}
        for vid, (nome, gen) in EDGE_VOICE_LABELS.items()
    ]
    for v in azure_voices():
        gen = {"Male": "M", "Female": "F"}.get(v["genero"], v["genero"][:1])
        out.append({"id": v["id"], "nome": v["nome"], "genero": gen, "azure": True, "peso": weights.get(v["id"], 0)})
    return out


def _token_status(name: str) -> str:
    """"ok", "missing" ou "invalid"."""
    token_file = ROOT / "credentials" / f"token_{name}.json"
    if not token_file.exists():
        return "missing"
    cached = _TOKEN_CACHE.get(name)
    if cached and time.time() - cached[0] < _TOKEN_CACHE_TTL:
        return cached[1]
    try:
        from src.upload import _load_credentials

        creds = _load_credentials(token_file)
        status = "ok" if creds.valid else "invalid"
    except Exception:  # noqa: BLE001 — RefreshError, rede: trata como inválido
        status = "invalid"
    _TOKEN_CACHE[name] = (time.time(), status)
    return status


def _can_comment(name: str) -> bool:
    """Token tem o escopo de comentar (ver src/upload.py -> COMMENT_SCOPE)?
    Só lê o arquivo, sem rede."""
    from src.upload import COMMENT_SCOPE

    try:
        token = json.loads((ROOT / "credentials" / f"token_{name}.json").read_text())
    except (OSError, ValueError):
        return False
    return COMMENT_SCOPE in (token.get("scopes") or [])


# views por vídeo, com cache — videos.list custa 1 unidade de cota por
# chamada (até 50 ids), barato, mas não precisa bater no Google a cada F5.
_VIEWS_CACHE: dict[str, tuple[float, int]] = {}
_VIEWS_CACHE_TTL = 900
# duração em segundos (não muda — sem validade), vem na mesma chamada
_DURATIONS: dict[str, int] = {}
# Short do YouTube vai até 3 minutos
SHORT_MAX_SECONDS = 180


def _iso_seconds(value: str) -> int | None:
    """PT1M5S -> 65."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def _video_views(jobs) -> dict[str, int]:
    """{youtube_video_id: views} dos jobs publicados, usando o token de cada
    canal. Canal com token inválido/sem rede fica sem número (não quebra o painel)."""
    now = time.time()
    by_channel: dict[str, list[str]] = {}
    for j in jobs:
        vid = j.youtube_video_id
        fresh = vid in _VIEWS_CACHE and now - _VIEWS_CACHE[vid][0] < _VIEWS_CACHE_TTL
        if vid and not (fresh and vid in _DURATIONS):
            by_channel.setdefault(j.channel, []).append(vid)
    for name, ids in by_channel.items():
        if _token_status(name) != "ok":
            continue
        try:
            from googleapiclient.discovery import build

            from src.upload import _load_credentials

            creds = _load_credentials(ROOT / "credentials" / f"token_{name}.json")
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            for i in range(0, len(ids), 50):
                resp = youtube.videos().list(part="statistics,contentDetails", id=",".join(ids[i:i + 50])).execute()
                for item in resp.get("items", []):
                    views = int(item.get("statistics", {}).get("viewCount", 0))
                    _VIEWS_CACHE[item["id"]] = (now, views)
                    seconds = _iso_seconds(item.get("contentDetails", {}).get("duration", ""))
                    if seconds is not None:
                        _DURATIONS[item["id"]] = seconds
        except Exception as exc:  # noqa: BLE001 — cota/rede: mostra sem views
            app.logger.warning("views do canal %s indisponíveis: %s", name, exc)
    return {vid: v for vid, (_, v) in _VIEWS_CACHE.items()}


def _date_label(created_at: str | None) -> str:
    if not created_at:
        return ""
    try:
        return datetime.fromisoformat(created_at).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return created_at[:10]


def _used_topics() -> dict:
    if USED_TOPICS_FILE.exists():
        return json.loads(USED_TOPICS_FILE.read_text())
    return {}


def _load_channels() -> list[dict]:
    used = _used_topics()
    channels = []
    for yaml_path in sorted((ROOT / "channels").glob("*.yaml")):
        cfg = yaml.safe_load(yaml_path.read_text())
        name = yaml_path.stem
        topics = cfg.get("topics", [])
        used_topics = set(used.get(name, []))
        channels.append(
            {
                "name": name,
                "title": cfg.get("channel_title") or name,
                "color": _visible_color(cfg.get("accent", "#888888")),
                "youtube_handle": cfg.get("youtube_handle", ""),
                "niche": cfg.get("niche", ""),
                "uploads_per_day": cfg.get("uploads_per_day", 1),
                "upload_privacy": cfg.get("upload_privacy", "private"),
                "token_status": _token_status(name),
                "can_comment": _can_comment(name),
                "require_approval": cfg.get("require_approval", True),
                # canais sem fila de temas (botafogo: tema vem da partida)
                # não usam temas virais.
                "uses_topic_queue": "topics" in cfg,
                "viral_queries": cfg.get("viral_queries", []),
                "viral_share_pct": round(cfg.get("viral_share", 0.5) * 100),
                "identity_text": [
                    (key, label, (" ".join(cfg.get(key, [])) if key == "hashtags" else cfg.get(key, "")))
                    for key, label in IDENTITY_TEXT_FIELDS
                ],
                "scenes_per_video": cfg.get("scenes_per_video", 8),
                "long_form_scenes": cfg.get("long_form_scenes", 30),
                "accent": cfg.get("accent", "#ffffff"),
                "captions": cfg.get("captions", True),
                "prompt_base": (cfg.get("prompt_base") or "").strip(),
                "topics": topics,
                "used_topics": used_topics,
                "topics_pending": len([t for t in topics if t not in used_topics]),
                "short_min_minutes": cfg.get("short_min_minutes", 3),
                "short_max_minutes": cfg.get("short_max_minutes", 6),
                "long_min_minutes": cfg.get("long_min_minutes", 15),
                "long_max_minutes": cfg.get("long_max_minutes", 20),
                "daily_format": cfg.get("daily_format", "short"),
                "voices": _voice_options(cfg.get("tts_voice_weights", {})),
                # política no formato curto ignora `topics` (lista acima) e
                # sorteia um destes ~20 fatos reais do banco de transparência
                # (ver src/politica_data.py) — sem isso listado aqui, a fila
                # real de temas do canal fica invisível na UI.
                "fact_labels": [label for label, *_ in FACT_FETCHERS] if name == "politica" else [],
            }
        )
    return channels


IDENTITY_TEXT_FIELDS = (
    ("channel_title", "Nome do canal (usado na playlist)"),
    ("youtube_handle", "@handle no YouTube (link de inscrição)"),
    ("watermark", "Marca d'água gravada no vídeo"),
    ("website", "Site do canal (vai na descrição, comentário e fim do vídeo)"),
    ("website_cta", "Texto do link do site"),
    ("niche", "Nicho (usado pra gerar temas e roteiro)"),
    ("hashtags", "Hashtags (separadas por espaço, 3-5)"),
    ("language", "Idioma"),
)

DURATION_FIELDS = ("short_min_minutes", "short_max_minutes", "long_min_minutes", "long_max_minutes")


# status que ainda podem virar vídeo (rodando agora ou esperando o render)
IN_PROGRESS = ("pending", "scripted", "narrated", "rendered", "script_ready", "script_approved")
# falha que custou o vídeo (retried = tentativa substituída por outra)
FAILED = ("failed", "abandoned")
GRID_DAYS = 14


def _local(created_at: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(created_at).astimezone() if created_at else None
    except ValueError:
        return None


def _video(job, views: dict) -> dict:
    when = _local(job.created_at)
    return {
        "id": job.id, "channel": job.channel, "topic": job.topic, "vid": job.youtube_video_id,
        "views": views.get(job.youtube_video_id), "ts": when.timestamp() if when else 0,
        "seconds": _DURATIONS.get(job.youtube_video_id),
        "date_label": _date_label(job.created_at),
    }


def _visible_color(hex_color: str) -> str:
    """Cor do canal como marcador no painel: quase branca (Botafogo,
    #e5e5e5) some no tema claro — vira um cinza médio."""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return "#888888"
    return "#8a939d" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 200 else hex_color


# motivo de falha por trecho da mensagem de erro (ordem importa: o 1º que
# casar vence)
FAILURE_CAUSES = (
    ("Revisão de fatos reprovou o roteiro", ("revisão de fatos",)),
    ("Roteiro longo demais pro Short", ("longo demais pro short",)),
    ("Revisão do vídeo pronto reprovou", ("revisão antes do upload", "revisão do vídeo", "revisão de áudio")),
    ("Nenhuma IA respondeu (cota)", ("nenhum provedor", "llm respondeu")),
    ("Tema recusado", ("tema recusado", "temas seguidos recusados")),
    ("Upload no YouTube (token, cota)", ("invalid_grant", "upload", "quota", "uploadlimitexceeded")),
    ("Imagem ou narração", ("imagem", "tts", "narração", "voz")),
)


def _failure_cause(error: str) -> str:
    low = error.lower()
    return next((label for label, keys in FAILURE_CAUSES if any(k in low for k in keys)), "Outros")


def _channel_summary(c: dict, jobs: list, videos: list[dict], running: bool) -> dict:
    """Situação de hoje, grade dos últimos GRID_DAYS dias e desempenho do canal."""
    today = date.today()
    mine = [j for j in jobs if j.channel == c["name"]]
    by_day: dict[date, list] = {}
    for j in mine:
        when = _local(j.created_at)
        if when:
            by_day.setdefault(when.date(), []).append(j)
    grid = []
    for i in range(GRID_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        day_jobs = by_day.get(d, [])
        ups = sum(j.status == "uploaded" for j in day_jobs)
        fails = sum(j.status in FAILED + ("retried",) for j in day_jobs)
        state = "ok" if ups else ("fail" if fails else "none")
        grid.append({"label": d.strftime("%d/%m"), "state": state, "ups": ups, "fails": fails})

    today_jobs = by_day.get(today, [])
    published = [j for j in today_jobs if j.status == "uploaded"]
    pending = [j for j in today_jobs if j.status in IN_PROGRESS]
    if published:
        state, text = "ok", f"{len(published)} vídeo(s) no ar hoje"
    elif pending and running:
        state, text = "busy", "gerando o vídeo de hoje"
    elif today_jobs:
        state, text = "fail", "falhou hoje — veja em Problemas"
    elif datetime.now().hour < 10:
        state, text = "none", "roda às 10h"
    else:
        state, text = "fail", "nenhum vídeo hoje"
    if c["token_status"] != "ok":
        state, text = "fail", "token do YouTube expirado — rode auth_youtube.py"

    mine_videos = [v for v in videos if v["channel"] == c["name"]]
    cutoff = time.time() - 30 * 86400
    last30 = [v["views"] for v in mine_videos if v["ts"] >= cutoff and v["views"] is not None]
    today_video = next((v for v in mine_videos if v["id"] == published[0].id), None) if published else None
    return {
        **c, "state": state, "state_text": text, "grid": grid, "today_video": today_video,
        "missed_days": sum(g["state"] != "ok" for g in grid[:-1]),
        "videos_30d": len(last30), "views_30d": sum(last30),
        "median_30d": round(statistics.median(last30)) if last30 else 0,
        "best": max(mine_videos, key=lambda v: v["views"] or -1, default=None),
    }


# Problemas da máquina, não de um vídeo: a limpeza do daily_run.py guarda pasta
# de job no máximo RETRY_KEEP_DAYS (14) — mais velha que isso é estado que a
# limpeza não conhece (já aconteceu: "narrated" de 06/10 esquecida). Fila e
# aprovação ficam de fora, essas esperam de propósito.
STALE_JOB_DAYS = 15
WAITING_STATUSES = ("pending", "awaiting_approval", "script_approved")
OUTPUT_WARN_GB = 8
DISK_FREE_WARN_PCT = 10
DAILY_RUN_LOG = ROOT / "logs" / "daily_run.log"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _system_problems(statuses: dict[int, str]) -> list[dict]:
    problems = []
    cutoff = time.time() - STALE_JOB_DAYS * 86400
    stale, stale_bytes, total_bytes = [], 0, 0
    for job_dir in (ROOT / "output").glob("job_*"):
        try:
            job_id = int(job_dir.name.removeprefix("job_"))
        except ValueError:
            continue
        size = _dir_size(job_dir)
        total_bytes += size
        status = statuses.get(job_id) or (getattr(get_job(job_id), "status", None) or "sem registro")
        if job_dir.stat().st_mtime < cutoff and status not in WAITING_STATUSES:
            stale.append(f"job {job_id} ({status})")
            stale_bytes += size
    if stale:
        problems.append({
            "title": f"{len(stale)} pasta(s) de job esquecida(s) em output/ ({stale_bytes / 1e9:.1f} GB)",
            "detail": f"Mais de {STALE_JOB_DAYS} dias e a limpeza diária não apagou — status que ela não conhece: " + ", ".join(sorted(stale)[:20]),
        })
    if total_bytes > OUTPUT_WARN_GB * 1e9:
        problems.append({
            "title": f"output/ com {total_bytes / 1e9:.1f} GB",
            "detail": f"Acima de {OUTPUT_WARN_GB} GB — normal é ficar só com os vídeos dos últimos dias.",
        })
    try:
        last_run = next((line[:19] for line in reversed(DAILY_RUN_LOG.read_text(errors="ignore").splitlines()[-400:])
                         if "rodada diária concluída" in line), None)
    except OSError:
        last_run = None
    last_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S") if last_run else None
    if not last_dt or datetime.now() - last_dt > timedelta(hours=26):
        problems.append({
            "title": "Rodada diária (e a limpeza) não terminou nas últimas 26h",
            "detail": f"Última conclusão: {last_dt.strftime('%d/%m %H:%M') if last_dt else 'não encontrada'} — veja logs/daily_run.log.",
        })
    disk = shutil.disk_usage(ROOT)
    free_pct = 100 * disk.free / disk.total
    if free_pct < DISK_FREE_WARN_PCT:
        problems.append({
            "title": f"Disco quase cheio: {free_pct:.0f}% livre ({disk.free / 1e9:.0f} GB)",
            "detail": "Pouco espaço pode fazer o render falhar no meio.",
        })
    return problems


@app.route("/")
def index():
    channels = _load_channels()
    raw_jobs = recent_jobs(limit=600)
    raw_published = published_jobs()
    views = _video_views(raw_published)
    videos = [_video(j, views) for j in raw_published]
    running = _pipeline_already_running()
    summaries = [_channel_summary(c, raw_jobs, videos, running) for c in channels]
    summaries.sort(key=lambda c: {"fail": 0, "busy": 1, "none": 2, "ok": 3}[c["state"]])
    colors = {c["name"]: c["color"] for c in channels}
    titles = {c["name"]: c["title"] for c in channels}
    problems = [
        {"id": j.id, "channel": j.channel, "topic": j.topic, "status": j.status,
         "error": (j.error or "").strip()[:1200], "date_label": _date_label(j.created_at)}
        for j in raw_jobs if j.status in FAILED + ("retried",)
    ][:150]
    system_problems = _system_problems({j.id: j.status for j in raw_jobs})
    week_ago = datetime.now().astimezone() - timedelta(days=7)
    causes: dict[str, int] = {}
    for j in raw_jobs:
        when = _local(j.created_at)
        if j.status in FAILED + ("retried",) and when and when >= week_ago:
            cause = _failure_cause(j.error or "")
            causes[cause] = causes.get(cause, 0) + 1
    causes_7d = sorted(causes.items(), key=lambda kv: kv[1], reverse=True)
    total_uploads = sum(int(c["uploads_per_day"] or 0) for c in channels)
    flashes = {
        "saved": "Configuração salva.", "topic_added": "Fila de temas atualizada.",
        "approved": "Vídeo publicado.", "busy": "Já tem um vídeo sendo gerado. Espere ele terminar pra rodar outro.",
        "started": "Vídeo começou a ser gerado. Leva uns 20 minutos.",
    }
    flash = next((msg for key, msg in flashes.items() if request.args.get(key) == "1"), None)
    if request.args.get("topic_error"):
        flash = f"Tema recusado: {request.args['topic_error']}"
    elif request.args.get("approve_error"):
        flash = f"Não consegui publicar: {request.args['approve_error']}"
    return render_template(
        "painel.html", channels=summaries, causes_7d=causes_7d, videos=videos, problems=problems, system_problems=system_problems, colors=colors, titles=titles,
        flash=flash, total_uploads=total_uploads, running=running, today=date.today().strftime("%d/%m"),
        pending=jobs_with_status("awaiting_approval"), azure_ready=bool(AZURE_SPEECH_KEYS),
    )


@app.route("/duration/<channel>", methods=["POST"])
def save_duration(channel: str):
    """Persiste os minutos min/max (short e longo) direto no YAML do canal.
    Faz substituição de texto em vez de yaml.safe_dump pra não perder
    comentários e o estilo de bloco (`prompt_base: >`) do arquivo original."""
    path = ROOT / "channels" / f"{channel}.yaml"
    if not path.exists():
        return redirect(url_for("index"))

    text = path.read_text()
    for field_name in DURATION_FIELDS:
        try:
            value = float(request.form.get(field_name, ""))
        except ValueError:
            continue
        value_str = str(int(value)) if value == int(value) else str(value)
        pattern = rf"^{field_name}:.*$"
        replacement = f"{field_name}: {value_str}"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
        else:
            text = f"{replacement}\n{text}"

    daily_format = request.form.get("daily_format", "")
    if daily_format in ("short", "long"):
        pattern = r"^daily_format:.*$"
        replacement = f'daily_format: "{daily_format}"'
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
        else:
            text = f"{replacement}\n{text}"

    path.write_text(text)
    return redirect(url_for("index", saved=1))


@app.route("/voices/<channel>", methods=["POST"])
def save_voices(channel: str):
    """Persiste o peso de sorteio de cada voz grátis (mapa de tamanho fixo,
    sempre as 3 mesmas chaves — substitui o bloco inteiro em vez de tentar
    editar item a item como a fila de temas)."""
    path = ROOT / "channels" / f"{channel}.yaml"
    if not path.exists():
        return redirect(url_for("index"))

    weights = {}
    for field_name, raw in request.form.items():
        if not field_name.startswith("w__"):
            continue
        try:
            weight = max(int(raw), 0)
        except ValueError:
            weight = 0
        voice_id = field_name[3:]
        # vozes do edge sempre ficam no yaml (mesmo com 0, como antes);
        # as da Azure só entram se tiverem peso
        if weight or voice_id in EDGE_VOICES:
            weights[voice_id] = weight
    if not any(weights.values()):
        return redirect(url_for("index"))  # não deixa zerar tudo (ninguém narraria)

    block = "tts_voice_weights:\n" + "".join(f"  {voice}: {w}\n" for voice, w in weights.items())
    text = path.read_text()
    pattern = r"^tts_voice_weights:[ \t]*\n(?:  .*\n)*"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, block, text, count=1, flags=re.MULTILINE)
    else:
        text = block + text
    path.write_text(text)
    return redirect(url_for("index", saved=1))


def _yaml_set_scalar(text: str, key: str, value: str) -> str:
    """Troca (ou cria no topo) `key: value` sem reescrever o resto do YAML."""
    pattern = rf"^{re.escape(key)}:.*$"
    replacement = f"{key}: {value}"
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, lambda _: replacement, text, count=1, flags=re.MULTILINE)
    return f"{replacement}\n{text}"


@app.route("/publishing/<channel>", methods=["POST"])
def save_publishing(channel: str):
    path = ROOT / "channels" / f"{channel}.yaml"
    if not path.exists():
        return redirect(url_for("index"))
    text = path.read_text()
    try:
        per_day = min(max(int(request.form.get("uploads_per_day", "")), 0), 6)
        text = _yaml_set_scalar(text, "uploads_per_day", str(per_day))
    except ValueError:
        pass
    approval = request.form.getlist("require_approval")
    if approval:
        text = _yaml_set_scalar(text, "require_approval", "true" if approval[-1] == "1" else "false")
    privacy = request.form.get("upload_privacy", "")
    if privacy in ("public", "unlisted", "private"):
        text = _yaml_set_scalar(text, "upload_privacy", f'"{privacy}"')
    path.write_text(text)
    return redirect(url_for("index", saved=1))


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@app.route("/identity/<channel>", methods=["POST"])
def save_identity(channel: str):
    """Identidade do canal + prompt do roteiro. Edição cirúrgica por chave
    (não yaml.safe_dump) pra preservar comentários e o resto do arquivo."""
    path = ROOT / "channels" / f"{channel}.yaml"
    if not path.exists():
        return redirect(url_for("index"))
    text = path.read_text()

    for key, _label in IDENTITY_TEXT_FIELDS:
        if key not in request.form:
            continue
        value = request.form.get(key, "").strip()
        if key == "hashtags":
            tags = [t if t.startswith("#") else f"#{t}" for t in value.split()]
            text = _yaml_set_scalar(text, key, "[" + ", ".join(_yaml_quote(t) for t in tags) + "]")
        elif key == "niche" and not value:
            continue  # nicho vazio quebraria a geração de temas
        else:
            text = _yaml_set_scalar(text, key, _yaml_quote(value))

    for key, lo, hi in (("scenes_per_video", 3, 40), ("long_form_scenes", 6, 60)):
        try:
            text = _yaml_set_scalar(text, key, str(min(max(int(request.form.get(key, "")), lo), hi)))
        except ValueError:
            pass

    accent = request.form.get("accent", "")
    if re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        text = _yaml_set_scalar(text, "accent", _yaml_quote(accent))

    # checkbox: hidden "0" + checkbox "1" — o último valor enviado vence
    captions = request.form.getlist("captions")
    if captions:
        text = _yaml_set_scalar(text, "captions", "true" if captions[-1] == "1" else "false")

    prompt = request.form.get("prompt_base", "").replace("\r\n", "\n").strip()
    if prompt:
        # bloco dobrado (">"), mesmo estilo do arquivo original. No ">" uma
        # quebra real exige linha em branco entre as linhas — então cada
        # quebra de linha do textarea vira uma quebra de verdade no texto.
        lines = [line.strip() for line in prompt.split("\n") if line.strip()]
        block = "prompt_base: >\n" + "\n".join(
            textwrap.fill(line, width=78, initial_indent="  ", subsequent_indent="  ", break_on_hyphens=False) + "\n"
            for line in lines
        )
        pattern = r"^prompt_base:.*\n(?:  .*\n|\n(?=  ))*"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, lambda _: block, text, count=1, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + "\n" + block

    yaml.safe_load(text)  # nunca grava um yaml quebrado
    path.write_text(text)
    return redirect(url_for("index", saved=1))


@app.route("/viral/<channel>", methods=["POST"])
def save_viral(channel: str):
    """viral_share (0-1) + viral_queries (lista) — ver src/topics.py."""
    path = ROOT / "channels" / f"{channel}.yaml"
    if not path.exists():
        return redirect(url_for("index"))
    text = path.read_text()
    try:
        pct = min(max(int(request.form.get("viral_share_pct", "")), 0), 100)
        text = _yaml_set_scalar(text, "viral_share", str(pct / 100))
    except ValueError:
        pass

    queries = [q.strip() for q in request.form.get("viral_queries", "").splitlines() if q.strip()]
    block = "viral_queries:\n" + "".join(
        '  - "{}"\n'.format(q.replace("\\", "\\\\").replace('"', '\\"')) for q in queries
    )
    if not queries:
        block = "viral_queries: []\n"
    pattern = r"^viral_queries:.*\n(?:  - .*\n)*"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, lambda _: block, text, count=1, flags=re.MULTILINE)
    else:
        text = re.sub(r"^topics:", lambda _: block + "topics:", text, count=1, flags=re.MULTILINE)
    path.write_text(text)
    return redirect(url_for("index", saved=1))


def _yaml_append_list_item(text: str, key: str, value: str) -> str:
    """Adiciona `value` como último item da lista YAML `key:` (formato
    `  - "..."`), sem reescrever o arquivo inteiro (preserva comentários e
    formatação do resto do YAML)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'  - "{escaped}"'
    pattern = rf"(^{re.escape(key)}:[ \t]*\n(?:  - .*\n)*)"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match:
        insert_at = match.end()
        return text[:insert_at] + new_line + "\n" + text[insert_at:]
    sep = "" if text.endswith("\n") else "\n"
    return text + sep + f"{key}:\n{new_line}\n"


def _yaml_list_block_match(text: str, key: str):
    pattern = rf"^{re.escape(key)}:[ \t]*\n((?:  - .*\n)*)"
    return re.search(pattern, text, flags=re.MULTILINE)


def _yaml_replace_list_item(text: str, key: str, index: int, new_value: str) -> str | None:
    """Substitui o item `index` (0-based, ordem de aparição no YAML) da
    lista `key:` pelo texto novo. Devolve None se o índice/chave não bater
    (ex.: alguém editou o YAML por fora entre a página carregar e o
    submit) — o chamador ignora a mudança em vez de corromper o arquivo."""
    match = _yaml_list_block_match(text, key)
    if not match:
        return None
    lines = match.group(1).splitlines(keepends=True)
    if index < 0 or index >= len(lines):
        return None
    escaped = new_value.replace("\\", "\\\\").replace('"', '\\"')
    lines[index] = f'  - "{escaped}"\n'
    start, end = match.start(1), match.end(1)
    return text[:start] + "".join(lines) + text[end:]


def _yaml_delete_list_item(text: str, key: str, index: int) -> str | None:
    match = _yaml_list_block_match(text, key)
    if not match:
        return None
    lines = match.group(1).splitlines(keepends=True)
    if index < 0 or index >= len(lines):
        return None
    del lines[index]
    start, end = match.start(1), match.end(1)
    return text[:start] + "".join(lines) + text[end:]


LIST_TOPIC_RE = re.compile(r"^(\d+)(\s)")
MAX_LIST_ITEMS = 10


def _clamp_list_topic(topic: str) -> str:
    """Tema de lista ("N fatos/coisas sobre...") nunca passa de 10 — o
    vídeo vira 1 cena por item, então 20/30/40 ficava maçante e o selo de
    número desproporcional. Ajusta o N sozinho em vez de rejeitar."""
    m = LIST_TOPIC_RE.match(topic)
    if m and int(m.group(1)) > MAX_LIST_ITEMS:
        return f"{MAX_LIST_ITEMS}{m.group(2)}{topic[m.end():]}"
    return topic


@app.route("/topics/<channel>", methods=["POST"])
def add_topic(channel: str):
    """Adiciona um tema fixo na fila do canal (`topics` — fila única pra
    curto e longo) — fica lá esperando o pipeline consumir (cron ou disparo
    manual, nunca repete — ver src/topics.py)."""
    path = ROOT / "channels" / f"{channel}.yaml"
    topic = _clamp_list_topic(request.form.get("topic", "").strip())
    if not path.exists() or not topic:
        return redirect(url_for("index"))
    problem = topic_problem(topic, ChannelConfig.load(channel).niche)
    if problem:
        return redirect(url_for("index", topic_error=problem))

    text = path.read_text()
    text = _yaml_append_list_item(text, "topics", topic)
    path.write_text(text)
    return redirect(url_for("index", topic_added=1))


@app.route("/topics/<channel>/update", methods=["POST"])
def update_topic(channel: str):
    """Edita o texto de um tema já existente na lista fixa (ex.: trocar '30
    fatos' por '10 fatos') sem mexer na posição nem nos outros itens."""
    path = ROOT / "channels" / f"{channel}.yaml"
    new_topic = _clamp_list_topic(request.form.get("topic", "").strip())
    try:
        index = int(request.form.get("index", ""))
    except ValueError:
        return redirect(url_for("index"))
    if not path.exists() or not new_topic:
        return redirect(url_for("index"))
    problem = topic_problem(new_topic, ChannelConfig.load(channel).niche)
    if problem:
        return redirect(url_for("index", topic_error=problem))

    text = path.read_text()
    updated = _yaml_replace_list_item(text, "topics", index, new_topic)
    if updated is None:
        return redirect(url_for("index"))
    path.write_text(updated)
    return redirect(url_for("index", topic_added=1))


@app.route("/topics/<channel>/delete", methods=["POST"])
def delete_topic(channel: str):
    """Remove um tema da lista fixa (não mexe em quem já foi usado — só
    tira da fila de pendentes)."""
    path = ROOT / "channels" / f"{channel}.yaml"
    try:
        index = int(request.form.get("index", ""))
    except ValueError:
        return redirect(url_for("index"))
    if not path.exists():
        return redirect(url_for("index"))

    text = path.read_text()
    updated = _yaml_delete_list_item(text, "topics", index)
    if updated is None:
        return redirect(url_for("index"))
    path.write_text(updated)
    return redirect(url_for("index", topic_added=1))


@app.route("/approve/<int:job_id>", methods=["POST"])
def approve_job(job_id: int):
    from src.upload import publish_video

    job = get_job(job_id)
    if not job or job.status != "awaiting_approval" or not job.youtube_video_id:
        return redirect(url_for("index"))
    try:
        publish_video(ChannelConfig.load(job.channel), job.youtube_video_id)
    except Exception as exc:  # noqa: BLE001 — token/cota: mostra no painel em vez de dar erro 500
        return redirect(url_for("index", approve_error=str(exc)[:200]))
    update(job_id, status="uploaded")
    return redirect(url_for("index", approved=1))


@app.route("/reject/<int:job_id>", methods=["POST"])
def reject_job(job_id: int):
    """Não apaga do YouTube — só deixa privado e tira da fila de aprovação
    (dá pra apagar ou editar à mão no Studio depois)."""
    job = get_job(job_id)
    if job and job.status == "awaiting_approval":
        update(job_id, status="rejected")
    return redirect(url_for("index"))


def _pipeline_already_running() -> bool:
    """A máquina já roda pouca RAM sobrando com outros serviços — rodar 2+
    pipelines (LLM + ffmpeg) ao mesmo tempo é o tipo de coisa que derruba o
    processo por falta de memória. Só permite 1 job por vez."""
    out = subprocess.run(["pgrep", "-f", "scripts/run_pipeline.py"], capture_output=True, text=True)
    return bool(out.stdout.strip())


@app.route("/run/<channel>", methods=["POST"])
def run_channel(channel: str):
    if _pipeline_already_running():
        return redirect(url_for("index", busy=1))

    # 3 estados: "1" força --long, "0" força --no-long, ausente não passa
    # flag nenhuma (run_pipeline.py resolve sozinho pelo daily_format do
    # yaml — ver commit b1542ee). Sem isso, os botões "Rodar short" e
    # "Fatos reais" desse painel ficavam sem efeito assim que um canal
    # tinha daily_format: "long" salvo (bug real, achado pelo usuário):
    # omitir a flag não força mais curto, só herda o padrão do yaml.
    long_param = request.args.get("long")
    custom_topic = request.form.get("topic", "").strip()
    fact_label = request.form.get("fact_label", "").strip()
    if custom_topic:
        problem = topic_problem(custom_topic, ChannelConfig.load(channel).niche)
        if problem:
            return redirect(url_for("index", topic_error=problem))
    # nice/ionice — mesmo tratamento que o cron já dá pro daily_run.py, pra
    # não competir por CPU/IO com os outros serviços da máquina quando
    # disparado manualmente pelo painel.
    cmd = ["nice", "-n", "19", "ionice", "-c", "3", sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), "--channel", channel]

    if long_param == "1":
        cmd.append("--long")
    elif long_param == "0":
        cmd.append("--no-long")
    # long_param ausente: nem --long nem --no-long, deixa run_pipeline.py
    # resolver pelo daily_format do yaml. --fact-label funciona nos dois
    # formatos agora (curto/longo é só formato e duração — não decide
    # sozinho se o roteiro usa dado real do banco, ver run_pipeline.py).

    if channel == "botafogo" and not custom_topic:
        # botões do painel no Botafogo = vídeo de temas do botafogo.win
        # (prévia/pós-jogo continuam saindo sozinhos no daily_run)
        cmd += ["--botafogo-task", "portal"]

    if custom_topic:
        cmd += ["--topic", custom_topic]
    # sem tema digitado: run_pipeline.py escolhe sozinho da fila única do
    # canal (nunca repete — ver src/topics.py), ou usa fato real pro
    # politica no formato curto.

    if fact_label:
        # força um fato específico de src.politica_data.FACT_FETCHERS em vez
        # de sortear — botão "Fatos reais do banco de dados" no painel.
        cmd += ["--fact-label", fact_label]

    subprocess.Popen(cmd, cwd=ROOT)
    return redirect(url_for("index", started=1))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5088, debug=False)
