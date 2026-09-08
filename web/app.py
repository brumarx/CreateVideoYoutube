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
from pathlib import Path

import yaml
from flask import Flask, redirect, render_template_string, request, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import recent_jobs  # noqa: E402
from src.topics import STATE_FILE as USED_TOPICS_FILE  # noqa: E402

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Painel — YouTube AI Pipeline</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 32px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: #9aa0a8; font-size: 13px; margin-bottom: 28px; }
  .channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 36px; }
  .card { background: #171a21; border: 1px solid #262b35; border-radius: 10px; padding: 18px; }
  .card h2 { font-size: 16px; margin: 0 0 6px; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 20px; margin-left: 6px; }
  .ok { background: #16351f; color: #5fd685; }
  .warn { background: #3a2a12; color: #e0a94f; }
  .niche { color: #9aa0a8; font-size: 13px; margin: 6px 0 12px; }
  .meta { font-size: 12px; color: #7d838c; }
  form { margin-top: 12px; }
  button { background: #2d6cdf; color: white; border: none; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { background: #3d7bef; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #262b35; }
  th { color: #9aa0a8; font-weight: 500; }
  a { color: #6ea8ff; }
  .status-uploaded { color: #5fd685; }
  .status-failed, .status-error { color: #e05f5f; }
  .flash { background: #16351f; color: #5fd685; padding: 10px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 13px; }
  .durations { display: flex; gap: 8px; margin: 10px 0; }
  .durations label { flex: 1; font-size: 11px; color: #9aa0a8; }
  .durations input { width: 100%; box-sizing: border-box; padding: 5px 6px; margin-top: 2px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #e6e6e6; font-size: 12px; }
  .save-link { background: none; border: none; color: #6ea8ff; font-size: 11px; cursor: pointer; padding: 0; text-decoration: underline; }
  details.topics { margin-top: 10px; font-size: 12px; }
  details.topics summary { cursor: pointer; color: #9aa0a8; }
  details.topics summary:hover { color: #c7cbd1; }
  .topic-edit { display: flex; gap: 4px; margin-top: 4px; align-items: center; }
  .topic-edit input[type=text] { flex: 1; min-width: 0; box-sizing: border-box; padding: 4px 6px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #c7cbd1; font-size: 12px; }
  .topic-edit input[type=text].used { color: #55595f; text-decoration: line-through; }
  .topic-edit button { padding: 4px 8px; font-size: 11px; }
  .topic-edit button.danger { background: #3a1616; color: #e08f8f; }
  .topic-edit button.danger:hover { background: #4a1c1c; }
  .topic-add { display: flex; gap: 6px; margin-top: 8px; }
  .topic-add input { flex: 1; min-width: 0; box-sizing: border-box; padding: 5px 6px; background: #0f1115; border: 1px solid #262b35; border-radius: 6px; color: #e6e6e6; font-size: 12px; }
  .topic-add button { padding: 5px 10px; font-size: 12px; }
</style>
</head>
<body>
  <h1>Pipeline de vídeos IA</h1>
  <div class="sub">Roda todo dia às 10h via cron. Uploads saem {{ 'públicos' if any_public else 'privados' }} direto.</div>

  {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

  <div class="channels">
    {% for c in channels %}
    <div class="card">
      <h2>{{ c.name }}
        {% if c.has_token %}<span class="badge ok">token OK</span>{% else %}<span class="badge warn">sem token</span>{% endif %}
      </h2>
      <div class="niche">{{ c.niche }}</div>
      <div class="meta">{{ c.uploads_per_day }} vídeo(s)/dia · privacidade: {{ c.upload_privacy }}</div>

      <form method="post" action="{{ url_for('save_duration', channel=c.name) }}">
        <label style="display:block; font-size:11px; color:#9aa0a8; margin-top:6px;">Formato do cron diário
          <select name="daily_format" style="width:100%; box-sizing:border-box; padding:5px 6px; margin-top:2px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6; font-size:12px;">
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
        {% if c.name == "politica" %}
        <div style="font-size:11px; color:#7d838c; margin-top:6px;">no formato curto, política ignora esta fila e usa sempre um fato real do banco de transparência — só o formato longo consome estes temas.</div>
        {% endif %}
      </details>

      {% if c.has_token %}
      <form method="post">
        <input type="text" name="topic" placeholder="Tema (opcional — vazio sorteia da lista)"
               style="width:100%; box-sizing:border-box; padding:6px 8px; margin-bottom:8px; background:#0f1115; border:1px solid #262b35; border-radius:6px; color:#e6e6e6; font-size:12px;">
        <button type="submit" formaction="{{ url_for('run_channel', channel=c.name) }}">Rodar short</button>
        <button type="submit" formaction="{{ url_for('run_channel', channel=c.name, long=1) }}" style="margin-left:6px">Rodar longo (16:9)</button>
      </form>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  <h1>Jobs recentes</h1>
  <table>
    <tr><th>ID</th><th>Canal</th><th>Tópico</th><th>Status</th><th>Vídeo</th></tr>
    {% for j in jobs %}
    <tr>
      <td>{{ j.id }}</td>
      <td>{{ j.channel }}</td>
      <td>{{ j.topic[:60] }}</td>
      <td class="status-{{ j.status }}">{{ j.status }}</td>
      <td>{% if j.youtube_video_id %}<a href="https://youtu.be/{{ j.youtube_video_id }}" target="_blank">assistir</a>{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
"""


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
        token_file = ROOT / "credentials" / f"token_{name}.json"
        topics = cfg.get("topics", [])
        used_topics = set(used.get(name, []))
        channels.append(
            {
                "name": name,
                "niche": cfg.get("niche", ""),
                "uploads_per_day": cfg.get("uploads_per_day", 1),
                "upload_privacy": cfg.get("upload_privacy", "private"),
                "has_token": token_file.exists(),
                "topics": topics,
                "used_topics": used_topics,
                "topics_pending": len([t for t in topics if t not in used_topics]),
                "short_min_minutes": cfg.get("short_min_minutes", 3),
                "short_max_minutes": cfg.get("short_max_minutes", 6),
                "long_min_minutes": cfg.get("long_min_minutes", 15),
                "long_max_minutes": cfg.get("long_max_minutes", 20),
                "daily_format": cfg.get("daily_format", "short"),
            }
        )
    return channels


DURATION_FIELDS = ("short_min_minutes", "short_max_minutes", "long_min_minutes", "long_max_minutes")


@app.route("/")
def index():
    channels = _load_channels()
    jobs = recent_jobs(limit=30)
    any_public = any(c["upload_privacy"] == "public" for c in channels)
    if request.args.get("saved") == "1":
        flash = "Duração salva."
    elif request.args.get("topic_added") == "1":
        flash = "Tema adicionado à fila."
    elif request.args.get("busy") == "1":
        flash = "Já tem um vídeo sendo gerado agora — espere terminar antes de rodar outro (evita estourar a memória da máquina)."
    else:
        flash = None
    return render_template_string(TEMPLATE, channels=channels, jobs=jobs, any_public=any_public, flash=flash)


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


@app.route("/topics/<channel>", methods=["POST"])
def add_topic(channel: str):
    """Adiciona um tema fixo na fila do canal (`topics` — fila única pra
    curto e longo) — fica lá esperando o pipeline consumir (cron ou disparo
    manual, nunca repete — ver src/topics.py)."""
    path = ROOT / "channels" / f"{channel}.yaml"
    topic = request.form.get("topic", "").strip()
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
    new_topic = request.form.get("topic", "").strip()
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

    long_form = request.args.get("long") == "1"
    custom_topic = request.form.get("topic", "").strip()
    # nice/ionice — mesmo tratamento que o cron já dá pro daily_run.py, pra
    # não competir por CPU/IO com os outros serviços da máquina quando
    # disparado manualmente pelo painel.
    cmd = ["nice", "-n", "19", "ionice", "-c", "3", sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), "--channel", channel]

    if long_form:
        cmd.append("--long")

    if custom_topic:
        cmd += ["--topic", custom_topic]
    # sem tema digitado: run_pipeline.py escolhe sozinho da fila única do
    # canal (nunca repete — ver src/topics.py), ou usa fato real pro
    # politica no formato curto.

    subprocess.Popen(cmd, cwd=ROOT)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5088, debug=False)
