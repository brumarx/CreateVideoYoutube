#!/usr/bin/env python3
"""Orquestrador diário: roda N vídeos por canal, respeitando a cota da
YouTube Data API (~10.000 unidades/dia por projeto Cloud; upload = 1.600
unidades, então ~6 uploads/dia no total, somando TODOS os canais desse
mesmo projeto Google Cloud).

Uso (via cron, 1x/dia):
  .venv/bin/python3 scripts/daily_run.py

Cada canal tem uma cota diária definida em `channels/<nome>.yaml`
(`uploads_per_day`, padrão 1). Tópicos ficam em `channels/<nome>.yaml`
(`topics` pro formato curto, `long_form_topics` pro longo) — nunca repete um
tema já usado (registro persistente em `data/used_topics.json`, ver
`src/topics.py`); quando a lista fixa acaba, gera um tema novo via LLM
dentro do nicho do canal. O canal `politica` no formato curto ignora
tópicos fixos e usa sempre um fato real aleatório do banco de transparência.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.topics import pick_topic  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily_run")


def run_channel(channel_name: str, cfg: dict) -> None:
    uploads_per_day = cfg.get("uploads_per_day", 1)
    topics = cfg.get("topics", [])
    # curto (padrão) ou longo — configurável por canal em channels/<nome>.yaml
    # (daily_format), editável no painel web.
    long_form = cfg.get("daily_format", "short") == "long"
    token_file = ROOT / "credentials" / f"token_{channel_name}.json"

    if not token_file.exists():
        log.warning("[%s] sem token OAuth (%s) — pulando, rode auth_youtube.py primeiro", channel_name, token_file.name)
        return

    for i in range(uploads_per_day):
        if long_form:
            # run_pipeline.py já sorteia sozinho de long_form_topics (ou, pro
            # canal politica, também usa long_form_topics em vez de fato
            # aleatório — ver run_pipeline.py) quando --topic não é passado.
            topic = None
        elif channel_name == "politica":
            topic = None  # run_pipeline.py busca fato real sozinho
        elif not topics:
            log.warning("[%s] sem tópicos configurados em channels/%s.yaml — pulando", channel_name, channel_name)
            continue
        else:
            topic = pick_topic(channel_name, "short", topics, cfg.get("niche", ""))

        cmd = [
            sys.executable, str(ROOT / "scripts" / "run_pipeline.py"),
            "--channel", channel_name,
        ]
        if long_form:
            cmd.append("--long")
        if topic:
            cmd += ["--topic", topic]

        log.info(
            "[%s] upload %d/%d (%s) — tópico: %s",
            channel_name, i + 1, uploads_per_day, "longo" if long_form else "curto",
            topic or "(sorteado automaticamente)",
        )
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            log.error("[%s] pipeline falhou (exit %d) — seguindo pros próximos", channel_name, result.returncode)


def main() -> None:
    channels_dir = ROOT / "channels"

    for yaml_path in sorted(channels_dir.glob("*.yaml")):
        channel_name = yaml_path.stem
        cfg = yaml.safe_load(yaml_path.read_text())
        run_channel(channel_name, cfg)

    log.info("rodada diária concluída")


if __name__ == "__main__":
    main()
