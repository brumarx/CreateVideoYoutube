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


def _generate_new_topic(niche: str, used: list[str]) -> str:
    used_block = "\n".join(f"- {t}" for t in used) or "(nenhum ainda)"
    prompt = (
        f"Você ajuda a planejar temas de vídeos do YouTube para um canal "
        f"sobre: {niche}.\n\n"
        f"Temas JÁ USADOS neste canal (NÃO pode repetir nenhum destes, nem "
        f"algo muito parecido/reformulado):\n{used_block}\n\n"
        "Responda com UM ÚNICO tema novo, específico e ainda não coberto "
        "acima, em português, numa linha só, sem numeração, sem aspas, sem "
        "explicação — só o texto do tema."
    )
    messages = [{"role": "user", "content": prompt}]
    topic = complete(messages, max_tokens=200).strip().strip('"').strip("-").strip()
    # às vezes o modelo devolve mais de uma linha mesmo pedindo pra não —
    # fica só com a primeira linha não vazia.
    for line in topic.splitlines():
        line = line.strip()
        if line:
            return line
    return topic


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
