"""Puxa estatísticas reais dos vídeos já enviados (YouTube Data API, videos.list,
1 unidade de cota por 50 vídeos) e grava em data/queue.db (video_performance).

Uso: python scripts/collect_performance.py [--csv saida.csv]
Agendado semanalmente (segunda 09:00) pelo crontab; saída em logs/performance.log.
Estado da última execução em data/performance_status.json (lido pelo painel).
Falha de algum canal (ex.: token OAuth vencido) manda aviso no Telegram.
"""
import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from googleapiclient.discovery import build  # noqa: E402

from src.config import ChannelConfig  # noqa: E402
from src.notify import send_telegram  # noqa: E402
from src.upload import _load_credentials  # noqa: E402

QUEUE_DB = ROOT / "data" / "queue.db"
STATUS_FILE = ROOT / "data" / "performance_status.json"
log = logging.getLogger("collect_performance")
_ISO = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _seconds(iso: str) -> int:
    m = _ISO.fullmatch(iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def fetch(channel: str, ids: list[str]) -> list[dict]:
    cfg = ChannelConfig.load(channel)
    youtube = build("youtube", "v3", credentials=_load_credentials(cfg.token_file), cache_discovery=False)
    out = []
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            stats, snip = item.get("statistics", {}), item.get("snippet", {})
            out.append({
                "video_id": item["id"],
                "published_at": snip.get("publishedAt", ""),
                "title": snip.get("title", ""),
                "duration_seconds": _seconds(item.get("contentDetails", {}).get("duration", "")),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
            })
    return out


def _dia_semana(iso: str) -> int:
    """0 = segunda ... 6 = domingo, do horário de publicação (UTC)."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).weekday()
    except ValueError:
        return 0


def _ensure_title_column(con: sqlite3.Connection) -> None:
    try:
        con.execute("ALTER TABLE video_performance ADD COLUMN title TEXT")
    except sqlite3.OperationalError:
        pass  # já existe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    con = sqlite3.connect(QUEUE_DB)
    _ensure_title_column(con)
    rows = con.execute(
        "SELECT id, channel, youtube_video_id FROM jobs WHERE status='uploaded' "
        "AND youtube_video_id IS NOT NULL AND youtube_video_id != ''"
    ).fetchall()
    by_channel: dict[str, dict[str, int]] = {}
    for job_id, channel, vid in rows:
        by_channel.setdefault(channel, {})[vid] = job_id

    results, errors = [], {}
    for channel, ids in by_channel.items():
        if not (ROOT / "channels" / f"{channel}.yaml").exists():
            log.info("[%s] sem channels/%s.yaml (canal desativado): ignorado", channel, channel)
            continue
        try:
            videos = fetch(channel, list(ids))
        except Exception as exc:  # noqa: BLE001 — um canal com problema não derruba os outros
            log.warning("[%s] não consegui buscar estatísticas: %s", channel, exc)
            errors[channel] = str(exc)[:200]
            continue
        for v in videos:
            job_id = ids[v["video_id"]]
            is_short = 0 < v["duration_seconds"] <= 180
            con.execute(
                """INSERT INTO video_performance
                   (job_id, channel, published_at, duration_seconds, is_short, views_current, likes, comments,
                    engagement_rate, publish_hour, publish_day_of_week, title, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
                   ON CONFLICT(job_id) DO UPDATE SET
                    channel=excluded.channel, published_at=excluded.published_at,
                    duration_seconds=excluded.duration_seconds, is_short=excluded.is_short,
                    views_current=excluded.views_current, likes=excluded.likes, comments=excluded.comments,
                    engagement_rate=excluded.engagement_rate, publish_hour=excluded.publish_hour,
                    publish_day_of_week=excluded.publish_day_of_week, title=excluded.title,
                    updated_at=excluded.updated_at""",
                (job_id, channel, v["published_at"], v["duration_seconds"], int(is_short), v["views"],
                 v["likes"], v["comments"], (v["likes"] + v["comments"]) / v["views"] if v["views"] else 0.0,
                 int(v["published_at"][11:13] or 0), _dia_semana(v["published_at"]), v["title"]),
            )
            results.append({**v, "channel": channel, "job_id": job_id, "is_short": int(is_short)})
        con.commit()
        log.info("[%s] %d vídeo(s) atualizados", channel, len(videos))
    con.close()

    status = {
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "videos": len(results),
        "errors": errors,
        "ok": not errors and bool(results),
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors or not results:
        detail = "; ".join(f"{c}: {e}" for c, e in errors.items()) or "nenhum vídeo retornou"
        send_telegram(f"⚠️ Coleta de desempenho falhou: {detail}\nVeja logs/performance.log")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else ["job_id"])
            w.writeheader()
            w.writerows(results)
        print(f"CSV: {args.csv}")
    log.info("%d vídeos gravados em video_performance", len(results))


if __name__ == "__main__":
    main()
