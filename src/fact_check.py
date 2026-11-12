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
from dataclasses import dataclass, field
from datetime import date

from .providers import complete
from .web_search import search_topic_facts

log = logging.getLogger("fact_check")

_TYPES = {
    "ferramenta_inexistente", "fato_nao_confirmado", "atribuicao_falsa",
    "acusacao_sem_base", "data_desatualizada", "titulo_enganoso", "fora_da_fonte",
    "caso_generico", "deboche", "copia_da_fonte",
    "frase_quebrada", "promessa_sem_conteudo", "unidade_errada",
}
# tipos que dá pra confirmar com uma busca na internet (existe ou não
# existe); os outros são problema de redação/atribuição e reprovam direto.
_SEARCHABLE = {"ferramenta_inexistente", "fato_nao_confirmado"}

# avisos de qualidade textual: entram em style_warnings e NUNCA reprovam, nem disparam
# conserto, reescrita ou nova tentativa (ver review_script_result)
STYLE_TYPES = {"frase_vaga"}


@dataclass
class ReviewResult:
    """Resultado da revisão em duas listas independentes.
    blocking: erros que reprovam o roteiro (dispararam conserto/reescrita).
    style: avisos de redação, só informativos."""
    blocking: list[dict] = field(default_factory=list)
    style: list[dict] = field(default_factory=list)


def _com_fatos_verificados(fontes: str, script: dict) -> str:
    """Os fatos que a extração já confirmou nas fontes entram como fonte: o revisor
    não pode reprovar o que disserem literalmente (rodada 5: 538,4 bilhões, 23,3 km e
    a data voltavam a cada rodada, mesmo confirmados)."""
    fatos = script.get("_fatos_verificados") or []
    if not fatos:
        return fontes
    bloco = ("FATOS JÁ CONFIRMADOS NAS FONTES OFICIAIS (extraídos e checados; contam como fonte). "
             "Não reprove o que disserem literalmente, nem número, data ou valor igual a eles. "
             "Se outra fonte (ex.: Wikipédia, buscador) der número diferente, vale a lista: "
             "a fonte primária manda sobre a secundária:\n"
             + "\n".join(f"- {x}" for x in fatos))
    return f"{fontes}\n\n{bloco}" if fontes else bloco


def _review_prompt(script: dict, topic: str, facts: dict | None, web_facts: list[dict] | None) -> str:
    narration = "\n".join(f"[cena {i + 1}] {s.get('narration', '')}" for i, s in enumerate(script.get("scenes", [])))
    # título e miniatura também são conferidos contra a narração (09/11: título
    # "fixa 4.000 t" com o roteiro dizendo que 4.000 t/ano é só capacidade)
    if script.get("title") or script.get("thumbnail_text"):
        narration = (
            f"TÍTULO DO VÍDEO: {script.get('title', '')}\n"
            f"TEXTO DA MINIATURA: {script.get('thumbnail_text', '')}\n\nNARRAÇÃO:\n{narration}"
        )
    else:
        narration = f"NARRAÇÃO:\n{narration}"
    from .fontes import bloco_fontes
    bloco = bloco_fontes(facts, web_facts)
    sources = ("FONTES (o mesmo bloco que o roteirista e o conserto leem):\n" + bloco) if bloco else \
        "NENHUMA — o roteiro foi escrito só com conhecimento geral."
    sources = _com_fatos_verificados(sources, script)

    today = date.today().strftime("%d/%m/%Y")
    return f"""Você é revisor de fatos de um canal do YouTube. Revise o roteiro
abaixo ANTES de ele virar vídeo. Aponte SÓ problemas concretos destes tipos:

Pergunta dirigida ao espectador ("Você teria coragem...?", "O que você acha...?") não
é afirmação: não exija fonte para ela e não a aponte, a menos que embuta um fato falso.

- "ferramenta_inexistente": cita pelo nome um produto, app, ferramenta,
  recurso ou empresa que você não tem certeza de que existe, ou atribui a
  uma ferramenta real um recurso que ela não tem.
- "fato_nao_confirmado": número, data, nome de pessoa ou evento específico
  que você acha PROVÁVEL que esteja errado ou inventado. Fato verdadeiro e
  conhecido que só não tem fonte citada NÃO é problema.
- "atribuicao_falsa": diz que algo foi "revelado", "mostrado" ou "está nos
  dados" de uma fonte (Portal da Transparência, TSE, dados oficiais etc.)
  mas as FONTES não trazem esse dado.
- "titulo_nao_confere": o TÍTULO ou o TEXTO DA MINIATURA afirma um fato ou
  número com sentido DIFERENTE do que a narração diz. Ex.: título "fixa 4.000 t
  de CO₂ em 2021" e a narração diz que 4.000 t/ano é a CAPACIDADE declarada
  de uma fábrica (planejado não é feito). Aponte só quando o sentido muda.
- "frase_quebrada": frase sem sentido ou incompleta, com palavra faltando ou
  trocada ("a milhas" sem o número, "St." cortado). Só aponte se a frase NÃO se entende.
- "promessa_sem_conteudo": o roteiro manda fazer ou usar algo que nunca entrega
  ("copie este prompt" sem o prompt). Aponte só se o conteúdo prometido não aparece.
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



# Enchimento e frase vazia que o revisor do LLM deixou passar (job 793):
# "tecnologia avançada e eficiente", "a torneira mais segura", "um marco".
_FILLER_RE = re.compile(
    r"tecnologia (?:avançad[ao]|moderna|eficiente)|(?:avançad[ao]|eficiente) e (?:eficiente|avançad[ao])|"
    r"a torneira mais (?:segura|eficiente)|é o que faz a diferença|um marco (?:histórico|para)|"
    r"revoluciona(?:r|ndo)? o (?:mundo|setor)",
    re.IGNORECASE,
)
_SUPERLATIVE_RE = re.compile(
    r"\b(maior|maiores|menor|menores|primeir[oa]s?|únic[oa]s?|mais (?:antig|alt|grand|rápid|barat|potent)[oa]s?)\b",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


# palavras que se repetem em qualquer texto sem prejudicar a leitura (forma sem acento)
_PALAVRAS_NEUTRAS = {
    "quando", "porque", "entre", "pessoas", "tambem", "depois", "durante", "atraves", "mesmo", "mesma",
    "outro", "outra", "outros", "outras", "cada", "onde", "assim", "entao", "apenas", "ainda", "sempre",
    "muito", "muitos", "muitas", "pouco", "pouca", "fazer", "feito", "podem", "pode", "todos", "todas",
    "sobre", "forma", "parte", "lugar", "coisa", "vezes", "grande", "grandes", "ficou", "estava",
    "estavam", "foram", "sendo", "agora", "antes", "pelos", "pelas", "pelo", "pela", "desde", "embora",
}
# unidades e escalas: "quilômetros" duas vezes numa frase é precisão, não repetição
_UNIDADES = {
    "quilometro", "quilometros", "metro", "metros", "milhao", "milhoes", "bilhao", "bilhoes",
    "trilhao", "trilhoes", "ano", "anos", "dias", "horas", "hora", "ienes", "iene", "dolar", "dolares",
    "reais", "euros", "libras", "tonelada", "toneladas", "graus", "porcento", "centimetros", "quilos",
    "quilograma", "quilogramas", "segundos", "minutos", "semanas", "meses", "seculo", "seculos",
}
_DISTANCIA_REPETICAO = 8  # palavras: a mesma palavra a até 8 posições na mesma frase


def _repeticoes_prejudiciais(sentence: str, tema: set[str]) -> list[str]:
    """Palavra de conteúdo repetida na MESMA frase e a pouca distância (ou 3 vezes).
    Não conta número, unidade, nome próprio, palavra neutra nem vocabulário do tema:
    o assunto do vídeo é para ser repetido."""
    posicoes: dict[str, list[int]] = {}
    palavras: dict[str, str] = {}
    for idx, tok in enumerate(re.findall(r"\w+", sentence)):
        if len(tok) < 5 or any(ch.isdigit() for ch in tok) or tok[:1].isupper():
            continue
        lw = _fold(tok)
        if lw in _PALAVRAS_NEUTRAS or lw in _UNIDADES or lw[:6] in tema:
            continue
        posicoes.setdefault(lw[:6], []).append(idx)
        palavras.setdefault(lw[:6], lw)
    achados = []
    for stem, idxs in posicoes.items():
        perto = any(b - a <= _DISTANCIA_REPETICAO for a, b in zip(idxs, idxs[1:]))
        if perto or len(idxs) >= 3:
            achados.append(palavras[stem])
    return achados


def _vague_problems(script: dict, topic: str = "") -> list[dict]:
    """Avisos de REDAÇÃO (tipo frase_vaga): enchimento sem informação e palavra repetida
    perto na mesma frase. Não reprovam (ver review_script_result). Superlativo no título
    que a narração não diz continua sendo erro (titulo_nao_confere). Sem LLM."""
    tema = {w[:6] for w in re.findall(r"\w{5,}", _fold(str(script.get("title", "")) + " " + topic))}
    problems: list[dict] = []
    for i, sc in enumerate(script.get("scenes", [])):
        text = str(sc.get("narration", ""))
        m = _FILLER_RE.search(text)
        if m:
            problems.append({"tipo": "frase_vaga", "termo": m.group(0), "motivo": f"cena {i + 1}: enchimento sem informação"})
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            for word in _repeticoes_prejudiciais(sentence, tema):
                problems.append({"tipo": "frase_vaga", "termo": sentence.strip()[:80],
                                 "motivo": f"cena {i + 1}: palavra repetida perto na mesma frase ({word})"})
    narration = _fold(" ".join(str(sc.get("narration", "")) for sc in script.get("scenes", [])))
    for sup in _SUPERLATIVE_RE.findall(str(script.get("title", ""))):
        if _fold(sup) not in narration:
            problems.append({"tipo": "titulo_nao_confere", "termo": f"título diz '{sup}'",
                             "motivo": f"título diz '{sup}' e a narração não diz"})
    return problems


def _title_problems(script: dict) -> list[dict]:
    """Checagem própria só de título/miniatura × narração. O revisor geral
    deixou passar "fixa 4.000 t" com a narração dizendo que é capacidade
    (09/11): ele olha o roteiro inteiro e não marca isso. Aqui é uma pergunta
    só, com o título e a narração. Sem resposta válida = não bloqueia."""
    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", []))
    title, thumb = script.get("title", ""), script.get("thumbnail_text", "")
    if not (title and narration):
        return []
    prompt = (
        "Você confere se o TÍTULO e o TEXTO DA MINIATURA de um vídeo afirmam a mesma coisa "
        "que a NARRAÇÃO, no mesmo sentido.\n"
        f"TÍTULO: {title}\nMINIATURA: {thumb}\nNARRAÇÃO: {narration}\n\n"
        "Reprove (ok=false) se o título ou a miniatura afirma um fato ou número com sentido "
        "DIFERENTE: ex.: capacidade planejada/meta/previsão apresentada como feito; número "
        "que a narração dá a outra coisa; ação que a narração não diz que aconteceu. "
        "Título que só resume o assunto sem afirmar nada novo é ok.\n"
        'Responda SÓ JSON: {"ok": true ou false, "motivo": "1 frase"}'
    )

    def _valid(raw: str) -> bool:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            return bool(m) and isinstance(json.loads(m.group(0)).get("ok"), bool)
        except (ValueError, json.JSONDecodeError):
            return False

    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=300, validate=_valid, temperature=0.1)
    except RuntimeError as exc:
        log.warning("checagem de título indisponível (%s) — seguindo", exc)
        return []
    verdict = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
    if verdict["ok"]:
        return []
    return [{"tipo": "titulo_nao_confere", "termo": title, "motivo": str(verdict.get("motivo", ""))}]


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
    de direito autoral. Citação curta (< 9 palavras) passa. Compilação da
    semana fica de fora: a fonte é a narração dos próprios Shorts, já
    revisada — repetir é o objetivo; forçar paráfrase tirou nomes e deixou
    o texto truncado ("bólido rival", "pretensões francesas", job 739)."""
    if facts and facts.get("tipo") == "compilacao":
        return []
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


def _compilation_review(script: dict, topic: str, facts: dict) -> list[dict]:
    """Revisão da compilação semanal: o texto novo não pode trazer número ou
    nome próprio que não esteja nas histórias arquivadas, nos dados nem no tema.
    Transição que parafraseia uma história não é fato novo."""
    from .script_gen import _fact_tokens

    old = [sc.get("narration", "") for sc in script.get("scenes", []) if sc.get("_arquivo")]
    new = [sc.get("narration", "") for sc in script.get("scenes", []) if not sc.get("_arquivo")]
    dados = " ".join(str(item.get("narracao", "")) + " " + str(item.get("titulo", "")) for item in facts.get("dados") or [])
    allowed = _fact_tokens(" ".join(old) + " " + dados + " " + topic, every_capital=True)
    allowed |= {str(len(facts.get("dados") or []))}  # "N histórias" no título é contagem do código "História N de n." é do código, não é fato; palavra comum no
    # começo de frase ("Hoje", "Você") não conta como nome próprio
    import re as _re

    titulo = " ".join([str(script.get("title", "")), str(script.get("thumbnail_text", ""))])
    new_text = _re.sub(r"História \d+ de \d+\.", " ", " ".join(new) + " " + titulo)
    # palavra comum ou nome de país do próprio canal não é fato novo ("Brasil",
    # "Começamos", "Finalmente" reprovaram o 780: regra certa é número/nome real)
    comuns = {"brasil", "começamos", "finalmente", "hoje", "você", "agora", "então", "mas", "nesta",
              "neste", "isso", "essa", "esse", "depois", "antes", "além", "assim", "veja", "confira",
              "ainda", "também", "primeiro", "segundo", "terceira", "última", "história", "histórias",
              "um", "uma", "dois", "duas", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez",
              # marca dos canais (nome/título) não é fato: "Números da República", "Ângulo Interno"
              "números", "república", "ângulo", "interno", "fractal", "curioso", "aprender", "via", "botafogo"}
    extra = sorted(t for t in _fact_tokens(new_text) - allowed if t.lower() not in comuns)
    return [{"tipo": "fato_nao_confirmado", "termo": t,
             "motivo": "no texto novo da compilação (abertura, transição, pergunta ou fechamento) e não está nas histórias"}
            for t in extra[:8]]


def _review_valid(r: str) -> bool:
    """Validador da revisão do roteiro: registra o motivo quando recusa (10/11: a recusa
    do NVIDIA no job 815 não deixou rastro)."""
    if _parse(r) is not None:
        return True
    log.warning("revisão do roteiro recusada: %d caracteres; sem JSON com 'problemas' em lista; fim %r",
                len(r), r[-120:])
    return False


def review_script_result(
    script: dict, topic: str, facts: dict | None = None, web_facts: list[dict] | None = None,
) -> ReviewResult | None:
    """Devolve a lista de problemas que sobraram depois da confirmação na
    internet ([] = aprovado), ou None se o revisor não conseguiu responder
    (quem chama decide — o pipeline segue, já que a aprovação manual no
    painel é a segunda barreira)."""
    if script.get("_fatos_verificados"):
        # 12/11 (job 871): canal de fatos — título e miniatura são descartados e refeitos do texto
        # validado (packaging_from_final); revisar os do escritor só reprovava o roteiro à toa
        script = {**script, "title": "", "thumbnail_text": ""}
    if facts and facts.get("tipo") == "compilacao":
        # compilação: as histórias arquivadas já passaram na revisão; o texto novo
        # (abertura, início, perguntas, fechamento, título, miniatura) é conferido
        # só contra o que as histórias dizem — antes do filtro das cenas arquivadas
        res = _compilation_review(script, topic, facts)
        return None if res is None else ReviewResult(res, [])
    if facts and facts.get("tipo") == "compilacao":
        # cenas que vieram dos Shorts publicados já passaram na revisão:
        # só o texto novo (abertura, transições, fechamento) e o título
        new = [sc for sc in script.get("scenes", []) if not sc.get("_arquivo")]
        script = {**script, "scenes": new or script.get("scenes", [])[:1]}
    copied = copied_from_source(script, facts, web_facts)
    if len(copied) >= 2:
        return ReviewResult(copied, [])
    unknown = unknown_names(script, topic, facts, web_facts)
    raw_problems: list[dict] | None = None
    from .topics import _SMALL_MODEL_RE

    try:
        raw = complete(
            [{"role": "user", "content": _review_prompt(script, topic, facts, web_facts)}],
            # julgamento (temperatura baixa) liga o raciocínio do Nemotron, que come o
            # orçamento: com 3000 (+4096) o JSON saía cortado (job 818: 7.096 tokens, finish=length)
            max_tokens=12000,
            validate=_review_valid,
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
    problems = problems + _title_problems(script) + _vague_problems(script, topic) + unknown + numbers_not_in_facts(script, known) + (
        [] if facts else numbers_not_in_sources(script, web_facts)) + broken_sentences(script) + unfulfilled_promises(script)
    # separação: avisos de estilo saem da lista bloqueante. Daqui em diante só erros
    # bloqueantes decidem (a checagem factual roda mesmo com avisos de estilo)
    style = [p for p in problems if p.get("tipo") in STYLE_TYPES]
    problems = [p for p in problems if p.get("tipo") not in STYLE_TYPES]
    if not problems and (facts or web_facts):
        # aprovado pelo revisor: só então a prova por citação (1 chamada a
        # mais por roteiro, não por rodada). Fora do ar = revisão incompleta
        grounded = ungrounded_sentences(script, facts, web_facts)
        if grounded is None:
            return None
        # a citação não reprova sozinha: o modelo às vezes não acha o trecho
        # que ESTÁ na fonte ("parou de aceitar reservas para depois de 2028",
        # job 730). As frases que ela não sustentou vão ao juiz final como
        # suspeitas, conferidas com atenção redobrada — ele decide
        suspects = [p["termo"] for p in grounded]
    else:
        suspects = []
    if not problems and (facts or web_facts):
        # última barreira: o roteiro COMPLETO (título + todas as frases) vai
        # pro maior modelo que temos, que julga frase por frase se é real.
        # Passaram "80% reprovam" (eram só os jovens, job 717) e "o Falcon 9
        # está sendo aposentado" (ninguém anunciou, job 714) — 04/11
        verdict = verify_full_script(script, topic, facts, web_facts, suspects=suspects)
        if verdict is None:
            return None
        problems = verdict
    if facts and facts.get("tipo") == "compilacao":
        problems = _only_in_reviewed(problems, script)
    if style:
        log.info("revisão: %d aviso(s) de estilo (não reprovam): %s", len(style),
                 "; ".join(str(p.get("motivo", ""))[:80] for p in style[:5]))
    # 11/11: título e miniatura não são revisados aqui. Eles são gerados DEPOIS da validação, a
    # partir do roteiro final (ver script_gen.packaging_from_final); revisar o título antigo reprovava
    # roteiro certo por causa de um título que ainda ia ser trocado
    if script.get("_fatos_verificados"):
        problems = [p for p in problems if p.get("tipo") not in ("titulo_nao_confere", "titulo_enganoso")]
    style = style
    return ReviewResult(problems, style)


def review_script(
    script: dict, topic: str, facts: dict | None = None, web_facts: list[dict] | None = None,
) -> list[dict] | None:
    """Só os erros BLOQUEANTES ([] = aprovado), ou None se o revisor não respondeu.
    Os avisos de estilo ficam de fora: não reprovam, não disparam conserto nem reescrita
    (ver review_script_result)."""
    res = review_script_result(script, topic, facts, web_facts)
    return None if res is None else res.blocking


_NUM_UNIDADE = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*(km|quilômetros?|quilometros?|metros?|bilhões|bilhão|bilhoes|milhões|milhão|milhoes|"
    r"mil|toneladas?|anos?|dias?|pessoas?|operários?|operario|trabalhadores?|mortos?|mortes?|%|por cento|ienes|dólares?|dolares|graus|"
    r"m(?![\wÀ-ÿ])|km(?![\wÀ-ÿ]))?",
    re.IGNORECASE)
_UNIDADE_NORMAL = {"quilômetros": "km", "quilometros": "km", "quilômetro": "km", "metros": "m", "metro": "m",
                   "bilhões": "bi", "bilhão": "bi", "bilhoes": "bi", "milhões": "mi", "milhão": "mi", "milhoes": "mi",
                   "toneladas": "t", "tonelada": "t", "anos": "ano", "ano": "ano", "dias": "dia", "dia": "dia",
                   "pessoas": "pessoa", "pessoa": "pessoa", "mortos": "morte", "morto": "morte", "mortes": "morte",
                   "morte": "morte", "operários": "pessoa", "operário": "pessoa", "trabalhadores": "pessoa", "por cento": "%", "%": "%", "dólares": "dolar", "dólar": "dolar", "dolares": "dolar",
                   "ienes": "iene", "graus": "grau", "km": "km", "m": "m", "t": "t", "mil": "mil"}


def _pares_numero_unidade(texto: str) -> list[tuple[str, str]]:
    """Números com a unidade logo depois (normalizada). Sem unidade, unidade vazia."""
    out = []
    for m in _NUM_UNIDADE.finditer(texto):
        numero = m.group(1).replace(".", "").replace(",", ".")
        unidade = _UNIDADE_NORMAL.get((m.group(2) or "").lower(), "")
        out.append((numero, unidade))
    return out


def _conteudo(texto: str) -> set[str]:
    neutras = {"sobre", "entre", "depois", "durante", "quando", "porque", "ainda", "apenas", "mesmo", "outro"}
    return {w for w in (_fold(x) for x in re.findall(r"[^\W\d_]{5,}", texto)) if w not in neutras}


_PARADAS_ASSUNTO = {"que", "tem", "dos", "das", "com", "sob", "por", "uma", "num", "foi", "sao", "era", "nao",
                    "mais", "para", "pelo", "pela", "como", "isso", "esse", "essa", "este", "esta", "ele", "ela",
                    "the", "ate", "sua", "seu", "nos", "nas"}


def _palavras_assunto(texto: str) -> set[str]:
    return {w for w in (_fold(x) for x in re.findall(r"[^\W\d_]{3,}", texto)) if w not in _PARADAS_ASSUNTO}


def _casa(a: str, b: str) -> bool:
    """Mesma palavra ou mesma família: "morreram"/"morrido", "mar"/"marinho"."""
    if len(a) >= 4 and len(b) >= 4:
        return a[:4] == b[:4]
    return a.startswith(b) or b.startswith(a)


def _mesmo_assunto(frase: str, fato: str, lista: list[str]) -> bool:
    """Frase e fato falam da mesma coisa: pelo menos uma palavra (ou família) em comum,
    sem contar o vocabulário que aparece em metade ou mais da lista (é o tema do vídeo,
    "túnel", "Seikan"). Com menos de 4 fatos não dá para saber o tema: nada é descartado."""
    tema: set[str] = set()
    if len(lista) >= 4:
        contagem: dict[str, int] = {}
        for f in lista:
            for r in {w[:4] for w in _palavras_assunto(f)}:
                contagem[r] = contagem.get(r, 0) + 1
        tema = {r for r, n in contagem.items() if n * 2 >= len(lista)}
    a = {w for w in _palavras_assunto(frase) if w[:4] not in tema}
    b = {w for w in _palavras_assunto(fato) if w[:4] not in tema}
    return any(_casa(x, y) for x in a for y in b)


def substituir_pela_lista(script: dict, problems: list[dict]) -> tuple[dict, list[dict]]:
    """Troca o NÚMERO de uma frase reprovada pelo valor da lista de fatos verificados,
    quando há correspondência segura: mesma unidade e mesmo assunto (pelo menos 2
    palavras de conteúdo em comum), e só um valor candidato. A frase não é reescrita:
    só o número muda, e o número novo já está na lista (validado). Sem correspondência
    segura, o problema continua bloqueante. Devolve (roteiro, problemas que sobraram)."""
    lista = script.get("_fatos_verificados") or []
    if not lista:
        return script, problems
    fatos_pares = [(numero, unidade, fato) for fato in lista for numero, unidade in _pares_numero_unidade(fato)
                   if unidade]
    if not fatos_pares:
        return script, problems
    todos_pares = [(numero, unidade, fato) for fato in lista for numero, unidade in _pares_numero_unidade(fato)]
    scenes = [dict(sc) for sc in script.get("scenes", [])]
    restantes = []
    for p in problems:
        if p.get("tipo") not in ("fato_nao_confirmado", "fora_da_fonte"):
            restantes.append(p)
            continue
        termo = str(p.get("termo", "")).strip()
        achou = False
        for sc in scenes:
            for frase in re.split(r"(?<=[.!?])\s+", sc.get("narration", "")):
                if not termo or _fold(termo)[:40] not in _fold(frase):
                    continue
                novo = frase
                ok = True
                casou = False  # algum número da frase bateu com a lista (mesma unidade e mesmo assunto)
                casados: list[str] = []
                for numero, unidade in _pares_numero_unidade(frase):
                    if not unidade:
                        continue
                    iguais = [fato for n, u, fato in fatos_pares
                              if n == numero and u == unidade and _mesmo_assunto(frase, fato, lista)]
                    if iguais:
                        casou = True
                        casados += iguais
                        continue
                    if any(n == numero for n, _, _ in todos_pares):
                        # número que já está na lista (com outro assunto ou outra grafia da unidade): o
                        # problema não é o número — trocar punha o valor de Gatún em Miraflores (job 865)
                        ok = False
                        break
                    candidatos = {n for n, u, fato in fatos_pares
                                  if u == unidade and len(_conteudo(frase) & _conteudo(fato)) >= 2}
                    if len(candidatos) != 1:
                        ok = False
                        break
                    alvo = candidatos.pop()
                    # troca a primeira ocorrência do número original pelo número da lista
                    original = next(m.group(1) for m in _NUM_UNIDADE.finditer(novo)
                                    if m.group(1).replace(".", "").replace(",", ".") == numero)
                    novo = novo.replace(original, alvo.replace(".", ","), 1)
                    casou = True
                if ok and casou and novo != frase:
                    sc["narration"] = sc.get("narration", "").replace(frase, novo, 1)
                    achou = True
                elif ok and casou:
                    # os números da frase já estão na lista, com o mesmo assunto: resolvido, sem trocar —
                    # se a frase não disser mais que esses fatos ("quase a metade do total de Gatún", job 865)
                    from .fatos import _alem_dos_fatos
                    achou = not _alem_dos_fatos(frase, [{"texto": f} for f in casados])
                # frase sem número da lista (ex.: "Elas serviam de refúgio"): não é resolvida aqui
                break
            if achou:
                break
        if not achou:
            restantes.append(p)
    if len(restantes) == len(problems):
        return script, problems
    return {**script, "scenes": scenes, "_edited": True}, restantes




def _fato_usado(fato: str, narracao: str) -> bool:
    if _fold(fato)[:40] in _fold(narracao):
        return True
    pares = [(n, u) for n, u in _pares_numero_unidade(fato) if u]
    return bool(pares) and set(pares) <= set(_pares_numero_unidade(narracao))


_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def _frase_narravel(fato: str) -> str | None:
    """Fato pronto para a voz ler: sem parênteses (japonês, notas de fonte) e sem
    caracteres que a voz em português não lê. None se não dá para narrar."""
    texto = re.sub(r"\s*\([^)]*\)", "", fato).strip()
    texto = re.sub(r"\s+", " ", texto)
    if not texto or _CJK.search(texto) or re.search(r"\bfonte\b|\busar com\b", texto, re.I):
        return None
    return texto if texto[-1] in ".!?" else texto + "."


def achar_frase(termo: str, frases: list[str]) -> int | None:
    """Índice da frase que o revisor apontou: o trecho exato primeiro; senão a frase com mais
    palavras em comum (pelo menos metade das palavras do trecho), porque o revisor parafraseia
    ("recorde de profundidade para túnel ferroviário")."""
    alvo = _fold(termo)[:40]
    exato = next((i for i, f in enumerate(frases) if alvo and alvo in _fold(f)), None)
    if exato is not None:
        return exato
    palavras = {w for w in re.findall(r"[a-z]{4,}", _fold(termo))}
    if not palavras:
        return None
    melhor, nota = None, 0.0
    for i, f in enumerate(frases):
        comum = len(palavras & {w for w in re.findall(r"[a-z]{4,}", _fold(f))}) / len(palavras)
        if comum > nota:
            melhor, nota = i, comum
    return melhor if nota >= 0.5 else None


def verificar_sem_fato(script: dict, web_facts: list[dict] | None) -> dict:
    """Afirmações que o escritor não marcou com fato ([F#]): uma checagem por citação nas fontes
    (o trecho tem de existir na fonte). Sustentada fica; sem sustentação sai. Checagem fora do ar:
    saem todas (sem prova, não vai ao ar)."""
    pendentes = script.get("_sem_fato") or []
    script = {k: v for k, v in script.items() if k != "_sem_fato"}
    if not pendentes:
        return script
    sem_prova = ungrounded_sentences({"scenes": [{"narration": " ".join(pendentes)}]}, None, web_facts, estrito=True)
    if sem_prova is None:
        tirar = set(pendentes)
    else:
        nao = {_fold(str(p.get("termo", "")))[:60] for p in sem_prova}
        tirar = {f for f in pendentes if _fold(f)[:60] in nao}
    log.warning("afirmações sem marca: %d verificadas, %d sustentadas pelas fontes, %d removidas",
                len(pendentes), len(pendentes) - len(tirar), len(tirar))
    if not tirar:
        return script
    cenas = []
    for sc in script.get("scenes", []):
        frases = [f for f in re.split(r"(?<=[.!?])\s+", sc.get("narration", "")) if f.strip() and f.strip() not in tirar]
        cenas.append({**sc, "narration": " ".join(frases)})
    return {**script, "scenes": [sc for sc in cenas if sc["narration"].strip()]}


def cortar_e_trocar_por_fato(script: dict, problems: list[dict]) -> tuple[dict, int] | None:
    """Saída sem correspondência segura: a frase reprovada sai e, no lugar dela, entra
    um fato VERIFICADO que o roteiro ainda não usou (já validado, sem reescrita).
    Com fatos com origem (_fatos), "usado" é o que a conferência marcou em cada cena;
    sem eles, a comparação pelo texto. None se nada foi trocado."""
    fatos = script.get("_fatos") or []
    scenes = [dict(sc) for sc in script.get("scenes", [])]
    narracao = " ".join(sc.get("narration", "") for sc in scenes)
    from .fatos import mede_o_que_ja_foi_dito
    if fatos:
        usados = {i for sc in scenes for i in sc.get("fatos", [])}
        # fato com medida que a narração já diz não entra (job 872: "85 pés" ao lado de "26 metros")
        disponiveis = [(f["id"], t) for f in fatos if f["id"] not in usados
                       and not mede_o_que_ja_foi_dito(f["texto"], narracao)
                       for t in [_frase_narravel(f["texto"])] if t and 20 <= len(t) <= 220]
    else:
        lista = [f for f in (script.get("_fatos_verificados") or []) if 20 <= len(f) <= 220]
        if not lista:
            return None
        usados = {f for f in lista if _fato_usado(f, narracao)}
        disponiveis = [(f, t) for f in lista if f not in usados for t in [_frase_narravel(f)] if t]
    trocadas = 0
    for prob in problems:
        if prob.get("tipo") not in ("fato_nao_confirmado", "fora_da_fonte", "frase_quebrada"):
            continue
        for sc in scenes:
            frases = re.split(r"(?<=[.!?])\s+", sc.get("narration", ""))
            idx = achar_frase(str(prob.get("termo", "")), frases)
            if idx is None:
                continue
            if disponiveis:
                chave, texto = disponiveis.pop(0)
                frases[idx] = texto
                usados.add(chave)
                if fatos:
                    sc["fatos"] = list(sc.get("fatos", [])) + [chave]
            else:
                frases[idx] = ""
            sc["narration"] = " ".join(f for f in frases if f.strip())
            trocadas += 1
            break
    if not trocadas:
        return None
    novo = {**script, "scenes": [sc for sc in scenes if sc.get("narration", "").strip()], "_edited": True}
    # 12/11: sem mínimo de fatos marcados aqui (o escritor recebe 8 e exigir 8 usados reprovava qualquer
    # corte, job 866); o corpo do texto final é conferido depois, em fatos.roteiro_magro
    return novo, trocadas


def confirmar_afirmacoes(script: dict, problems: list[dict], topic: str,
                         web_facts: list[dict] | None) -> tuple[list[dict], list[dict] | None]:
    """Afirmação SEM número apontada como fora da fonte ("atração para visitantes"): antes de cortar,
    busca fonte para ela e tenta provar por citação (o trecho tem de existir na fonte, conferido em
    código). Provada, deixa de ser problema e a fonte entra na lista. Devolve (problemas que
    sobraram, fontes). Checagem fora do ar: nada muda."""
    alvo = [p for p in problems if p.get("tipo") in ("fora_da_fonte", "fato_nao_confirmado")
            and not re.search(r"\d", str(p.get("termo", "")))]
    frase_de: dict[int, str] = {}
    for p in alvo:
        for sc in script.get("scenes", []):
            frases = re.split(r"(?<=[.!?])\s+", sc.get("narration", ""))
            idx = achar_frase(str(p.get("termo", "")), frases)
            frase = frases[idx].strip() if idx is not None else None
            if frase and len(frase) > 15:
                frase_de[id(p)] = frase
                break
    if not frase_de:
        return problems, web_facts
    evidencia = evidence_for_problems([p for p in alvo if id(p) in frase_de], topic) or []
    fontes = merge_web_facts(web_facts, evidencia) or web_facts
    sem_prova = ungrounded_sentences({"scenes": [{"narration": " ".join(frase_de.values())}]}, None, fontes)
    if sem_prova is None:
        return problems, web_facts
    nao_provadas = {_fold(str(x.get("termo", "")))[:60] for x in sem_prova}
    restantes = [p for p in problems if id(p) not in frase_de or _fold(frase_de[id(p)])[:60] in nao_provadas]
    provadas = len(problems) - len(restantes)
    if provadas:
        log.warning("afirmações sem número confirmadas por citação na busca: %d de %d", provadas, len(frase_de))
    return restantes, fontes


def _only_in_reviewed(problems: list[dict], script: dict) -> list[dict]:
    """Compilação: o revisor lê as narrações dos Shorts como fonte e apontava
    fato DELAS ("Neymar 10 mil reais aos 15") que nem estava no texto novo
    revisado (job 743). Só vale o apontamento cujas palavras estão no texto
    revisado (título, thumb e cenas novas)."""
    text = " ".join([script.get("title", ""), script.get("thumbnail_text", "")]
                    + [sc.get("narration", "") for sc in script.get("scenes", [])]).lower()
    words_in = set(re.findall(r"\w+", text))
    out = []
    for p in problems:
        term = str(p.get("termo", "")).lower()
        words = {w for w in re.findall(r"\w+", term) if len(w) > 3}
        nums = set(re.findall(r"\d+", term))
        if (nums and not nums <= words_in) or (words and len(words & words_in) / len(words) < 0.5):
            log.info("compilação: apontamento fora do texto novo (%r) — ignorado", p.get("termo"))
            continue
        out.append(p)
    return out


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
    # + números com escala em inglês ("$542 million"): dado copiado de fonte
    # estrangeira reprovava "542 milhões" como inventado (job 723, 04/11)
    known = [n for n in _fact_numbers(facts) if n] + _source_numbers(json.dumps(facts, ensure_ascii=False))
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
    r"(mil\b|milh(?:ão|ões)|bilh(?:ão|ões)|%|por cento|metros|m\b|quil[ôo]metros|km|milhas|milha|pés|toneladas|litros|"
    r"habitantes|pessoas|mortos|soldados|militares|navios|embarca[çc][õo]es|hectares|graus)",
    re.IGNORECASE,
)
_SOURCE_NUM_RE = re.compile(
    r"((?:(?<![\w\-−])[-−])?(?:[1-9]\d{0,2}(?:[.,\s\u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?))\s*(mil\b|thousand|milh(?:ão|ões)|million|"
    r"bilh(?:ão|ões)|billion)?\s*(°\s?[CF]?|[a-zà-ÿ]+)?", re.IGNORECASE)
# a unidade tem que ser da mesma família na fonte: "4.800 metros" casava com
# uma contagem de tropas perto de 4.800 (job 705)
_UNIT_FAMILY = {
    **dict.fromkeys(["metros", "m", "meters", "metres", "ft", "feet", "quilômetros", "quilometros", "km",
                     "kilometers", "kilometres", "miles", "mi", "milhas", "milha", "pés", "pes"], "comprimento"),
    **dict.fromkeys(["toneladas", "tons", "tonnes", "t"], "massa"),
    **dict.fromkeys(["litros", "liters", "litres", "l"], "volume"),
    **dict.fromkeys(["habitantes", "pessoas", "mortos", "people", "inhabitants", "residents", "dead", "killed",
                     "deaths", "casualties", "persons"], "pessoas"),
    **dict.fromkeys(["soldados", "militares", "soldiers", "troops", "men", "sailors", "personnel", "homens",
                     "marinheiros"], "tropas"),
    **dict.fromkeys(["navios", "embarcações", "embarcacoes", "ships", "vessels", "boats", "barcos"], "barcos"),
    **dict.fromkeys(["hectares", "ha"], "area"),
    # a fonte escreve "15 °C" (símbolo), a fala "15 graus" (05/11)
    **dict.fromkeys(["graus", "degrees", "°", "°c", "°f", "° c", "° f", "celsius", "fahrenheit"], "temperatura"),
}
_SRC_SCALE = {"mil": 1e3, "thousand": 1e3, "milhão": 1e6, "milhões": 1e6, "million": 1e6,
              "bilhão": 1e9, "bilhões": 1e9, "billion": 1e9}


def _source_number(raw: str) -> float | None:
    """Um número escrito como na fonte: "3.812"/"3,812"/"3 812" (milhar),
    "3,5"/"3.5" (decimal), "0,125" (decimal de 3 casas), sinal negativo, ou os
    dois separadores juntos — "R$ 353.248,04" (pt-BR) e "12,345.67" (en)."""
    raw = raw.strip()
    if not raw:
        return None
    sign = -1.0 if raw[:1] in "-−" else 1.0
    raw = raw.lstrip("-−").strip()
    # só milhar (1-3 dígitos sem zero à frente): remove todo separador
    if re.fullmatch(r"[1-9]\d{0,2}(?:[.,\s\u00a0]\d{3})+", raw):
        return sign * float(re.sub(r"\D", "", raw))
    # último separador é o decimal (1-2 casas) ou há dois tipos de separador;
    # inteiro zero ("0,125") é decimal mesmo com 3 casas
    sep = max(raw.rfind("."), raw.rfind(","))
    if sep != -1:
        integer, fraction = re.sub(r"\D", "", raw[:sep]), re.sub(r"\D", "", raw[sep + 1:])
        if integer and fraction and (len(fraction) <= 2 or re.search(r"[.,]", raw[:sep]) or integer == "0"):
            return sign * float(f"{integer}.{fraction}")
    try:
        return sign * float(re.sub(r"\D", "", raw))
    except ValueError:
        return None



_RANGE_RE = re.compile(
    r"(?<![\w.,\-−])(\d[\d.,]*)\s*(?:-|–|a|até)\s*(\d[\d.,]*)\s+([a-zà-ÿ°]+)", re.IGNORECASE)


def _expand_ranges(text: str) -> str:
    """"entre 5-10 km" -> "5 km e 10 km": intervalo com unidade só no fim
    vale pros dois extremos. Datas ("2020-2021") não têm unidade, não entram."""
    return _RANGE_RE.sub(lambda m: f"{m[1]} {m[3]} {m[2]} {m[3]}", text)

def _source_numbers(text: str, family: str | None = None) -> list[float]:
    """Números da fonte; com `family`, só os seguidos de unidade dessa família."""
    out = []
    for m in _SOURCE_NUM_RE.finditer(_expand_ranges(text)):
        if family and _UNIT_FAMILY.get((m.group(3) or "").lower()) != family:
            continue
        value = _source_number(m.group(1))
        if value is None:
            continue
        out.append(value * _SRC_SCALE.get((m.group(2) or "").lower(), 1))
    return out


# tamanho em metros de cada unidade de distância (pra comparar milhas com km)
_LENGTH_M = {
    "km": 1000.0, "quilômetros": 1000.0, "quilometros": 1000.0, "quilômetro": 1000.0, "kilometers": 1000.0,
    "milhas": 1609.344, "milha": 1609.344, "miles": 1609.344, "mi": 1609.344,
    "metros": 1.0, "m": 1.0, "meters": 1.0,
    "pés": 0.3048, "pes": 0.3048, "ft": 0.3048, "feet": 0.3048,
}


def _source_lengths_m(text: str) -> list[float]:
    """Distâncias da fonte já convertidas pra metros."""
    out = []
    for m in _SOURCE_NUM_RE.finditer(_expand_ranges(text)):
        unit = (m.group(3) or "").lower()
        if unit not in _LENGTH_M:
            continue
        value = _source_number(m.group(1))
        if value is None:
            continue
        out.append(value * _SRC_SCALE.get((m.group(2) or "").lower(), 1) * _LENGTH_M[unit])
    return out


# ---------------------------------------------------------------- redação

# palavra funcional no fim da frase = frase cortada ("...até 10 mil reais por")
_DANGLING_END = re.compile(
    r"\b(de|da|do|das|dos|a|o|as|os|em|no|na|nos|nas|para|pra|com|por|que|e|ou|um|uma|ao|se)\s*$", re.IGNORECASE)
# artigo/preposição + unidade sem número ("a milhas", "de quilômetros")
# "a milhas" (artigo + unidade no plural, sem número): o bug real. "de milhas",
# "as milhas", "milha aérea" são português normal e não entram
_UNIT_WITHOUT_NUMBER = re.compile(r"(?:^|[,;]\s*)(a|o)\s+(milhas|quil[ôo]metros|metros)\b", re.IGNORECASE)
# "St." seguido de maiúscula é nome próprio (St. Patrick, St. Peter, St. John's);
# só é cortado com minúscula, pontuação ou início de frase comum ("St. Ele havia")
_ST_SENTENCE_START = re.compile(
    r"\bSt\.(?:\s+(?:Ele|Ela|Eles|Elas|Em|Na|No|O|A|Os|As|Um|Uma|Depois|Mas|Quando|Ao|Foi|Era|Havia)\b|\s+[a-zà-ú]|\s*[,;!?]|\s*$)")
_REPEATED_WORD = re.compile(r"(?<![.\w])([a-zà-ú]{2,})\s+\1\b", re.IGNORECASE)  # "google.com com" é domínio, não repetição
_REPEAT_OK = {"muito", "mais", "bem", "sim", "não", "nao", "cada", "tudo"}
_PROMISE_RE = re.compile(
    r"\b(copie|cole|use|escreva|digite|pergunte)\s+(este|esse|o|um|seguinte)\s+(prompt|comando|código|codigo|texto)\b"
    r"|\b(este|esse)\s+prompt\b", re.IGNORECASE)
_QUOTED_BLOCK = re.compile(r"[“\"][^”\"]{25,}[”\"]|`{1,3}[^`]{25,}`{1,3}")


def _sentences(text: str) -> list[str]:
    # "St. John's" não é fim de frase: protege a abreviação antes de dividir
    protected = re.sub(r"\bSt\. (?=[A-Z])", "St\u0001", text or "")
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [s.replace("\u0001", ". ").strip() for s in parts if s.strip()]


def broken_sentences(script: dict) -> list[dict]:
    """Frase que não se lê: termina em palavra funcional, tem artigo com unidade
    sem número, "St." que não é nome próprio, ou palavra repetida. Visto:
    "a milhas" e "St." cortado no Terry Fox (744) e Svalbard (752)."""
    out = []
    for scene in script.get("scenes", []):
        text = scene.get("narration", "") or ""
        problems = []
        for sentence in _sentences(text):
            core = sentence.rstrip(" .!?…,;:")
            if _DANGLING_END.search(core):
                problems.append(("termina em palavra solta", sentence))
            if _UNIT_WITHOUT_NUMBER.search(sentence):
                problems.append(("unidade sem número", sentence))
            if _ST_SENTENCE_START.search(sentence):
                problems.append(("abreviação 'St.' sem nome", sentence))
            for m in _REPEATED_WORD.finditer(sentence):
                if m.group(1).lower() not in _REPEAT_OK:
                    problems.append((f"palavra repetida ('{m.group(1)} {m.group(1)}')", sentence))
                    break
        for why, sentence in problems:
            out.append({"tipo": "frase_quebrada", "termo": sentence[:120],
                        "motivo": f"frase com erro de redação ({why}) — reescreva inteira, com o número ou o nome que falta"})
    return out


def unfulfilled_promises(script: dict) -> list[dict]:
    """O roteiro manda usar um prompt/texto e não entrega o conteúdo. Visto:
    "Copie este prompt no NotebookLM" sem prompt nenhum (745, NotebookLM)."""
    narrations = [sc.get("narration", "") or "" for sc in script.get("scenes", [])]
    promise = next((s for n in narrations for s in _sentences(n) if _PROMISE_RE.search(s)), None)
    if promise is None:
        return []
    if any(_QUOTED_BLOCK.search(n) for n in narrations):
        return []
    return [{"tipo": "promessa_sem_conteudo", "termo": promise[:120],
             "motivo": "o roteiro manda usar um prompt/texto que nunca aparece — inclua o conteúdo completo ou tire a promessa"}]


# frase com número/data/medida que precisa de fonte (canal sem pesquisa própria)
_CLAIM_RE = re.compile(
    r"\b(1[5-9]\d\d|20\d\d)\b|\d[\d.,]*\s*(?:mil\b|milh|bilh|km|quil|milha|metros|%|por cento)", re.IGNORECASE)
_claim_cache: dict[str, list[dict]] = {}


def evidence_for_problems(problems: list[dict], topic: str, limit: int = 3) -> list[dict]:
    """Insumo pro conserto: uma busca focada em cada trecho reprovado ("tema +
    trecho"). A busca do tema traz visão geral; o detalhe reprovado (730 dias,
    escola em casa) só aparece numa busca com o próprio trecho, e sem ele o
    reescritor não tem o que trocar a frase."""
    from .web_search import search_topic_facts

    terms: list[str] = []
    for p in problems or []:
        term = str(p.get("termo") or "").strip()
        if term and term not in terms:
            terms.append(term)
    out: list[dict] = []
    for term in terms[:limit]:
        query = f"{topic} {term}"[:200]
        if query not in _claim_cache:
            _claim_cache[query] = search_topic_facts(query) or []
        out.extend(_claim_cache[query])
    if out:
        log.info("insumo pro conserto: %d busca(s) por trecho reprovado, %d fonte(s)", min(len(terms), limit), len(out))
    return out

def merge_web_facts(web_facts: list[dict] | None, extra: list[dict] | None) -> list[dict] | None:
    """Fontes do tema + fontes por afirmação, sem repetir URL. None se vazio."""
    seen = {f.get("url") for f in web_facts or [] if f.get("url")}
    merged = list(web_facts or [])
    for fact in extra or []:
        url = fact.get("url")
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        merged.append(fact)
    return merged or None


def claim_sources(script: dict, topic: str, max_queries: int = 8) -> list[dict]:
    """Fontes da internet pras frases com número/data da narração, pra canal
    que não tem pesquisa de tema (motivacional, curiosidades). Sem isso a
    revisão de fatos não tinha o que conferir e aprovava Senna e Terry Fox
    errados. Cada frase vira uma busca curta (nomes, números e o tema)."""
    from .web_search import search_topic_facts

    claims = [s for sc in script.get("scenes", []) for s in _sentences(sc.get("narration", ""))
              if _CLAIM_RE.search(s)][:max_queries]
    out, seen = [], set()
    for claim in claims:
        keys = re.findall(
            r"[A-ZÀ-Ú][\wà-ú'-]+|(?:R\$|US\$|€)?\s?\d[\d.,]*(?:\s*(?:milhões|bilhões|milhas|milha|quilômetros|metros|mil|km|%|reais|anos|por cento))?",
            claim)
        keys = [k.strip().rstrip('.,') for k in keys if k.strip().rstrip('.,')]
        query = f"{topic} {' '.join(keys)}"[:200]
        if query not in _claim_cache:
            _claim_cache[query] = search_topic_facts(query) or []
        for fact in _claim_cache[query]:
            url = fact.get("url")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            out.append(fact)
    log.info("fontes por afirmação: %d busca(s), %d fonte(s)", len(claims), len(out))
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
            if unit in _LENGTH_M:
                # distância: compara em metros, pra "5.300 milhas" não passar
                # por "5.373 km" (1 milha = 1.609 m, não 1.000)
                metros = value * _LENGTH_M[unit]
                if any(abs(metros - k) <= tolerance * abs(k) for k in _source_lengths_m(source)):
                    continue
                if not _source_lengths_m(source):
                    out.append({
                        "tipo": "fora_da_fonte", "termo": m.group(0).strip(),
                        "motivo": "distância que não aparece nas fontes pesquisadas",
                    })
                    continue
                out.append({
                    "tipo": "unidade_errada", "termo": m.group(0).strip(),
                    "motivo": "distância que não bate com a fonte nessa unidade — confira a conversão (1 milha = 1,6 km) ou use o número da fonte",
                })
                continue
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
# "sem_fonte" (prova por citação) também reprova: fora daqui, a última
# tentativa tratava como "ressalva leve" e soltava o roteiro SEM cortar a
# frase (job 719, 04/11: "humanos para Marte", "capital próprio")
BLOCKING_TYPES = set(_TYPES) | {"sem_fonte"}


def feedback_for_rewrite(problems: list[dict]) -> str:
    items = "\n".join(f"- {p.get('termo', '')}: {p.get('motivo', '')}" for p in problems)
    return f"""
REVISÃO DE FATOS DA VERSÃO ANTERIOR (reprovada — corrija TODOS estes pontos):
{items}
APAGUE o que foi apontado — não troque por outro número, medida, data ou
detalhe que também não esteja nas fontes (é o que reprova a versão nova).
NÃO acrescente nada novo nesta versão: nenhum superlativo ("o mais", "o
maior", "o primeiro", "o único", "recorde", "histórico"), nenhuma
generalização e nenhuma causa ou consequência que a fonte não diga. Frase a
menos é melhor do que frase nova sem fonte.
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


# afirmação sem número que a checagem por número/nome deixava passar (Panamá,
# job 875: "e, minutos depois, sai 26 metros mais alto" — o tempo não está em
# fonte nenhuma). Raiz pt -> raízes aceitas no trecho (pt e en)
_AFIRMACAO_SEM_NUMERO = {
    "segundo": ("segund", "second"), "minuto": ("minut",), "hora": ("hora", "hour"),
    "semana": ("seman", "week"), "mes": ("mes", "month"), "decada": ("decad",),
    "seculo": ("secul", "centur"), "dezena": ("dezen", "dozen"), "centena": ("centen", "hundred"),
    "milhar": ("milhar", "thousand"), "recorde": ("recorde", "record"),
    "unico": ("unic", "only", "sole", "unique"), "primeiro": ("primeir", "first", "pione"),
    "maior": ("maior", "larg", "bigg", "great", "long", "high", "tall"),
    "menor": ("menor", "small", "short", "least"),
}
_AFIRMACAO_RE = re.compile(
    r"\b(segundos|minutos?|horas?|semanas?|mes(es)?|decadas?|seculos?|dezenas|centenas|milhares|recordes?"
    r"|unic[oa]s?|primeir[oa]s?|maior(es)?|menor(es)?)\b")


def afirmacoes_sem_trecho(frase: str, trecho: str) -> list[str]:
    """Palavras de afirmação (tempo, quantidade vaga, superlativo) da frase
    que o trecho citado não traz. Quando seguem um número ("17 anos", "3
    horas") o número já foi conferido; "segundo" como "de acordo com" fica."""
    f, t = _fold_text(frase), _fold_text(trecho)
    faltam = []
    for m in _AFIRMACAO_RE.finditer(f):
        antes = f[:m.start()].rstrip()
        if re.search(r"\d[\d.,]*$", antes) or (m[0] == "segundo" and not re.search(r"\b(um|o|alguns|poucos)$", antes)):
            continue
        raiz = next(k for k in _AFIRMACAO_SEM_NUMERO if m[0].startswith(k[:4]))
        if not any(re.search(r"\b" + r, t) for r in _AFIRMACAO_SEM_NUMERO[raiz]):
            faltam.append(m[0])
    return faltam


def ungrounded_sentences(script: dict, facts: dict | None, web_facts: list[dict] | None,
                         estrito: bool = False) -> list[dict] | None:
    """Frases de fato sem trecho real da fonte que as sustente — viram
    problema "sem_fonte" (o termo é a própria frase, pra o corte achar).
    [] = tudo sustentado; None = sem fonte pra conferir ou checagem fora do ar."""
    parts = [f"{f.get('titulo', '')}: {f.get('trecho', '')}" for f in web_facts or []]
    if facts:
        dados = facts.get("dados") if isinstance(facts, dict) else None
        if isinstance(dados, dict):
            # cada fonte do arquivo separada (nome da fonte + trecho): o ano
            # ou o nome pode estar no NOME da fonte ("CNBC, 2º tri de 2026")
            parts += [f"{k}: {v}" for k, v in dados.items()]
            parts.append(json.dumps({k: v for k, v in facts.items() if k != "dados"}, ensure_ascii=False))
        else:
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
    if estrito:
        # canal de fatos (job 874: "20 de 20 sustentadas" em 3 s): frase que a IA omitiu ou chamou de
        # "não é fato" só passa sem citação se o código reconhece como livre (pergunta sem premissa,
        # saudação, convite); o resto precisa de trecho da fonte
        from .fatos import _frase_livre
        julgados: dict[int, dict] = {}
        for item in items:
            try:
                julgados[int(item.get("n")) - 1] = item
            except (TypeError, ValueError, AttributeError):
                continue
        items = []
        for i, frase in enumerate(sentences):
            item = julgados.get(i) or {"n": i + 1, "fato": True, "trecho": None}
            if not item.get("fato") and not _frase_livre(frase):
                item = {**item, "fato": True}
            if item.get("fato") or not _frase_livre(frase):
                items.append(item)
    for item in items:
        try:
            idx = int(item.get("n")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= idx < len(sentences)) or not item.get("fato"):
            continue
        sentence, quote = sentences[idx], item.get("trecho")
        # número/nome da frase conferidos contra a FONTE de onde o trecho saiu
        # (trecho + nome da fonte), não só o trecho: "2026" no nome "CNBC, 2º
        # tri de 2026" reprovava a frase certa (job 730, 05/11)
        chunk = next((pt for pt in parts if quote and _quote_in_source(str(quote), _fold_text(pt))), str(quote or ""))
        faltam = afirmacoes_sem_trecho(sentence, chunk) if estrito and quote else []
        if quote and _quote_in_source(str(quote), source_folded) and _claim_matches_quote(sentence, chunk) and not faltam:
            continue
        why = ("nenhum trecho da fonte sustenta" if not quote else
               "o trecho citado não existe na fonte" if not _quote_in_source(str(quote), source_folded) else
               f"a fonte não diz {', '.join(faltam)}" if faltam and _claim_matches_quote(sentence, chunk) else
               "o trecho citado não traz o número/nome da frase")
        problems.append({"tipo": "sem_fonte", "termo": " ".join(sentence.split()[:14]),
                         "motivo": f"fato sem trecho da fonte que o sustente ({why}) — tire ou reescreva com o que a fonte diz"})
    if problems:
        log.warning("checagem por citação: %d de %d frases sem fonte", len(problems), len(sentences))
    return problems


# ---------------------------------------------------------------- verificação final

_VERIFY_PROMPT = """Você é o checador de fatos de um canal de vídeos. Nada pode ir ao ar errado: um erro faz o
público fugir. Abaixo estão as FONTES pesquisadas e o ROTEIRO COMPLETO (título + frases numeradas).

Julgue CADA item (o título também — mesmo em forma de pergunta, ele AFIRMA a premissa: "Por que X decidiu Y?"
afirma que X decidiu Y):
- "ok": a fonte sustenta o item INTEIRO, ou o item não afirma fato (pergunta retórica sem premissa, opinião,
  convite, transição, instrução pura como "abra o app").
- "falso": a fonte (ou fato amplamente conhecido) CONTRADIZ o item.
- "sem_prova": a fonte não sustenta o item inteiro. Inclui, SEMPRE:
  * generalizar o que a fonte diz de um GRUPO para todos ("80% dos jovens" virou "80% dos brasileiros");
  * transformar plano, possibilidade, previsão, boato ou "pode" em decisão ou fato consumado
    ("planeja", "estuda", "pode encerrar" virou "está encerrando", "foi aposentado");
  * trocar o sujeito, o número, a data, o lugar, o cargo ou o time;
  * acrescentar causa, consequência, motivo, citação ou adjetivo forte ("o mais", "recorde", "histórico")
    que a fonte não diz;
  * afirmar que um app/IA tem uma função que a fonte não mostra.
NÃO reprove:
  * arredondamento honesto ("cerca de 5.300 km", título "5.300 km" para 5.373 da fonte) — diferença de até
    5% sem mudar o sentido;
  * frase de reflexão, emoção, lição ou mensagem ("não abandonou o sonho", "a mensagem ecoou", "a ação
    começa com um passo") que não afirma um fato novo — é "ok";
  * fato amplamente conhecido e incontroverso sobre o assunto (Terry Fox perdeu a perna para um câncer).
Conta simples com números da fonte vale como sustentada se o resultado bate (58,2 mi − 51,1 mi = 7,1 mi
a mais). Na dúvida, "sem_prova". Seja rigoroso: é melhor cortar uma frase verdadeira do que publicar uma falsa.

FONTES:
{fontes}

TEMA: {tema}

ROTEIRO:
{itens}

Em cada item diga também "concreto": true se ele traz algo ESPECÍFICO — um fato com quem/o quê/quando/
quanto/onde, um nome próprio fazendo algo, um número, ou um passo de tutorial que dá pra seguir ("toque em
Compartilhar"). false se é generalidade ou jargão ("levanta preocupações sem precedentes", "riscos
emergentes", "reforça a urgência de redefinir limites operacionais").

Responda SÓ com JSON: {{"itens": [{{"n": 0, "veredito": "ok", "concreto": true}}, {{"n": 1, "veredito": "sem_prova", "concreto": false, "motivo": "1 frase"}}]}}
(n=0 é o título)"""


def _has_key(key: str):
    """Validador do juiz: a resposta tem de trazer a chave. Quando recusa, registra o
    tamanho e o fim da resposta (10/11: a recusa do Nemotron não dizia o motivo)."""
    def check(r: str) -> bool:
        if f'"{key}"' in r:
            return True
        log.warning("checador recusado: sem a chave %r; %d caracteres; fim %r", key, len(r), r[-120:])
        return False
    return check


def _judge(prompt: str, key: str, max_tokens: int = 3000, whole: dict | None = None) -> list | None:
    """Pergunta ao modelo FORTE (Nemotron 550B, gpt-oss-120b) e devolve a
    lista `key` do JSON. Sem modelo forte = None (modelo pequeno aprova o que
    não devia — é exatamente o que se quer evitar aqui)."""
    from .providers import is_strong_model

    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=0,
                       validate=_has_key(key), model_filter=is_strong_model)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        items = data.get(key)
        if whole is not None:
            whole.update(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("checador forte indisponível: %s", exc)
        return None
    return items if isinstance(items, list) else None


def _sources_text(facts: dict | None, web_facts: list[dict] | None, limit: int | None = None) -> str:
    """Mesmo bloco de fontes da extração e do conserto (src/fontes.py); `limit` não é mais usado."""
    from .fontes import bloco_fontes
    return bloco_fontes(facts, web_facts)


def verify_full_script(script: dict, topic: str, facts: dict | None, web_facts: list[dict] | None,
                       suspects: list[str] | None = None) -> list[dict] | None:
    """Roteiro inteiro, de uma vez, pro checador forte: [] = tudo real;
    problemas "fora_da_fonte" (frase) e "titulo_enganoso" (título); None =
    checador fora do ar (revisão incompleta)."""
    source = _com_fatos_verificados(_sources_text(facts, web_facts), script)
    if not source.strip():
        return None
    sentences = [f.strip() for sc in script.get("scenes", []) for f in _GROUND_SPLIT.split(sc.get("narration", ""))
                 if len(f.strip()) > 3]
    title = str(script.get("title") or "").strip()
    def suspect(sentence: str) -> bool:
        return any(sentence.startswith(t.rstrip(".")) or t.startswith(sentence[:60]) for t in suspects or [])

    itens = "\n".join([f"0) TÍTULO: {title}"] + [
        f"{i + 1}) {'[SUSPEITA: o verificador automático não achou trecho da fonte — procure com cuidado; sem trecho que prove, sem_prova] ' if suspect(s) else ''}{s}"
        for i, s in enumerate(sentences)])
    whole: dict = {}
    items = _judge(_VERIFY_PROMPT.format(fontes=source, tema=topic, itens=itens), "itens",
                   max_tokens=max(12000, 600 + 80 * len(sentences)), whole=whole)
    if items is None:
        return None
    judged = set()
    problems = []
    for item in items:
        try:
            idx = int(item.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        judged.add(idx)
        verdict = str(item.get("veredito", "")).lower()
        if verdict == "ok" or not (0 <= idx <= len(sentences)):
            continue
        why = str(item.get("motivo") or verdict)
        if idx == 0:
            problems.append({"tipo": "titulo_enganoso", "termo": title[:100],
                             "motivo": f"título com premissa que a fonte não sustenta: {why}"})
        else:
            problems.append({"tipo": "fora_da_fonte", "termo": " ".join(sentences[idx - 1].split()[:14]),
                             "motivo": f"checagem final ({verdict}): {why}"})
    if len(judged & set(range(len(sentences) + 1))) < (len(sentences) + 1) * 0.8:
        log.warning("checagem final: resposta incompleta (%d de %d itens)", len(judged), len(sentences) + 1)
        return None
    concrete = [bool(it.get("concreto")) for it in items if isinstance(it, dict) and str(it.get("n")) != "0"]
    if len(concrete) >= 3 and sum(concrete) / len(concrete) < 0.4:
        # o conserto em laço tirava cada fato reprovado e sobrava jargão, que
        # a checagem aprovava por não ter fato a reprovar (job 722, 04/11)
        problems.append({"tipo": "caso_generico", "termo": title[:100],
                         "motivo": "roteiro vago: conte O QUE aconteceu (quem, o quê, quando, quanto) com os fatos da fonte"})
    if problems:
        log.warning("checagem final: %d de %d itens reprovados — %s", len(problems), len(sentences) + 1,
                    "; ".join(f"{p['termo'][:50]} ({p['motivo'][:80]})" for p in problems))
    else:
        log.info("checagem final: roteiro completo aprovado (%d itens)", len(sentences) + 1)
    return problems


_PREMISE_PROMPT = """Tema de um vídeo: "{tema}"

FONTES pesquisadas:
{fontes}

O tema afirma ou pressupõe algum fato (mesmo em forma de pergunta: "Por que X decidiu Y?" pressupõe que X
decidiu Y)? As FONTES sustentam essa premissa INTEIRA — sem transformar plano/possibilidade em decisão, sem
generalizar um grupo para todos, sem trocar número, data ou sujeito?

Se sustentam (ou o tema não tem premissa), responda {{"ok": true}}.
Se não, reescreva o tema sobre o MESMO assunto, só com o que as fontes dizem, no mesmo estilo (pergunta ou
afirmação curta): {{"ok": false, "motivo": "1 frase", "tema": "tema corrigido"}}
Responda SÓ com JSON."""


def _nomes_proprios(texto: str) -> set[str]:
    """Nomes próprios do tema (palavra com maiúscula fora do início): "Panamá", "Esclusas", "Gatún"."""
    palavras = re.findall(r"[\wÀ-ÿ]+", texto or "")
    return {_fold(w) for w in palavras[1:] if w[:1].isupper() and len(w) > 2}


def mantem_assunto(original: str, corrigido: str) -> bool:
    """A premissa corrigida mantém o ASSUNTO: todos os nomes próprios do tema original continuam
    ("Esclusas de Gatún" não pode virar "expansão do Canal do Panamá", job 864)."""
    return _nomes_proprios(original) <= _nomes_proprios(corrigido)


def check_premise(topic: str, facts: dict | None, web_facts: list[dict] | None) -> tuple[bool, str, str] | None:
    """(ok, tema a usar, motivo). O tema vindo da manchete/tendência pode
    trazer premissa falsa ("Por que a SpaceX decidiu encerrar o Falcon 9",
    job 714) e o roteiro inteiro nasce torto. Mantém o ASSUNTO, corrige a
    afirmação. None = sem fonte ou checador fora do ar."""
    source = _sources_text(facts, web_facts, limit=8000)
    if not source.strip():
        return None
    from .providers import is_strong_model

    try:
        raw = complete([{"role": "user", "content": _PREMISE_PROMPT.format(tema=topic, fontes=source)}],
                       max_tokens=2000, temperature=0, validate=lambda r: bool(re.search(r'"ok"\s*:\s*(true|false)', r)),
                       model_filter=is_strong_model)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("checagem da premissa do tema indisponível: %s", exc)
        return None
    if data.get("ok") is True:
        return True, topic, ""
    if not data:
        log.warning("checagem da premissa do tema: resposta ilegível: %r", raw[:200])
    fixed = str(data.get("tema") or "").strip()
    if data.get("ok") is False and fixed:
        return False, fixed, str(data.get("motivo") or "")
    return None

