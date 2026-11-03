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
    "caso_generico", "deboche", "copia_da_fonte",
}
# tipos que dá pra confirmar com uma busca na internet (existe ou não
# existe); os outros são problema de redação/atribuição e reprovam direto.
_SEARCHABLE = {"ferramenta_inexistente", "fato_nao_confirmado"}


def _review_prompt(script: dict, topic: str, facts: dict | None, web_facts: list[dict] | None) -> str:
    narration = "\n".join(f"[cena {i + 1}] {s.get('narration', '')}" for i, s in enumerate(script.get("scenes", [])))
    sources = "NENHUMA — o roteiro foi escrito só com conhecimento geral."
    web_block = "Trechos de pesquisa na internet fornecidos ao roteirista:\n" + "\n".join(
        f"- {f['titulo']}: {f['trecho']}" for f in web_facts or []
    )
    if facts:
        sources = "DADOS REAIS fornecidos ao roteirista:\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        if web_facts:
            # perfil do banco + internet (politica_data.perfil_parlamentar)
            sources += "\n\n" + web_block
    elif web_facts:
        sources = web_block

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
  alguém que..." — não é problema. Afirmação geral sobre costume,
  comportamento ou sensação — "o bar vira refúgio", "no bar todo mundo fica
  igual" — também NÃO é caso: caso é um acontecimento com quem/quando/onde.
  Fato COM nome próprio — "Ronaldo marcou 8 gols na Copa de 2002", "o túnel
  Sonnenberg" — NUNCA é caso_generico; se duvidar dele, é fato_nao_confirmado.)
- "deboche": zomba, ridiculariza ou apelida pessoa real identificável
  (idade, aparência, desempenho — "aguardando a aposentadoria compulsória",
  "esse eu não lembro quem é", "voltou por saudade das churrascarias"),
  MESMO que a fonte faça isso (coluna irônica copiada vira voz do canal).
  Só conta o que está escrito no ROTEIRO — deboche que aparece só nas
  FONTES e o roteiro não repetiu NÃO é problema.
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
{_fora_da_fonte_strict if facts else _fora_da_fonte_web if web_facts else ""}
Opinião, tom, estilo e afirmações genéricas e verdadeiras NÃO são problema.
O TEMA foi escolhido e conferido pelo editor: o que está escrito no próprio
TEMA vale como fonte. Fato famoso de pessoa/lugar conhecido (títulos, gols e
artilharias de Copa, medalhas, recordes, datas de conquistas) que você SABE
que é verdadeiro é conhecimento geral, não problema.
Não invente problema: se o roteiro está ok, devolva lista vazia.

TEMA: {topic}
TÍTULO: {script.get('title', '')}

FONTES:
{sources}

ROTEIRO:
{narration}

{_checagem_instr if facts else _checagem_web_instr if web_facts else ""}Responda SÓ com JSON, neste formato exato:
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
Frases que repetem ou variam a "linha_editorial" das FONTES são a opinião
declarada do canal (ou contexto verdadeiro dela): não entram na checagem e
NÃO são problema — mas número, nome ou fato novo colado nelas entra.

"""
# Tema digitado ancorado em busca na internet (curiosidade, crônica, viral):
# os trechos são APOIO, não a fonte única do vídeo — exigir trecho literal de
# toda frase reprovava até descrição e opinião ("iluminação baixa", "o bar vira
# refúgio": job 275, 3 de 3 reprovadas). Aqui só fato específico precisa de
# trecho; conhecimento geral verdadeiro segue a regra do fato_nao_confirmado.
_checagem_web_instr = """ANTES dos problemas, faça a CHECAGEM só dos FATOS ESPECÍFICOS do TÍTULO e
do ROTEIRO: números, porcentagens, datas, nomes de pessoas, lugares, estudos,
pesquisas e eventos, e declarações atribuídas a alguém. Pra cada um, copie o
trecho LITERAL das FONTES que o sustenta; sem trecho, "trecho": null — MAS se
for conhecimento geral amplamente conhecido e verdadeiro (ex.: "a ocitocina
está ligada a vínculo social", "a Grécia Antiga tinha simpósios"), copie
"conhecimento geral" no trecho. Descrição de ambiente, opinião, sensação,
conselho, comparação e frase de efeito NÃO entram na checagem.

"""
_fora_da_fonte_strict = """- "fora_da_fonte": qualquer afirmação, consequência,
  interpretação ou relação que as FONTES não dizem. Confira frase por frase.
  Casos típicos: jogador "emprestado pelo clube X" joga em OUTRO clube — dizer
  que a lesão/fase dele afeta o time X agora é fora_da_fonte; "ex-X" já saiu
  antes — tratar como saída de agora é fora_da_fonte; "negocia/pode/avalia"
  narrado como fato consumado; causa ou impacto que a matéria não cita.
"""
_fora_da_fonte_web = """- "fora_da_fonte": fato ESPECÍFICO (número, data, nome, estudo, evento,
  declaração) que as FONTES não trazem e que não é conhecimento geral
  verdadeiro. Descrição, opinião, sensação e frase de efeito NÃO são
  fora_da_fonte.
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


_COPY_NGRAM = 9  # 9 palavras seguidas iguais à fonte = trecho copiado


def _ngrams(text: str, n: int = _COPY_NGRAM) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def copied_from_source(script: dict, facts: dict | None, web_facts: list[dict] | None) -> list[dict]:
    """Cenas cuja narração copia trecho longo da fonte palavra por palavra —
    checagem por código, não depende do LLM (que com cota esgotada cai em
    modelo fraco). Foi assim que uma coluna irônica debochando dos próprios
    jogadores virou a voz do canal (job 237); copiar matéria também é risco
    de direito autoral. Citação curta (< 9 palavras) passa."""
    source = json.dumps(facts, ensure_ascii=False) if facts else ""
    source += " ".join(f.get("trecho", "") for f in (web_facts or []))
    if not source:
        return []
    src = _ngrams(source)
    out = []
    for i, scene in enumerate(script.get("scenes", [])):
        grams = _ngrams(scene.get("narration", ""))
        if grams and len(grams & src) / len(grams) > 0.4:
            out.append({
                "tipo": "copia_da_fonte", "termo": scene.get("narration", "")[:80],
                "motivo": f"cena {i + 1} copia a fonte palavra por palavra — reescreva com a voz do canal",
            })
    return out


def review_script(
    script: dict, topic: str, facts: dict | None = None, web_facts: list[dict] | None = None,
) -> list[dict] | None:
    """Devolve a lista de problemas que sobraram depois da confirmação na
    internet ([] = aprovado), ou None se o revisor não conseguiu responder
    (quem chama decide — o pipeline segue, já que a aprovação manual no
    painel é a segunda barreira)."""
    copied = copied_from_source(script, facts, web_facts)
    if len(copied) >= 2:
        return copied
    unknown = unknown_names(script, topic, facts, web_facts)
    raw_problems: list[dict] | None = None
    from .topics import _SMALL_MODEL_RE

    try:
        raw = complete(
            [{"role": "user", "content": _review_prompt(script, topic, facts, web_facts)}],
            max_tokens=3000,
            validate=lambda r: _parse(r) is not None,
            # modelo pequeno aprovava "pesquisador Trent-Von Haesler" e
            # "o caso de Dobelle" inventados (job 306)
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
            # 0.8 (padrão, bom pra roteiro) fazia o revisor apontar frases
            # diferentes a cada leitura do mesmo texto (teste de 21/10)
            temperature=0.1,
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

    narration_words = set(re.findall(r"\w+", " ".join(sc.get("narration", "") for sc in script.get("scenes", [])).lower()))
    script_text = " ".join(
        [script.get("title", ""), script.get("thumbnail_text", "")] + [sc.get("narration", "") for sc in script.get("scenes", [])]
    ).lower()
    problems = []
    for p in raw_problems:
        term = str(p.get("termo", "")).strip()
        if p["tipo"] == "deboche":
            # modelo fraco acusava deboche que estava só na FONTE (job 253 e
            # 255): só vale se as palavras do trecho acusado estão na narração
            words = {w for w in re.findall(r"\w+", term.lower()) if len(w) > 3}
            if words and len(words & narration_words) / len(words) < 0.6:
                log.info("deboche apontado não está no roteiro (%r) — ignorado", term)
                continue
        if term and len(term.split()) <= 2 and term[:1].isupper() and term.lower() not in script_text:
            # revisor apontou nome que nem está no roteiro ("Ziyeh" num texto
            # que só tem "Ziyech" — job 320)
            log.info("termo apontado não está no roteiro (%r) — ignorado", term)
            continue
        if p["tipo"] in _SEARCHABLE and term and not (facts or web_facts) and _confirmed_online(term, strict=p["tipo"] != "ferramenta_inexistente"):
            log.info("revisor não conhecia %r, mas a busca confirmou — ok", term)
            continue
        problems.append(p)
    known = {**facts, "_internet": [f.get("trecho", "") for f in web_facts or []]} if facts else None
    problems = problems + unknown + numbers_not_in_facts(script, known) + (
        [] if facts else numbers_not_in_sources(script, web_facts))
    if not problems and (facts or web_facts):
        # aprovado pelo revisor: só então a prova por citação (1 chamada a
        # mais por roteiro, não por rodada). Fora do ar = revisão incompleta
        grounded = ungrounded_sentences(script, facts, web_facts)
        if grounded is None:
            return None
        problems = grounded
    return problems


# valor em reais ou com escala ("R$ 2.309.985,78", "11,8 milhões", "35%")
_VALUE_RE = re.compile(
    r"(R\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*(mil\b|milh(?:ão|ões)|bilh(?:ão|ões)|%)?",
    re.IGNORECASE,
)
_SCALE = {"mil": 1e3, "milhão": 1e6, "milhões": 1e6, "bilhão": 1e9, "bilhões": 1e9}


def _to_number(digits: str) -> float:
    return float(digits.replace(".", "").replace(",", "."))


def _fact_numbers(obj) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _fact_numbers(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _fact_numbers(v)]
    if isinstance(obj, str):
        out = []
        for m in _VALUE_RE.finditer(obj):
            n = _to_number(m.group(2)) * _SCALE.get((m.group(3) or "").lower(), 1)
            out.append(n)
        return out
    return []


def numbers_not_in_facts(script: dict, facts: dict | None, tolerance: float = 0.05) -> list[dict]:
    """Trava no código pra vídeo com DADOS REAIS: valor em reais, com escala
    (mil/milhões) ou porcentagem na narração que não bate (±5%, pra caber
    arredondamento) com nenhum número dos dados é conta ou número inventado.
    Visto: "esses cinco pagamentos somam quase R$ 11,8 milhões" (a soma era
    10,8) passou na revisão do modelo (job 594, 21/10)."""
    if not facts:
        return []
    known = [n for n in _fact_numbers(facts) if n]
    out = []
    for scene in script.get("scenes", []):
        text = scene.get("narration", "")
        for m in _VALUE_RE.finditer(text):
            money, digits, scale = m.group(1), m.group(2), (m.group(3) or "").lower()
            if not (money or scale):
                continue  # número solto (ano, posição, contagem): fica com o revisor
            value = _to_number(digits) * _SCALE.get(scale, 1)
            if scale == "%":
                value = _to_number(digits)
            if any(abs(value - k) <= tolerance * max(abs(k), 1) for k in known):
                continue
            out.append({
                "tipo": "fora_da_fonte", "termo": m.group(0).strip(),
                "motivo": "valor que não está nos dados (conta feita pelo roteiro ou número inventado) — cite só valores dos dados",
            })
    return out


# número com unidade na narração que a fonte da internet/Wikipédia não tem:
# "lago Titicaca, a 4.800 metros de altitude" (são 3.812) passou no revisor
# por LLM (job 704). Só confere número com unidade — ano, dia e contagem
# solta ficam com o revisor.
_MEASURE_RE = re.compile(
    r"(R\$|US\$|€)?\s?(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*"
    r"(mil\b|milh(?:ão|ões)|bilh(?:ão|ões)|%|por cento|metros|m\b|quil[ôo]metros|km|toneladas|litros|"
    r"habitantes|pessoas|mortos|soldados|militares|navios|embarca[çc][õo]es|hectares|graus)",
    re.IGNORECASE,
)
_SOURCE_NUM_RE = re.compile(
    r"(\d{1,3}(?:[.,\s\u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(mil\b|thousand|milh(?:ão|ões)|million|"
    r"bilh(?:ão|ões)|billion)?\s*([a-zà-ÿ]+)?", re.IGNORECASE)
# a unidade tem que ser da mesma família na fonte: "4.800 metros" casava com
# uma contagem de tropas perto de 4.800 (job 705)
_UNIT_FAMILY = {
    **dict.fromkeys(["metros", "m", "meters", "metres", "ft", "feet", "quilômetros", "quilometros", "km",
                     "kilometers", "kilometres", "miles", "mi"], "comprimento"),
    **dict.fromkeys(["toneladas", "tons", "tonnes", "t"], "massa"),
    **dict.fromkeys(["litros", "liters", "litres", "l"], "volume"),
    **dict.fromkeys(["habitantes", "pessoas", "mortos", "people", "inhabitants", "residents", "dead", "killed",
                     "deaths", "casualties", "persons"], "pessoas"),
    **dict.fromkeys(["soldados", "militares", "soldiers", "troops", "men", "sailors", "personnel", "homens",
                     "marinheiros"], "tropas"),
    **dict.fromkeys(["navios", "embarcações", "embarcacoes", "ships", "vessels", "boats", "barcos"], "barcos"),
    **dict.fromkeys(["hectares", "ha"], "area"),
    **dict.fromkeys(["graus", "degrees"], "temperatura"),
}
_SRC_SCALE = {"mil": 1e3, "thousand": 1e3, "milhão": 1e6, "milhões": 1e6, "million": 1e6,
              "bilhão": 1e9, "bilhões": 1e9, "billion": 1e9}


def _source_numbers(text: str, family: str | None = None) -> list[float]:
    """Números da fonte; com `family`, só os seguidos de unidade dessa família."""
    out = []
    for m in _SOURCE_NUM_RE.finditer(text):
        if family and _UNIT_FAMILY.get((m.group(3) or "").lower()) != family:
            continue
        raw = m.group(1)
        # "3.812", "3,812", "3 812" (milhar) ou "3,5"/"3.5" (decimal)
        if re.fullmatch(r"\d{1,3}(?:[.,\s\u00a0]\d{3})+", raw):
            value = float(re.sub(r"\D", "", raw))
        else:
            value = float(raw.replace(",", "."))
        out.append(value * _SRC_SCALE.get((m.group(2) or "").lower(), 1))
    return out


def numbers_not_in_sources(script: dict, web_facts: list[dict] | None, tolerance: float = 0.02) -> list[dict]:
    """Tolerância de 2% (não 5%): com várias cifras na fonte, 5% deixava
    "4.800 metros" passar por causa dos "5.000 militares" (job 704)."""
    if not web_facts:
        return []
    source = " ".join(f"{f.get('titulo', '')} {f.get('trecho', '')}" for f in web_facts)
    all_known = _source_numbers(source)
    out = []
    for scene in script.get("scenes", []):
        for m in _MEASURE_RE.finditer(scene.get("narration", "")):
            unit = (m.group(3) or "").lower()
            value = _to_number(m.group(2)) * _SCALE.get(unit, 1)
            family = _UNIT_FAMILY.get(unit.replace("ç", "c").replace("õ", "o")) or _UNIT_FAMILY.get(unit)
            known = _source_numbers(source, family) if family else all_known

            def close(v: float) -> bool:
                return any((v == k) if v < 100 else abs(v - k) <= tolerance * abs(k) for k in known)

            # "5 mil" na fala e "5.000" (ou "5 thousand") na fonte
            if close(value) or (unit in _SCALE and close(_to_number(m.group(2)))):
                continue
            out.append({
                "tipo": "fora_da_fonte", "termo": m.group(0).strip(),
                "motivo": "número que não aparece nas fontes pesquisadas (Wikipédia/internet) — use só os das fontes",
            })
    return out


# nome próprio que não precisa de confirmação (aparece em qualquer roteiro)
_COMMON_NAMES = {
    "brasil", "deus", "terra", "lua", "sol", "europa", "américa", "áfrica", "ásia",
    "internet", "google", "youtube", "chatgpt", "instagram", "whatsapp", "copa",
    "canva", "microsoft", "apple", "amazon", "meta", "openai", "photoshop", "excel", "word",
}
_NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-ZÀ-Ý][a-zà-ÿ]+(?:[- ](?:de |da |do |von |van )?[A-ZÀ-Ý][a-zà-ÿ]+){0,3})")
_name_cache: dict[str, bool] = {}
# só nome em posição de ATRIBUIÇÃO ("o caso de Dobelle", "segundo o
# pesquisador X") é checado: checar todo nome reprovava país e lugar real
# (Grécia, Chile, Etiópia, Xochimilco — job 315)
_ATTRIBUTION_RE = re.compile(
    r"(?:\b(?:caso|pesquisa|estudo|teoria|método|relato|experimento|livro|descoberta|trabalho|equipe|análise)s?"
    r"(?: (?:feit[oa]s?|conduzid[oa]s?|liderad[oa]s?|publicad[oa]s?))?(?: (?:de|do|da|por|pelo|pela))"
    r"|\b(?:segundo|conforme|explica|afirma|disse|relatou|descobriu|criou|criado por|descoberto por)"
    r"|\b(?:dr|dra|doutor|doutora|professor|professora|pesquisador|pesquisadora|cientista|médico|médica|"
    r"psicólogo|psicóloga|neurocientista|engenheiro|engenheira|autor|autora|especialista|sargento|capitão))"
    r"\.?(?: (?:o|a|os|as|do|da|dos|das|norte-americano|americano|americana|britânico|britânica|"
    r"brasileiro|brasileira|alemão|alemã|francês|francesa|russo|russa))* $",
    re.IGNORECASE,
)
_INSTITUTION_RE = re.compile(
    r"(Justiça|Tribunal|Supremo|Câmara|Senado|Ministério|Universidade|Instituto|Polícia|Governo|"
    r"Banco|Receita|Assembleia|Congresso|Prefeitura|Secretaria|Agência|Fundação|Museu|Hospital)\b"
)


def unknown_names(script: dict, topic: str, facts: dict | None, web_facts: list[dict] | None) -> list[dict]:
    """Nome próprio citado na narração que não está em nenhuma fonte e a
    busca (nome + tema) não acha: inventado. Checagem por código — o revisor
    LLM aprovou "a pesquisa de Trent-Von Haesler" e "o caso de Dobelle"
    (job 306, experiências de quase-morte)."""
    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", []))
    known = " ".join([
        topic, json.dumps(facts or {}, ensure_ascii=False),
        " ".join(f"{f.get('titulo', '')} {f.get('trecho', '')}" for f in (web_facts or [])),
    ]).lower()
    names: list[str] = []
    contexts: dict[str, str] = {}
    for sentence in re.split(r"(?<=[.!?])\s+", narration):
        # 1ª palavra da frase é maiúscula por ser começo de frase
        for m in _NAME_RE.finditer(sentence):
            name = m.group(1)
            if m.start() == 0 and " " not in name and "-" not in name:
                continue
            if not _ATTRIBUTION_RE.search(sentence[max(0, m.start() - 60):m.start()]):
                continue
            first = name.split()[0].split("-")[0].lower()  # "Google Imagens"
            if first in _COMMON_NAMES or name.lower() in known or len(name) < 4:
                continue
            if name not in names:
                names.append(name)
                contexts[name] = sentence
    problems = []
    for name in names[:6]:
        if name not in _name_cache:
            # pessoa (1-2 palavras) só vale no contexto do tema — "Dobelle"
            # existe, mas não em experiência de quase-morte; instituição
            # (3+ palavras, "Universidade Federal de São Paulo") basta existir
            # tema inteiro diluía a busca ("Justiça Eleitoral" e a tática
            # finlandesa "Motti" reprovados à toa): nome + 3 palavras-chave
            institution = len(name.split()) >= 3 or _INSTITUTION_RE.match(name)
            keywords = sorted(set(re.findall(r"\w{5,}", topic.lower())), key=len, reverse=True)[:3]
            query = name if institution else f"{name} {' '.join(keywords)}"
            results = search_topic_facts(query, max_results=3) or []
            words = re.findall(r"\w{3,}", name.lower())
            def ok(r: dict) -> bool:
                text = f"{r['titulo']} {r['trecho']}".lower()
                # pessoa: nome E assunto no mesmo resultado ("Dobelle" existe,
                # mas não em matéria de quase-morte)
                return all(w in text for w in words) and (institution or not keywords or any(k in text for k in keywords))

            found = any(ok(r) for r in results)
            if not found and not institution:
                # as palavras longas do tema às vezes são só enchimento
                # ("registrados continuaram chocaram") e a busca não achava
                # a Elizabeth Smart (07/10): tenta o nome sozinho, com o
                # contexto vindo da frase que cita a pessoa — ano e nome
                # próprio, que valem em qualquer idioma (resultado costuma
                # vir em inglês: "sequestrada" nunca batia com "abducted")
                ctx = contexts[name]
                keywords = [
                    w.lower() for w in re.findall(r"\b(?:1[5-9]\d\d|20\d\d)\b|\b[A-ZÀ-Ý][a-zà-ÿ]{2,}\b", ctx)
                    if w.lower() not in name.lower() and not ctx.startswith(w)
                ][:6]
                if keywords:  # sem contexto, nome sozinho não prova nada
                    results = search_topic_facts(name, max_results=5) or []
                    found = any(ok(r) for r in results)
            _name_cache[name] = found
        if not _name_cache[name]:
            log.warning("nome sem fonte nem resultado na busca: %r", name)
            problems.append({
                "tipo": "fato_nao_confirmado", "termo": name,
                "motivo": "nome próprio que não aparece nas fontes nem na busca — provavelmente inventado",
            })
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
APAGUE o que foi apontado — não troque por outro número, medida, data ou
detalhe que também não esteja nas fontes (é o que reprova a versão nova).
Nunca cite ferramenta, produto,
número, nome ou evento que você não tem certeza de que é real. Se o título
prometia algo que não dá pra entregar com fatos reais, mude o título.
"""


# ---------------------------------------------------------------- citação obrigatória

# O revisor por LLM dá OPINIÃO ("parece certo") e aprovou "a batalha não foi
# naval" (Guerra do Pacífico, job 702), "alguns se afogaram por não saber
# nadar" (job 706) e os 4.800 m do Titicaca. Aqui cada frase com fato tem
# que trazer o TRECHO LITERAL da fonte que a sustenta, e quem confere se o
# trecho existe na fonte é o CÓDIGO — o LLM não consegue "achar que sabe".
_GROUNDING_PROMPT = """Abaixo há FONTES e as FRASES de um roteiro de vídeo.

Para CADA frase, diga se ela afirma um FATO verificável (data, número, nome, evento, causa, consequência,
característica de algo real, o que alguém fez ou disse) ou não (pergunta, opinião, convite, reflexão,
transição, saudação).

ATENÇÃO, passo a passo de app/IA: a instrução que diz o que o app ou a IA FAZ, TEM ou MOSTRA ("o Lens
cria um contato pronto", "a IA mostra os melhores preços", "toque em Adicionar ao calendário") TAMBÉM é
fato — a função tem que existir, então precisa de trecho da fonte. Só a ordem pura sem afirmar função
("Abra o app", "Copie o texto") não é fato.

Se afirma fato: copie da FONTE, LETRA POR LETRA, o trecho (até 30 palavras, uma frase da fonte) que
sustenta o fato. Não traduza, não resuma, não junte pedaços: copie igual. Se NENHUM trecho da fonte
sustenta o fato inteiro (inclusive o número e o nome), responda "trecho": null — não invente.

FONTES:
{fontes}

FRASES:
{frases}

Responda SÓ com JSON: {{"frases": [{{"n": 1, "fato": true, "trecho": "..."}}, {{"n": 2, "fato": false}}]}}"""

_GROUND_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _quote_in_source(quote: str, source_folded: str) -> bool:
    """O trecho citado existe na fonte? Igual (sem acento/pontuação), ou com
    pelo menos 10 palavras seguidas iguais (o LLM às vezes corta uma ponta)."""
    q = _fold_text(quote)
    if len(q.split()) < 4:
        return False
    if q in source_folded:
        return True
    words = q.split()
    run = min(10, len(words))
    return any(" ".join(words[i:i + run]) in source_folded for i in range(len(words) - run + 1))


def _claim_matches_quote(sentence: str, quote: str) -> bool:
    """O trecho fala do que a frase diz? Todo número da frase está no
    trecho, e os nomes próprios da frase também (citar um trecho real sobre
    OUTRA coisa não vale)."""
    s_digits = {re.sub(r"\D", "", n) for n in re.findall(r"\d[\d.,]*", sentence) if len(re.sub(r"\D", "", n)) >= 2}
    q_digits = re.sub(r"[.,\s]", "", quote)
    if any(d not in q_digits for d in s_digits):
        return False
    q = _fold_text(quote)
    names = {_fold_text(w)[:5] for w in re.findall(r"(?<![.!?]\s)(?<!^)\b[A-ZÀ-Ý][a-zà-ÿ]{3,}", sentence)}
    names = {n for n in names if n}
    if names and not any(n in q for n in names):
        return False
    return True


def ungrounded_sentences(script: dict, facts: dict | None, web_facts: list[dict] | None) -> list[dict] | None:
    """Frases de fato sem trecho real da fonte que as sustente — viram
    problema "sem_fonte" (o termo é a própria frase, pra o corte achar).
    [] = tudo sustentado; None = sem fonte pra conferir ou checagem fora do ar."""
    parts = [f"{f.get('titulo', '')}: {f.get('trecho', '')}" for f in web_facts or []]
    if facts:
        parts.append(json.dumps(facts, ensure_ascii=False))
    source = "\n\n".join(parts)
    if not source.strip():
        return None
    sentences = [f.strip() for sc in script.get("scenes", []) for f in _GROUND_SPLIT.split(sc.get("narration", ""))
                 if len(f.strip()) > 15]
    if not sentences:
        return []
    from .topics import _SMALL_MODEL_RE

    prompt = _GROUNDING_PROMPT.format(fontes=source[:9000], frases="\n".join(f"{i + 1}) {s}" for i, s in enumerate(sentences)))
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.1,
                       validate=lambda r: '"frases"' in r, model_filter=lambda m: not _SMALL_MODEL_RE.search(m))
        m = re.search(r"\{.*\}", raw, re.S)
        items = json.loads(m.group(0)).get("frases", []) if m else None
    except Exception as exc:  # noqa: BLE001
        log.warning("checagem por citação indisponível: %s", exc)
        return None
    if not isinstance(items, list):
        return None
    source_folded = _fold_text(source)
    problems = []
    for item in items:
        try:
            idx = int(item.get("n")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= idx < len(sentences)) or not item.get("fato"):
            continue
        sentence, quote = sentences[idx], item.get("trecho")
        if quote and _quote_in_source(str(quote), source_folded) and _claim_matches_quote(sentence, str(quote)):
            continue
        why = ("nenhum trecho da fonte sustenta" if not quote else
               "o trecho citado não existe na fonte" if not _quote_in_source(str(quote), source_folded) else
               "o trecho citado não traz o número/nome da frase")
        problems.append({"tipo": "sem_fonte", "termo": " ".join(sentence.split()[:14]),
                         "motivo": f"fato sem trecho da fonte que o sustente ({why}) — tire ou reescreva com o que a fonte diz"})
    if problems:
        log.warning("checagem por citação: %d de %d frases sem fonte", len(problems), len(sentences))
    return problems
