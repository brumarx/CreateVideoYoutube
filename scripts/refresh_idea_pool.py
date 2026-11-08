"""Atualiza a parte do GitHub no pool de ideias do canal (data/idea_pool/<canal>.json).

Uso: python scripts/refresh_idea_pool.py [canal]   (padrão: aprendervia)
Usa GITHUB_PAT do ~/.secrets/keys.env. Sem PAT a API do GitHub limita a poucas
buscas por hora; com PAT (sem escopos extras) é o suficiente.
"""
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(Path("~/.secrets/keys.env").expanduser())

from src.idea_sources import refresh_github  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Uso: python scripts/refresh_idea_pool.py [canal]   (padrão: aprendervia)")
        raise SystemExit(0)
    channel = sys.argv[1] if len(sys.argv) > 1 else "aprendervia"
    topics = refresh_github(channel)
    for t in topics[:10]:
        print("-", t)
    print(f"{len(topics)} temas gravados em data/idea_pool/{channel}.json")
