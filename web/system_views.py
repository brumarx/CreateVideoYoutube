"""Dados das abas Desempenho e Sistema do painel (leitura do banco e do disco)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.db import connect_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUEUE_DB = ROOT / "data" / "queue.db"
PERF_STATUS = ROOT / "data" / "performance_status.json"
HK_STATUS = ROOT / "data" / "housekeeping_status.json"
LOGS = ROOT / "logs"
OUTPUT = ROOT / "output"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def performance_view() -> dict:
    """Mediana de views dos vídeos curtos por canal, e o efeito do título (60 caracteres)."""
    rows: list[tuple] = []
    if QUEUE_DB.exists():
        con = connect_db(QUEUE_DB)
        rows = con.execute(
            "SELECT channel, is_short, views_current, COALESCE(title,'') FROM video_performance"
        ).fetchall()
        con.close()
    channels = {}
    for channel in sorted({r[0] for r in rows}):
        shorts = [r[2] for r in rows if r[0] == channel and r[1]]
        channels[channel] = {
            "shorts": len(shorts),
            "median_short": int(statistics.median(shorts)) if shorts else 0,
            "zero_or_low": sum(v <= 10 for v in shorts),
        }
    shorts_all = [r for r in rows if r[1]]
    longs = [r[2] for r in rows if not r[1]]
    long_titles = [r[2] for r in shorts_all if len(r[3]) > 60]
    short_titles = [r[2] for r in shorts_all if 0 < len(r[3]) <= 60]

    def med(values: list[int]) -> int | None:
        return int(statistics.median(values)) if values else None

    status = _read_json(PERF_STATUS)
    return {
        "total": len(rows),
        "channels": channels,
        "short_median": med([r[2] for r in shorts_all]),
        "long_median": med(longs),
        "long_count": len(longs),
        "title_long_median": med(long_titles),
        "title_long_count": len(long_titles),
        "title_short_median": med(short_titles),
        "title_short_count": len(short_titles),
        "last_run": status.get("last_run", ""),
        "errors": status.get("errors", {}),
        "ok": status.get("ok", False),
    }


def _du(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def _window() -> dict:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from housekeeping import load_config

    return load_config()


def system_view() -> dict:
    disk = shutil.disk_usage(ROOT)
    logs = []
    for path in sorted(LOGS.glob("*.log*"), key=lambda p: p.stat().st_size, reverse=True)[:40]:
        logs.append({
            "name": path.name,
            "size": _mb(path.stat().st_size),
            "bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m %H:%M"),
        })
    job_dirs = sorted(OUTPUT.glob("job_*"), key=lambda p: _du(p), reverse=True)[:8]
    return {
        "disk_used_pct": round(100 * disk.used / disk.total),
        "disk_free": _mb(disk.free),
        "logs_total": _mb(_du(LOGS)),
        "output_total": _mb(_du(OUTPUT)),
        "data_total": _mb(_du(ROOT / "data")),
        "logs": logs,
        "biggest_jobs": [{"name": d.name, "size": _mb(_du(d))} for d in job_dirs],
        "housekeeping": _read_json(HK_STATUS),
        "window": _window(),
    }


def tail(name: str, lines: int = 300) -> str | None:
    """Últimas linhas de um log de logs/. Só nomes diretos dentro de logs/."""
    if "/" in name or ".." in name:
        return None
    path = (LOGS / name).resolve()
    if path.parent != LOGS.resolve() or not path.is_file():
        return None
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200_000))
        data = f.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
