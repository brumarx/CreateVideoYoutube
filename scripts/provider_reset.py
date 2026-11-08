"""Zera o estado de saúde dos provedores (chaves banidas ou em descanso).
Uso: python scripts/provider_reset.py all      (ou o id mostrado em data/provider_health.json)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.providers import health_reset  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        raise SystemExit(0)
    print(f"{health_reset(sys.argv[1])} chave(s) zerada(s)")
