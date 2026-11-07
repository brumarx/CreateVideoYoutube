"""Fonte extra de temas: projetos famosos do GitHub + listas de ideias de projetos.

Não substitui a lista fixa do canal nem o trending: é um pool de temas que
`src/topics.pick_topic` mistura à fila (ver `IDEA_SHARE`). Fica em
`data/idea_pool/<canal>.json`, com duas partes:

- `curated`: temas escritos à mão a partir de listas de ideias de projetos
  (DataCamp, UnlockingTech). Ideias não têm copyright, mas o texto dessas
  páginas não é copiado: só o assunto, reescrito no formato do canal.
- `github`: gerada por `scripts/refresh_idea_pool.py` a partir da API de
  busca de repositórios do GitHub (com `GITHUB_PAT` do ~/.secrets/keys.env).

Os temas daqui passam pelas mesmas travas de qualquer tema (repetição,
checagem de assunto e fato na pesquisa, revisão do roteiro).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("idea_sources")

ROOT = Path(__file__).resolve().parent.parent
POOL_DIR = ROOT / "data" / "idea_pool"

# chance de, quando ainda há ideia não usada, sortear dela em vez da fila normal
IDEA_SHARE = 0.3

# repositórios que não servem pra um público que não programa, ou que são
# perigosos de ensinar como vídeo de "faça você mesmo" (deepfake, pentest, trading)
GITHUB_DENY = re.compile(
    r"deep-live-cam|strix|h4cker|nautilus|kong|deepfake|face.?swap|pentest|hacking",
    re.IGNORECASE,
)

GITHUB_QUERIES = [
    "topic:artificial-intelligence stars:>5000",
    "topic:llm stars:>5000",
]


def _pool_path(channel: str) -> Path:
    return POOL_DIR / f"{channel}.json"


def load_pool(channel: str) -> dict:
    path = _pool_path(channel)
    if not path.exists():
        return {"curated": [], "github": [], "updated": None}
    return json.loads(path.read_text(encoding="utf-8"))


def load_ideas(channel: str) -> list[str]:
    """Temas do pool do canal (curados primeiro). Lista vazia se não há pool."""
    try:
        pool = load_pool(channel)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("[%s] pool de ideias ilegível: %s", channel, exc)
        return []
    seen: set[str] = set()
    out: list[str] = []
    for topic in [*pool.get("curated", []), *pool.get("github", [])]:
        if topic and topic not in seen:
            seen.add(topic)
            out.append(topic)
    return out


def github_topic(full_name: str, stars: int) -> str:
    """Tema de vídeo a partir de um repositório. Determinístico (sem LLM)."""
    name = full_name.split("/")[-1]
    mil = f"{stars / 1000:.0f} mil" if stars >= 1000 else str(stars)
    return f"{name}: o projeto de IA de código aberto com {mil} estrelas no GitHub, o que é e como usar de graça"


def filter_repos(items: list[dict]) -> list[dict]:
    """Tira repositórios fora do escopo (ver GITHUB_DENY) e duplicados."""
    out, seen = [], set()
    for repo in items:
        full = repo.get("full_name") or ""
        desc = repo.get("description") or ""
        if not full or full in seen or GITHUB_DENY.search(full + " " + desc):
            continue
        seen.add(full)
        out.append(repo)
    return out


def fetch_github_repos(query: str, per_page: int = 30, token: str | None = None) -> list[dict]:
    import httpx

    token = token or os.getenv("GITHUB_PAT", "")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "youtube-pipeline"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def refresh_github(channel: str, queries: list[str] | None = None, limit: int = 25) -> list[str]:
    """Reescreve a parte `github` do pool do canal. Devolve os temas gerados."""
    repos: list[dict] = []
    for query in queries or GITHUB_QUERIES:
        repos.extend(fetch_github_repos(query))
    repos = sorted(filter_repos(repos), key=lambda r: r.get("stargazers_count", 0), reverse=True)
    topics = [github_topic(r["full_name"], r.get("stargazers_count", 0)) for r in repos[:limit]]

    pool = load_pool(channel)
    pool["github"] = topics
    pool["updated"] = datetime.now(timezone.utc).isoformat()
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    _pool_path(channel).write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[%s] pool de ideias: %d temas do GitHub", channel, len(topics))
    return topics
