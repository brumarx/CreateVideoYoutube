#!/usr/bin/env python3
"""Painel de administração do pipeline — lista canais, jobs recentes e
permite disparar uma rodada manual por canal. Roda local, acessível na rede
local (mesmo esquema do ariaBot: http://192.168.1.70:PORTA).

Uso: .venv/bin/python3 web/app.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import yaml
from flask import Flask, redirect, render_template_string, request, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import AZURE_SPEECH_KEYS, ChannelConfig  # noqa: E402
from src.tts import EDGE_VOICES, azure_voices  # noqa: E402
from src.orchestrator import get_job, jobs_with_status, recent_jobs, update  # noqa: E402
from src.politica_data import FACT_FETCHERS  # noqa: E402
from src.topics import STATE_FILE as USED_TOPICS_FILE  # noqa: E402

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Painel — YouTube AI Pipeline</title>
<style>
  * { -webkit-tap-highlight-color: transparent; }
  html { -webkit-text-size-adjust: 100%; }
  body { font-family: system-ui, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 32px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: #9aa0a8; font-size: 13px; margin-bottom: 28px; }
  .channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 36px; }
  .card { background: #171a21; border: 1px solid #262b35; border-radius: 10px; padding: 18px; min-width: 0; }
  .card h2 { font-size: 16px; margin: 0 0 6px; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 20px; margin-left: 6px; }
  .ok { background: #16351f; color: #5fd685; }
  .warn { background: #3a2a12; color: #e0a94f; }
  .niche { color: #9aa0a8; font-size: 13px; margin: 6px 0 12px; }
  .meta { font-size: 12px; color: #7d838c; }
  form { margin-top: 12px; }
  button { background: #2d6cdf; color: white; border: none; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; min-height: 34px; }
  button:hover { background: #3d7bef; }
  input, select, button { font-family: inherit; }
  input[type=text], input[type=number], select { font-size: 16px; }
  .table-wrap { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 480px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #262b35; }
  th { color: #9aa0a8; font-weight: 500; }
  a { color: #6ea8ff; }
  .status-uploaded { color: #5fd685; }
  .status-awaiting_approval { color: #e0a94f; }
  .status-rejected { color: #7d838c; }
  .status-failed, .status-error { color: #e05f5f; }
  .flash { background: #16351f; color: #5fd685; padding: 10px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 13px; }
  .durations { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
  .durations label { flex: 1; min-width: 120px; font-size: 11px; color: #9aa0a8; }
  .durations input { width: 100%; box-sizing: border-box; padding: 8px 6px; margin-top: 2px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #e6e6e6; font-size: 16px; }
  .save-link { background: none; border: none; color: #6ea8ff; font-size: 13px; cursor: pointer; padding: 6px 0; text-decoration: underline; min-height: 34px; }
  details.topics { margin-top: 10px; font-size: 12px; }
  details.topics summary { cursor: pointer; color: #9aa0a8; padding: 6px 0; }
  details.topics summary:hover { color: #c7cbd1; }
  .topic-edit { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; align-items: center; }
  .topic-edit input[type=text] { flex: 1 1 100%; min-width: 0; box-sizing: border-box; padding: 7px 8px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #c7cbd1; font-size: 14px; }
  .topic-edit input[type=text].used { color: #55595f; text-decoration: line-through; }
  .topic-edit button { padding: 7px 10px; font-size: 12px; min-height: 34px; }
  .topic-edit button.danger { background: #3a1616; color: #e08f8f; }
  .topic-edit button.danger:hover { background: #4a1c1c; }
  .topic-add { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .topic-add input { flex: 1 1 100%; min-width: 0; box-sizing: border-box; padding: 7px 8px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #e6e6e6; font-size: 14px; }
  .topic-add button { padding: 7px 14px; font-size: 12px; min-height: 34px; }

  @media (max-width: 600px) {
    body { padding: 16px; }
    h1 { font-size: 19px; }
    .channels { grid-template-columns: 1fr; gap: 12px; }
    .card { padding: 14px; }
    button { width: 100%; }
    .topic-edit button, .topic-add button { width: auto; }
    table { font-size: 12px; min-width: 420px; }
    th, td { padding: 8px 6px; }
    td.topic-cell { white-space: normal; overflow-wrap: anywhere; }
  }
</style>
</head>
<body>
  <h1>Pipeline de vídeos IA</h1>
  <div class="sub">Roda todo dia às 10h via cron. Todo roteiro passa por revisão de fatos antes de virar vídeo; canais com aprovação ligada sobem privados e esperam você aprovar aqui.</div>

  {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
  {% if total_uploads > 5 %}<div class="flash" style="background:#3a2a12; color:#e0a94f;">Soma de vídeos/dia = {{ total_uploads }}. A cota da API do YouTube aguenta ~5 uploads/dia no total (1.700 unidades cada, limite 10.000) — os últimos canais da rodada vão falhar.</div>{% endif %}

  {% if pending %}
  <h1>Aguardando aprovação ({{ pending|length }})</h1>
  <div class="sub">Estes vídeos já estão no YouTube como <b>privados</b>. Assista (logado na conta do canal) e aprove para publicar.</div>
  <div class="channels">
    {% for j in pending %}
    <div class="card">
      <div class="meta">{{ j.channel }} · job {{ j.id }}</div>
      <h2 style="margin-top:4px;">{{ j.topic }}</h2>
      <div class="meta" style="margin:6px 0 10px;">
        <a href="https://youtu.be/{{ j.youtube_video_id }}" target="_blank">assistir</a> ·
        <a href="https://studio.youtube.com/video/{{ j.youtube_video_id }}/edit" target="_blank">editar no Studio</a>
      </div>
      <div style="display:flex; gap:6px; flex-wrap:wrap;">
        <form method="post" action="{{ url_for('approve_job', job_id=j.id) }}" style="flex:1; margin:0;"><button type="submit" style="width:100%; background:#1f7a45;">Aprovar e publicar</button></form>
        <form method="post" action="{{ url_for('reject_job', job_id=j.id) }}" style="flex:1; margin:0;"><button type="submit" style="width:100%; background:#3a1616; color:#e08f8f;">Rejeitar (fica privado)</button></form>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div class="channels">
    {% for c in channels %}
    <div class="card">
      <h2>{{ c.name }}
        {% if c.token_status == "ok" %}<span class="badge ok">token OK</span>
        {% elif c.token_status == "missing" %}<span class="badge warn">sem token</span>
        {% else %}<span class="badge warn" title="rode scripts/auth_youtube.py --channel {{ c.name }}">token expirado</span>{% endif %}
      </h2>
      {% if c.youtube_handle %}
      <div class="meta"><a href="https://www.youtube.com/{{ c.youtube_handle }}" target="_blank">youtube.com/{{ c.youtube_handle }}</a></div>
      {% endif %}
      <div class="niche">{{ c.niche }}</div>
      <form method="post" action="{{ url_for('save_publishing', channel=c.name) }}">
        <div class="durations">
          <label>Vídeos por dia<input type="number" min="0" max="6" step="1" name="uploads_per_day" value="{{ c.uploads_per_day }}"></label>
          <label>Privacidade
            <select name="upload_privacy" style="width:100%; box-sizing:border-box; padding:8px 6px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6; font-size:16px;">
              {% for opt, lbl in [("public", "público"), ("unlisted", "não listado"), ("private", "privado")] %}
              <option value="{{ opt }}" {{ "selected" if c.upload_privacy == opt }}>{{ lbl }}</option>
              {% endfor %}
            </select>
          </label>
        </div>
        <label style="display:flex; align-items:center; gap:8px; font-size:12px; color:#c7cbd1; margin-top:4px;">
          <input type="hidden" name="require_approval" value="0">
          <input type="checkbox" name="require_approval" value="1" {{ "checked" if c.require_approval }} style="width:auto;"> exigir minha aprovação antes de publicar
        </label>
        <button type="submit" class="save-link">salvar publicação</button>
      </form>

      <form method="post" action="{{ url_for('save_duration', channel=c.name) }}">
        <label style="display:block; font-size:11px; color:#9aa0a8; margin-top:6px;">Formato do cron diário
          <select name="daily_format" style="width:100%; box-sizing:border-box; padding:8px 6px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6;">
            <option value="short" {{ "selected" if c.daily_format == "short" }}>Curto (vertical, Shorts)</option>
            <option value="long" {{ "selected" if c.daily_format == "long" }}>Longo (16:9, documentário)</option>
          </select>
        </label>
        <div class="durations">
          <label>Short min (min)<input type="number" step="0.5" min="0.5" name="short_min_minutes" value="{{ c.short_min_minutes }}"></label>
          <label>Short max (min)<input type="number" step="0.5" min="0.5" name="short_max_minutes" value="{{ c.short_max_minutes }}"></label>
        </div>
        <div class="durations">
          <label>Longo min (min)<input type="number" step="0.5" min="1" name="long_min_minutes" value="{{ c.long_min_minutes }}"></label>
          <label>Longo max (min)<input type="number" step="0.5" min="1" name="long_max_minutes" value="{{ c.long_max_minutes }}"></label>
        </div>
        <button type="submit" class="save-link">salvar duração e formato</button>
      </form>

      <form method="post" action="{{ url_for('save_voices', channel=c.name) }}">
        <label style="display:block; font-size:11px; color:#9aa0a8; margin-top:10px;">Voz da narração (sorteada por vídeo, peso relativo — 0 = nunca)</label>
        <div class="durations">
          {% for v in c.voices %}
          <label>{{ v.nome }} ({{ v.genero }}){% if v.azure %} · <span style="color:#6ea8ff;">Azure</span>{% endif %}<input type="number" min="0" step="1" name="w__{{ v.id }}" value="{{ v.peso }}"></label>
          {% endfor %}
        </div>
        {% if not azure_ready %}<div style="font-size:11px; color:#7d838c;">mais vozes aparecem aqui quando AZURE_SPEECH_KEYS estiver no .env</div>{% endif %}
        <button type="submit" class="save-link">salvar vozes</button>
      </form>

      <details class="topics">
        <summary>Fila de temas ({{ c.topics_pending }} pendente(s) de {{ c.topics|length }})</summary>
        {% for t in c.topics %}
        <form class="topic-edit" method="post" action="{{ url_for('update_topic', channel=c.name) }}">
          <input type="hidden" name="index" value="{{ loop.index0 }}">
          <input type="text" name="topic" value="{{ t }}" class="{{ 'used' if t in c.used_topics }}">
          <button type="submit" title="salvar edição">salvar</button>
          <button type="submit" formaction="{{ url_for('delete_topic', channel=c.name) }}" class="danger" title="remover">remover</button>
        </form>
        {% endfor %}
        <form class="topic-add" method="post" action="{{ url_for('add_topic', channel=c.name) }}">
          <input type="text" name="topic" placeholder="Novo tema..." required>
          <button type="submit">+</button>
        </form>
        <div style="font-size:11px; color:#7d838c; margin-top:6px;">temas de lista ("N fatos/coisas sobre...") são limitados a no máximo 10 itens — números maiores são ajustados sozinhos ao salvar.</div>
        {% if c.name == "politica" %}
        <div style="font-size:11px; color:#7d838c; margin-top:6px;">no formato curto, política ignora esta fila e usa sempre um fato real do banco de transparência — só o formato longo consome estes temas.</div>
        {% endif %}
      </details>

      {% if c.uses_topic_queue %}
      <details class="topics">
        <summary>Temas virais do YouTube ({{ c.viral_share_pct }}% dos vídeos{{ "" if c.viral_queries else " — desligado, sem buscas" }})</summary>
        <form method="post" action="{{ url_for('save_viral', channel=c.name) }}">
          <div style="font-size:11px; color:#7d838c; margin: 4px 0 8px;">busca os vídeos mais vistos do mês com estas buscas e cria um tema original no mesmo gancho. Quando a fila de temas acaba, é sempre viral.</div>
          <div class="durations">
            <label>% dos vídeos com tema viral<input type="number" min="0" max="100" step="5" name="viral_share_pct" value="{{ c.viral_share_pct }}"></label>
          </div>
          <label style="display:block; font-size:11px; color:#9aa0a8;">Buscas no YouTube (uma por linha)
            <textarea name="viral_queries" rows="4" style="width:100%; box-sizing:border-box; padding:8px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6; font-size:14px; font-family:inherit;">{{ c.viral_queries|join("\n") }}</textarea>
          </label>
          <button type="submit" class="save-link">salvar temas virais</button>
        </form>
      </details>
      {% endif %}

      <details class="topics">
        <summary>Identidade e roteiro do canal</summary>
        <form method="post" action="{{ url_for('save_identity', channel=c.name) }}">
          {% for key, label, value in c.identity_text %}
          <label style="display:block; font-size:11px; color:#9aa0a8; margin-top:8px;">{{ label }}
            <input type="text" name="{{ key }}" value="{{ value }}" style="width:100%; box-sizing:border-box; padding:8px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6;">
          </label>
          {% endfor %}
          <div class="durations">
            <label>Cenas (curto)<input type="number" min="3" max="40" step="1" name="scenes_per_video" value="{{ c.scenes_per_video }}"></label>
            <label>Cenas (longo)<input type="number" min="6" max="60" step="1" name="long_form_scenes" value="{{ c.long_form_scenes }}"></label>
            <label>Cor de destaque<input type="color" name="accent" value="{{ c.accent }}" style="height:38px; padding:2px;"></label>
          </div>
          <label style="display:flex; align-items:center; gap:8px; font-size:12px; color:#c7cbd1; margin-top:6px;">
            <input type="hidden" name="captions" value="0">
            <input type="checkbox" name="captions" value="1" {{ "checked" if c.captions }} style="width:auto;"> legenda embutida estilo karaokê
          </label>
          <label style="display:block; font-size:11px; color:#9aa0a8; margin-top:8px;">Prompt do roteiro (instruções de estilo pro LLM)
            <textarea name="prompt_base" rows="8" style="width:100%; box-sizing:border-box; padding:8px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6; font-size:14px; font-family:inherit;">{{ c.prompt_base }}</textarea>
          </label>
          <button type="submit" class="save-link">salvar identidade e roteiro</button>
        </form>
      </details>

      {% if c.fact_labels %}
      <details class="topics">
        <summary>Fatos reais do banco de dados ({{ c.fact_labels|length }} tipos — sorteado no formato curto)</summary>
        <div style="font-size:11px; color:#7d838c; margin: 4px 0 8px;">estes não são editáveis (vêm de src/politica_data.py, não do yaml) — escolha um pra forçar esse fato específico numa rodada manual, em vez de sortear.</div>
        {% for label in c.fact_labels %}
        <form method="post" action="{{ url_for('run_channel', channel=c.name) }}" style="margin-bottom:4px;">
          <input type="hidden" name="fact_label" value="{{ label }}">
          <button type="submit" style="width:100%; text-align:left; padding:4px 6px; font-size:12px;">{{ label }}</button>
        </form>
        {% endfor %}
      </details>
      {% endif %}

      {% if c.token_status == "ok" %}
      <form method="post" style="margin-top:12px; padding:8px; border:1px solid #2a7a4a; border-radius:8px; background:#0f1a13;">
        <label style="display:block; font-size:11px; color:#5fd88a; margin-bottom:4px; font-weight:600;">▶ RODAR AGORA (tema digitado aqui é buscado na internet antes de escrever)</label>
        <input type="text" name="topic" placeholder="Tema (opcional — vazio sorteia da lista)"
               style="width:100%; box-sizing:border-box; padding:8px; margin-bottom:8px; background:#0f1115; border:1px solid #2a7a4a; border-radius:6px; color:#e6e6e6; font-size:16px;">
        <div style="display:flex; flex-wrap:wrap; gap:6px;">
          <button type="submit" formaction="{{ url_for('run_channel', channel=c.name, long=0) }}" style="flex:1; min-width:120px;">Rodar short</button>
          <button type="submit" formaction="{{ url_for('run_channel', channel=c.name, long=1) }}" style="flex:1; min-width:120px;">Rodar longo (16:9)</button>
        </div>
      </form>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  <h1>Jobs recentes</h1>
  <div class="table-wrap">
  <table>
    <tr><th>ID</th><th>Canal</th><th>Tópico</th><th>Status</th><th>Vídeo</th></tr>
    {% for j in jobs %}
    <tr>
      <td>{{ j.id }}</td>
      <td>{{ j.channel }}</td>
      <td class="topic-cell">{{ j.topic }}</td>
      <td class="status-{{ j.status }}">{{ j.status }}</td>
      <td>{% if j.youtube_video_id %}<a href="https://youtu.be/{{ j.youtube_video_id }}" target="_blank">assistir</a>{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
</body>
</html>
"""


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
                "youtube_handle": cfg.get("youtube_handle", ""),
                "niche": cfg.get("niche", ""),
                "uploads_per_day": cfg.get("uploads_per_day", 1),
                "upload_privacy": cfg.get("upload_privacy", "private"),
                "token_status": _token_status(name),
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
    ("niche", "Nicho (usado pra gerar temas e roteiro)"),
    ("hashtags", "Hashtags (separadas por espaço, 3-5)"),
    ("language", "Idioma"),
)

DURATION_FIELDS = ("short_min_minutes", "short_max_minutes", "long_min_minutes", "long_max_minutes")


@app.route("/")
def index():
    channels = _load_channels()
    jobs = recent_jobs(limit=30)
    any_public = any(c["upload_privacy"] == "public" for c in channels)
    total_uploads = sum(int(c["uploads_per_day"] or 0) for c in channels)
    if request.args.get("saved") == "1":
        flash = "Configuração salva."
    elif request.args.get("topic_added") == "1":
        flash = "Tema adicionado à fila."
    elif request.args.get("approved") == "1":
        flash = "Vídeo publicado."
    elif request.args.get("approve_error"):
        flash = f"Não consegui publicar: {request.args['approve_error']}"
    elif request.args.get("busy") == "1":
        flash = "Já tem um vídeo sendo gerado agora — espere terminar antes de rodar outro (evita estourar a memória da máquina)."
    else:
        flash = None
    return render_template_string(
        TEMPLATE, channels=channels, jobs=jobs, any_public=any_public, flash=flash, total_uploads=total_uploads,
        pending=jobs_with_status("awaiting_approval"),
        azure_ready=bool(AZURE_SPEECH_KEYS),
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
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5088, debug=False)
