"""Revisão factual do roteiro ANTES de narrar/renderizar.

Motivação (casos reais, 2026-09-24): um tutorial inteiro sobre uma
ferramenta de IA "Lumina" que não existe, e um vídeo do canal de política
dizendo "gastos revelados pelos dados do Portal da Transparência" sem um
único número — acusações genéricas contra ministros do STF. Nenhum dos dois
podia ir ao ar: tutorial de ferramenta inventada queima o canal, e acusação
sem dado contra pessoa/instituição real é risco de strike e de processo.

Um segundo LLM lê o roteiro como revisor e aponta só problemas concretos.
Nome de ferramenta/produto/evento que o revisor não reconhece NÃO reprova
direto — os temas virais do mês falam justamente de coisas mais novas que o
conhecimento do modelo — primeiro é conferido na busca real da internet
(src/web_search.py); só reprova se a busca também não achar.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date

from .providers import complete
from .web_search import search_topic_facts

log = logging.getLogger("fact_check")

_TYPES = {
    "ferramenta_inexistente", "fato_nao_confirmado", "atribuicao_falsa",
    "acusacao_sem_base", "data_desatualizada", "titulo_enganoso",
}
# tipos que dá pra confirmar com uma busca na internet (existe ou não
# existe); os outros são problema de redação/atribuição e reprovam direto.
_SEARCHABLE = {"ferramenta_inexistente", "fato_nao_confirmado"}


def _review_prompt(script: dict, topic: str, facts: dict | None, web_facts: list[dict] | None) -> str:
    narration = "\n".join(f"[cena {i + 1}] {s.get('narration', '')}" for i, s in enumerate(script.get("scenes", [])))
    sources = "NENHUMA — o roteiro foi escrito só com conhecimento geral."
    if facts:
        sources = "DADOS REAIS fornecidos ao roteirista:\n" + json.dumps(facts, ensure_ascii=False, indent=2)
    elif web_facts:
        sources = "Trechos de pesquisa na internet fornecidos ao roteirista:\n" + "\n".join(
            f"- {f['titulo']}: {f['trecho']}" for f in web_facts
        )

    today = date.today().strftime("%d/%m/%Y")
    return f"""Você é revisor de fatos de um canal do YouTube. Revise o roteiro
abaixo ANTES de ele virar vídeo. Aponte SÓ problemas concretos destes tipos:

- "ferramenta_inexistente": cita pelo nome um produto, app, ferramenta,
  recurso ou empresa que você não tem certeza de que existe, ou atribui a
  uma ferramenta real um recurso que ela não tem.
- "fato_nao_confirmado": número, data, nome de pessoa ou evento específico
  que você acha PROVÁVEL que esteja errado ou inventado. Fato verdadeiro e
  conhecido que só não tem fonte citada NÃO é problema.
- "atribuicao_falsa": diz que algo foi "revelado", "mostrado" ou "está nos
  dados" de uma fonte (Portal da Transparência, TSE, dados oficiais etc.)
  mas as FONTES não trazem esse dado.
- "acusacao_sem_base": acusa pessoa ou instituição identificável de
  irregularidade, desvio ou crime sem dado concreto nas FONTES.
- "data_desatualizada": trata como atual/recente um ano, versão ou fato
  que já é passado (ex.: "em 2024", "este ano de 2025", "o lançamento mais
  recente é X" quando já existe coisa mais nova), ou chama de "novidade" algo
  antigo. HOJE É {today}.
- "titulo_enganoso": o título promete algo que o roteiro não entrega (ex.:
  promete "5 gastos revelados pelos dados" e não mostra dado nenhum).

Opinião, tom, estilo e afirmações genéricas e verdadeiras NÃO são problema.
Não invente problema: se o roteiro está ok, devolva lista vazia.

TEMA: {topic}
TÍTULO: {script.get('title', '')}

FONTES:
{sources}

ROTEIRO:
{narration}

Responda SÓ com JSON, neste formato exato:
{{"problemas": [{{"tipo": "...", "termo": "nome ou afirmação curta (até 8 palavras)", "motivo": "por que é problema, 1 frase"}}]}}"""


def _parse(raw: str) -> list[dict] | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    problems = data.get("problemas")
    if not isinstance(problems, list):
        return None
    # modelo às vezes inventa tipo fora da lista ("fonte não citada") —
    # isso é estilo, não erro factual; só conta o que está definido. Acento
    # vira sem acento ("atribuição_falsa" -> "atribuicao_falsa").
    out = []
    for p in problems:
        if not isinstance(p, dict):
            continue
        tipo = unicodedata.normalize("NFKD", str(p.get("tipo", ""))).encode("ascii", "ignore").decode().strip().lower()
        if tipo in _TYPES:
            out.append({**p, "tipo": tipo})
    return out


def _confirmed_online(term: str, strict: bool) -> bool:
    """True se a busca real acha o termo citado (ex.: ferramenta nova que o
    revisor não conhece por ser mais recente que o modelo)."""
    results = search_topic_facts(term, max_results=3)
    if not results:
        return False
    words = [w for w in re.findall(r"\w{3,}", term.lower())]
    if not words:
        return False
    for r in results:
        text = f"{r['titulo']} {r['trecho']}".lower()
        # nome de ferramenta: basta a maioria das palavras; fato/número: todas
        needed = len(words) if strict else max(1, len(words) // 2 + 1)
        if sum(w in text for w in words) >= needed:
            return True
    return False


def review_script(
    script: dict, topic: str, facts: dict | None = None, web_facts: list[dict] | None = None,
) -> list[dict] | None:
    """Devolve a lista de problemas que sobraram depois da confirmação na
    internet ([] = aprovado), ou None se o revisor não conseguiu responder
    (quem chama decide — o pipeline segue, já que a aprovação manual no
    painel é a segunda barreira)."""
    raw_problems: list[dict] | None = None
    try:
        raw = complete(
            [{"role": "user", "content": _review_prompt(script, topic, facts, web_facts)}],
            max_tokens=1500,
            validate=lambda r: _parse(r) is not None,
        )
        raw_problems = _parse(raw)
    except RuntimeError as exc:
        log.warning("revisor de fatos indisponível (%s) — seguindo sem revisão", exc)
        return None
    if raw_problems is None:
        return None

    problems = []
    for p in raw_problems:
        term = str(p.get("termo", "")).strip()
        if p["tipo"] in _SEARCHABLE and term and not (facts or web_facts) and _confirmed_online(term, strict=p["tipo"] != "ferramenta_inexistente"):
            log.info("revisor não conhecia %r, mas a busca confirmou — ok", term)
            continue
        problems.append(p)
    return problems


# problemas que impedem o vídeo de sair mesmo depois das reescritas; os
# outros (fato duvidoso, título exagerado) só pedem reescrita — o revisor às
# vezes implica com fato verdadeiro, e a aprovação no painel ainda vem depois.
BLOCKING_TYPES = {"ferramenta_inexistente", "atribuicao_falsa", "acusacao_sem_base", "data_desatualizada"}


def feedback_for_rewrite(problems: list[dict]) -> str:
    items = "\n".join(f"- {p.get('termo', '')}: {p.get('motivo', '')}" for p in problems)
    return f"""
REVISÃO DE FATOS DA VERSÃO ANTERIOR (reprovada — corrija TODOS estes pontos):
{items}
Remova ou troque o que foi apontado. Nunca cite ferramenta, produto,
número, nome ou evento que você não tem certeza de que é real. Se o título
prometia algo que não dá pra entregar com fatos reais, mude o título.
"""
