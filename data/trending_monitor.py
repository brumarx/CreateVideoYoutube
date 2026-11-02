"""Monitor de trending topics — rastreia uso e sucesso de cada fonte.

Logs em data/trending_monitor.json para visualizar no dashboard.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

MONITOR_FILE = Path(__file__).resolve().parent / "trending_monitor.json"

log = logging.getLogger("trending_monitor")


def _load_monitor() -> dict:
    """Carrega histórico de trending topics."""
    if not MONITOR_FILE.exists():
        return {
            "sources": {
                "x_twitter": {"success": 0, "failed": 0, "last_success": None},
                "google_trends": {"success": 0, "failed": 0, "last_success": None},
                "youtube_trends": {"success": 0, "failed": 0, "last_success": None},
                "config_fallback": {"success": 0, "failed": 0, "last_success": None},
            },
            "channels": {},
            "history": [],
        }
    return json.loads(MONITOR_FILE.read_text())


def _save_monitor(data: dict) -> None:
    """Persiste monitoramento."""
    MONITOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def record_trend_use(
    channel: str,
    topic: str,
    source: str,
    success: bool = True,
    details: str | None = None,
) -> None:
    """Registra quando um trending topic é usado.

    Args:
        channel: nome do canal (aprendervia, curiosidades, etc)
        topic: tema usado
        source: fonte ("x_twitter", "google_trends", "youtube_trends", "config_fallback")
        success: se conseguiu buscar dados
        details: informações extras (erro, velocidade, etc)
    """
    data = _load_monitor()

    # Atualiza contadores da fonte
    if source in data["sources"]:
        if success:
            data["sources"][source]["success"] += 1
            data["sources"][source]["last_success"] = datetime.now(timezone.utc).isoformat()
        else:
            data["sources"][source]["failed"] += 1

    # Atualiza canais
    if channel not in data["channels"]:
        data["channels"][channel] = {"count": 0, "last_topic": None, "source": None}
    data["channels"][channel]["count"] += 1
    data["channels"][channel]["last_topic"] = topic
    data["channels"][channel]["source"] = source

    # Histórico (últimas 100 transações)
    data["history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "topic": topic[:60],  # trunca
        "source": source,
        "success": success,
        "details": details,
    })
    data["history"] = data["history"][-100:]

    _save_monitor(data)
    log.info("[%s] trend: %s (via %s) %s", channel, topic[:40], source, "✓" if success else "✗")


def get_stats() -> dict:
    """Retorna estatísticas atuais."""
    data = _load_monitor()

    total_success = sum(s["success"] for s in data["sources"].values())
    total_failed = sum(s["failed"] for s in data["sources"].values())
    total = total_success + total_failed

    return {
        "total_uses": total,
        "success_rate": f"{100*total_success/total:.1f}%" if total > 0 else "N/A",
        "sources": data["sources"],
        "channels": data["channels"],
        "history": data["history"][-20:],  # últimas 20
    }
