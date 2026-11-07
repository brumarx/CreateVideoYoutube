"""Puxa estatísticas reais dos vídeos já enviados (YouTube Data API, videos.list,
1 unidade de cota por 50 vídeos) e grava em data/queue.db (video_performance).

Uso: python scripts/collect_performance.py [--csv saida.csv]
Usa o token OAuth de cada canal (data/tokens via channels/<canal>.yaml).
"""
import argparse
import csv
import logging
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from googleapiclient.discovery import build  # noqa: E402

from src.config import ChannelConfig  # noqa: E402
QUEUE_DB = ROOT / "data" / "queue.db"
from src.upload import _load_credentials  # noqa: E402

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
                "channel": channel,
                "published_at": snip.get("publishedAt", ""),
                "title": snip.get("title", ""),
                "duration_seconds": _seconds(item.get("contentDetails", {}).get("duration", "")),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    con = sqlite3.connect(QUEUE_DB)
    rows = con.execute(
        "SELECT id, channel, youtube_video_id FROM jobs WHERE status='uploaded' AND youtube_video_id IS NOT NULL AND youtube_video_id != ''"
    ).fetchall()
    by_channel: dict[str, dict[str, int]] = {}
    for job_id, channel, vid in rows:
        by_channel.setdefault(channel, {})[vid] = job_id

    results = []
    for channel, ids in by_channel.items():
        try:
            videos = fetch(channel, list(ids))
        except Exception as exc:  # noqa: BLE001 — um canal com token vencido não derruba os outros
            log.warning("[%s] não consegui buscar estatísticas: %s", channel, exc)
            continue
        for v in videos:
            job_id = ids[v["video_id"]]
            is_short = 0 < v["duration_seconds"] <= 180
            con.execute(
                """INSERT OR REPLACE INTO video_performance
                   (job_id, channel, published_at, duration_seconds, is_short, views_current, likes, comments,
                    engagement_rate, publish_hour, publish_day_of_week, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (job_id, channel, v["published_at"], v["duration_seconds"], int(is_short), v["views"],
                 v["likes"], v["comments"], (v["likes"] + v["comments"]) / v["views"] if v["views"] else 0.0,
                 int(v["published_at"][11:13] or 0), 0),
            )
            results.append({**v, "job_id": job_id, "is_short": int(is_short)})
        con.commit()
        log.info("[%s] %d vídeo(s) atualizados", channel, len(videos))
    con.close()

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else ["job_id"])
            w.writeheader()
            w.writerows(results)
        print(f"CSV: {args.csv}")
    print(f"{len(results)} vídeos gravados em video_performance")


if __name__ == "__main__":
    main()
