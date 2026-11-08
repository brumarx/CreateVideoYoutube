"""Limpeza automática de disco: logs antigos e arquivos intermediários de vídeo.

Regras (só mexe em logs/ e em output/job_<id>/; nunca em data/, queue.db nem roteiros):
- logs/*.log: maiores que LOG_MAX_MB são cortados (fica o final); com mais de LOG_KEEP_DAYS dias, apagados.
- output/job_<id>/ de jobs já encerrados (uploaded, abandoned, deleted, failed, retried):
  * depois de intermediate_hours (padrão 24 h): apaga intermediários (scene_*.mp4, raw.mp4, scene_*.mp3/png/ass,
    *.wordtimes.json). Roteiro, fatos e final.mp4 ficam.
  * depois de final_days (padrão 30 dias): apaga também o final.mp4 (vídeo já está no YouTube).

Uso: python scripts/housekeeping.py [--apply]   (sem --apply só mostra o que sairia)
Agendado diariamente pelo crontab com --apply; resultado em data/housekeeping_status.json.
"""
import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
OUTPUT = ROOT / "output"
QUEUE_DB = ROOT / "data" / "queue.db"
STATUS_FILE = ROOT / "data" / "housekeeping_status.json"

LOG_MAX_MB = 10
LOG_KEEP_DAYS = 30
CONFIG_FILE = ROOT / "data" / "housekeeping_config.json"
DEFAULT_INTERMEDIATE_HOURS = 24   # intermediários (cenas, áudio, legendas): 24 h por padrão
DEFAULT_FINAL_DAYS = 30


def load_config() -> dict:
    """Janela de limpeza, editável na aba Sistema do painel."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    return {
        "intermediate_hours": float(cfg.get("intermediate_hours", DEFAULT_INTERMEDIATE_HOURS)),
        "final_days": float(cfg.get("final_days", DEFAULT_FINAL_DAYS)),
    }


def save_config(intermediate_hours: float, final_days: float) -> None:
    if intermediate_hours < 1 or final_days < 1:
        raise ValueError("a janela mínima é 1 hora (intermediários) e 1 dia (final)")
    CONFIG_FILE.write_text(json.dumps({"intermediate_hours": intermediate_hours, "final_days": final_days}))
CLOSED = ("uploaded", "abandoned", "deleted", "failed", "retried")
INTERMEDIATE_GLOBS = ("scene_*.mp4", "raw.mp4", "scene_*.mp3", "scene_*.png", "scene_*.ass", "*.wordtimes.json")

log = logging.getLogger("housekeeping")


def _age_days(path: Path, now: float) -> float:
    return (now - path.stat().st_mtime) / 86400


def clean_logs(apply: bool, now: float) -> dict:
    freed = 0
    actions = []
    for path in sorted(LOGS.glob("*.log*")):
        size = path.stat().st_size
        age = _age_days(path, now)
        if age > LOG_KEEP_DAYS:
            actions.append(("apagar log antigo", path.name, size))
            freed += size
            if apply:
                path.unlink()
        elif size > LOG_MAX_MB * 1024 * 1024 and path.suffix == ".log":
            keep = LOG_MAX_MB * 1024 * 1024 // 2
            actions.append(("cortar log grande", path.name, size - keep))
            freed += size - keep
            if apply:
                with path.open("rb") as f:
                    f.seek(-keep, 2)
                    tail = f.read()
                path.write_bytes(b"[cortado pela limpeza automatica]\n" + tail)
    return {"freed": freed, "actions": actions}


CFG = load_config()


def clean_output(apply: bool, now: float) -> dict:
    freed = 0
    actions = []
    if not QUEUE_DB.exists():
        return {"freed": 0, "actions": [("sem queue.db", "", 0)]}
    con = sqlite3.connect(QUEUE_DB)
    status = dict(con.execute("SELECT id, status FROM jobs"))
    con.close()
    for job_dir in sorted(OUTPUT.glob("job_*")):
        try:
            job_id = int(job_dir.name.split("_", 1)[1])
        except ValueError:
            continue
        if status.get(job_id) not in CLOSED:
            continue  # job ainda em andamento ou pronto pra subir: não mexe
        # mais velho dos arquivos da pasta define a idade do job
        files = [p for p in job_dir.iterdir() if p.is_file()]
        if not files:
            continue
        newest = max(p.stat().st_mtime for p in files)
        age = (now - newest) / 86400
        if age * 24 > CFG["intermediate_hours"]:
            for pattern in INTERMEDIATE_GLOBS:
                for p in job_dir.glob(pattern):
                    size = p.stat().st_size
                    freed += size
                    actions.append(("intermediário", f"{job_dir.name}/{p.name}", size))
                    if apply:
                        p.unlink()
        final = job_dir / "final.mp4"
        if final.exists() and status.get(job_id) == "uploaded" and age > CFG["final_days"]:
            size = final.stat().st_size
            freed += size
            actions.append(("vídeo final já no YouTube", f"{job_dir.name}/final.mp4", size))
            if apply:
                final.unlink()
    return {"freed": freed, "actions": actions}


MEDIA_CACHE = ROOT / "data" / "cache" / "media"
MEDIA_CACHE_DAYS = 7


def clean_media_cache(apply: bool, now: float) -> dict:
    """Vídeos de origem baixados pro cache (real_media): sem uso há 7 dias saem.
    Cada uso atualiza a data, então o que está em uso recente fica."""
    freed, actions = 0, []
    for path in MEDIA_CACHE.glob("*.bin") if MEDIA_CACHE.exists() else []:
        if _age_days(path, now) > MEDIA_CACHE_DAYS:
            size = path.stat().st_size
            freed += size
            actions.append(("cache de mídia sem uso", path.name, size))
            if apply:
                path.unlink()
    return {"freed": freed, "actions": actions}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="apaga de verdade (sem isso, só simula)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = time.time()
    logs = clean_logs(args.apply, now)
    out = clean_output(args.apply, now)
    media = clean_media_cache(args.apply, now)
    out["freed"] += media["freed"]
    out["actions"] += media["actions"]
    total = logs["freed"] + out["freed"]
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "applied": args.apply,
        "freed_bytes": total,
        "logs": {"freed": logs["freed"], "items": len(logs["actions"])},
        "output": {"freed": out["freed"], "items": len(out["actions"])},
        "sample": [a[1] for a in (logs["actions"] + out["actions"])[:20]],
    }
    if args.apply:
        STATUS_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    verb = "liberados" if args.apply else "seriam liberados"
    log.info("%.1f MB %s (logs %.1f MB, output %.1f MB)", total / 1e6, verb, logs["freed"] / 1e6, out["freed"] / 1e6)
    for kind, name, size in (logs["actions"] + out["actions"])[:40]:
        log.info("  %s: %s (%.1f MB)", kind, name, size / 1e6)


if __name__ == "__main__":
    main()
