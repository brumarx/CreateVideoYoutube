"""Escolha de tema sem repetição, pra qualquer canal/formato.

Mantém um registro persistente (`data/used_topics.json`) de todo tema já
usado — cron diário, roda manual pelo painel e formato longo, todos passam
por aqui. Enquanto a lista fixa do canal (`channels/<nome>.yaml`) tiver tema
não usado, sorteia dali. Quando a lista fixa se esgota, pede pro LLM um tema
novo dentro do nicho do canal, mostrando a lista completa de temas já usados
pra ele não repetir nada — assim o canal nunca fica sem tema, mesmo depois
de meses rodando todo dia.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path

from .providers import complete

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


def _is_valid_topic(topic: str) -> bool:
    return len(topic) >= _MIN_TOPIC_LEN and not _GARBAGE_RE.search(topic)


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
        f"sobre: {niche}.\n\n"
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


def pick_topic(channel_name: str, pool: list[str], niche: str) -> str:
    """Escolhe um tema não usado ainda pra `channel_name` — fila única
    (curto e longo compartilham a mesma lista, já que só sai 1 vídeo/dia por
    canal). Marca como usado e persiste antes de devolver, pra nunca
    sortear o mesmo tema duas vezes.
    """
    state = _load()
    used = state.setdefault(channel_name, [])
    used_set = set(used)

    unused = [t for t in pool if t not in used_set]
    if unused:
        topic = random.choice(unused)
    else:
        log.info("[%s] lista fixa de temas esgotada — pedindo tema novo ao LLM", channel_name)
        topic = _generate_new_topic(niche, used)
        _append_topic_to_yaml(channel_name, topic)

    used.append(topic)
    _save(state)
    return topic
