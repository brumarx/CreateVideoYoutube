"""Escolha de tema sem repetição, pra qualquer canal/formato.

Mantém um registro persistente (`data/used_topics.json`) de todo tema já
usado — cron diário, roda manual pelo painel e formato longo, todos passam
por aqui. Enquanto a lista fixa do canal (`channels/<nome>.yaml`) tiver tema
não usado, sorteia dali. Quando a lista fixa se esgota, pede pro LLM um tema
novo dentro do nicho do canal, mostrando a lista completa de temas já usados
pra ele não repetir nada — assim o canal nunca fica sem tema, mesmo depois
de meses rodando todo dia.

Temas virais: se o canal tem `viral_queries` no yaml, parte dos vídeos
(`viral_share`, padrão 50%, e 100% quando a lista fixa esgota) usa um tema
inspirado no que está bombando no YouTube no mês — busca os vídeos mais
vistos do nicho via yt-dlp (sem gastar cota da YouTube Data API, que fica
toda pros uploads) e pede pro LLM um tema original do canal no mesmo
gancho. Se a busca ou o LLM falhar, cai na lista fixa normalmente.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path

from .providers import complete
from .script_gen import current_date_rule

log = logging.getLogger("topics")

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "used_topics.json"
CHANNELS_DIR = ROOT / "channels"


def _append_topic_to_yaml(channel_name: str, topic: str) -> None:
    """Grava um tema gerado na hora (LLM, lista fixa esgotada) de volta em
    channels/<nome>.yaml -> topics, pra ele aparecer na fila do painel em
    vez de ficar só invisível em data/used_topics.json."""
    path = CHANNELS_DIR / f"{channel_name}.yaml"
    if not path.exists():
        return
    text = path.read_text()
    escaped = topic.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'  - "{escaped}"'
    pattern = r"^topics:[ \t]*\n((?:  - .*\n)*)"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match:
        insert_at = match.end()
        text = text[:insert_at] + new_line + "\n" + text[insert_at:]
    else:
        sep = "" if text.endswith("\n") else "\n"
        text = text + sep + f"topics:\n{new_line}\n"
    path.write_text(text)


def _load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# Tamanho mínimo pra considerar um tema "de verdade" — qualquer coisa mais
# curta é quase certamente lixo (tag de raciocínio vazada, pontuação solta,
# resposta cortada). Visto na prática: tema virou literalmente "<think>".
_MIN_TOPIC_LEN = 12
# Sinais de que o LLM vazou raciocínio interno ou markdown em vez de
# responder só com o tema — mesmo depois do provider já tentar limpar isso.
_GARBAGE_RE = re.compile(r"<think|</think|^```|^\{|^\[", re.IGNORECASE)
# Conversa em vez de tema (visto: "Claro, estou aqui para ajudar com ideias
# de vídeos... Aqui estão alguns temas:").
_CHATTER_RE = re.compile(
    r"^(claro|certo|ok|aqui est|segue|com certeza|entendi|sure|here)\b|estou aqui|:\s*$",
    re.IGNORECASE,
)
_MAX_TOPIC_LEN = 180
# Resposta cortada no meio (visto: "...e faturar até 10 mil reais por").
_TRUNCATED_RE = re.compile(
    r"\b(por|para|pra|de|do|da|dos|das|em|no|na|com|e|ou|o|a|os|as|um|uma|que|sem|até)$",
    re.IGNORECASE,
)


def _is_valid_topic(topic: str) -> bool:
    return (
        _MIN_TOPIC_LEN <= len(topic) <= _MAX_TOPIC_LEN
        and not _GARBAGE_RE.search(topic)
        and not _CHATTER_RE.search(topic)
        and not _TRUNCATED_RE.search(topic.rstrip(" .,;-"))
    )


def _first_line(raw: str) -> str:
    # às vezes o modelo devolve mais de uma linha mesmo pedindo pra não —
    # fica só com a primeira linha não vazia.
    cleaned = raw.strip().strip('"').strip("-").strip()
    for line in cleaned.splitlines():
        line = line.strip()
        if line:
            return line
    return cleaned


def _generate_new_topic(niche: str, used: list[str]) -> str:
    used_block = "\n".join(f"- {t}" for t in used) or "(nenhum ainda)"
    prompt = (
        f"Você ajuda a planejar temas de vídeos do YouTube para um canal "
        f"sobre: {niche}.\n\n{current_date_rule()}\n\n"
        f"Temas JÁ USADOS neste canal (NÃO pode repetir nenhum destes, nem "
        f"algo muito parecido/reformulado):\n{used_block}\n\n"
        "Responda com UM ÚNICO tema novo, específico e ainda não coberto "
        "acima, em português, numa linha só, sem numeração, sem aspas, sem "
        "explicação — só o texto do tema. Se for um tema de lista ('N "
        "fatos/coisas sobre...'), o N NUNCA pode passar de 10 — nada de "
        "20, 30, 40 itens."
    )
    messages = [{"role": "user", "content": prompt}]

    # `validate` faz a cascata de provedores/modelos em providers.complete()
    # já pular pro próximo modelo na hora que um vier com lixo (raciocínio
    # vazado, resposta cortada) — em vez de aceitar a primeira resposta não
    # vazia e só descobrir depois que era garbage. Mesmo assim mantém um
    # retry externo: às vezes TODOS os modelos disponíveis nesse momento
    # estão instáveis (rate limit, sobrecarga) e uma nova rodada da cascata
    # já resolve.
    last_topic = ""
    for attempt in range(3):
        try:
            raw = complete(messages, max_tokens=200, validate=lambda r: _is_valid_topic(_first_line(r)))
        except RuntimeError as exc:
            last_topic = str(exc)
            log.warning("tentativa %d/3: nenhum provedor devolveu tema válido (%s), tentando de novo", attempt + 1, exc)
            continue
        topic = _first_line(raw)
        if _is_valid_topic(topic):
            return topic
        last_topic = topic  # defensivo: não deveria acontecer com validate acima

    raise RuntimeError(
        f"LLM não devolveu um tema válido após 3 tentativas (último: {last_topic!r})"
    )


# Filtro da busca do YouTube: ordenar por visualizações, enviados este mês,
# só vídeos (sem canais/playlists).
_YT_VIRAL_FILTER = "CAMSBAgEEAE"
# Abaixo disso não é "viral", é só o que tinha na busca.
_VIRAL_MIN_VIEWS = 20_000


def _viral_titles(queries: list[str], per_query: int = 15, limit: int = 25) -> list[tuple[str, int]]:
    """Títulos mais vistos no YouTube no último mês pras buscas do canal,
    ordenados por visualizações. Lista vazia se o yt-dlp falhar."""
    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp não instalado — sem temas virais")
        return []
    from urllib.parse import quote

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": per_query,
        "http_headers": {"Accept-Language": "pt-BR,pt;q=0.9"},
    }
    seen: dict[str, int] = {}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for q in queries:
            url = f"https://www.youtube.com/results?search_query={quote(q)}&sp={_YT_VIRAL_FILTER}&gl=BR&hl=pt"
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as exc:  # noqa: BLE001 — rede/layout do YouTube, nunca derruba o pipeline
                log.warning("busca viral falhou (%r): %s", q, exc)
                continue
            for e in info.get("entries") or []:
                title, views = e.get("title"), e.get("view_count") or 0
                if title and views >= _VIRAL_MIN_VIEWS:
                    seen[title] = max(seen.get(title, 0), views)
    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def _generate_viral_topic(niche: str, viral: list[tuple[str, int]], used: list[str]) -> str:
    viral_block = "\n".join(f"- {t} ({v:,} views)".replace(",", ".") for t, v in viral)
    used_block = "\n".join(f"- {t}" for t in used) or "(nenhum ainda)"
    prompt = (
        f"Você ajuda a planejar temas de vídeos do YouTube para um canal "
        f"brasileiro sobre: {niche}.\n\n{current_date_rule()}\n\n"
        f"Vídeos que estão BOMBANDO no YouTube neste mês (podem estar em "
        f"outro idioma):\n{viral_block}\n\n"
        f"Temas JÁ USADOS neste canal (NÃO pode repetir nenhum destes, nem "
        f"algo muito parecido/reformulado):\n{used_block}\n\n"
        "Crie UM tema novo para este canal que aproveite o assunto/gancho "
        "de um dos vídeos virais acima, mas adaptado ao nicho e ao público "
        "brasileiro — nunca copie o título, e ignore vídeos que não têm "
        "nada a ver com o nicho. Só fatos reais, nada de boato. Responda em "
        "português, numa linha só, sem numeração, sem aspas, sem explicação "
        "— só o texto do tema. Se for um tema de lista ('N fatos/coisas "
        "sobre...'), o N NUNCA pode passar de 10."
    )
    raw = complete(
        [{"role": "user", "content": prompt}],
        max_tokens=400,
        validate=lambda r: _is_valid_topic(_first_line(r)),
    )
    topic = _first_line(raw)
    if not _is_valid_topic(topic):
        raise RuntimeError(f"tema viral inválido: {topic!r}")
    return topic


def _try_viral_topic(channel_name: str, niche: str, queries: list[str], used: list[str]) -> str | None:
    viral = _viral_titles(queries)
    if not viral:
        log.info("[%s] nenhum vídeo viral encontrado — usando a fila normal", channel_name)
        return None
    try:
        topic = _generate_viral_topic(niche, viral, used)
    except RuntimeError as exc:
        log.warning("[%s] LLM não gerou tema viral (%s) — usando a fila normal", channel_name, exc)
        return None
    if topic in used:
        return None
    log.info("[%s] tema viral: %s (inspirado em %d vídeos em alta)", channel_name, topic, len(viral))
    return topic


def pick_topic(
    channel_name: str,
    pool: list[str],
    niche: str,
    viral_queries: list[str] | None = None,
    viral_share: float = 0.5,
) -> str:
    """Escolhe um tema não usado ainda pra `channel_name` — fila única
    (curto e longo compartilham a mesma lista, já que só sai 1 vídeo/dia por
    canal). Marca como usado e persiste antes de devolver, pra nunca
    sortear o mesmo tema duas vezes.
    """
    state = _load()
    used = state.setdefault(channel_name, [])
    used_set = set(used)

    unused = [t for t in pool if t not in used_set]
    topic = None
    if viral_queries and (not unused or random.random() < viral_share):
        topic = _try_viral_topic(channel_name, niche, viral_queries, used)
    if topic is None and unused:
        topic = random.choice(unused)
    elif topic is None:
        log.info("[%s] lista fixa de temas esgotada — pedindo tema novo ao LLM", channel_name)
        topic = _generate_new_topic(niche, used)
    if topic not in pool:
        # tema viral ou gerado na hora: grava no yaml pra aparecer no painel
        _append_topic_to_yaml(channel_name, topic)

    used.append(topic)
    _save(state)
    return topic
