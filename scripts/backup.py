"""Backup diário do que não pode se perder: banco da fila (cópia consistente
pela API do SQLite), configurações dos canais, temas usados e roteiros
publicados. Fica em data/backups/<data>/; guarda os últimos 14 dias."""
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "data" / "backups"
KEEP_DAYS = 14


def main() -> None:
    target = DEST / date.today().isoformat()
    target.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(ROOT / "data" / "queue.db", timeout=30)
    dst = sqlite3.connect(target / "queue.db")
    src.backup(dst)  # cópia consistente mesmo com o pipeline escrevendo
    dst.close()
    src.close()
    shutil.copytree(ROOT / "channels", target / "channels", dirs_exist_ok=True)
    for name in ("used_topics.json", "used_political_facts.json", "yt_quota.json", "provider_health.json"):
        if (ROOT / "data" / name).exists():
            shutil.copy2(ROOT / "data" / name, target / name)
    if (ROOT / "data" / "published_scripts").exists():
        shutil.copytree(ROOT / "data" / "published_scripts", target / "published_scripts", dirs_exist_ok=True)
    # rotação: apaga backups mais antigos que KEEP_DAYS
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    for old in DEST.iterdir() if DEST.exists() else []:
        try:
            if old.is_dir() and date.fromisoformat(old.name) < cutoff:
                shutil.rmtree(old)
        except ValueError:
            continue
    print(f"backup em {target}")


if __name__ == "__main__":
    main()
