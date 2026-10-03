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
    "acusacao_sem_base", "data_desatualizada", "titulo_enganoso", "fora_da_fonte",
    "caso_generico",
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
- "caso_generico": acontecimento, descoberta, crime ou história apresentado
  como REAL sem um nome próprio verificável (nome do sítio arqueológico, da
  pessoa, do navio, do caso famoso) — ex.: "em 1980 mergulhadores
  encontraram uma cidade no Mar Vermelho", "um homem em Tóquio acenou pra
  câmera", "a empresa X em 2022", "Lucas decidiu mudar de vida". Isso NÃO
  se confirma por busca: é invenção com cara de fato. (Exemplo hipotético
  claramente apresentado como hipotético — "imagine que...", "pense em
  alguém que..." — não é problema.)
- "acusacao_sem_base": acusa pessoa ou instituição identificável de
  irregularidade, desvio ou crime sem dado concreto nas FONTES.
- "data_desatualizada": trata como ATUAL/RECENTE um ano, versão ou fato
  que já é passado (ex.: "este ano de 2025", "neste 2024", "o lançamento mais
  recente é X" quando já existe coisa mais nova), ou chama de "novidade" algo
  antigo. Citar o ano de um acontecimento passado como passado ("em 2024,
  uma câmera registrou...") está CERTO e NÃO é problema. HOJE É {today}.
- "titulo_enganoso": o título promete algo que o roteiro não entrega (ex.:
  promete "5 gastos revelados pelos dados" e não mostra dado nenhum, ou
  promete "7 descobertas" e o roteiro traz 3), ou
  distorce a fonte (ex.: "a saída de Fulano" quando a fonte diz que ele já
  era ex-jogador).
- "fora_da_fonte" (SÓ quando há FONTES abaixo): qualquer afirmação, consequência,
  interpretação ou relação que as FONTES não dizem. Confira frase por frase.
  Casos típicos: jogador "emprestado pelo clube X" joga em OUTRO clube — dizer
  que a lesão/fase dele afeta o time X agora é fora_da_fonte; "ex-X" já saiu
  antes — tratar como saída de agora é fora_da_fonte; "negocia/pode/avalia"
  narrado como fato consumado; causa ou impacto que a matéria não cita.

Opinião, tom, estilo e afirmações genéricas e verdadeiras NÃO são problema.
Não invente problema: se o roteiro está ok, devolva lista vazia.

TEMA: {topic}
TÍTULO: {script.get('title', '')}

FONTES:
{sources}

ROTEIRO:
{narration}

{_checagem_instr if (facts or web_facts) else ""}Responda SÓ com JSON, neste formato exato:
{{{_checagem_campo if (facts or web_facts) else ""}"problemas": [{{"tipo": "...", "termo": "nome ou afirmação curta (até 8 palavras)", "motivo": "por que é problema, 1 frase"}}]}}"""


# Com fonte, o revisor tem que ancorar CADA afirmação num trecho literal —
# só "procure problemas" deixava passar distorção de relação (ex-jogador
# tratado como saída de agora: testado, passou 2 de 2 vezes). Afirmação sem
# trecho vira fora_da_fonte por código (_parse), não pelo julgamento dele.
_checagem_instr = """ANTES dos problemas, faça a CHECAGEM: liste cada afirmação factual do
TÍTULO e do ROTEIRO (fatos, números, datas, relações de pessoa com clube/
cargo, consequências, impactos) e, pra cada uma, copie o trecho LITERAL das
FONTES que a sustenta — com o MESMO sentido (ex.: "perde o goleiro" NÃO é
sustentado por "ex-goleiro"; "terá de ajustar o elenco" NÃO é sustentado por
"emprestado a outro clube"). Sem trecho que sustente, "trecho": null.
Opinião/emoção de torcedor e chamadas ("deixa nos comentários") não entram.

"""
_checagem_campo = '"checagem": [{"afirmacao": "...", "trecho": "trecho literal da fonte ou null"}], '



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
    for c in data.get("checagem") or []:
        trecho = str(c.get("trecho") or "").strip() if isinstance(c, dict) else ""
        if isinstance(c, dict) and c.get("afirmacao") and trecho.lower() in ("", "null", "none", "-"):
            problems.append({
                "tipo": "fora_da_fonte", "termo": str(c["afirmacao"])[:80],
                "motivo": "nenhum trecho das fontes sustenta essa afirmação",
            })
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
            max_tokens=3000,
            validate=lambda r: _parse(r) is not None,
        )
        raw_problems = _parse(raw)
    except RuntimeError as exc:
        log.warning("revisor de fatos indisponível (%s) — seguindo sem revisão", exc)
        return None
    if raw_problems is None:
        return None
    if not (facts or web_facts):
        # sem fonte não existe "fora da fonte"
        raw_problems = [p for p in raw_problems if p["tipo"] != "fora_da_fonte"]

    problems = []
    for p in raw_problems:
        term = str(p.get("termo", "")).strip()
        if p["tipo"] in _SEARCHABLE and term and not (facts or web_facts) and _confirmed_online(term, strict=p["tipo"] != "ferramenta_inexistente"):
            log.info("revisor não conhecia %r, mas a busca confirmou — ok", term)
            continue
        problems.append(p)
    return problems


# Todo problema apontado impede o vídeo de sair depois das reescritas: os
# canais publicam sozinhos (sem aprovação manual no painel), então não existe
# segunda barreira — vídeo errado no ar é pior que o dia sem vídeo (e o
# daily_run já tenta de novo com outro tema). Antes "fato duvidoso" e
# "título enganoso" passavam; foi assim que saiu o vídeo do Botafogo que
# tratava lesão de jogador emprestado como problema do elenco.
BLOCKING_TYPES = set(_TYPES)


def feedback_for_rewrite(problems: list[dict]) -> str:
    items = "\n".join(f"- {p.get('termo', '')}: {p.get('motivo', '')}" for p in problems)
    return f"""
REVISÃO DE FATOS DA VERSÃO ANTERIOR (reprovada — corrija TODOS estes pontos):
{items}
Remova ou troque o que foi apontado. Nunca cite ferramenta, produto,
número, nome ou evento que você não tem certeza de que é real. Se o título
prometia algo que não dá pra entregar com fatos reais, mude o título.
"""
