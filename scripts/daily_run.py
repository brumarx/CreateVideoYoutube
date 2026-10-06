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

import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import DB_PATH, update  # noqa: E402
from src.upload import UPLOAD_META_FILE  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily_run")


# mesmo valor de scripts/run_pipeline.py -> EXIT_SCRIPT_REJECTED
EXIT_SCRIPT_REJECTED = 3


# Tentativas por vídeo. Cada uma pega outro tema/notícia/dado (o usado fica
# marcado). Antes era só 1 repetição, e só pra roteiro reprovado: o politica
# passou 27-30/09 sem vídeo, e falha passageira (TTS "No audio was received",
# Pollinations fora, JSON malformado do LLM) perdia o dia direto.
MAX_ATTEMPTS = 4

# Pasta output/job_N só era apagada depois de upload com sucesso — falha,
# retentativa e abandono deixavam tudo pra trás (9,9 GB em 06/10). Apaga a
# de job que já terminou sem volta, depois de uns dias pra dar tempo de
# investigar a falha. retry_uploads.py só reenvia "failed" (com
# upload_meta.json), então "rendered" de dias atrás é execução que morreu
# no meio e "dry_run" é teste — os dois também saem.
CLEANUP_STATUSES = ("failed", "retried", "abandoned", "deleted", "uploaded", "rendered", "dry_run", "script_ready", "rendered_from_script")
CLEANUP_AFTER_DAYS = 3
# upload que falhou (token OAuth vence a cada ~7 dias) fica com
# upload_meta.json esperando o retry_uploads — guarda por mais tempo
RETRY_KEEP_DAYS = 14


def _last_job(channel_name: str, after_id: int):
    """Job criado por esta tentativa (id maior que o último antes dela)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT id, status, video_path FROM jobs WHERE channel = ? AND id > ? ORDER BY id DESC LIMIT 1",
            (channel_name, after_id),
        ).fetchone()


def _max_job_id() -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM jobs").fetchone()[0]


def cleanup_output() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        old = {
            row[0]: (row[1], row[2]) for row in conn.execute(
                f"SELECT id, status, datetime(updated_at) < datetime('now', ?) FROM jobs "
                f"WHERE status IN ({','.join('?' * len(CLEANUP_STATUSES))}) "
                "AND datetime(updated_at) < datetime('now', ?)",
                (f"-{RETRY_KEEP_DAYS} days", *CLEANUP_STATUSES, f"-{CLEANUP_AFTER_DAYS} days"),
            )
        }
    freed = removed = 0
    for job_dir in (ROOT / "output").glob("job_*"):
        try:
            job_id = int(job_dir.name.removeprefix("job_"))
        except ValueError:
            continue
        if job_id not in old:
            continue
        status, past_retry_window = old[job_id]
        if status == "failed" and (job_dir / UPLOAD_META_FILE).exists() and not past_retry_window:
            continue  # vídeo pronto esperando reenvio
        freed += sum(f.stat().st_size for f in job_dir.rglob("*") if f.is_file())
        shutil.rmtree(job_dir, ignore_errors=True)
        removed += 1
    if removed:
        log.info("limpeza: %d pasta(s) de job antigo removida(s), %.1f GB liberados", removed, freed / 1e9)


def _run_pipeline(channel_name: str, extra: list[str], label: str, fallback_short: bool = False) -> None:
    """Roda o pipeline até MAX_ATTEMPTS vezes — melhor um vídeo certo do que
    o dia perdido. Repete roteiro reprovado na revisão de fatos e qualquer
    falha ANTES do render; falha depois do render (upload: token, rede, cota)
    NÃO repete — é do retry_uploads, que reenvia o mesmo vídeo sem duplicar.
    A tentativa que falhou e foi substituída vira status "retried" (erro
    guardado), pra "failed" no painel ser só vídeo perdido de verdade.
    fallback_short: canal no formato longo tenta a última vez no curto (no
    politica, o curto usa sempre dado real do banco — o que mais passa na
    revisão)."""
    base_cmd = [sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), "--channel", channel_name, *extra]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cmd = base_cmd
        if fallback_short and attempt == MAX_ATTEMPTS:
            cmd = [*base_cmd, "--no-long"]
            log.warning("[%s] %s: última tentativa no formato curto", channel_name, label)
        log.info("[%s] %s (tentativa %d/%d)", channel_name, label, attempt, MAX_ATTEMPTS)
        before = _max_job_id()
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc == 0:
            return
        job = _last_job(channel_name, before)
        if job is None:
            # nem criou job: erro de configuração, repetir não resolve
            log.error("[%s] %s falhou (exit %d) sem criar job — seguindo pros próximos", channel_name, label, rc)
            return
        job_id, status, video_path = job
        if video_path:
            log.error("[%s] %s: vídeo %d renderizado mas não publicado — fica pro retry_uploads", channel_name, label, job_id)
            return
        motivo = "roteiro reprovado na revisão de fatos" if rc == EXIT_SCRIPT_REJECTED else f"falhou antes do render (exit {rc})"
        if attempt == MAX_ATTEMPTS:
            log.error("[%s] %s: %s — %d tentativas, sem vídeo hoje", channel_name, label, motivo, MAX_ATTEMPTS)
            return
        log.warning("[%s] %s: %s (job %d) — tentando de novo", channel_name, label, motivo, job_id)
        if status == "failed":
            update(job_id, status="retried")


def _render_approved(channel_name: str) -> None:
    """Renderiza (sem publicar) o roteiro aprovado mais antigo do canal com
    o comando salvo pelo --script-only. Falhou (ex.: revisão do vídeo):
    volta pra fila normal do dia, o job vira 'abandoned'."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE channel = ? AND status = 'script_approved' ORDER BY id LIMIT 1",
            (channel_name,),
        ).fetchone()
    if not row:
        return
    cmd_file = ROOT / "output" / f"job_{row[0]}" / "render_cmd.json"
    if not cmd_file.exists():
        update(row[0], status="abandoned", error="render_cmd.json sumiu")
        return
    cmd = json.loads(cmd_file.read_text())
    log.info("[%s] renderizando roteiro aprovado (job %s)", channel_name, row[0])
    rc = subprocess.run([sys.executable, *cmd[1:]], cwd=ROOT).returncode
    update(row[0], status="rendered_from_script" if rc == 0 else "abandoned",
           error=None if rc == 0 else f"render do roteiro aprovado falhou (exit {rc})")


def run_channel(channel_name: str, cfg: dict) -> None:
    uploads_per_day = cfg.get("uploads_per_day", 1)
    daily_format = cfg.get("daily_format", "short")
    token_file = ROOT / "credentials" / f"token_{channel_name}.json"

    if not token_file.exists():
        log.warning("[%s] sem token OAuth (%s) — pulando, rode auth_youtube.py primeiro", channel_name, token_file.name)
        return

    # roteiro revisado e aprovado (status script_approved, ver
    # run_pipeline --script-only) renderiza primeiro; vídeo já renderizado
    # com --no-upload ocupa a vaga do dia antes de gerar outro
    from retry_uploads import upload_pending

    _render_approved(channel_name)
    ready = upload_pending(channel_name, "ready", limit=1)

    if channel_name == "botafogo":
        # 2 vídeos independentes: prévia/pós-jogo quando houver jogo (regras
        # de sempre, silêncio sem jogo) + 1 por dia com temas do botafogo.win
        for task in ("jogo", "portal") if not ready else ("jogo",):
            _run_pipeline(channel_name, ["--botafogo-task", task], task)
        return

    for i in range(ready, uploads_per_day):
        # sem --long/--no-long: run_pipeline.py lê channels/<nome>.yaml ->
        # daily_format sozinho (fonte única de verdade, editável no painel —
        # não duplica a decisão aqui pra nunca dessincronizar da UI).
        # sem --topic: run_pipeline.py escolhe sozinho da fila única (ou
        # fato real, pro politica no formato curto).
        _run_pipeline(
            channel_name, [], f"upload {i + 1}/{uploads_per_day} ({daily_format})",
            fallback_short=daily_format == "long",
        )


def main() -> None:
    channels_dir = ROOT / "channels"
    cleanup_output()

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
