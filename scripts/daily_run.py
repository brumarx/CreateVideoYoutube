#!/usr/bin/env python3
"""Orquestrador diário: roda N vídeos por canal, respeitando a cota da
YouTube Data API (~10.000 unidades/dia por projeto Cloud; upload = 1.600
unidades, então ~6 uploads/dia no total, somando TODOS os canais desse
mesmo projeto Google Cloud).

Uso (via cron, 1x/dia):
  .venv/bin/python3 scripts/daily_run.py

Cada canal tem uma cota diária definida em `channels/<nome>.yaml`
(`uploads_per_day`, padrão 1). Tópicos ficam em `channels/<nome>.yaml`
(`topics`, uma lista) — cada rodada consome o próximo tópico não usado
ainda (controlado via `data/topics_state.json`); quando a lista acaba,
recomeça do início. O canal `politica` ignora tópicos fixos e usa sempre
um fato real aleatório do banco de transparência.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "topics_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily_run")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _next_topic(channel_name: str, topics: list[str], state: dict) -> str | None:
    if not topics:
        return None
    idx = state.get(channel_name, 0) % len(topics)
    state[channel_name] = idx + 1
    return topics[idx]


def run_channel(channel_name: str, cfg: dict, state: dict) -> None:
    uploads_per_day = cfg.get("uploads_per_day", 1)
    topics = cfg.get("topics", [])
    token_file = ROOT / "credentials" / f"token_{channel_name}.json"

    if not token_file.exists():
        log.warning("[%s] sem token OAuth (%s) — pulando, rode auth_youtube.py primeiro", channel_name, token_file.name)
        return

    for i in range(uploads_per_day):
        if channel_name == "politica":
            topic = None  # run_pipeline.py busca fato real sozinho
        else:
            topic = _next_topic(channel_name, topics, state)
            if topic is None:
                log.warning("[%s] sem tópicos configurados em channels/%s.yaml — pulando", channel_name, channel_name)
                continue

        cmd = [
            sys.executable, str(ROOT / "scripts" / "run_pipeline.py"),
            "--channel", channel_name,
        ]
        if topic:
            cmd += ["--topic", topic]

        log.info("[%s] upload %d/%d — tópico: %s", channel_name, i + 1, uploads_per_day, topic or "(fato real aleatório)")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            log.error("[%s] pipeline falhou (exit %d) — seguindo pros próximos", channel_name, result.returncode)


def main() -> None:
    state = _load_state()
    channels_dir = ROOT / "channels"

    for yaml_path in sorted(channels_dir.glob("*.yaml")):
        channel_name = yaml_path.stem
        cfg = yaml.safe_load(yaml_path.read_text())
        run_channel(channel_name, cfg, state)
        _save_state(state)  # salva progresso incremental, não só no fim

    log.info("rodada diária concluída")


if __name__ == "__main__":
    main()
