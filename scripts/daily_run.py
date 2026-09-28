#!/usr/bin/env python3
"""Orquestrador diário: roda N vídeos por canal, respeitando a cota da
YouTube Data API (~10.000 unidades/dia por projeto Cloud; upload = 1.600
unidades, então ~6 uploads/dia no total, somando TODOS os canais desse
mesmo projeto Google Cloud).

Uso (via cron, 1x/dia):
  .venv/bin/python3 scripts/daily_run.py

Cada canal tem uma cota diária definida em `channels/<nome>.yaml`
(`uploads_per_day`, padrão 1). Uma fila única de temas fica em
`channels/<nome>.yaml` -> `topics` (o mesmo tema pode virar um vídeo curto
ou longo, dependendo de `daily_format` — só sai 1 vídeo/dia por canal
mesmo, não faz sentido ter fila separada por formato). run_pipeline.py
escolhe o próximo tema sozinho e nunca repete um já usado (registro em
`data/used_topics.json`, ver `src/topics.py`); quando a lista fixa acaba,
gera um tema novo via LLM dentro do nicho do canal. O canal `politica` no
formato curto ignora a fila e usa sempre um fato real aleatório do banco de
transparência.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily_run")


# mesmo valor de scripts/run_pipeline.py -> EXIT_SCRIPT_REJECTED
EXIT_SCRIPT_REJECTED = 3


def _run_pipeline(channel_name: str, extra: list[str], label: str) -> None:
    """Roda o pipeline; roteiro reprovado na revisão de fatos ganha UMA nova
    tentativa (tema/notícia/dado já usado fica marcado, então sai outro) —
    melhor um vídeo certo do que o dia perdido. Qualquer outra falha não é
    repetida aqui (upload falho é do retry_uploads, sem vídeo duplicado)."""
    cmd = [sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), "--channel", channel_name, *extra]
    for attempt in (1, 2):
        log.info("[%s] %s (tentativa %d)", channel_name, label, attempt)
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc == 0:
            return
        if rc != EXIT_SCRIPT_REJECTED:
            log.error("[%s] %s falhou (exit %d) — seguindo pros próximos", channel_name, label, rc)
            return
        log.warning("[%s] %s: roteiro reprovado na revisão de fatos", channel_name, label)
    log.error("[%s] %s: reprovado 2x — sem vídeo hoje", channel_name, label)


def run_channel(channel_name: str, cfg: dict) -> None:
    uploads_per_day = cfg.get("uploads_per_day", 1)
    daily_format = cfg.get("daily_format", "short")
    token_file = ROOT / "credentials" / f"token_{channel_name}.json"

    if not token_file.exists():
        log.warning("[%s] sem token OAuth (%s) — pulando, rode auth_youtube.py primeiro", channel_name, token_file.name)
        return

    if channel_name == "botafogo":
        # 2 vídeos independentes: prévia/pós-jogo quando houver jogo (regras
        # de sempre, silêncio sem jogo) + 1 por dia com temas do botafogo.win
        for task in ("jogo", "portal"):
            _run_pipeline(channel_name, ["--botafogo-task", task], task)
        return

    for i in range(uploads_per_day):
        # sem --long/--no-long: run_pipeline.py lê channels/<nome>.yaml ->
        # daily_format sozinho (fonte única de verdade, editável no painel —
        # não duplica a decisão aqui pra nunca dessincronizar da UI).
        # sem --topic: run_pipeline.py escolhe sozinho da fila única (ou
        # fato real, pro politica no formato curto).
        _run_pipeline(channel_name, [], f"upload {i + 1}/{uploads_per_day} ({daily_format})")


def main() -> None:
    channels_dir = ROOT / "channels"

    for yaml_path in sorted(channels_dir.glob("*.yaml")):
        channel_name = yaml_path.stem
        cfg = yaml.safe_load(yaml_path.read_text())
        run_channel(channel_name, cfg)

    # vídeos já renderizados cujo upload falhou (hoje ou antes — ex. token
    # OAuth expirado e reautorizado depois). Roda no fim: upload que falhou
    # por token não gasta cota, então sobra cota pro reenvio.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "retry_uploads.py")], cwd=ROOT)

    log.info("rodada diária concluída")


if __name__ == "__main__":
    main()
