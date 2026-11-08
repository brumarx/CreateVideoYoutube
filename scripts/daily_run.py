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
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import DB_PATH, update  # noqa: E402
from src.upload import UPLOAD_META_FILE  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily_run")


# erro que é do ROTEIRO (não dá pra reaproveitar o script.json salvo)
_SCRIPT_PROBLEMS = ("longo demais", "roteiro do arquivo", "revisão de fatos")

# mesmo valor de scripts/run_pipeline.py -> EXIT_SCRIPT_REJECTED
EXIT_SCRIPT_REJECTED = 3


# Tentativas por vídeo. Cada uma pega outro tema/notícia/dado (o usado fica
# marcado). Antes era só 1 repetição, e só pra roteiro reprovado: o politica
# passou 27-30/09 sem vídeo, e falha passageira (TTS "No audio was received",
# Pollinations fora, JSON malformado do LLM) perdia o dia direto.
MAX_ATTEMPTS = 4
# --no-upload: gera e renderiza tudo sem subir (vídeo fica "ready" e a rodada
# seguinte publica ele no lugar de gerar outro) — pra testar fora do horário
# sem gastar a cota do YouTube, que é uma só pros 5 canais
NO_UPLOAD = "--no-upload" in sys.argv
# a partir da última tentativa normal, o vídeo do dia SEMPRE sai
# (--must-publish: revisões cortam frase/avisam em vez de descartar); repete
# esse modo até MUST_PUBLISH_ATTEMPTS vezes se ainda cair (render, LLM fora)
MUST_PUBLISH_ATTEMPTS = 3

# Pasta output/job_N só era apagada depois de upload com sucesso — falha,
# retentativa e abandono deixavam tudo pra trás (9,9 GB em 06/10). Apaga a
# de job que já terminou sem volta, depois de uns dias pra dar tempo de
# investigar a falha. retry_uploads.py só reenvia "failed" (com
# upload_meta.json), então "rendered" de dias atrás é execução que morreu
# no meio e "dry_run" é teste — os dois também saem. "scripted"/"narrated"
# são etapas intermediárias como "rendered": parado nelas dias depois é
# execução morta (pasta de 06/10 com 0,28 GB ficou esquecida). "pending" não
# entra — é fila que o orchestrator ainda vai pegar.
CLEANUP_STATUSES = ("failed", "retried", "abandoned", "deleted", "uploaded", "rendered", "dry_run", "script_ready", "rendered_from_script", "scripted", "narrated", "pending")
# job que parou no meio (nunca publicado) há mais de CLEANUP_AFTER_DAYS vira
# abandoned — sem isso ficava "pending"/"scripted" pra sempre (112 em 03/11)
# "dry_run" não entra: desde 03/11 grava upload_meta e é publicado depois da
# revisão manual (retry_uploads --status dry_run) — virava abandoned em 3 dias
STALE_STATUSES = ("pending", "scripted", "script_ready", "narrated", "rendered", "rendered_from_script")
CLEANUP_AFTER_DAYS = 3
# upload que falhou (token OAuth vence a cada ~7 dias) fica com
# upload_meta.json esperando o retry_uploads — guarda por mais tempo
RETRY_KEEP_DAYS = 14


def _last_job(channel_name: str, after_id: int):
    """Job criado por esta tentativa (id maior que o último antes dela)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT id, status, video_path, topic, error FROM jobs WHERE channel = ? AND id > ? ORDER BY id DESC LIMIT 1",
            (channel_name, after_id),
        ).fetchone()


def _max_job_id() -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM jobs").fetchone()[0]


def cleanup_output() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        stale = conn.execute(
            f"UPDATE jobs SET status = 'abandoned', video_path = NULL, thumbnail_path = NULL "
            f"WHERE status IN ({','.join('?' * len(STALE_STATUSES))}) AND datetime(updated_at) < datetime('now', ?)",
            (*STALE_STATUSES, f"-{CLEANUP_AFTER_DAYS} days"),
        ).rowcount
    if stale:
        log.info("limpeza: %d job(s) parado(s) há mais de %d dias, nunca publicado(s), marcado(s) como abandoned",
                 stale, CLEANUP_AFTER_DAYS)
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
        if status in ("failed", "dry_run") and (job_dir / UPLOAD_META_FILE).exists() and not past_retry_window:
            continue  # vídeo pronto esperando reenvio / revisão manual do teste (até 14 dias)
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
    if NO_UPLOAD:
        base_cmd.append("--no-upload")
    total = MAX_ATTEMPTS + MUST_PUBLISH_ATTEMPTS - 1
    for attempt in range(1, total + 1):
        cmd = base_cmd
        if attempt >= MAX_ATTEMPTS:
            # o vídeo do dia SEMPRE sai: as revisões (fatos/vídeo) cortam
            # frase e avisam em vez de descartar. O roteiro novo já recebe os
            # motivos das reprovações anteriores (script_gen.recent_mistakes)
            cmd = [*cmd, "--must-publish"]
        if fallback_short and attempt >= MAX_ATTEMPTS:
            cmd = [*cmd, "--no-long"]
            log.warning("[%s] %s: última tentativa no formato curto", channel_name, label)
        log.info("[%s] %s (tentativa %d/%d)", channel_name, label, attempt, total)
        before = _max_job_id()
        rc = _run_limited(cmd, _timeout_for(cmd, channel_name))
        if rc == 0:
            return
        job = _last_job(channel_name, before)
        if job is None:
            # nem criou job: erro de configuração, repetir não resolve
            log.error("[%s] %s falhou (exit %d) sem criar job — seguindo pros próximos", channel_name, label, rc)
            return
        job_id, status, video_path, topic, error = job
        if rc == 124:
            # travado além do limite: não repete (outra tentativa travaria de novo, horas de rodada)
            update(job_id, status="failed", error=f"tempo esgotado ({_timeout_for(cmd, channel_name) // 60} min)")
            log.error("[%s] %s: interrompido por tempo — job %d marcado como falha, sem repetir", channel_name, label, job_id)
            return
        if video_path:
            log.error("[%s] %s: vídeo %d renderizado mas não publicado — fica pro retry_uploads", channel_name, label, job_id)
            return
        # o motivo gravado no job: exit 3 é qualquer reprovação (tema, fatos,
        # Short longo demais, áudio, vídeo), não só a revisão de fatos
        motivo = (error or "").splitlines()[0][:160] if error else (
            "roteiro reprovado" if rc == EXIT_SCRIPT_REJECTED else f"falhou antes do render (exit {rc})"
        )
        if attempt == total:
            log.error("[%s] %s: %s — %d tentativas, sem vídeo hoje", channel_name, label, motivo, total)
            return
        log.warning("[%s] %s: %s (job %d) — tentando de novo", channel_name, label, motivo, job_id)
        if status == "failed":
            update(job_id, status="retried")
        # o tema escolhido é o que vai pro YouTube: a tentativa seguinte
        # refaz o MESMO tema (antes sorteava outro e jogava fora o roteiro já
        # aprovado — curiosidades 21/10: 8 temas gastos, 1 virou vídeo)
        work = ROOT / "output" / f"job_{job_id}"
        facts_file, script_file = work / "facts.json", work / "script.json"
        if facts_file.exists():
            # os dados da tentativa reprovada já saíram da fila (notícias do
            # Botafogo, fato do politica): refaz com eles, não busca outros
            same = ["--facts-file", str(facts_file)]
        elif topic:
            same = ["--topic", topic]
        else:
            continue
        if script_file.exists() and not any(k in (error or "") for k in _SCRIPT_PROBLEMS):
            # roteiro já aprovado na revisão de fatos e o que reprovou veio
            # depois (imagem, vídeo, áudio, render): refaz só essa parte
            same += ["--script-file", str(script_file)]
        base_cmd = [sys.executable, str(ROOT / "scripts" / "run_pipeline.py"), "--channel", channel_name,
                    *same, *(["--no-upload"] if NO_UPLOAD else [])]

def _render_approved(channel_name: str, lock_held: bool = False) -> None:
    """Renderiza (sem publicar) o roteiro aprovado mais antigo do canal com
    o comando salvo pelo --script-only. Falhou (ex.: revisão do vídeo):
    volta pra fila normal do dia, o job vira 'abandoned'."""
    from src.runlock import try_acquire

    # quem já segura a trava (a rodada diária) não tenta de novo: flock não é reentrante
    trava = None
    if not lock_held:
        trava = try_acquire()
        if trava is None:
            log.info("[%s] trava ocupada — roteiro aprovado fica pra próxima rodada", channel_name)
            return
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
    rc = _run_limited([sys.executable, *cmd[1:]], _timeout_for(cmd, channel_name))
    # render ok = vídeo pronto pra subir (status 'ready', que o upload já conhece);
    # falha = 'failed', que o painel consegue reenviar (não 'abandoned', que é descartado)
    update(row[0], status="ready" if rc == 0 else "failed",
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

    _render_approved(channel_name, lock_held=True)
    ready = 0 if NO_UPLOAD else upload_pending(channel_name, "ready", limit=1)

    if channel_name == "botafogo":
        # 2 vídeos independentes: prévia/pós-jogo quando houver jogo (regras
        # de sempre, silêncio sem jogo) + 1 por dia com temas do botafogo.win
        # um vídeo por dia no canal: tenta a prévia/pós-jogo; sem jogo (silêncio),
        # cai no tema do botafogo.win. Se já há vídeo do dia, para aqui.
        for task in ("jogo", "portal"):
            if ready or _published_today(channel_name):
                break
            _run_pipeline(channel_name, ["--botafogo-task", task], task)
        return

    from datetime import date

    from src.script_archive import weekday_index

    if not ready and weekday_index(cfg.get("longo_semanal")) == date.today().weekday():
        # longo semanal no lugar do Short do dia (a cota de upload é uma só
        # pros 5 canais): compilação dos Shorts da semana; a última tentativa
        # sai em curto, pro dia não ficar sem vídeo
        _run_pipeline(channel_name, ["--long", "--compilation"], "longo semanal", fallback_short=True)
        ready = 1
    for i in range(ready, uploads_per_day):
        if _published_today(channel_name):
            # já há vídeo do canal hoje (pronto, no ar ou aguardando): não gera outro
            log.info("[%s] já tem vídeo do dia — não gera outro", channel_name)
            break
        # sem --long/--no-long: run_pipeline.py lê channels/<nome>.yaml ->
        # daily_format sozinho (fonte única de verdade, editável no painel —
        # não duplica a decisão aqui pra nunca dessincronizar da UI).
        # sem --topic: run_pipeline.py escolhe sozinho da fila única (ou
        # fato real, pro politica no formato curto).
        _run_pipeline(
            channel_name, [], f"upload {i + 1}/{uploads_per_day} ({daily_format})",
            fallback_short=daily_format == "long",
        )


PIPELINE_TIMEOUT_S = 45 * 60    # Short: render leva ~20 min
LONG_TIMEOUT_S = 150 * 60      # longo/compilação (48 cenas levou ~2 h)
RETRY_TIMEOUT_S = 30 * 60


def _published_today(channel_name: str) -> bool:
    """Já tem vídeo do canal hoje (no ar, pronto ou aguardando)?"""
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE channel = ? AND date(created_at, 'localtime') = date('now', 'localtime') "
            "AND status IN ('uploaded', 'ready', 'awaiting_approval')",
            (channel_name,),
        ).fetchone()[0]
    return n > 0


def _timeout_for(cmd: list[str], channel_name: str | None = None) -> int:
    """Longo = flag --long OU canal cujo formato padrão é longo (YAML)."""
    if "--long" in cmd:
        return LONG_TIMEOUT_S
    if channel_name:
        try:
            cfg = yaml.safe_load((ROOT / "channels" / f"{channel_name}.yaml").read_text()) or {}
            if cfg.get("daily_format") == "long":
                return LONG_TIMEOUT_S
        except (OSError, yaml.YAMLError):
            pass
    return PIPELINE_TIMEOUT_S


def _run_limited(cmd: list[str], timeout: int) -> int:
    """Tempo limite que mata o grupo inteiro (ffmpeg e TTS junto); estourou,
    devolve 124 e a rodada segue pros próximos canais."""
    from src.proc_limit import run_limited

    return run_limited(cmd, timeout, cwd=str(ROOT))


def _acquire_lock():
    """Trava única com o painel (src/runlock.py): ocupada, esta rodada sai."""
    from src.runlock import try_acquire

    handle = try_acquire()
    if handle is None:
        log.error("outra geração de vídeo já está em andamento — esta rodada sai sem fazer nada")
        raise SystemExit(0)
    return handle


def _channels_underperforming(limite: int = 20) -> list[str]:
    """Canais cuja mediana de views dos últimos 10 Shorts publicados está abaixo
    do limite. Só avisa: não muda formato nem tema sozinho."""
    import statistics

    out = []
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        for (canal,) in conn.execute("SELECT DISTINCT channel FROM video_performance").fetchall():
            rows = conn.execute(
                "SELECT views_current FROM video_performance WHERE channel = ? AND is_short = 1 "
                "AND views_current IS NOT NULL ORDER BY published_at DESC LIMIT 10", (canal,)
            ).fetchall()
            if len(rows) >= 5 and statistics.median(r[0] for r in rows) < limite:
                out.append(f"{canal} ({int(statistics.median(r[0] for r in rows))} views)")
    return out


def _alerta_do_dia() -> None:
    """Resumo no Telegram quando algo não saiu: canal sem vídeo no ar hoje,
    job travado, falha permanente ou vídeo/roteiro esperando você."""
    from src.notify import send_telegram

    hoje = date.today().isoformat()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        rows = conn.execute(
            "SELECT channel, status, id FROM jobs WHERE date(created_at, 'localtime') = ?", (hoje,)
        ).fetchall()
    canais = [p.stem for p in sorted((ROOT / "channels").glob("*.yaml"))]
    no_ar = {c for c, st, _ in rows if st == "uploaded"}
    sem_video = [c for c in canais if c not in no_ar]
    problemas = [f"job {i} ({c}): {st}" for c, st, i in rows
                 if st in ("failed", "failed_permanent", "awaiting_approval", "ready", "script_ready", "pending")]
    fracos = _channels_underperforming()
    if not sem_video and not problemas and not fracos:
        return
    linhas = ["📋 Resumo da rodada de hoje"]
    if fracos:
        linhas.append("Desempenho fraco (mediana dos últimos 10 Shorts): " + ", ".join(fracos))
    if sem_video:
        linhas.append("Sem vídeo no ar hoje: " + ", ".join(sem_video))
    linhas += problemas[:15]
    send_telegram("\n".join(linhas))


def main() -> None:
    _lock = _acquire_lock()  # mantém a trava até o fim do processo
    # órfãos: com a trava na mão, nenhum pipeline está rodando; job 'pending' parado há
    # mais de 6 h virou fantasma (ex.: processo morreu) e é fechado como falha
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'órfão: processo não existe mais' "
            "WHERE status IN ('pending', 'scripted', 'narrated') AND updated_at < datetime('now', '-6 hours')"
        )
        conn.commit()
    channels_dir = ROOT / "channels"
    cleanup_output()

    for yaml_path in sorted(channels_dir.glob("*.yaml")):
        channel_name = yaml_path.stem
        try:
            cfg = yaml.safe_load(yaml_path.read_text())
            run_channel(channel_name, cfg)
        except Exception:  # noqa: BLE001 — um canal quebrado não derruba os outros
            log.exception("[%s] erro na rodada deste canal — seguindo pros próximos", channel_name)

    # vídeos já renderizados cujo upload falhou (hoje ou antes — ex. token
    # OAuth expirado e reautorizado depois). Roda no fim: upload que falhou
    # por token não gasta cota, então sobra cota pro reenvio.
    if not NO_UPLOAD:
        _run_limited([sys.executable, str(ROOT / "scripts" / "retry_uploads.py")], RETRY_TIMEOUT_S)

    try:
        _alerta_do_dia()
    except Exception:  # noqa: BLE001 — o alerta nunca derruba a limpeza
        log.exception("resumo do fim da rodada falhou")

    # consultas à Wikimedia (src/wiki_cache.py) só servem enquanto os vídeos
    # são feitos (e refeitos, no conserto em laço): apaga quando o vídeo já
    # foi feito E publicado — com vídeo ainda na esteira (roteiro aprovado,
    # pronto esperando a vez, upload que falhou), fica pra ele.
    # Mapas/ícones/personagens (data/cache/infographic) ficam: são material.
    from src.wiki_cache import clear

    with closing(sqlite3.connect(DB_PATH)) as conn:
        # job parado há semanas (morreu sem marcar) não segura o cache
        in_flight = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE (status IN ('pending', 'scripted', 'script_ready', 'script_approved', "
            "'narrated', 'rendered', 'rendered_from_script', 'ready') "
            "OR (status = 'failed' AND video_path IS NOT NULL)) "
            "AND datetime(updated_at) > datetime('now', '-3 days')"
        ).fetchone()[0]
    if in_flight:
        log.info("consultas da Wikimedia mantidas: %d vídeo(s) ainda na esteira", in_flight)
    else:
        clear()
    log.info("rodada diária concluída")


if __name__ == "__main__":
    main()
