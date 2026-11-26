"""Banco de conhecimento e evidências (modo de roteiro "redacao", 19/11).

A pesquisa traz páginas (`web_facts`: [{titulo, url, trecho}]). Aqui um modelo forte lê esse
material como pesquisador de redação e propõe EVIDÊNCIAS: uma afirmação em português e, para
cada uma, a frase da fonte que a sustenta, copiada no idioma original. O código não confia na
citação: confere se o trecho existe de fato no texto baixado (`trecho_existe`) e se os números
da afirmação estão no trecho. Fato sem número (qualitativo) recebe o mesmo tratamento do
numérico: entra se o trecho existe, independentemente de ter número.

Nada some calado: trecho que o modelo não reproduziu literalmente é procurado no texto da fonte
(por código e, se preciso, numa segunda chamada); o que mesmo assim não se prova fica no banco
como `nao_verificado`, com o motivo, e não sustenta afirmação nenhuma no roteiro.

Contrato de cada evidência (dict):
    id          "E1", "E2"...  (estável dentro do banco)
    afirmacao   frase em português, fiel à fonte
    tipo        "quantitativo" (tem número/data) | "qualitativo"
    citacoes    [{fonte: "S3", url, titulo, dominio, classe, trecho, verificado: bool, recuperado: bool}]
    status      "oficial"      — algum trecho verificado vem de fonte oficial/acadêmica
                "corroborado"  — trechos verificados de 2+ publicadores independentes
                "fonte_unica"  — um publicador só
                "contraditorio"— outra evidência mais forte (ou igualmente forte) dá outro valor
                                 para a mesma medida; ver `conflitos`
                "nao_verificado" — nenhum trecho provado no texto baixado
    estimado    bool — o próprio trecho apresenta o valor como estimativa/aproximação
    numeros     números da afirmação (forma canônica de fatos.canon)
    conflitos   ["E4"] — ids das evidências que divergem desta
    motivo      só em nao_verificado: por que não se provou

Contrato do banco:
    {versao, tema, fontes: [{id: "S1", url, titulo, dominio, classe}], evidencias: [...],
     lacunas: [perguntas que as fontes não respondem], nao_lidos: [{url, nota, trecho}]}
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
import re
from typing import Callable

from .fatos import _algum_igual, _mesma_coisa, _pares, _palavras, dominio, dominio_raiz, numeros, numeros_fonte, temperaturas
from .fontes import MARCA_CORTE, dobrar, palavras_chave, selecionar
from .ordem import travar_ordem
from . import unidades, vocabulario
from .termos import travar_termos

log = logging.getLogger("evidencias")

# v4, 21/11: contexto de cada citação, seleção das fontes das lacunas; ordem conferida em código, trecho sem
#            corte cego em 600 caracteres (banco antigo em cache não volta)
VERSAO = 5  # 22/11: segunda passagem de cobertura ("cobertura" no banco) e teto de evidências proporcional ao lote
STATUS_USAVEIS = ("oficial", "corroborado", "fonte_unica")
FORCA = {"oficial": 3, "corroborado": 2, "fonte_unica": 1, "contraditorio": 0, "nao_verificado": -1}
# 19/11: o pedido inteiro (fontes + resposta reservada) cabe nos ~8 mil tokens por pedido do Groq grátis
# (providers._SMALL_TPM): com 16 mil caracteres + 12 mil de resposta, o gpt-oss-120b (8 chaves) nunca era
# usado e a extração dependia da única chave NVIDIA
LIMITE_LOTE = 9000           # caracteres de fonte por chamada de extração
POR_FONTE = 4000             # teto de uma fonte num lote (uma Wikipédia não empurra as outras)
MAX_LOTES = 5                # 25/11: 3→5 (página oficial inteira em partes); o que não coube num lote e é relevante vai no seguinte, nunca some calado
RESPOSTA_EXTRACAO = 4000     # tokens reservados para o JSON das evidências
MIN_PALAVRAS_TRECHO = 5
MAX_LACUNAS_BUSCADAS = 2

_ACADEMICO = re.compile(r"(doi\.org|openalex\.org|scielo|researchgate|springer|nature\.com|sciencedirect|wiley|"
                        r"arxiv\.org|jstor|pubmed|ncbi\.nlm|tandfonline|mdpi\.com|frontiersin|plos\.org|"
                        r"cambridge\.org|academic\.oup|ieee\.org|copernicus\.org|agu\.org)", re.I)
_OFICIAL = re.compile(r"(^|\.)((gov|gob|gouv|govt|gv|mil|edu)(\.[a-z]{2})?|(go|ac)\.[a-z]{2}|int|europa\.eu|un\.org|"
                      r"who\.int|noaa\.gov|nasa\.gov|usgs\.gov)$")
_ENCICLOPEDIA = re.compile(r"(wikipedia\.org|britannica\.com|wikidata\.org)$")
_JORNAL = re.compile(r"(globo\.com|folha\.uol\.com\.br|uol\.com\.br|estadao\.com\.br|bbc\.(com|co\.uk)|reuters\.com|"
                     r"apnews\.com|nytimes\.com|theguardian\.com|cnn\.com|elpais\.com|lemonde\.fr|dw\.com|"
                     r"nationalgeographic\.com|smithsonianmag\.com|scientificamerican\.com|newscientist\.com|"
                     r"abril\.com\.br|r7\.com|terra\.com\.br|cnnbrasil\.com\.br|olhardigital\.com\.br|"
                     r"washingtonpost\.com|economist\.com|bloomberg\.com|ft\.com|wsj\.com|afp\.com|"
                     r"japantimes\.co\.jp|themoscowtimes\.com|aljazeera\.com|time\.com|newsweek\.com)$")
# 20/11 (diversidade): hipótese é outra coisa que estimativa — a fonte propõe, não afirma
_HIPOTESE = re.compile(r"\b(hypothes\w*|theori[sz]ed|theory|may have|might have|could (?:explain|have)|is thought|are thought|"
                       r"it is believed|believes?|believed|suggests? that|suggested that|has been suggested|"
                       r"proposed that|speculat\w*|possibly|perhaps|"
                       r"acredita-se|hip[óo]tese|teoria|suspeita-se|pode ter|podem ter|especula\w*|sup[õo]e-se|"
                       r"provavelmente|sugere[m]? que|prop[õo]e[m]? que|propôs que)\b", re.I)
# 20/11: "proposed a tunnel" é uma PROPOSTA (fato histórico); "proposed that X explains" é hipótese
_ESTIMATIVA = re.compile(r"\b(estima|estimad|aproximad|cerca de|em torno de|por volta de|calcula-se|"
                         r"estimat|approximately|approx\.|about|around|roughly|nearly|some \d|up to|até)\b", re.I)


class PesquisaInsuficiente(Exception):
    """O banco não tem evidência suficiente para contar a história (motivo na mensagem)."""


# ---------------------------------------------------------------- fontes
def classe_fonte(item: dict) -> str:
    """"oficial" | "academica" | "enciclopedia" | "jornalistica" | "outra". A marca explícita
    "oficial" do arquivo de fontes (--fontes-file) vale primeiro."""
    if item.get("oficial") is True:
        return "oficial"
    host = dominio(item.get("url", ""))
    if _ACADEMICO.search(host) or "openalex" in str(item.get("titulo", "")).lower():
        return "academica"
    if item.get("oficial") is not False and _OFICIAL.search(host):
        return "oficial"
    if _ENCICLOPEDIA.search(host):
        return "enciclopedia"
    if _JORNAL.search(host):
        return "jornalistica"
    return "outra"


def _fontes_do_banco(web_facts: list[dict]) -> list[dict]:
    return [{"id": f"S{i + 1}", "url": f.get("url", ""), "titulo": f.get("titulo", ""),
             "dominio": dominio(f.get("url", "")), "classe": classe_fonte(f)} for i, f in enumerate(web_facts)]


# ---------------------------------------------------------------- citação literal
def _tokens(texto: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", dobrar(texto))


def trecho_existe(trecho: str, texto_fonte: str) -> bool:
    """O trecho citado está no texto baixado? Compara sem acento, caixa, pontuação e espaços.
    Aceita reticências (cada parte tem de estar, na ordem) e diferença mínima de digitação
    (90% das palavras na mesma sequência, sem espalhar). Trecho curto demais não prova nada."""
    partes = [p for p in re.split(r"\.{3}|…|\[\.\.\.\]", str(trecho or "")) if p.strip()]
    if not partes:
        return False
    fonte = _tokens(texto_fonte)
    if not fonte:
        return False
    juntas = " " + " ".join(fonte) + " "
    total = sum(len(_tokens(p)) for p in partes)
    if total < MIN_PALAVRAS_TRECHO:
        return False
    pos = 0
    for parte in partes:
        q = _tokens(parte)
        if not q:
            continue
        alvo = " " + " ".join(q) + " "
        achou = juntas.find(alvo, pos)
        if achou >= 0:
            pos = achou + len(alvo) - 1
            continue
        if not _quase_igual(q, fonte):
            return False
    return True


def _quase_igual(q: list[str], fonte: list[str]) -> bool:
    if len(q) < 8:
        return False
    sm = difflib.SequenceMatcher(None, fonte, q, autojunk=False)
    blocos = [b for b in sm.get_matching_blocks() if b.size]
    if not blocos:
        return False
    casadas = sum(b.size for b in blocos)
    vao = blocos[-1].a + blocos[-1].size - blocos[0].a
    return casadas >= 0.9 * len(q) and vao <= 1.3 * len(q) + 3


def _frases_da_fonte(texto: str) -> list[str]:
    return [f.strip() for f in re.split(r"(?<=[.!?])\s+|\n+", str(texto or "")) if len(f.strip()) > 20]


def numeros_conferidos(afirmacao: str, nums: set[str], trecho: str) -> tuple[bool, list[dict]]:
    """22/11 (E82): cada número da afirmação está no trecho — literalmente, ou como a MESMA quantidade em outra
    unidade (src/unidades.py: conversão, precisão, sentido "mais de/menos de"). Devolve (ok, conversões aceitas);
    a conversão é registrada, o valor e a unidade da fonte ficam como estão."""
    if unidades.unidades_trocadas(afirmacao, trecho):
        return False, []                    # o número está lá, mas é de outra unidade: não é a mesma medida
    faltando = set(nums) - numeros_fonte(trecho)
    if not faltando:
        return True, []
    cobertos, conversoes = unidades.por_conversao(afirmacao, trecho, faltando, numeros)
    return faltando <= cobertos, (conversoes if faltando <= cobertos else [])


LIMITE_TRECHO = 600


def _cortar_trecho(trecho: str, afirmacao: str, limite: int = LIMITE_TRECHO) -> str:
    """21/11 (E43/E47): a citação era conferida inteira e guardada cortada em 600 caracteres — o parágrafo de 900
    copiado pelo extrator perdia justo a frase com "SPF 1 million" e o juiz não achava o que julgar. Trecho longo
    vira o pedaço CONTÍNUO de frases inteiras (até `limite`) que traz mais números da afirmação e mais palavras dela;
    se nenhum pedaço curto traz os números, fica o trecho inteiro (até 3x o limite) em vez de perder a prova."""
    trecho = str(trecho or "").strip()
    if len(trecho) <= limite:
        return trecho
    nums = numeros(afirmacao)
    pal = {t for t in _tokens(afirmacao) if len(t) > 3}
    frases = _frases_com_posicao(trecho)
    melhor, nota_melhor = None, None
    for a in range(len(frases)):
        for b in range(a, len(frases)):
            ini, fim = frases[a][0], frases[b][1]
            if fim - ini > limite and b > a:
                break
            pedaco = trecho[ini:fim].strip()
            nota = (len(nums & numeros_fonte(pedaco)), len(pal & set(_tokens(pedaco))), -(fim - ini > limite), -a)
            if nota_melhor is None or nota > nota_melhor:
                melhor, nota_melhor = pedaco, nota
    if melhor and (not nums or nums <= numeros_fonte(melhor)) and len(melhor) <= limite:
        return melhor
    if len(trecho) <= 3 * limite:
        return trecho
    return melhor or trecho[:limite]


def _recuperar_por_codigo(afirmacao: str, trecho_modelo: str, texto_fonte: str) -> str | None:
    """O modelo parafraseou ou encurtou a citação: procura na própria fonte a frase que a sustenta
    (mesmos números da afirmação + a maior parte das palavras da citação do modelo)."""
    nums = numeros(afirmacao)
    q = set(_tokens(trecho_modelo)) - {"the", "and", "of", "de", "da", "do", "a", "o", "e", "in", "em"}
    melhor, nota_melhor = None, 0.0
    for frase in _frases_da_fonte(texto_fonte):
        if nums and not nums <= numeros_fonte(frase):
            continue
        t = set(_tokens(frase))
        if not q:
            continue
        nota = len(q & t) / len(q)
        if nota > nota_melhor:
            melhor, nota_melhor = frase, nota
    return melhor if melhor and nota_melhor >= 0.6 else None


# ---------------------------------------------------------------- extração
def _bloco(web_facts: list[dict], escolhidos: dict[int, str]) -> str:
    return "\n".join(f"[S{i + 1}] {web_facts[i].get('titulo', '')} ({dominio(web_facts[i].get('url', ''))}): {t}"
                     for i, t in sorted(escolhidos.items()))


# REGRA DEFINITIVA DE ATOMICIDADE (22/11, decisão do editor):
#  1. Uma evidência representa, de preferência, UM fato independente.
#  2. Uma afirmação composta pode ficar junta quando o MESMO trecho literal da MESMA fonte sustenta todas as partes
#     (o juiz aprova a afirmação inteira por aquele trecho) — não se gasta chamada de IA para separá-la depois.
#  3. Parte não sustentada, com confiança diferente (estimativa/hipótese ao lado de fato) ou que depende de outro
#     trecho ou outra fonte é separada: no pedido (abaixo), na segunda chance da citação (_recuperar_por_llm) e no
#     juiz ("parcial" -> _desmembrar).
#  4. Parte comprovada nunca valida outra: cada parte é julgada sozinha, e a não comprovada fica bloqueada.
#  5. Rastreabilidade: cada evidência guarda as citações que a comprovam; a separada guarda "desmembrada_de" (ou
#     "afirmacao_original"), e a parte que já estava no banco fica em "partes_repetidas" com o id igual.
# Regras de cada evidência: as MESMAS na extração inicial e na passagem de cobertura (22/11).
_REGRAS_EVIDENCIA = """UMA evidência = UM fato independente, de preferência. Pode ficar junto o que o MESMO trecho literal
da MESMA fonte diz inteiro (o valor com a sua unidade, data e sujeito; dois dados de uma mesma frase). Separe em
DUAS evidências, cada uma com o próprio trecho, quando as partes vêm de frases ou fontes diferentes, ou quando uma
parte é estimativa ou hipótese e a outra não. Ex.: "usa 126 atuadores" (uma frase) e "o alinhamento leva meses"
(outra frase) são duas evidências, nunca "usa 126 atuadores e o alinhamento leva meses". O roteiro
junta fatos depois; o banco não.
Para cada evidência:
- "afirmacao": uma frase em português do Brasil, fiel à fonte, sem acrescentar nada (nem causa,
  nem comparação, nem número que a fonte não dá). Unidades e números como estão na fonte (sem
  converter); termo técnico traduzido pelo sentido.
- "tipo": "quantitativo" se tem número ou data, senão "qualitativo".
- "citacoes": lista de {{"fonte": "S3", "trecho": "..."}} — o trecho é uma frase COPIADA
  LITERALMENTE da fonte, no idioma original, sem traduzir, resumir ou juntar pedaços
  (8 a 45 palavras), e contém tudo o que a afirmação diz (inclusive os números). Se mais de uma
  fonte diz a mesma coisa, uma citação de cada.
Não junte fontes diferentes num número novo. Se duas fontes dão valores diferentes para a mesma
coisa, crie uma evidência para cada valor (não escolha um).
"[…]" marca texto que a pesquisa pulou: as frases dos dois lados NÃO são vizinhas na fonte — nunca
ligue uma à outra (nem a data de uma ao acontecimento da outra) e nunca copie um trecho por cima da marca.
Ordem e tempo: "primeiro", "depois", "em seguida", "no mesmo dia" (first, next, then, later) só entram
na afirmação com a referência que a própria fonte dá (o dia, o acontecimento anterior, a etapa de que é o
primeiro passo). Se a fonte não diz em relação a quê, deixe a palavra de ordem fora. Nunca deduza a ordem."""

_PROMPT = """Tema do vídeo: {tema}
{historia}
FONTES (numeradas; é tudo o que existe — não use memória):
{fontes}

Você é o pesquisador de uma redação. Leia as fontes e monte o BANCO DE EVIDÊNCIAS que um
roteirista vai usar para contar a melhor história possível sobre o tema. Procure responder:
o que é / o que aconteceu; como funciona / como aconteceu; por que; quais as consequências;
o que torna o assunto interessante; aspectos pouco conhecidos; curiosidades que ajudam a entender.
Fatos qualitativos (como algo funciona, por que, onde, o que acontece) valem tanto quanto números.
Com a HISTÓRIA definida acima: priorize o que responde às perguntas dela; o que está fora do escopo não entra.

""" + _REGRAS_EVIDENCIA + """

Também liste "lacunas": até 4 perguntas importantes sobre o tema que as fontes NÃO respondem.

Quantidade: cada informação relevante, nova e verificável vira uma evidência, até {maximo} (o teto cresce
com o volume de fontes deste lote; é teto, não meta). Não repita a mesma informação com outras palavras e
não crie evidência fraca ou tangencial para chegar ao teto. Responda SÓ JSON:
{{"evidencias": [{{"afirmacao": "...", "tipo": "qualitativo", "citacoes": [{{"fonte": "S1", "trecho": "..."}}]}}],
 "lacunas": ["..."]}}"""


# 22/11: teto de evidências por chamada proporcional ao material do lote (antes: "entre 10 e 20" para qualquer
# lote — um lote de 9000 caracteres e 8 fontes tinha o mesmo espaço que um de 2000 e 1 fonte). Teto, nunca quota.
CARACTERES_POR_EVIDENCIA = 450
MAX_POR_CHAMADA = 24


def limite_de_evidencias(caracteres: int, n_fontes: int, minimo: int = 3) -> int:
    """Uma evidência a cada ~450 caracteres de fonte, e ao menos uma vaga por fonte com texto; entre
    `minimo` e MAX_POR_CHAMADA."""
    return max(minimo, min(MAX_POR_CHAMADA, max(-(-caracteres // CARACTERES_POR_EVIDENCIA), n_fontes)))


def _valido(raw: str) -> bool:
    try:
        return isinstance(_json(raw).get("evidencias"), list)
    except ValueError:
        return False


def _json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise ValueError("sem JSON")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("JSON não é objeto")
    return data


def _chamar(prompt: str, max_tokens: int = RESPOSTA_EXTRACAO, validar: Callable[[str], bool] = None) -> dict | None:
    """Uma chamada de julgamento factual: SÓ modelo forte (providers.is_strong_model), temperatura
    baixa, JSON validado antes de aceitar. None se nenhum provedor forte respondeu."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    # a cascata espera sozinha o descanso curto das chaves (providers.complete); descanso longo chega
    # aqui como ProvedoresIndisponiveis e sobe até o pipeline, que guarda o trabalho e retoma depois
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=0.1,
                       validate=validar or _valido, model_filter=is_strong_model)
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("extração de evidências indisponível: %s", exc)
        return None
    try:
        return _json(raw)
    except ValueError:
        return None


def _lotes(web_facts: list[dict], topic: str) -> tuple[list[dict[int, str]], list[dict]]:
    """Os trechos das fontes divididos em até MAX_LOTES chamadas, por relevância ao tema. O que
    não coube em nenhum lote volta em `nao_lidos` (registrado no banco e no log)."""
    lotes: list[dict[int, str]] = []
    restante = [dict(f) for f in web_facts]
    nao_lidos: list[dict] = []
    for n in range(MAX_LOTES):
        escolhidos, descartados = selecionar(restante, topic, LIMITE_LOTE, por_fonte=POR_FONTE, custo_fixo=80)
        if not escolhidos:
            break
        lotes.append(escolhidos)
        nao_lidos = descartados
        if not descartados or n == MAX_LOTES - 1:
            break
        # o próximo lote só com os trechos relevantes que ficaram de fora (mesmos índices [S#])
        fora = {}
        for d in descartados:
            fora.setdefault(d["url"], []).append(d["trecho"])
        proximo = []
        for i, f in enumerate(restante):
            usados = escolhidos.get(i, "")
            texto = str(f.get("trecho", ""))[:POR_FONTE]
            sobra = texto.replace(usados, " ") if usados and usados in texto else (texto if i not in escolhidos else "")
            proximo.append({**f, "trecho": sobra if f.get("url") in fora else ""})
        restante = proximo
    if nao_lidos:
        log.warning("banco de evidências: %d trecho(s) relevante(s) não lidos depois de %d lote(s) — o mais "
                    "relevante: %s", len(nao_lidos), len(lotes), nao_lidos[0]["url"][:80])
    return lotes, nao_lidos


# ---------------------------------------------------------------- sujeito da afirmação
_CIRILICO = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))
_NAO_NOME = {"como", "por", "qual", "quais", "quem", "onde", "quando", "o", "a", "os", "as", "e", "um", "uma", "de", "do",
             "da", "no", "na", "em", "que", "porque", "sera", "sabia", "voce"}


def _translit(texto: str) -> str:
    """Cirílico em letras latinas ("Якутск" -> "yakutsk"), para achar o nome numa fonte em russo."""
    return "".join(_CIRILICO.get(ch, ch) for ch in str(texto or "").lower())


def sujeitos_do_tema(topic: str) -> set[str]:
    """Nomes próprios do tema ("Como Yakutsk, a maior cidade..." -> {"yakutsk"}), dobrados (sem acento,
    minúsculos). São o sujeito de quase toda afirmação do banco."""
    nomes = set()
    for m in re.finditer(r"(?<![\w-])([A-ZÀ-Ý][\wÀ-ÿ'-]{2,})", str(topic or "")):
        nome = dobrar(m.group(1))
        if nome not in _NAO_NOME:
            nomes.add(nome)
    return nomes


def _menciona(nome: str, texto: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(nome), dobrar(texto)) is not None or \
        re.search(r"(?<![a-z0-9])" + re.escape(nome), dobrar(_translit(texto))) is not None


def fonte_fala_de(nome: str, fonte: dict) -> bool:
    """A fonte (texto baixado, título ou endereço) fala de `nome`? Aceita o nome dentro de outra forma
    ("Yakutsk's") e a escrita em cirílico."""
    return any(_menciona(nome, str(fonte.get(k, ""))) for k in ("trecho", "titulo", "url"))


def _conferir_sujeito(evidencias: list[dict], web_facts: list[dict], topic: str) -> None:
    """20/11 (E18 do job 998): o extrator pôs "Yakutsk" numa frase genérica de um relatório que não fala
    de Yakutsk ("average annual temperature is around 0°C" — zona de permafrost isolado, arctic.gov).
    A citação literal existia, então passava. Agora: afirmação que nomeia o sujeito do tema só fica
    verificada pela fonte que fala dele. 20/11: quando o nome não aparece na página, a citação vai ao juiz
    com esse aviso (pode ser o mesmo assunto em outro idioma); nada é descartado só por isso."""
    nomes = sujeitos_do_tema(topic)
    if not nomes:
        return
    for e in evidencias:
        citados = {n for n in nomes if _menciona(n, e["afirmacao"])}
        if not citados:
            continue
        for c in e["citacoes"]:
            if not c.get("verificado"):
                continue
            i = int(c["fonte"][1:]) - 1
            fonte = web_facts[i] if 0 <= i < len(web_facts) else {}
            if not any(fonte_fala_de(n, fonte) for n in citados):
                # 20/11 (diversidade): o nome em português pode não aparecer numa página em outro idioma, que
                # chama o assunto de outro jeito. O código não sabe se é outro nome do mesmo assunto ou outro
                # assunto (E18): não descarta; o juiz de sustentação decide, avisado de que o nome não aparece
                c["sujeito_nao_achado"] = sorted(citados)
                log.info("evidência %r: o nome %s não aparece na fonte %s — o juiz confere o sujeito",
                         e["afirmacao"][:70], ", ".join(sorted(citados)), c.get("dominio") or c["fonte"])


def _validar(brutos: list, web_facts: list[dict], fontes: list[dict]) -> list[dict]:
    """Citações conferidas no texto baixado; status, estimativa e números calculados por código."""
    saida = []
    for b in brutos:
        if not isinstance(b, dict):
            continue
        afirmacao = re.sub(r"\s+", " ", str(b.get("afirmacao", ""))).strip()
        if not afirmacao:
            continue
        nums = numeros(afirmacao)
        citacoes = []
        for c in b.get("citacoes") or []:
            if not isinstance(c, dict):
                continue
            m = re.search(r"\d+", str(c.get("fonte", "")))
            if not m or not 1 <= int(m.group()) <= len(web_facts):
                continue
            i = int(m.group()) - 1
            texto = str(web_facts[i].get("trecho", ""))
            original = str(c.get("trecho", "")).strip()
            trecho = _cortar_trecho(original, afirmacao)  # conferido e guardado o MESMO texto
            nums_ok, conversoes = numeros_conferidos(afirmacao, nums, trecho)
            ok = trecho_existe(trecho, texto) and nums_ok
            recuperado = False
            if not ok:
                achado = _recuperar_por_codigo(afirmacao, original, texto)
                if achado:
                    achado = _cortar_trecho(achado, afirmacao)
                    nums_ok, conversoes = numeros_conferidos(afirmacao, nums, achado)
                    if nums_ok:
                        trecho, ok, recuperado = achado, True, True
            citacoes.append({**{k: fontes[i][k] for k in ("url", "titulo", "dominio", "classe")},
                             "fonte": fontes[i]["id"], "trecho": trecho, "verificado": ok,
                             "recuperado": recuperado, **({"conversoes": conversoes} if ok and conversoes else {})})
        saida.append({"afirmacao": afirmacao, "tipo": "quantitativo" if nums else "qualitativo",
                      "citacoes": citacoes, "numeros": sorted(nums)})
    return saida


_PROMPT_RECUPERAR = """Cada AFIRMAÇÃO abaixo não foi achada literalmente na(s) FONTE(S) indicada(s). Para cada uma:
- se ela diz UM fato só: copie da fonte a frase que o sustenta, LITERALMENTE (no idioma original, sem
  traduzir, resumir nem juntar pedaços);
- se ela junta MAIS DE UM fato (dois números, duas etapas, um fato e a sua causa ou comparação): separe em
  fatos independentes, um por item, cada um com a sua própria frase literal;
- fato que nenhuma fonte indicada sustenta: "trecho": "" (não invente).
Cada "afirmacao" em português do Brasil, fiel à fonte, sem acrescentar nada.
Responda SÓ JSON: {{"evidencias": [{{"n": 1, "partes": [{{"afirmacao": "...", "fonte": "S3", "trecho": "..."}}]}}]}}

{itens}"""
MAX_RECUPERAR = 12        # evidências por extração que ganham a segunda chance (lotes de 6 por chamada)
MAX_PARTES = 5            # fatos independentes de uma afirmação composta


def _recuperar_por_llm(pendentes: list[dict], web_facts: list[dict],
                       fontes: list[dict] | None = None) -> dict[int, list[dict]] | None:
    """Segunda chance: o modelo copia de novo a frase da fonte e (22/11, evidências atômicas) SEPARA a afirmação
    que junta mais de um fato — a causa das 3 evidências sem verificação do lote grande da rodada 11: nenhum trecho
    literal tinha todos os números de duas frases juntadas. Cada parte passa pela MESMA conferência literal
    (_validar) e vira uma evidência própria. Devolve {id(evidência): partes que a substituem} — só quando alguma
    parte foi verificada; sem isso a evidência fica como estava (bloqueada). O formato antigo ({"n", "trecho"}) vale
    como uma parte só, com a afirmação original."""
    if len(pendentes) > MAX_RECUPERAR:
        log.warning("segunda chance da citação: %d evidência(s) além de %d ficam sem ela", len(pendentes) - MAX_RECUPERAR,
                    MAX_RECUPERAR)
        pendentes = pendentes[:MAX_RECUPERAR]
    trocas: dict[int, list[dict]] = {}
    for k in range(0, len(pendentes), 6):
        lote = pendentes[k:k + 6]
        itens = []
        for n, e in enumerate(lote, 1):
            ids = list(dict.fromkeys(c["fonte"] for c in e["citacoes"]))[:3]
            textos = "\n".join(f"FONTE {s}: {str(web_facts[int(s[1:]) - 1].get('trecho', ''))[:2500]}" for s in ids
                               if 1 <= int(s[1:]) <= len(web_facts))
            itens.append(f"{n}) AFIRMAÇÃO: {e['afirmacao']}\n{textos}")
        data = _chamar(_PROMPT_RECUPERAR.format(itens="\n\n".join(itens)), max_tokens=1500)
        if not data:
            continue
        for r in data.get("evidencias") or []:
            try:
                e = lote[int(r.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            partes = r.get("partes")
            if not isinstance(partes, list):  # formato antigo: a mesma afirmação, um trecho novo
                partes = [{"afirmacao": e["afirmacao"], "fonte": e["citacoes"][0]["fonte"], "trecho": r.get("trecho", "")}]
            partes = [p for p in partes if isinstance(p, dict) and str(p.get("afirmacao", "")).strip()][:MAX_PARTES]
            if len(partes) == 1 and re.sub(r"\s+", " ", str(partes[0]["afirmacao"])).strip() == e["afirmacao"]:
                c = next((x for x in e["citacoes"] if x["fonte"] == str(partes[0].get("fonte"))), e["citacoes"][0])
                trecho = _cortar_trecho(str(partes[0].get("trecho", "")).strip(), e["afirmacao"])
                texto = str(web_facts[int(c["fonte"][1:]) - 1].get("trecho", ""))
                nums_ok, conversoes = numeros_conferidos(e["afirmacao"], set(e["numeros"]), trecho) if trecho else (False, [])
                if trecho and trecho_existe(trecho, texto) and nums_ok:
                    c.update({"trecho": trecho, "verificado": True, "recuperado": True,
                              **({"conversoes": conversoes} if conversoes else {})})
                continue
            if fontes is None:
                continue
            ids_e = {c["fonte"] for c in e["citacoes"]}
            brutos = [{"afirmacao": p["afirmacao"],
                       "citacoes": [{"fonte": p.get("fonte"), "trecho": p.get("trecho", "")}]
                       if str(p.get("trecho", "")).strip() and str(p.get("fonte")) in ids_e else []} for p in partes]
            novas = _validar(brutos, web_facts, fontes)
            if not any(c["verificado"] for x in novas for c in x["citacoes"]):
                continue
            base = {k2: v for k2, v in e.items() if k2 not in ("afirmacao", "citacoes", "numeros", "tipo")}
            for x in novas:
                for c in x["citacoes"]:
                    c["recuperado"] = True
                if not x["citacoes"]:  # parte sem trecho: fica registrada, bloqueada, com a citação original
                    x["citacoes"] = [{**copy.deepcopy(c), "verificado": False} for c in e["citacoes"][:1]]
            trocas[id(e)] = [{**copy.deepcopy(base), **x, "desmembrada_de": e["afirmacao"]} for x in novas]
    return trocas


def _aplicar_trocas(evidencias: list[dict], trocas: dict[int, list[dict]] | None, existentes: list[dict] | None = None,
                    log_repetidas: list | None = None) -> list[dict]:
    """Troca cada afirmação composta pelas suas partes atômicas. Parte que já está no banco (ou em outra evidência
    desta extração) não entra de novo — sem duplicação; fica anotada em `log_repetidas`."""
    if not trocas:
        return evidencias
    saida: list[dict] = []
    for e in evidencias:
        if id(e) not in trocas:
            saida.append(e)
            continue
        for p in trocas[id(e)]:
            outras = (existentes or []) + saida + [x for x in evidencias if x is not e and id(x) not in trocas]
            igual = _ja_no_banco(p["afirmacao"], outras, p["citacoes"])
            if igual:
                if log_repetidas is not None:
                    log_repetidas.append({"afirmacao": p["afirmacao"], "igual_a": igual, "desmembrada_de": e["afirmacao"]})
                continue
            saida.append(p)
        log.info("afirmação composta separada em %d fato(s): %r", len(trocas[id(e)]), e["afirmacao"][:70])
    return saida


# ---------------------------------------------------------------- sustentação (trecho comprova a afirmação?)
MARGEM_CONTEXTO = 600   # caracteres de cada lado do trecho: o que é preciso para interpretar "a cidade", "ali"
LOTE_SUSTENTACAO = 8
# 25/11 (Svalbard 1019): com 1800 a resposta (decisão + partes + termos de 8 itens) vinha cortada em quase todo lote
# e era refeita inteira com 3600 — cada lote pagava duas chamadas (3–4 min em vez de 1,5–2). Já começa com 3600
RESPOSTA_SUSTENTACAO = 3600
DECISOES = ("sustenta", "parcial", "irrelevante", "contradiz")


def _posicao(trecho: str, texto: str) -> int | None:
    toks = re.findall(r"\w+", str(trecho or ""))[:8]
    if len(toks) >= 3:
        m = re.search(r"\W+".join(map(re.escape, toks)), str(texto or ""), re.I)
        if m:
            return m.start()
    return None


def contexto_do_trecho(trecho: str, texto: str, margem: int = MARGEM_CONTEXTO) -> str:
    """O texto da página em volta do trecho citado (o trecho sozinho se não for achado por posição).
    21/11 (E86): o contexto para na marca de corte — o que está do outro lado não é vizinho na fonte."""
    pos = _posicao(trecho, texto)
    if pos is None:
        return str(trecho or "")
    esq, dir_ = texto[max(0, pos - margem):pos], texto[pos:pos + len(trecho) + margem]
    if MARCA_CORTE in esq:
        esq = MARCA_CORTE + " " + esq.rsplit(MARCA_CORTE, 1)[1].lstrip()
    corte = dir_.find(MARCA_CORTE, len(trecho) // 2)
    if corte >= 0:
        dir_ = dir_[:corte].rstrip() + " " + MARCA_CORTE
    return esq + dir_


# ---------------------------------------------------------------- contexto de cada citação (21/11, E86)
# A frase literal pode estar certa e a leitura dela, errada: "First, commands deployed the aft momentum flap" é o
# primeiro passo DE UMA ETAPA (a que a frase anterior data), não da missão. Cada citação verificada guarda o que
# é preciso para interpretá-la, copiado da fonte: o parágrafo, a frase anterior e a seguinte, as palavras de
# ordem e a referência de tempo mais próxima. Nada é deduzido: só o que está no texto baixado, e a vizinhança
# só vale onde a pesquisa garante que não pulou frase (cortes marcados).
_SEQUENCIA = re.compile(
    r"\b(first(?:ly)?|then|next|later|afterwards?|subsequently|finally|eventually|initially|previously|earlier|"
    r"meanwhile|before|after|followed by|(?:on )?the (?:same|next|following|previous) day|"
    r"primeir[oa]s?|depois|em seguida|a seguir|posteriormente|mais tarde|(?:n)?o mesmo dia|no dia seguinte|"
    r"inicialmente|finalmente|por fim|antes|após|anteriormente|logo depois|"
    r"primer[oa]?|después|luego|más tarde|el mismo día)\b", re.I)
# 26/11: tabela única de meses (src/vocabulario.py); a antiga era por prefixo ("mar\\w*" casava "maritime")
_MESES = vocabulario.re_mes()
_DATA = re.compile(rf"\b(\d{{1,2}}(?:º|st|nd|rd|th)?\s+(?:de\s+)?(?:{_MESES})(?:\s+(?:de\s+)?\d{{4}})?|"
                   rf"(?:{_MESES})\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?|"
                   r"(?:on )?day (?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)|(?:no )?dia \d+|"
                   r"(?:1[5-9]|20)\d{2})\b", re.I)


def _frases_com_posicao(texto: str) -> list[tuple[int, int, str]]:
    """(início, fim, frase) de cada frase; a nota de rodapé colada ao ponto ("destination.[35] On 28...") fica
    com a frase dela, e a marca de corte é uma fronteira."""
    out = []
    for m in re.finditer(r"[^\n]+", texto):
        ini = m.start()
        fronteiras = [(b.start() + 1, b.end()) for b in
                      re.finditer(r"[.!?](?:\[[^\]\n]{1,8}\]|[\"'”’)])*(?:\s+|(?<=\])(?=[A-ZÀ-Ý]))", m.group())]
        fronteiras += [(c.start(), c.end()) for c in re.finditer(r"\s*" + re.escape(MARCA_CORTE) + r"\s*", m.group())]
        pos = 0
        for a, b in sorted(fronteiras) + [(len(m.group()), len(m.group()))]:
            if a < pos:
                continue
            parte = m.group()[pos:a]
            if parte.strip() and parte.strip() != MARCA_CORTE:
                out.append((ini + pos, ini + a, parte.strip()))
            pos = b
    return out


def contexto_da_citacao(trecho: str, texto: str, cortes_marcados: bool = False) -> dict:
    """O que a fonte diz em volta da citação, literal. `contiguidade`: "marcada" quando a pesquisa marcou toda
    frase pulada (as vizinhas são vizinhas de verdade) ou "nao_garantida" (texto antigo: frases que parecem
    vizinhas podem ter tido outras entre elas)."""
    texto = str(texto or "")
    ctx: dict = {"contiguidade": "marcada" if cortes_marcados else "nao_garantida",
                 "marcadores": sorted({m.group().lower() for m in _SEQUENCIA.finditer(str(trecho or ""))})}
    pos = _posicao(trecho, texto)
    if pos is None:
        return ctx
    fim = pos + len(str(trecho or ""))
    # o pedaço contínuo (entre marcas de corte) e o parágrafo (entre quebras de linha) que contêm o trecho
    ini_seg = texto.rfind(MARCA_CORTE, 0, pos)
    ini_seg = 0 if ini_seg < 0 else ini_seg + len(MARCA_CORTE)
    fim_seg = texto.find(MARCA_CORTE, fim)
    fim_seg = len(texto) if fim_seg < 0 else fim_seg
    ini_par = max(ini_seg, texto.rfind("\n", 0, pos) + 1)
    fim_par = texto.find("\n", fim)
    fim_par = min(fim_seg, len(texto) if fim_par < 0 else fim_par)
    paragrafo = texto[ini_par:fim_par].strip()
    if len(paragrafo) > 1200:  # parágrafo enorme: a janela em volta do trecho
        paragrafo = texto[max(ini_par, pos - 500):min(fim_par, fim + 500)].strip()
    ctx["paragrafo"] = paragrafo
    frases = [f for f in _frases_com_posicao(texto) if f[1] > ini_seg and f[0] < fim_seg]
    dentro = [k for k, f in enumerate(frases) if f[1] > pos and f[0] < fim]
    if dentro:
        a, b = dentro[0], dentro[-1]
        ctx["anterior"] = frases[a - 1][2][:400] if a > 0 else ""
        ctx["posterior"] = frases[b + 1][2][:400] if b + 1 < len(frases) else ""
    ctx["corte_antes"] = texto[ini_seg - len(MARCA_CORTE):ini_seg] == MARCA_CORTE and not ctx.get("anterior")
    ctx["corte_depois"] = texto[fim_seg:fim_seg + len(MARCA_CORTE)] == MARCA_CORTE and not ctx.get("posterior")
    # referência de tempo: a do próprio trecho; senão a última antes dele no mesmo pedaço contínuo
    proprio = _DATA.findall(str(trecho or ""))
    if proprio:
        ctx["referencia_temporal"] = {"expressao": proprio[0], "frase": "", "no_trecho": True}
    else:
        antes = [(f, m.group()) for f in frases if f[1] <= pos for m in _DATA.finditer(f[2])]
        if antes:
            f, expr = antes[-1]
            ctx["referencia_temporal"] = {"expressao": expr, "frase": f[2][:400], "no_trecho": False}
    return ctx


def atravessa_corte(trecho: str, texto: str) -> bool:
    """O trecho só existe juntando os dois lados de uma marca de corte (frases que não são vizinhas)."""
    if MARCA_CORTE not in str(texto or ""):
        return False
    return not any(trecho_existe(trecho, seg) for seg in str(texto).split(MARCA_CORTE))


def anotar_contexto(evidencias: list[dict], web_facts: list[dict]) -> None:
    """Contexto em toda citação verificada; citação que atravessa um corte deixa de valer (junta frases que a
    fonte não junta)."""
    for e in evidencias:
        for c in e["citacoes"]:
            if not c.get("verificado"):
                continue
            i = int(c["fonte"][1:]) - 1
            fonte = web_facts[i] if 0 <= i < len(web_facts) else {}
            texto = str(fonte.get("trecho", ""))
            if atravessa_corte(c["trecho"], texto):
                c.update(verificado=False, sustentacao={"decisao": "irrelevante", "por": "codigo", "justificativa":
                         "o trecho junta frases dos dois lados de um corte da pesquisa: na fonte elas não são vizinhas"})
                log.warning("evidência %r: trecho atravessa um corte da pesquisa", e["afirmacao"][:70])
                continue
            c["contexto"] = contexto_da_citacao(c["trecho"], texto, bool(fonte.get("cortes_marcados")))


def _aviso_de_ordem(afirmacao: str, c: dict) -> str:
    """Aviso ao juiz quando há ordem/tempo em jogo: a referência que a fonte dá e se a vizinhança vale."""
    ctx = c.get("contexto") or {}
    if not (ctx.get("marcadores") or _SEQUENCIA.search(afirmacao)):
        return ""
    partes = ["há ORDEM/TEMPO em jogo"]
    ref = ctx.get("referencia_temporal")
    if ref and not ref.get("no_trecho"):
        partes.append(f"a data mais próxima ANTES do trecho na fonte é \"{ref['expressao']}\" (frase: \"{ref['frase'][:200]}\")")
    if ctx.get("contiguidade") == "nao_garantida":
        partes.append("este texto é um RECORTE antigo da pesquisa, sem marca de corte: frases que parecem vizinhas "
                      "podem ter tido outras entre elas — a frase anterior NÃO comprova a que se refere \"first\"/\"depois\"")
    elif ctx.get("corte_antes"):
        partes.append("há um corte da pesquisa logo antes do trecho: a referência da palavra de ordem não está no texto")
    return "; ".join(partes)


def _sobre_o_sujeito(nome: str, fonte: dict) -> bool:
    """A página é SOBRE o sujeito (título ou endereço o nomeiam), não só o menciona em algum lugar."""
    return _menciona(nome, str(fonte.get("titulo", ""))) or _menciona(nome, str(fonte.get("url", "")).replace("_", " "))


def sustentacao_por_codigo(afirmacao: str, trecho: str, fonte: dict, sujeitos: set[str]) -> str | None:
    """Conferência determinística antes da IA. Motivo da recusa ou None.
    - sujeito: em página que não é sobre o sujeito, o trecho (com o contexto) tem de nomeá-lo — fonte que
      fala de Yakutsk em outra parte não comprova qualquer frase sobre Yakutsk (E19, par.nsf.gov);
    - medida: temperatura com o sinal trocado, ou o mesmo valor em outra unidade."""
    ctx = contexto_do_trecho(trecho, str(fonte.get("trecho", "")))
    nomes = [n for n in sujeitos if _menciona(n, afirmacao)]
    # 20/11 (diversidade): o assunto é identificado por QUALQUER palavra do nome (num nome composto, basta uma
    # que a página também use); e só se recusa quando a página comprovadamente fala dele EM OUTRA PARTE — se o nome não aparece em
    # lugar nenhum (outro idioma, outro assunto), quem decide é o juiz, avisado
    if nomes and not any(_sobre_o_sujeito(n, fonte) or _menciona(n, ctx) for n in nomes):
        longe = [n for n in nomes if _menciona(n, str(fonte.get("trecho", "")))]
        if longe:
            return f"o trecho não fala de {longe[0]}: a página trata de outro assunto e cita {longe[0]} só em outra parte"
    t_af, t_tr = temperaturas(afirmacao), temperaturas(trecho)
    for v, u in t_af:
        if (v, u) not in t_tr and any(u2 == u and v2.lstrip("-") == v.lstrip("-") for v2, u2 in t_tr):
            return f"o trecho dá {v.lstrip('-')} {u} com o outro sinal"
    p_af, p_tr = _pares(afirmacao), _pares(trecho)
    for v, u in p_af:
        outras = {u2 for v2, u2 in p_tr if v2 == v}
        if outras and u not in outras:
            return f"o trecho dá {v} em {', '.join(sorted(outras))}, não em {u}"
    return None


_PROMPT_SUSTENTACAO = """Você confere se o TRECHO de uma fonte comprova a AFIRMAÇÃO. Tema: {tema}
Cada item traz a AFIRMAÇÃO, a FONTE (título), o TRECHO literal e o CONTEXTO (texto em volta do trecho).
Leia o trecho no seu contexto e decida:
- "sustenta": comprova a afirmação INTEIRA — mesmo sujeito, mesma medida, mesmo valor, unidade e sinal;
- "parcial": comprova só uma parte (a afirmação generaliza — "a maioria", "sempre" —, acrescenta causa,
  lugar ou número que o trecho não traz);
- "irrelevante": o trecho trata de outra coisa ou de outro sujeito — inclusive quando fala da CLASSE em
  geral (ondas gigantes, desertos, prédios altos) e a afirmação diz que aquilo vale para um lugar, ser ou
  objeto específico: o fenômeno pode ser o mesmo, o sujeito não é ("sujeito": "outro");
- "contradiz": o trecho diz o contrário ou outro valor.
ORDEM E TEMPO: palavra de ordem na afirmação ou no trecho ("primeiro", "depois", "em seguida", "first",
"next", "then") só é "sustenta" se o TRECHO ou o CONTEXTO CONTÍNUO disser em relação a quê, e a afirmação
disser a MESMA referência ("first" num parágrafo sobre uma etapa é o primeiro passo daquela etapa, não da
missão). "[…]" é texto que a pesquisa pulou: nada liga os dois lados. Se a referência não está comprovada,
"parcial", e a parte_sustentada fica sem a palavra de ordem. Nunca deduza a ordem por conhecimento próprio.
TERMOS TÉCNICOS: para cada termo técnico que a afirmação TRADUZ do trecho (lado, direção, componente, peça,
grandeza, faixa, fenômeno, nome técnico), confira se a tradução tem o MESMO significado técnico NESTE contexto e
liste em "termos": {{"original": copiado do TRECHO, "traducao": copiado da AFIRMAÇÃO, "mesmo_sentido": true|false,
"justificativa": "uma frase"}}. Tradução que muda o significado (o lado oposto, outro componente, outra faixa,
outra grandeza) é "contradiz", mesmo que a frase pareça plausível. Sem termo técnico traduzido: "termos": [].
"trecho_chave": palavras COPIADAS do TRECHO que decidem (não do contexto, não traduzidas).
"parte_sustentada": só em "parcial" — a afirmação reescrita em português SÓ com o que o trecho comprova
(sem acrescentar nada); vazio nos outros casos.
"partes": só em "parcial" e quando a afirmação junta MAIS DE UM fato — cada fato independente dela, em português,
um por item, com "comprovada": true|false por ESTE trecho (um fato comprovado não comprova o outro); [] nos outros casos.
Responda SÓ JSON:
{{"julgamentos": [{{"n": 1, "decisao": "sustenta|parcial|irrelevante|contradiz", "sujeito": "ok|ausente|outro",
  "medida": "ok|diverge|nao_se_aplica", "trecho_chave": "...", "justificativa": "uma frase",
  "parte_sustentada": "", "partes": [{{"texto": "...", "comprovada": true}}], "termos": [{{"original": "...", "traducao": "...", "mesmo_sentido": true, "justificativa": "..."}}]}}]}}

{itens}"""


def _julgamento_valido(j: dict, trecho: str) -> bool:
    """Decisão estruturada e identificável: decisão conhecida, justificativa de verdade e trecho-chave
    que existe no trecho citado."""
    if not isinstance(j, dict) or j.get("decisao") not in DECISOES:
        return False
    if len(str(j.get("justificativa", "")).split()) < 4:
        return False
    # 21/11 (E43): o juiz pode apontar dois pedaços do trecho ligados por "..." — vale se CADA pedaço (2+ palavras)
    # existe literalmente no trecho citado; pedaço que não existe invalida a decisão inteira
    alvo = " " + " ".join(_tokens(trecho)) + " "
    partes = [_tokens(p) for p in re.split(r"\.{3}|…", str(j.get("trecho_chave", "")))]
    partes = [p for p in partes if p]
    return bool(partes) and all(len(p) >= 2 and (" " + " ".join(p) + " ") in alvo for p in partes)


def _julgar_lote(itens: list[dict], topic: str) -> dict[int, dict]:
    """Uma chamada ao modelo forte para até LOTE_SUSTENTACAO itens. {n: julgamento válido}."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    blocos = []
    for n, it in enumerate(itens, 1):
        blocos.append(f"{n}) AFIRMAÇÃO: {it['afirmacao']}\n   FONTE: {it['titulo']}\n   TRECHO: \"{it['trecho']}\"\n"
                      f"   CONTEXTO: \"{it['contexto']}\"" + (f"\n   ATENÇÃO: {it['aviso']}" if it.get("aviso") else ""))
    try:
        raw = complete([{"role": "user", "content": _PROMPT_SUSTENTACAO.format(tema=topic, itens="\n\n".join(blocos))}],
                       max_tokens=RESPOSTA_SUSTENTACAO, temperature=0.1, model_filter=is_strong_model,
                       validate=lambda r: isinstance(_json_seguro(r).get("julgamentos"), list))
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("juiz de sustentação indisponível: %s", exc)
        return {}
    out = {}
    julgs = [j for j in _json_seguro(raw).get("julgamentos") or [] if isinstance(j, dict)]
    # 26/11: decisão com rótulo/variação lida (e aprendida) em src/decisoes.py antes da validação
    from .decisoes import ler_lote
    for j, d in zip(julgs, ler_lote([j.get("decisao") for j in julgs], DECISOES, ("sustenta",), "sustentacao")):
        if d:
            j["decisao"] = d
    for j in julgs:
        try:
            n = int(j.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(itens) and _julgamento_valido(j, itens[n - 1]["trecho"]):
            out[n] = {k: str(j.get(k, "") or "") for k in ("decisao", "sujeito", "medida", "trecho_chave", "justificativa",
                                                          "parte_sustentada")}
            out[n]["termos"] = _termos_validos(j.get("termos"), itens[n - 1]["afirmacao"], itens[n - 1]["trecho"])
            out[n]["partes"] = _partes_validas(j.get("partes")) if out[n]["decisao"] == "parcial" else []
    return out


def _partes_validas(partes) -> list[dict]:
    """22/11 (evidências atômicas): os fatos independentes de uma afirmação "parcial", cada um com "comprovada"
    booleano. Menos de dois fatos válidos: não é composta (vale a parte_sustentada, como antes)."""
    if not isinstance(partes, list):
        return []
    out = []
    for p in partes[:MAX_PARTES]:
        if isinstance(p, dict) and isinstance(p.get("comprovada"), bool):
            t = re.sub(r"\s+", " ", str(p.get("texto", ""))).strip()
            if len(t.split()) >= 3:
                out.append({"texto": t, "comprovada": p["comprovada"]})
    return out if len(out) >= 2 else []


def _termos_validos(termos, afirmacao: str, trecho: str) -> list[dict]:
    """22/11 (E74): termos que o juiz conferiu — original copiado do trecho e tradução copiada da afirmação (os dois
    conferidos literalmente) e "mesmo_sentido" booleano. Termo que não confere não vale (nem para aprovar nem para
    bloquear); o glossário de src/termos.py confere de novo em código."""
    if not isinstance(termos, list):
        return []
    alvo_t, alvo_a = " " + " ".join(_tokens(trecho)) + " ", " " + " ".join(_tokens(afirmacao)) + " "
    out = []
    for t in termos:
        if not isinstance(t, dict) or not isinstance(t.get("mesmo_sentido"), bool):
            continue
        o, tr = _tokens(str(t.get("original", ""))), _tokens(str(t.get("traducao", "")))
        if o and tr and (" " + " ".join(o) + " ") in alvo_t and (" " + " ".join(tr) + " ") in alvo_a:
            out.append({"original": str(t["original"]).strip(), "traducao": str(t["traducao"]).strip(),
                        "mesmo_sentido": t["mesmo_sentido"], "justificativa": str(t.get("justificativa", "")).strip(),
                        "contexto": next((f for f in re.split(r"(?<=[.!?])\s+", trecho)
                                          if (" " + " ".join(o) + " ") in " " + " ".join(_tokens(f)) + " "), trecho)})
    return out


def _json_seguro(raw: str | None) -> dict:
    try:
        return _json(raw or "")
    except ValueError:
        return {}


_PROMPT_MESMA_MEDIDA = """Tema: {tema}
Cada par traz duas afirmações de fontes diferentes, com valores diferentes na mesma unidade.
Para cada par, decida se elas dão valor para a MESMA medida do MESMO sujeito (então as fontes divergem)
ou para medidas diferentes (outra parte, outro período, outra definição, outro sujeito).
Responda SÓ JSON: {{"pares": [{{"n": 1, "mesma_medida": true, "justificativa": "uma frase"}}]}}

{pares}"""


def mesma_medida_ia(pares: list[tuple[dict, dict]], topic: str) -> set[int]:
    """Índices dos pares que a IA forte diz serem a mesma medida. Par sem resposta válida conta como
    divergente: na dúvida, nenhum dos dois valores vira fato confirmado."""
    from . import disk_cache
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    # 21/11: o mesmo par (mesmas afirmações, mesmos trechos verificados) não é perguntado de novo
    regras = _regras_mesma_medida()

    def chave(a: dict, b: dict) -> list:
        return [regras, topic] + [[x["afirmacao"], sorted((c.get("fonte", ""), c.get("trecho", ""))
                                                          for c in x.get("citacoes", []) if c.get("verificado"))]
                                  for x in (a, b)]
    decididos: dict[int, bool] = {}
    for k, (a, b) in enumerate(pares):
        guardado = disk_cache.get("juiz_mesma_medida", chave(a, b))
        if isinstance(guardado, dict) and isinstance(guardado.get("mesma_medida"), bool):
            decididos[k] = guardado["mesma_medida"]
    _contar_cache("mesma_medida", len(decididos), len(pares) - len(decididos))
    faltam = [k for k in range(len(pares)) if k not in decididos]
    if not faltam:
        return {k for k in range(len(pares)) if decididos[k]}
    todos, pares = pares, [pares[k] for k in faltam]
    texto = "\n".join(f"{n}) A: {a['afirmacao']}\n   B: {b['afirmacao']}" for n, (a, b) in enumerate(pares, 1))
    try:
        raw = complete([{"role": "user", "content": _PROMPT_MESMA_MEDIDA.format(tema=topic, pares=texto)}],
                       max_tokens=1200, temperature=0.1, model_filter=is_strong_model,
                       validate=lambda r: isinstance(_json_seguro(r).get("pares"), list))
        respostas = _json_seguro(raw).get("pares") or []
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("juiz de mesma medida indisponível (%s): os pares contam como divergentes", exc)
        respostas = []
    for r in respostas:
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(pares) and isinstance(r.get("mesma_medida"), bool) and len(str(r.get("justificativa", "")).split()) >= 4:
            k = faltam[n - 1]
            decididos[k] = r["mesma_medida"]
            disk_cache.put("juiz_mesma_medida", chave(*todos[k]), {"mesma_medida": r["mesma_medida"],
                                                                   "justificativa": str(r["justificativa"])[:300]})
            log.info("mesma medida? %s — %r × %r (%s)", r["mesma_medida"], pares[n - 1][0]["afirmacao"][:50],
                     pares[n - 1][1]["afirmacao"][:50], str(r["justificativa"])[:100])
    return {k for k in range(len(todos)) if decididos.get(k, True)}


# ---------------------------------------------------------------- cache das decisões dos juízes (21/11)
# A mesma citação era julgada de novo a cada rodada (banco, aprofundamento, busca da auditoria) e o mesmo par de
# "mesma medida" foi perguntado 3 vezes num job. A decisão só é reaproveitada para o MESMO material: a chave tem
# o texto inteiro de tudo o que o juiz lê (tema, afirmação, título, trecho, contexto, avisos), a impressão digital
# do texto completo da fonte (qualquer mudança na página baixada invalida) e a das regras (prompt + código que
# valida a resposta + versão manual): mudou qualquer um, a chave muda e o juiz é chamado de novo. Só decisão
# VÁLIDA é guardada (falha e "sem julgamento" nunca). Acertos e falhas vão para as métricas do job.
VERSAO_JUIZES = 3  # 22/11: termos técnicos (2) e fatos independentes de uma afirmação parcial (3)


def _impressao(*partes) -> str:
    return hashlib.sha1(json.dumps(partes, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _regras_sustentacao() -> str:
    import inspect
    return _impressao(VERSAO_JUIZES, _PROMPT_SUSTENTACAO, DECISOES, inspect.getsource(_julgamento_valido),
                      inspect.getsource(_julgar_lote), inspect.getsource(_termos_validos), inspect.getsource(_partes_validas))


def _regras_mesma_medida() -> str:
    import inspect
    return _impressao(VERSAO_JUIZES, _PROMPT_MESMA_MEDIDA, inspect.getsource(mesma_medida_ia))


def _contar_cache(juiz: str, acertos: int, falhas: int) -> None:
    from . import metricas
    if acertos:
        metricas.somar(f"cache_{juiz}_acertos", acertos)
    if falhas:
        metricas.somar(f"cache_{juiz}_falhas", falhas)


def chave_sustentacao(item: dict, topic: str) -> list:
    return [_regras_sustentacao(), topic, item["afirmacao"], item.get("titulo", ""), item["trecho"],
            item.get("contexto", ""), item.get("aviso", ""), item.get("fonte_hash", "")]


def _julgar_com_cache(itens: list[dict], topic: str, segunda_chance: bool = True) -> dict[int, dict]:
    """{n (1-based): julgamento válido} para os itens — do cache quando o material é idêntico, do juiz (em
    lotes de LOTE_SUSTENTACAO, com uma segunda chance para quem ficou sem decisão válida) quando não é.
    Itens idênticos no mesmo pedido são julgados uma vez só."""
    from . import disk_cache
    out: dict[int, dict] = {}
    chaves = [chave_sustentacao(it, topic) for it in itens]
    faltam: dict[str, list[int]] = {}
    for n, ch in enumerate(chaves, 1):
        guardado = disk_cache.get("juiz_sustentacao", ch)
        if isinstance(guardado, dict) and guardado.get("decisao") in DECISOES:
            out[n] = {**guardado, "cache": True}
        else:
            faltam.setdefault(_impressao(ch), []).append(n)
    _contar_cache("sustentacao", len(out), len(faltam))
    unicos = [ns[0] for ns in faltam.values()]
    for k in range(0, len(unicos), LOTE_SUSTENTACAO):
        lote = unicos[k:k + LOTE_SUSTENTACAO]
        julgados = _julgar_lote([itens[n - 1] for n in lote], topic)
        sem = [m for m in range(1, len(lote) + 1) if m not in julgados]
        if sem and segunda_chance:  # uma segunda chance só para os que ficaram sem decisão válida
            again = _julgar_lote([itens[lote[m - 1] - 1] for m in sem], topic)
            for novo, j in again.items():
                julgados[sem[novo - 1]] = j
        for m, j in julgados.items():
            n = lote[m - 1]
            disk_cache.put("juiz_sustentacao", chaves[n - 1], j)
            for igual in faltam[_impressao(chaves[n - 1])]:
                out[igual] = dict(j)
    return out


def julgar_sustentacao(evidencias: list[dict], web_facts: list[dict], topic: str) -> None:
    """20/11 (E19): cada citação verificada (trecho literal existe) passa a ter de COMPROVAR a afirmação.
    1º código (sujeito no contexto, sinal, unidade); 2º IA forte com decisão estruturada e justificativa
    (julgamento sem trecho-chave identificável é pedido de novo uma vez). Só "sustenta" com sujeito ok e
    medida sem divergência mantém a citação verificada. Recusa ou falta de julgamento: verificado=False,
    com o registro em c["sustentacao"] — a evidência fica no banco, fora do uso como fato."""
    sujeitos = sujeitos_do_tema(topic)
    pendentes: list[tuple[dict, dict]] = []
    for e in evidencias:
        for c in e["citacoes"]:
            if not c.get("verificado"):
                continue
            i = int(c["fonte"][1:]) - 1
            fonte = web_facts[i] if 0 <= i < len(web_facts) else {}
            motivo = sustentacao_por_codigo(e["afirmacao"], c["trecho"], fonte, sujeitos)
            if motivo:
                c.update(verificado=False, sustentacao={"decisao": "irrelevante", "por": "codigo", "justificativa": motivo})
                log.warning("evidência %r: %s", e["afirmacao"][:70], motivo)
                continue
            aviso = ""
            if c.get("sujeito_nao_achado") or (sujeitos and not any(fonte_fala_de(n, fonte) for n in sujeitos
                                                                    if _menciona(n, e["afirmacao"]))):
                nomes_av = c.get("sujeito_nao_achado") or sorted(n for n in sujeitos if _menciona(n, e["afirmacao"]))
                aviso = (f"o nome {', '.join(nomes_av)} não aparece nesta página: confira se ela fala do MESMO sujeito "
                         "(talvez com outro nome ou em outro idioma) ou de outro")
            ordem = _aviso_de_ordem(e["afirmacao"], c)
            aviso = "; ".join(x for x in (aviso, ordem) if x)
            texto_fonte = str(fonte.get("trecho", ""))
            pendentes.append((e, {"afirmacao": e["afirmacao"], "aviso": aviso, "titulo": fonte.get("titulo", c.get("titulo", "")),
                                  "trecho": c["trecho"], "contexto": contexto_do_trecho(c["trecho"], texto_fonte),
                                  "fonte_hash": hashlib.sha1(texto_fonte.encode()).hexdigest(), "_c": c}))
    julgados = _julgar_com_cache([it for _, it in pendentes], topic)
    grupos: dict[int, tuple[dict, list]] = {}
    for n, (e, it) in enumerate(pendentes, 1):
        c = it["_c"]
        j = julgados.get(n)
        if j is not None:
            grupos.setdefault(id(e), (e, []))[1].append((it, j))
        if j is None:
            c.update(verificado=False, sustentacao={"decisao": "sem_julgamento", "por": "ia",
                                                    "justificativa": "o juiz não devolveu decisão válida"})
            log.warning("evidência %r sem julgamento de sustentação — fora do uso como fato", e["afirmacao"][:70])
            continue
        c["sustentacao"] = {**j, "por": "ia"}
        if j["decisao"] != "sustenta" or j["sujeito"] != "ok" or j["medida"] == "diverge":
            c["verificado"] = False
            log.warning("evidência %r: trecho %s (%s)", e["afirmacao"][:70], j["decisao"], j["justificativa"][:120])
    _desmembrar(list(grupos.values()), evidencias, topic)
    # 22/11 (E74): tradução de termo técnico conferida em código depois do juiz e do cache (glossário + termos do juiz)
    tt = travar_termos(evidencias, log)
    if any(tt.values()):
        from . import metricas
        for k, v in tt.items():
            if v:
                metricas.somar(f"termos_{k}", v)
    # 21/11 (E27): a ordem é conferida em código DEPOIS do juiz e das decisões do cache — o juiz real aprovou
    # "Primeiro, ..." num recorte sem marca de corte mesmo avisado; nenhuma decisão de IA vira cronologia comprovada
    cont = travar_ordem(evidencias, log)
    if any(cont.values()):
        from . import metricas
        for k, v in cont.items():
            if v:
                metricas.somar(f"ordem_{k}", v)


def _aprovado(j: dict | None) -> bool:
    return bool(j) and j["decisao"] == "sustenta" and j["sujeito"] == "ok" and j["medida"] != "diverge"


def _desmembrar(grupos: list[tuple[dict, list[tuple[dict, dict]]]], evidencias: list[dict], topic: str) -> None:
    """Não descartar o que É verdade, nem deixar um fato aprovar outro (E52; 22/11, evidências atômicas).
    Evidência sem nenhuma citação que a comprove inteira e com julgamento "parcial": cada fato que o juiz diz
    comprovado (as "partes", ou a parte_sustentada) é julgado de novo como afirmação própria contra CADA trecho
    conferido da evidência — o fato da citação 1 pode estar na citação 2. Fato aprovado vira evidência atômica só
    com as citações que o aprovaram (a 1ª fica na própria evidência; a original vai para "afirmacao_original").
    Fato que nenhum trecho aprovou vira evidência bloqueada (verificado=False). Fato que já está no banco não entra
    de novo. Sem nenhum fato aprovado, a evidência fica como estava, fora do uso."""
    planos, pares = [], []
    for e, julg in grupos:
        if any(c.get("verificado") for c in e["citacoes"]):
            continue
        candidatas: list[str] = []
        negadas: list[str] = []
        for _, j in julg:
            if j["decisao"] != "parcial":
                continue
            ps = j.get("partes") or []
            cand = [p["texto"] for p in ps if p["comprovada"]] if ps else (
                [j["parte_sustentada"].strip()] if j.get("parte_sustentada", "").strip() else [])
            candidatas += [t for t in cand if t not in candidatas and not any(_quase_iguais(t, x) for x in candidatas)]
            negadas += [p["texto"] for p in ps if not p["comprovada"]]
        candidatas = candidatas[:MAX_PARTES]
        if not candidatas:
            continue
        planos.append((e, julg, candidatas, negadas))
        for t in candidatas:
            for it, _ in julg:
                if numeros_conferidos(t, numeros(t), it["trecho"])[0]:
                    pares.append((e, t, it))
    if not pares:
        return
    julgados = _julgar_com_cache([{**it, "afirmacao": t} for _, t, it in pares], topic, segunda_chance=False)
    aprovadas: dict[tuple[int, str], list[tuple[dict, dict]]] = {}
    for n, (e, t, it) in enumerate(pares, 1):
        if _aprovado(julgados.get(n)):
            aprovadas.setdefault((id(e), t), []).append((it, julgados[n]))
    for e, julg, candidatas, negadas in planos:
        ok = [t for t in candidatas if (id(e), t) in aprovadas]
        if not ok:
            continue
        original = e["afirmacao"]
        julgadas = {id(it["_c"]) for it, _ in julg}
        bloqueadas = [t for t in candidatas if t not in ok]
        bloqueadas += [t for t in negadas if not any(_quase_iguais(t, x) or _sobreposicao_mutua(t, x) for x in ok + bloqueadas)]
        base = {k: copy.deepcopy(v) for k, v in e.items() if k not in ("afirmacao", "citacoes", "numeros", "tipo")}
        linhas = []
        for k, t in enumerate(ok):
            cits = []
            for it, j in aprovadas[(id(e), t)]:
                c = it["_c"] if k == 0 else copy.deepcopy(it["_c"])
                c.update(verificado=True, sustentacao={**j, "por": "ia", "reduzida_de": original})
                cits.append(c)
            linhas.append((t, cits, True))
        for t in bloqueadas:
            cits = [{**copy.deepcopy(it["_c"]), "verificado": False,
                     "sustentacao": {"decisao": "parcial", "por": "ia", "desmembrada_de": original,
                                     "justificativa": "fato da afirmação composta que nenhum trecho citado comprova"}}
                    for it, _ in julg]
            linhas.append((t, cits, False))
        # fato que já está no banco não entra de novo (nem como a própria evidência); fato aprovado só se compara
        # com o que está verificado — um bloqueado igual não pode apagar o fato comprovado
        unicas: list[tuple[str, list[dict], bool]] = []
        for t, cits, aprovada in linhas:
            pool = [x for x in evidencias if x is not e] + [{"afirmacao": u[0], "citacoes": u[1]} for u in unicas]
            if aprovada:
                pool = [x for x in pool if any(c.get("verificado") for c in x["citacoes"])]
            igual = _ja_no_banco(t, pool, cits)
            if igual:
                e.setdefault("partes_repetidas", []).append({"afirmacao": t, "igual_a": igual})
                continue
            unicas.append((t, cits, aprovada))
        if not unicas:
            log.info("afirmação composta %r: todos os fatos já estão no banco", original[:70])
            evidencias[:] = [x for x in evidencias if x is not e]
            continue
        linhas = unicas
        t0, cits0, _ = linhas[0]
        e["afirmacao_original"] = original
        e["afirmacao"] = t0
        e["numeros"] = sorted(numeros(t0))
        e["tipo"] = "quantitativo" if e["numeros"] else "qualitativo"
        e["citacoes"] = cits0 + [c for c in e["citacoes"] if id(c) not in julgadas and all(c is not x for x in cits0)]
        log.info("evidência reduzida ao que o trecho comprova: %r -> %r", original[:60], t0[:60])
        for t, cits, aprovada in linhas[1:]:
            nums = sorted(numeros(t))
            evidencias.append({**copy.deepcopy(base), "afirmacao": t, "citacoes": cits, "numeros": nums,
                               "tipo": "quantitativo" if nums else "qualitativo", "desmembrada_de": original})
            log.info("fato da afirmação composta %s: %r", "separado" if aprovada else "bloqueado", t[:70])
        if len(linhas) > 1:
            from . import metricas
            metricas.somar("atomicas_desmembradas", 1)


def _status(e: dict) -> dict:
    verificadas = [c for c in e["citacoes"] if c["verificado"]]
    if not verificadas:
        ausente = sorted({n for c in e["citacoes"] for n in c.get("sujeito_ausente") or []})
        julgado = next((c["sustentacao"] for c in e["citacoes"] if c.get("sustentacao")), None)
        motivo = ("sem citação" if not e["citacoes"] else
                  f"a fonte citada não fala de {', '.join(ausente)} (sujeito atribuído pelo extrator)" if ausente else
                  f"o trecho não comprova a afirmação ({julgado['decisao']}): {julgado['justificativa']}" if julgado else
                  "trecho citado não existe no texto baixado da fonte" if not e["numeros"] else
                  "trecho citado não existe na fonte ou não contém os números da afirmação")
        return {**e, "status": "nao_verificado", "estimado": False, "motivo": motivo}
    classes = {c["classe"] for c in verificadas}
    publicadores = {dominio_raiz(c["url"]) for c in verificadas}
    status = ("oficial" if classes & {"oficial", "academica"} else
              "corroborado" if len(publicadores) >= 2 else "fonte_unica")
    estimado = bool(e["numeros"]) and any(_ESTIMATIVA.search(c["trecho"]) for c in verificadas)
    # hipótese só se TODA citação que a sustenta a dá como hipótese (uma fonte que afirma, afirma)
    hipotese = all(_HIPOTESE.search(c["trecho"]) for c in verificadas)
    return {**e, "status": status, "estimado": estimado, "hipotese": hipotese}


def _prova_local(e: dict, sujeitos: set[str]) -> bool:
    """O trecho verificado nomeia o sujeito que a afirmação nomeia (sem sujeito no tema: vale)."""
    citados = {n for n in sujeitos if _menciona(n, e["afirmacao"])}
    if not citados:
        return True
    return any(c.get("verificado") and any(_menciona(n, c.get("trecho", "")) for n in citados) for c in e["citacoes"])


_INDICADORES = (("amplitude", r"amplitude|variar|varia[çc]|diferen[çc]a|oscila"),
                ("minima", r"m[íi]nim|mais baix|recorde de frio|menor temperatura|lowest|record low"),
                ("maxima", r"m[áa]xim|mais alt|maior temperatura|highest|record high"),
                ("media", r"m[ée]di[ao]|average|mean"))


def _indicador(afirmacao: str) -> str:
    """Que medida de temperatura a afirmação dá: amplitude, mínima, máxima, média ou "" (outra)."""
    for nome, padrao in _INDICADORES:
        if re.search(padrao, afirmacao, re.I):
            return nome
    return ""


_PEQUENOS = {"dois": "2", "duas": "2", "três": "3", "tres": "3", "quatro": "4", "cinco": "5", "seis": "6",
             "sete": "7", "oito": "8", "nove": "9"}
_NAO_UNIDADE = {"de", "da", "do", "das", "dos", "e", "a", "o", "em", "por", "para", "mil", "milhão", "milhões", "bilhão",
                "bilhões", "vezes", "anos", "ano", "km", "m", "mm", "cm", "kg", "graus"}


def _pares_conflito(texto: str) -> set[tuple[str, str]]:
    """Medidas para comparar duas evidências: as de fatos._pares e, além delas, número de 2 a 9 por extenso
    e a palavra que vem depois do número como unidade quando ela não está no vocabulário ("cinco
    unidades" e "6 unidades"; "163 andares" e "154 andares")."""
    t = re.sub(r"\b(" + "|".join(_PEQUENOS) + r")\b", lambda m: _PEQUENOS[m.group(1).lower()], texto or "", flags=re.I)
    out = set(_pares(t))
    for m in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s+([a-zà-ú]{4,})", t, re.I):
        palavra = m.group(2).lower()
        if re.fullmatch(r"1\d{3}|20\d{2}", m.group(1)):
            continue  # ano, não quantidade
        # só substantivo de contagem no plural ("andares", "operários"); "1 vale 11 metros": "vale" é verbo
        if palavra.endswith("s") and palavra not in _NAO_UNIDADE and not any(v == canon_num(m.group(1)) for v, _ in out):
            out.add((canon_num(m.group(1)), palavra[:6]))
    return out


def canon_num(n: str) -> str:
    from .fatos import canon
    return canon(n)


def _identificadores(texto: str) -> set[str]:
    """Números da afirmação que não são medida (identificam a coisa: "voo 19", "fato número 10")."""
    medidas = {v.lstrip("-") for v, _ in _pares_conflito(texto)} | {v.lstrip("-") for v, _ in temperaturas(texto)}
    escalados = set()
    for v in medidas:  # "27 mil anos-luz" também é 27000
        try:
            escalados |= {format(round(float(v) * e), "d") for e in (1e3, 1e6, 1e9)}
        except ValueError:
            pass
    pequenos = {d for w, d in _PEQUENOS.items() if re.search(r"\b" + w + r"\b", texto, re.I)}
    # ano é contexto (o levantamento de 2012 × o de 2009 medem a mesma coisa), não identidade
    return {n for n in numeros(texto) - medidas - escalados - pequenos if not re.fullmatch(r"1\d{3}|20\d{2}", n)}


def _sem_sujeito(texto: str, sujeitos: set[str] | None) -> str:
    """A afirmação sem as palavras do nome do assunto (para medir se o resto é o mesmo)."""
    return re.sub("|".join(re.escape(n) for n in sujeitos), " ", dobrar(texto)) if sujeitos else texto


def _sobreposicao_mutua(a: str, b: str, minimo: float = 0.7) -> bool:
    """As duas afirmações dizem a mesma coisa (fora números): ao menos `minimo` das palavras de conteúdo de
    CADA uma está na outra. "A torre tem 828 m de altura" está contida em "o concreto da torre foi bombeado a
    606 m de altura", mas não o contrário — medidas diferentes. Números que identificam a coisa, iguais."""
    if _identificadores(a) != _identificadores(b):
        return False
    pa, pb = _palavras(re.sub(r"\d", " ", a)), _palavras(re.sub(r"\d", " ", b))
    return bool(pa and pb) and len(pa & pb) / len(pa) >= minimo and len(pa & pb) / len(pb) >= minimo


def _quase_iguais(a: str, b: str) -> bool:
    """Afirmações curtas quase idênticas (fora os números): o filtro de vocabulário do tema não as apaga.
    Número que não é medida identifica a coisa ("voo 19", "fato número 10"): tem de ser o mesmo nos dois."""
    if _identificadores(a) != _identificadores(b):
        return False
    wa, wb = (set(_tokens(re.sub(r"\d", " ", x))) for x in (a, b))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.75


def _comparaveis_duro(a: str, b: str) -> bool:
    """O que nenhuma paráfrase muda: a coisa identificada por número ("voo 19" × "voo 20") e o indicador de
    temperatura (média × mínima × amplitude)."""
    if _identificadores(a) != _identificadores(b):
        return False
    return not (temperaturas(a) and temperaturas(b) and _indicador(a) != _indicador(b))


def _comparaveis(a: str, b: str, sujeitos: set[str] | None) -> bool:
    """20/11: a média de −8 °C de Yakutsk não diverge da amplitude de 100 °C nem de uma frase genérica
    sobre permafrost que não fala de Yakutsk: mesma medida exige mesmo sujeito e mesmo indicador."""
    if _identificadores(a) != _identificadores(b):
        return False  # "voo 19" × "voo 20", "fato número 1" × "número 2": coisas diferentes, não valores divergentes
    if sujeitos and {n for n in sujeitos if _menciona(n, a)} != {n for n in sujeitos if _menciona(n, b)}:
        # só uma das duas nomeia o sujeito: comparável apenas se o resto da afirmação é quase o mesmo (o mesmo
        # evento com dois valores); frase genérica de outro assunto (E31 × E34 de Yakutsk) não
        if not _sobreposicao_mutua(_sem_sujeito(a, sujeitos), _sem_sujeito(b, sujeitos)):
            return False
    if temperaturas(a) and temperaturas(b) and _indicador(a) != _indicador(b):
        return False
    return True


def marcar_conflitos(evidencias: list[dict], sujeitos: set[str] | None = None,
                     confirmar: Callable[[list[tuple[dict, dict]]], set[int]] | None = None) -> list[dict]:
    """Duas evidências que dão valores DIFERENTES para a mesma medida (mesma unidade e mesmo
    assunto): a mais forte (oficial > corroborada > fonte única) fica com o seu status e a
    marca do conflito; a mais fraca vira "contraditorio". Força igual: as duas viram
    "contraditorio" — o sistema não escolhe um valor arbitrariamente. Nenhuma é apagada.
    20/11 (diversidade): o par que o código não decide sozinho — mesma unidade, valores diferentes, mas
    escrito com outras palavras ("se estende por mais de 1.600 km ao longo da costa do Pacífico" × "por cerca
    de 1.000 km ao longo da costa do Chile") — vai a `confirmar(pares)` (IA forte: mesma medida?), que
    devolve os índices dos que são a mesma medida."""
    usaveis = [e for e in evidencias if e["status"] in STATUS_USAVEIS and e["numeros"]]
    contagem: dict[str, int] = {}
    for e in usaveis:
        for w in _palavras(e["afirmacao"]):
            contagem[w] = contagem.get(w, 0) + 1
    tema = {w for w, n in contagem.items() if len(usaveis) >= 4 and n * 2 >= len(usaveis)}
    certos: list[tuple[dict, dict]] = []
    candidatos: list[tuple[dict, dict]] = []
    for a_i, a in enumerate(usaveis):
        for b in usaveis[a_i + 1:]:
            pa, pb = _pares_conflito(a["afirmacao"]), _pares_conflito(b["afirmacao"])
            unidades = {u for _, u in pa} & {u for _, u in pb}
            if not unidades or not _comparaveis_duro(a["afirmacao"], b["afirmacao"]):
                continue
            # 20/11: por unidade — "2012" e "2009" (anos) não podem tornar "iguais" 21.196 km e 8.850 km
            if not any(not _algum_igual({n for n, x in pa if x == u}, {n for n, x in pb if x == u}) for u in unidades):
                continue
            mesma = _mesma_coisa(a["afirmacao"], b["afirmacao"], tema) or _sobreposicao_mutua(
                _sem_sujeito(a["afirmacao"], sujeitos), _sem_sujeito(b["afirmacao"], sujeitos))
            if mesma and _comparaveis(a["afirmacao"], b["afirmacao"], sujeitos):
                certos.append((a, b))
            elif len(_palavras(_sem_sujeito(a["afirmacao"], sujeitos)) & _palavras(_sem_sujeito(b["afirmacao"], sujeitos))) >= 2:
                candidatos.append((a, b))
    if candidatos and confirmar is not None:
        sim = confirmar(candidatos)
        certos += [par for k, par in enumerate(candidatos) if k in sim]
    rebaixar: set[str] = set()
    for a, b in certos:
        a.setdefault("conflitos", []).append(b["id"])
        b.setdefault("conflitos", []).append(a["id"])
        fa, fb = FORCA[a["status"]], FORCA[b["status"]]
        # 20/11 (E18 × E34): a mais forte só prevalece se o próprio trecho dela fala do sujeito; senão
        # o sistema não sabe qual vale e as duas ficam fora como fato (contraditorio)
        if sujeitos and fa != fb and not _prova_local(a if fa > fb else b, sujeitos):
            fa = fb
        if fa > fb:
            rebaixar.add(b["id"])
        elif fb > fa:
            rebaixar.add(a["id"])
        else:
            rebaixar |= {a["id"], b["id"]}
        log.warning("evidências divergentes: %s (%s) × %s (%s): %r × %r", a["id"], a["status"], b["id"], b["status"],
                    a["afirmacao"][:60], b["afirmacao"][:60])
    for e in evidencias:
        if e["id"] in rebaixar:
            e["status_original"] = e["status"]
            e["status"] = "contraditorio"
    return evidencias


def _numerar(evidencias: list[dict], inicio: int = 1) -> list[dict]:
    return [{"id": f"E{inicio + k}", **{x: y for x, y in e.items() if x != "id"}} for k, e in enumerate(evidencias)]


def _extrair(topic: str, web_facts: list[dict], fontes: list[dict],
             historia: dict | None = None) -> tuple[list[dict], list[str], list[dict]] | None:
    lotes, nao_lidos = _lotes(web_facts, topic)
    if not lotes:
        return None
    brutos: list = []
    lacunas: list[str] = []
    respondeu = False
    for escolhidos in lotes:
        maximo = limite_de_evidencias(sum(len(t) for t in escolhidos.values()), sum(1 for t in escolhidos.values() if t.strip()))
        data = _chamar(_PROMPT.format(tema=topic, historia=_bloco_historia(historia), fontes=_bloco(web_facts, escolhidos),
                                      maximo=maximo))
        if data is None:
            continue
        respondeu = True
        brutos += data.get("evidencias") or []
        lacunas += [str(x).strip() for x in data.get("lacunas") or [] if str(x).strip()]
    if not respondeu:
        return None
    evidencias = _validar(brutos, web_facts, fontes)
    pendentes = [e for e in evidencias if e["citacoes"] and not any(c["verificado"] for c in e["citacoes"])]
    if pendentes:
        evidencias = _aplicar_trocas(evidencias, _recuperar_por_llm(pendentes, web_facts, fontes))
    anotar_contexto(evidencias, web_facts)
    _conferir_sujeito(evidencias, web_facts, topic)  # depois da recuperação: vale para toda citação
    return evidencias, list(dict.fromkeys(lacunas))[:4], nao_lidos


def _mesclar_iguais(evidencias: list[dict]) -> list[dict]:
    """A mesma afirmação vinda de lotes diferentes vira uma evidência só (com as citações somadas)."""
    por_texto: dict[str, dict] = {}
    for e in evidencias:
        chave = " ".join(_tokens(e["afirmacao"]))
        if chave in por_texto:
            alvo = por_texto[chave]
            vistos = {(c["fonte"], c["trecho"]) for c in alvo["citacoes"]}
            alvo["citacoes"] += [c for c in e["citacoes"] if (c["fonte"], c["trecho"]) not in vistos]
        else:
            por_texto[chave] = e
    return list(por_texto.values())


# ---------------------------------------------------------------- história do vídeo (22/11)
# Antes da extração: que história o vídeo conta, que perguntas ele PRECISA responder, o que fica fora. A extração
# prioriza isso; a segunda passagem mede o banco contra essas perguntas — não contra "tudo o que a fonte diz".
_PROMPT_HISTORIA = """Tema (título/pergunta) do vídeo: {tema}
{angulo}
Antes de ler as fontes, defina a HISTÓRIA que este vídeo vertical de 2,5 a 5 minutos vai contar. Não use fatos
de memória: só a estrutura do que o espectador precisa saber (sem números, nomes ou afirmações).
- "historia": uma ou duas frases — que história o vídeo conta, com que ângulo;
- "perguntas": de 4 a 7 perguntas que o vídeo PRECISA responder para contar essa história, na ordem da narrativa,
  cada uma com "id" ("P1", "P2"...) e "pergunta";
- "fora_do_escopo": assuntos vizinhos que NÃO entram nesta história (para o banco não virar enciclopédia).
Responda SÓ JSON: {{"historia": "...", "perguntas": [{{"id": "P1", "pergunta": "..."}}], "fora_do_escopo": ["..."]}}"""


def _historia_valida(data: dict) -> dict | None:
    if not isinstance(data, dict) or not str(data.get("historia", "")).strip():
        return None
    perguntas = [{"id": f"P{k}", "pergunta": str(p.get("pergunta", "")).strip()}
                 for k, p in enumerate((p for p in data.get("perguntas") or [] if isinstance(p, dict)
                                        and str(p.get("pergunta", "")).strip()), 1)]
    if not 3 <= len(perguntas) <= 8:
        return None
    return {"historia": str(data["historia"]).strip(), "perguntas": perguntas,
            "fora_do_escopo": [str(x).strip() for x in data.get("fora_do_escopo") or [] if str(x).strip()][:8]}


def _chamar_guardado(nome: str, prompt: str, validar: Callable[[str], bool], max_tokens: int) -> dict | None:
    """_chamar com o cache em disco: o MESMO pedido (mesmo material, mesmas regras) não é refeito. Só resposta
    válida é guardada."""
    from . import disk_cache
    chave = [VERSAO, nome, hashlib.sha1(prompt.encode()).hexdigest()]
    guardado = disk_cache.get(nome, chave)
    if isinstance(guardado, dict):
        _contar_cache(nome, 1, 0)
        return guardado
    _contar_cache(nome, 0, 1)
    data = _chamar(prompt, max_tokens=max_tokens, validar=validar)
    if data:
        disk_cache.put(nome, chave, data)
    return data


def planejar_historia(topic: str, angulo: str | None = None) -> dict | None:
    """A história do vídeo (uma chamada, guardada em cache). None sem resposta válida — a extração segue pelo tema
    e a segunda passagem não roda (sem história não há como dizer o que é lacuna)."""
    prompt = _PROMPT_HISTORIA.format(tema=topic, angulo=f"Ângulo pedido: {angulo}" if angulo else "")
    data = _chamar_guardado("historia_do_tema", prompt, lambda r: _historia_valida(_json_seguro(r)) is not None, 900)
    historia = _historia_valida(data or {})
    if historia:
        historia["angulo"] = angulo or ""
    else:
        log.warning("história do vídeo indisponível: extração pelo tema, sem segunda passagem")
    return historia


def _bloco_historia(historia: dict | None) -> str:
    if not historia:
        return ""
    linhas = [f"HISTÓRIA que o vídeo vai contar: {historia['historia']}", "Perguntas que o vídeo precisa responder:"]
    linhas += [f"  {p['id']}) {p['pergunta']}" for p in historia["perguntas"]]
    if historia.get("fora_do_escopo"):
        linhas.append("Fora do escopo desta história: " + "; ".join(historia["fora_do_escopo"]))
    return "\n".join(linhas)


# ---------------------------------------------------------------- cobertura: segunda passagem (22/11)
# James Webb (rodada 8): informações que estavam nas fontes perderam as vagas da extração. A segunda passagem
# (rodada 10: 4 lotes, 9 chamadas, 14 min) virou duas etapas curtas:
#   1. DIAGNÓSTICO (1 chamada, sem texto de fonte): o banco responde cada pergunta da história? O que falta e com
#      que palavras procurar. "Suficiente" encerra aqui — nenhuma outra chamada.
#   2. BUSCA DIRIGIDA: só as frases que nenhuma evidência usou E que têm as palavras das perguntas abertas; lote
#      sem frase relevante não é enviado. Uma chamada para as lacunas juntas (duas, se o texto relevante passar
#      de um lote).
# O banco vai uma vez (no diagnóstico); a busca dirigida recebe só as perguntas abertas. Pedidos idênticos vêm do
# cache. Cada evidência nova passa por tudo o que a primeira passa (trecho literal, números/unidades, sujeito,
# contexto, juiz, termos, ordem, conflitos, status).
MIN_NAO_COBERTO = 300          # menos que isso de texto sem uso em todas as fontes: nada a revisar
MAX_LOTES_DIRIGIDOS = 2
RESUMO_AFIRMACAO = 150

_PROMPT_DIAGNOSTICO = """Diagnóstico da cobertura do banco de evidências.
{historia}

BANCO DE EVIDÊNCIAS (id: afirmação):
{banco}

Para CADA pergunta da história, diga se o banco já a responde o suficiente para o vídeo:
- "respondida": com os ids das evidências que respondem;
- "falta": o que falta, em uma frase, e "chaves": de 3 a 8 palavras-chave para procurar a resposta nas fontes
  (substantivos e termos técnicos, em inglês, português e espanhol).
Só é "falta" o que é NECESSÁRIO para contar esta história. Detalhe a mais, curiosidade, pessoa ou assunto que
a história não pede, e o que está fora do escopo, não são lacunas. "suficiente": true se nada necessário falta.
Responda SÓ JSON: {{"suficiente": false, "perguntas": [{{"id": "P1", "situacao": "respondida", "evidencias": ["E3"],
 "falta": "", "chaves": []}}]}}"""

_PROMPT_DIRIGIDO = """Busca dirigida nas fontes de um banco de evidências.
{historia}

O banco ainda NÃO responde estas perguntas da história:
{abertas}

TRECHOS DAS FONTES (numerados como no banco; só frases que nenhuma evidência usou — é tudo o que existe, não use
memória):
{fontes}

Procure nesses trechos as respostas às perguntas acima, e só a elas. Se os trechos não respondem uma pergunta,
não responda essa pergunta (é uma resposta válida). Nada de fato vizinho, curiosidade ou o que não ajuda a
responder a pergunta.
""" + _REGRAS_EVIDENCIA + """

Quantidade: no máximo {maximo} evidências no total (teto, não meta). Responda SÓ JSON:
{{"respostas": [{{"id": "P2", "evidencias": [{{"afirmacao": "...", "tipo": "qualitativo",
   "citacoes": [{{"fonte": "S1", "trecho": "..."}}]}}]}}]}}"""


def trechos_nao_cobertos(evidencias: list[dict], web_facts: list[dict]) -> list[list[tuple[int, str]]]:
    """Para cada fonte (mesmos índices [S#]), as frases (posição, frase) que nenhuma citação do banco cobre."""
    cobertos: dict[int, list[tuple[int, int]]] = {}
    for e in evidencias:
        for c in e.get("citacoes") or []:
            m = re.search(r"\d+", str(c.get("fonte", "")))
            if not m or not 1 <= int(m.group()) <= len(web_facts):
                continue
            i = int(m.group()) - 1
            p = _posicao(str(c.get("trecho", "")), str(web_facts[i].get("trecho", "")))
            if p is not None:
                cobertos.setdefault(i, []).append((p, p + len(str(c.get("trecho", "")))))
    saida = []
    for i, f in enumerate(web_facts):
        frases = _frases_com_posicao(str(f.get("trecho", "")))
        saida.append([(k, frase) for k, (a, z, frase) in enumerate(frases)
                      if not any(a < z2 and z > a2 for a2, z2 in cobertos.get(i, []))])
    return saida


def _juntar(frases: list[tuple[int, str]], total: int) -> str:
    """Frases escolhidas de uma fonte, na ordem; pulo entre elas (ou antes da primeira) vira marca de corte."""
    partes, anterior = [], -1
    for k, frase in sorted(frases):
        if k != anterior + 1:
            partes.append(MARCA_CORTE)
        partes.append(frase)
        anterior = k
    if partes and anterior != total - 1:
        partes.append(MARCA_CORTE)
    return " ".join(partes)


def _palavras_de_busca(textos: list[str]) -> set[str]:
    out = set()
    for t in textos:
        out |= {w for w in re.findall(r"[a-z0-9]+", dobrar(t)) if len(w) >= 4} - _PARADAS_BUSCA
    return out


_PARADAS_BUSCA = {"como", "quais", "qual", "quando", "onde", "porque", "para", "pelo", "pela", "sobre", "entre", "depois",
                  "antes", "cada", "isso", "esse", "essa", "foram", "teve", "what", "which", "when", "where", "after",
                  "before", "with", "from", "that", "this", "were", "have", "does", "video", "historia", "espectador",
                  "precisa", "falta", "banco", "fonte", "fontes"}


def _frases_relevantes(sobra: list[list[tuple[int, str]]], web_facts: list[dict], chaves: set[str]) -> list[tuple[int, int, int, str]]:
    """(nota, fonte, posição, frase) das frases sem uso que têm palavras das perguntas abertas, da mais relevante.
    A nota é o número de palavras distintas; frase sem nenhuma não entra (lote sem conteúdo relevante não sai)."""
    achadas = []
    for i, frases in enumerate(sobra):
        for k, frase in frases:
            dob = set(re.findall(r"[a-z0-9]+", dobrar(frase)))
            nota = len(chaves & dob)
            if nota and len(frase.split()) >= 5:
                achadas.append((nota, i, k, frase))
    return sorted(achadas, key=lambda x: (-x[0], x[1], x[2]))


def _resumo_do_banco(evidencias: list[dict]) -> tuple[str, dict[str, dict]]:
    ids = {f"E{k}": e for k, e in enumerate(evidencias, 1)}
    return "\n".join(f"{i}: {e['afirmacao'][:RESUMO_AFIRMACAO]}" for i, e in ids.items()), ids


def _ja_no_banco(afirmacao: str, evidencias: list[dict], citacoes: list[dict] | None = None) -> str | None:
    """A afirmação já está no banco: quase igual, ou com as mesmas palavras de conteúdo dos dois lados, ou (22/11,
    E70 × E73 da rodada 11) tirada do MESMO trecho, com os mesmos números e metade das palavras em comum."""
    trechos = {(c.get("fonte"), " ".join(_tokens(c.get("trecho", "")))) for c in citacoes or []}
    for k, e in enumerate(evidencias, 1):
        if _quase_iguais(afirmacao, e["afirmacao"]) or _sobreposicao_mutua(afirmacao, e["afirmacao"]):
            return e.get("id") or f"E{k}"
        outros = {(c.get("fonte"), " ".join(_tokens(c.get("trecho", "")))) for c in e.get("citacoes") or []}
        if trechos and trechos & outros and numeros(afirmacao) == numeros(e["afirmacao"]) \
                and _sobreposicao_mutua(afirmacao, e["afirmacao"], minimo=0.5):
            return e.get("id") or f"E{k}"
    return None


def _valido_diagnostico(raw: str) -> bool:
    d = _json_seguro(raw)
    return isinstance(d.get("perguntas"), list) and isinstance(d.get("suficiente"), bool)


def _valido_dirigido(raw: str) -> bool:
    return isinstance(_json_seguro(raw).get("respostas"), list)


def cobrir_lacunas(topic: str, historia: dict | None, evidencias: list[dict], web_facts: list[dict],
                   fontes: list[dict]) -> tuple[list[dict], dict]:
    """Segunda passagem: devolve (evidências novas já conferidas como na extração, relatório). Sem história ou sem
    diagnóstico válido, nada entra e o relatório diz isso — ausência de resposta nunca vira "banco suficiente"."""
    from . import metricas
    rel: dict = {"decisao": None, "suficiente": None, "perguntas": [], "chamadas": 0, "lotes": 0,
                 "caracteres_enviados": 0, "frases_relevantes": 0, "descartadas_repetidas": [], "lacunas": []}
    if not historia:
        rel["decisao"] = "sem_historia"
        return [], rel
    sobra = trechos_nao_cobertos(evidencias, web_facts)
    if sum(len(f) for frases in sobra for _, f in frases) < MIN_NAO_COBERTO:
        rel["decisao"] = "nada_a_revisar"
        return [], rel
    resumo, ids = _resumo_do_banco(evidencias)
    with metricas.etapa("cobertura_diagnostico"):
        rel["chamadas"] += 1
        diag = _chamar_guardado("cobertura_diagnostico", _PROMPT_DIAGNOSTICO.format(
            historia=_bloco_historia(historia), banco=resumo), _valido_diagnostico, 1500)
    if not diag:
        rel["decisao"] = "sem_resposta"
        log.warning("cobertura do banco: diagnóstico sem resposta válida — nada acrescentado, cobertura NÃO avaliada")
        return [], rel
    por_id_resp = {str(p.get("id")): p for p in diag["perguntas"] if isinstance(p, dict)}
    abertas = []
    for p in historia["perguntas"]:
        r = por_id_resp.get(p["id"]) or {}
        citadas = [x for x in r.get("evidencias") or [] if x in ids]
        if r.get("situacao") == "respondida" and citadas:
            rel["perguntas"].append({**p, "situacao": "respondida", "evidencias": citadas})
            continue
        # sem resposta para a pergunta, ou "respondida" sem evidência que exista: não é dada como respondida
        situacao = "falta" if r.get("situacao") == "falta" else "nao_avaliada"
        falta = str(r.get("falta", "")).strip() or p["pergunta"]
        chaves = [str(c) for c in r.get("chaves") or [] if str(c).strip()]
        abertas.append({**p, "situacao": situacao, "falta": falta, "chaves": chaves})
    rel["perguntas"] += abertas
    rel["suficiente"] = not abertas
    if not abertas:
        rel["decisao"] = "suficiente"
        log.info("cobertura do banco: o banco responde as %d perguntas da história — nada a buscar", len(historia["perguntas"]))
        return [], rel
    chaves = _palavras_de_busca([" ".join(a["chaves"]) + " " + a["falta"] + " " + a["pergunta"] for a in abertas])
    relevantes = _frases_relevantes(sobra, web_facts, chaves)
    rel["frases_relevantes"] = len(relevantes)
    if not relevantes:
        rel["decisao"] = "lacunas_sem_material"
        log.info("cobertura do banco: %d pergunta(s) aberta(s), nenhuma frase sem uso fala delas", len(abertas))
        return [], rel
    lotes: list[dict[int, list[tuple[int, str]]]] = []
    fila = list(relevantes)
    while fila and len(lotes) < MAX_LOTES_DIRIGIDOS:
        if lotes:
            # 22/11 (prova da rodada 11: 69 frases com UMA palavra das perguntas, 2 lotes, 227 s): o segundo lote só
            # leva frase com ao menos 2 palavras das perguntas abertas; as outras ficam registradas como não enviadas
            fila = [x for x in fila if x[0] >= 2] or []
            if not fila:
                break
        lote: dict[int, list[tuple[int, str]]] = {}
        usado, resto = 0, []
        for nota, i, k, frase in fila:
            custo = len(frase) + 1 + (80 if i not in lote else 0)
            if usado + custo > LIMITE_LOTE:
                resto.append((nota, i, k, frase))
                continue
            lote.setdefault(i, []).append((k, frase))
            usado += custo
        lotes.append(lote)
        fila = resto
    rel["nao_enviadas"] = len(relevantes) - sum(len(fr) for lote in lotes for fr in lote.values())
    texto_abertas = "\n".join(f"  {a['id']}) {a['pergunta']} — falta: {a['falta']}" for a in abertas)
    brutos: list[dict] = []
    respondeu = False
    for lote in lotes:
        escolhidos = {i: _juntar(fr, len(_frases_com_posicao(str(web_facts[i].get("trecho", ""))))) for i, fr in lote.items()}
        texto = sum(len(t) for t in escolhidos.values())
        rel["lotes"] += 1
        rel["caracteres_enviados"] += texto
        maximo = limite_de_evidencias(texto, len(escolhidos), minimo=1)
        with metricas.etapa("cobertura_busca_dirigida"):
            rel["chamadas"] += 1
            data = _chamar_guardado("cobertura_busca_dirigida", _PROMPT_DIRIGIDO.format(
                historia=_bloco_historia(historia), abertas=texto_abertas, fontes=_bloco(web_facts, escolhidos),
                maximo=maximo), _valido_dirigido, RESPOSTA_EXTRACAO)
        if not data:
            continue
        respondeu = True
        validos = {a["id"] for a in abertas}
        for r in data["respostas"]:
            if not isinstance(r, dict) or str(r.get("id")) not in validos:
                continue  # resposta a pergunta que não estava aberta: não entra
            brutos += [{**b, "_pergunta": str(r["id"])} for b in (r.get("evidencias") or []) if isinstance(b, dict)][:maximo]
    if not respondeu:
        rel["decisao"] = "sem_resposta"
        log.warning("cobertura do banco: busca dirigida sem resposta válida — nada acrescentado")
        return [], rel
    rel["decisao"] = "avaliada"
    novas = _validar(brutos, web_facts, fontes)
    aceitas = []
    for e, b in zip(novas, brutos):
        e["origem"] = "cobertura"
        e["lacuna_cobertura"] = b["_pergunta"]
        igual = _ja_no_banco(e["afirmacao"], evidencias + aceitas, e["citacoes"])
        if igual:
            rel["descartadas_repetidas"].append({"afirmacao": e["afirmacao"], "igual_a": igual})
            continue
        aceitas.append(e)
    pendentes = [e for e in aceitas if e["citacoes"] and not any(c["verificado"] for c in e["citacoes"])]
    if pendentes:
        rel["chamadas"] += -(-min(len(pendentes), MAX_RECUPERAR) // 6)
        aceitas = _aplicar_trocas(aceitas, _recuperar_por_llm(pendentes, web_facts, fontes), evidencias,
                                  rel["descartadas_repetidas"])
    for a in abertas:
        a["evidencias_novas"] = [e["afirmacao"] for e in aceitas if e["lacuna_cobertura"] == a["id"]]
    rel["lacunas"] = [{"id": a["id"], "o_que_falta": a["falta"], "evidencias": a["evidencias_novas"]} for a in abertas]
    anotar_contexto(aceitas, web_facts)
    _conferir_sujeito(aceitas, web_facts, topic)
    log.info("cobertura do banco: %d pergunta(s) aberta(s), %d frase(s) relevante(s) sem uso, %d lote(s), %d "
             "evidência(s) nova(s) (%d repetida(s) descartada(s))", len(abertas), len(relevantes), rel["lotes"],
             len(aceitas), len(rel["descartadas_repetidas"]))
    return aceitas, rel


def montar_banco(topic: str, web_facts: list[dict] | None,
                 buscar: Callable[[str], list[dict] | None] | None = None, angulo: str | None = None) -> dict | None:
    """Pesquisa → banco de evidências. `buscar(pergunta)` (opcional) pesquisa as lacunas que as
    fontes não responderam (até MAX_LACUNAS_BUSCADAS, uma rodada). None se o extrator forte não
    respondeu. Guardado em cache pelo tema + conteúdo das fontes."""
    fontes_web = [f for f in web_facts or [] if str(f.get("trecho", "")).strip()]
    if not fontes_web:
        return None
    from . import disk_cache
    chave = [VERSAO, disk_cache.versao_do_codigo("evidencias.py"), topic, angulo or "",
             hashlib.sha1(json.dumps(fontes_web, ensure_ascii=False, sort_keys=True).encode()).hexdigest()]
    guardado = disk_cache.get("banco_evidencias", chave)
    if guardado:
        log.info("banco de evidências em cache: %d evidências", len(guardado.get("evidencias", [])))
        return guardado
    fontes = _fontes_do_banco(fontes_web)
    historia = planejar_historia(topic, angulo)  # antes da extração: que história o banco tem de sustentar
    extraido = _extrair(topic, fontes_web, fontes, historia)
    if extraido is None:
        return None
    evidencias, lacunas, nao_lidos = extraido
    selecao: list[dict] = []
    if buscar and lacunas:
        candidatas: list[tuple[str, dict]] = []
        for pergunta in lacunas[:MAX_LACUNAS_BUSCADAS]:
            try:
                candidatas += [(pergunta, f) for f in buscar(pergunta) or []]
            except Exception as exc:  # noqa: BLE001 — busca da lacuna nunca derruba o banco
                log.warning("busca da lacuna %r falhou: %s", pergunta[:60], exc)
        urls = {f.get("url") for f in fontes_web}
        vistas: set = set()
        unicas = []
        for pergunta, f in candidatas:
            if f.get("url") not in urls and f.get("url") not in vistas and str(f.get("trecho", "")).strip():
                vistas.add(f.get("url"))
                unicas.append((pergunta, f))
        novas, selecao = selecionar_fontes_lacunas(unicas, topic, fontes_web)
        log.info("fontes das lacunas: %d de %d entram (%d reduzidas aos trechos do assunto, %d excluídas)",
                 len(novas), len(unicas), sum(1 for r in selecao if r["decisao"] == "reduzida"),
                 sum(1 for r in selecao if r["decisao"] == "excluida"))
        if novas:
            todas = fontes_web + novas
            fontes = _fontes_do_banco(todas)
            extra = _extrair_novas(topic, todas, fontes, len(fontes_web), historia)
            if extra:
                evidencias += extra[0]
                respondidas = len(extra[0])
                log.info("lacunas pesquisadas: %d fonte(s) nova(s), %d evidência(s) a mais", len(novas), respondidas)
                lacunas = extra[1] if extra[0] else lacunas
            fontes_web = todas
    novas_cob, cobertura = cobrir_lacunas(topic, historia, evidencias, fontes_web, fontes)
    evidencias += novas_cob
    evidencias = _mesclar_iguais(evidencias)
    julgar_sustentacao(evidencias, fontes_web, topic)
    evidencias = marcar_conflitos(_numerar([_status(e) for e in evidencias]), sujeitos_do_tema(topic),
                                  confirmar=lambda pares: mesma_medida_ia(pares, topic))
    banco = {"versao": VERSAO, "tema": topic, "fontes": fontes, "evidencias": evidencias,
             "lacunas": lacunas, "nao_lidos": nao_lidos[:20], "selecao_lacunas": selecao, "cobertura": cobertura,
             "historia": historia}
    resumo = {s: sum(1 for e in evidencias if e["status"] == s) for s in FORCA}
    log.info("banco de evidências: %d evidências (%s); %d qualitativas verificadas; lacunas: %s",
             len(evidencias), ", ".join(f"{k} {v}" for k, v in resumo.items() if v),
             sum(1 for e in evidencias if e["tipo"] == "qualitativo" and e["status"] in STATUS_USAVEIS),
             "; ".join(lacunas) or "nenhuma")
    if banco["lacunas"]:
        aprofundar_lacunas(banco, topic, fontes_web)
    if len(usaveis(banco)) >= 5:
        disk_cache.put("banco_evidencias", chave, banco)
    return banco


# ---------------------------------------------------------------- fontes das lacunas (21/11)
# A busca de uma lacuna ("intervenção manual / flexibilidade") trouxe 12 páginas do Hubble para um tema sobre o
# James Webb: 13 evidências fora do assunto e 1/3 das chamadas do juiz. A seleção é pelo ASSUNTO do tema (nomes
# próprios e nomes alternativos aprendidos nas fontes do próprio tema: sigla, nome em outra escrita), nunca
# pelas palavras da pergunta — essas só ordenam. Na dúvida, a fonte fica. Página sobre outro assunto que cita o
# do tema fica só com os trechos que o citam (com as frases vizinhas e a marca de corte). Cada decisão é
# registrada no banco ("selecao_lacunas") com o motivo.
_PALAVRAS_TITULO = {"wikipedia", "wikipédia", "grokipedia", "the", "and", "of", "for", "de", "da", "do", "dos", "das",
                    "en", "pt", "es", "um", "una", "uma", "el", "la", "los", "las", "com", "por", "para", "links",
                    "page", "home", "news", "about", "overview", "report", "reports", "article", "articles"}


def _palavras_titulo(titulo: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9-]{2,}", dobrar(titulo)) if w not in _PALAVRAS_TITULO]


def nomes_alternativos(topic: str, base: list[dict]) -> set[str]:
    """Outros nomes do assunto do tema, tirados das fontes do PRÓPRIO tema: a sigla do nome completo
    ("James Webb Space Telescope (JWST)" ou "JWST" com as iniciais dele) e o título em escrita não latina
    (青函トンネル), que a transliteração não alcança. Dobrados (minúsculos, sem acento)."""
    sujeitos = sujeitos_do_tema(topic)
    if not sujeitos:
        return set()
    textos = [str(f.get("trecho", "")) + " " + str(f.get("titulo", "")) for f in base]
    tudo = " ".join(textos)
    nomes: set[str] = set()
    for m in re.finditer(r"((?:[A-ZÀ-Ý][\wÀ-ÿ'-]+\s+(?:(?:of|de|da|do|the)\s+)?){1,6}[A-ZÀ-Ý][\wÀ-ÿ'-]+)(\s*\(([A-Z][A-Z0-9]{1,7})\))?", tudo):
        run = m.group(1)
        if not any(_menciona(n, run) for n in sujeitos):
            continue
        if m.group(3):
            nomes.add(m.group(3).lower())
        sigla = "".join(w[0] for w in re.findall(r"[A-ZÀ-Ý][\wÀ-ÿ'-]*", run)).upper()
        if len(sigla) >= 3 and re.search(rf"(?<![\w-]){re.escape(sigla)}(?![\w-])", tudo):
            nomes.add(sigla.lower())
    for f in base:
        t = re.sub(r"^.*?:\s*", "", str(f.get("titulo", "")))
        if re.search(r"[\u0400-\u04ff\u0590-\u06ff\u0900-\u0dff\u3040-\u9fff\uac00-\ud7af]", t):
            nomes.add(dobrar(t.split(" - ")[0].strip()))
    return {n for n in nomes if n and n not in sujeitos}


def _mencoes(texto: str, nomes: set[str]) -> list[int]:
    """Posições (agrupadas: "James Webb Space Telescope (JWST)" conta uma vez) em que algum nome aparece."""
    alvo = dobrar(texto)
    alvo_t = dobrar(_translit(texto))
    pos = sorted({m.start() for n in nomes for t in (alvo, alvo_t)
                  for m in re.finditer(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t)})
    grupos: list[int] = []
    for p in pos:
        if not grupos or p - grupos[-1] > 40:
            grupos.append(p)
    return grupos


def _assunto_da_pagina(fonte: dict, nomes: set[str], genericas: set[str], n_mencoes: int) -> str | None:
    """Outro assunto próprio da página: palavra do título (fora dos nomes do tema e das palavras comuns aos
    títulos das fontes do tema) que se repete no texto bem mais que o assunto do tema."""
    texto = dobrar(fonte.get("trecho", ""))
    melhor, vezes = None, 0
    for w in _palavras_titulo(str(fonte.get("titulo", ""))):
        if w in nomes or w in genericas or w.isdigit():
            continue
        n = len(re.findall(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", texto))
        if n > vezes:
            melhor, vezes = w, n
    if not (vezes >= 3 and vezes >= 2 * max(1, n_mencoes)) and n_mencoes == 0:
        # página que nunca cita o tema: o nome próprio que domina o TEXTO (maiúscula no meio da frase, 3+ vezes)
        # também mostra o assunto dela — STS-31 não tem "Hubble" no título, e o texto é sobre o Hubble
        bruto = str(fonte.get("trecho", ""))
        nomes_txt: dict[str, int] = {}
        for m in re.finditer(r"(?<=[\w,;:)] )([A-ZÀ-Ý][\wÀ-ÿ'-]{2,})", bruto):
            w = dobrar(m.group(1))
            if w not in nomes and w not in genericas and w not in _PALAVRAS_TITULO:
                nomes_txt[w] = nomes_txt.get(w, 0) + 1
        if nomes_txt:
            w, n = max(nomes_txt.items(), key=lambda x: x[1])
            if n >= 3 and n > vezes:
                melhor, vezes = w, n
    return melhor if vezes >= 3 and vezes >= 2 * max(1, n_mencoes) else None


def _so_os_trechos_do_assunto(texto: str, nomes: set[str], vizinhas: int = 2) -> str:
    frases = [f[2] for f in _frases_com_posicao(texto)]
    marcadas = {k for k, f in enumerate(frases) if _mencoes(f, nomes)}
    janela = {j for k in marcadas for j in range(k - vizinhas, k + vizinhas + 1) if 0 <= j < len(frases)}
    partes, anterior = [], None
    for k in sorted(janela):
        if (anterior is None and k > 0) or (anterior is not None and k != anterior + 1):
            partes.append(MARCA_CORTE)
        partes.append(frases[k])
        anterior = k
    if janela and max(janela) < len(frases) - 1:
        partes.append(MARCA_CORTE)
    return " ".join(partes)


def selecionar_fontes_lacunas(candidatas: list[tuple[str, dict]], topic: str, base: list[dict]) -> tuple[list[dict], list[dict]]:
    """(fontes que entram, na ordem de prioridade; registro de cada decisão {lacuna, url, titulo, decisao, motivo}).
    decisao: "selecionada" (inteira), "reduzida" (só os trechos que citam o assunto) ou "excluida"."""
    sujeitos = sujeitos_do_tema(topic)
    registro: list[dict] = []
    if not sujeitos:  # tema sem nome próprio: não há assunto para conferir — nada sai
        for pergunta, f in candidatas:
            registro.append({"lacuna": pergunta, "url": f.get("url", ""), "titulo": f.get("titulo", ""),
                             "decisao": "selecionada", "motivo": "tema sem nome próprio: sem filtro de assunto"})
        return [f for _, f in candidatas], registro
    nomes = sujeitos | nomes_alternativos(topic, base)
    contagem: dict[str, int] = {}
    for f in base:
        for w in set(_palavras_titulo(str(f.get("titulo", "")))):
            contagem[w] = contagem.get(w, 0) + 1
    genericas = {w for w, n in contagem.items() if n >= 2}
    escolhidas: list[tuple[tuple, dict]] = []
    for pergunta, f in candidatas:
        texto = str(f.get("trecho", ""))
        titulo_url = f"{f.get('titulo', '')} {str(f.get('url', '')).replace('_', ' ').replace('-', ' ')}"
        n = len(_mencoes(texto, nomes))
        no_titulo = bool(_mencoes(titulo_url, nomes))
        outro = None if no_titulo else _assunto_da_pagina(f, nomes, genericas, n)
        classe = classe_fonte(f)
        q = palavras_chave(pergunta)
        afinidade = sum(1 for c in q if c in dobrar(texto)) / max(1, len(q))  # só ordena, nunca exclui
        nome_txt = ", ".join(sorted(nomes))
        if no_titulo:
            decisao, motivo, nivel = "selecionada", "o título ou o endereço nomeia o assunto", 0
        elif n == 0 and outro:
            decisao, motivo, nivel = "excluida", (f"a página trata de \"{outro}\" e não cita o assunto ({nome_txt}) "
                                                  "em lugar nenhum: nada nela pode ser atribuído ao tema"), 9
        elif n == 0:
            decisao, nivel = "selecionada", 3
            motivo = ("não nomeia o assunto, mas também não trata de outro"
                      + (" e é fonte oficial/acadêmica" if classe in ("oficial", "academica") else "") + ": na dúvida, fica")
        elif outro:
            decisao, nivel = "reduzida", 2
            motivo = (f"a página trata de \"{outro}\" e cita o assunto {n} vez(es): ficam os trechos que o citam, "
                      "com as frases vizinhas")
        else:
            decisao, motivo, nivel = "selecionada", f"cita o assunto {n} vez(es) e não trata de outro", 1
        registro.append({"lacuna": pergunta, "url": f.get("url", ""), "titulo": f.get("titulo", ""), "classe": classe,
                         "decisao": decisao, "motivo": motivo, "mencoes": n, "afinidade_pergunta": round(afinidade, 2)})
        if decisao == "excluida":
            log.info("fonte da lacuna excluída: %s — %s", f.get("url", "")[:80], motivo)
            continue
        if decisao == "reduzida":
            reduzido = _so_os_trechos_do_assunto(texto, nomes)
            registro[-1]["chars"] = [len(texto), len(reduzido)]
            f = {**f, "trecho": reduzido}
        oficial = 0 if classe in ("oficial", "academica") else 1
        escolhidas.append(((nivel, oficial, -n / max(1, len(texto)) * 1000, -afinidade), f))
    escolhidas.sort(key=lambda x: x[0])
    return [f for _, f in escolhidas], registro


def _extrair_novas(topic: str, todas: list[dict], fontes: list[dict], a_partir: int, historia: dict | None = None):
    """Extração só das fontes novas (as das lacunas), com a numeração [S#] do banco inteiro."""
    so_novas = [f if i >= a_partir else {**f, "trecho": ""} for i, f in enumerate(todas)]
    return _extrair(topic, so_novas, fontes, historia)


_PROMPT_CHAVES = """Perguntas que a pesquisa ainda não respondeu sobre o tema "{tema}":
{lacunas}
Para procurar as respostas em artigos de enciclopédia, dê de 6 a 12 palavras-chave (substantivos e termos
técnicos, sem palavras genéricas) em cada idioma pedido: {idiomas}.
Responda SÓ JSON: {{"chaves": {{"en": ["..."], "pt": ["..."]}}}}"""


def aprofundar_lacunas(banco: dict, topic: str, web_facts: list[dict], limite: int = 3500) -> int:
    """20/11: a pesquisa guarda poucos milhares de caracteres de cada artigo e prefere frases com
    número — o banco nasce cronológico, e o COMO (processo, método, dificuldade) fica de fora mesmo estando
    na fonte. Para as lacunas que sobraram: palavras-chave por idioma (IA), parágrafos do artigo COMPLETO da
    Wikipédia escolhidos só por elas, e a mesma extração/verificação/juiz de sempre. Devolve quantas
    evidências entraram."""
    from urllib.parse import unquote
    from . import web_search
    lacunas = [x for x in banco.get("lacunas") or [] if str(x).strip()]
    wikis = [f for f in web_facts if re.search(r"//(\w\w)\.wikipedia\.org/wiki/", str(f.get("url", "")))
             and "#lacunas" not in str(f.get("url", ""))][:3]
    if not lacunas or not wikis:
        return 0
    idiomas = sorted({re.search(r"//(\w\w)\.", f["url"]).group(1) for f in wikis})
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    try:
        raw = complete([{"role": "user", "content": _PROMPT_CHAVES.format(
            tema=topic, lacunas="\n".join(f"- {x}" for x in lacunas), idiomas=", ".join(idiomas))}],
            max_tokens=600, temperature=0.1, model_filter=is_strong_model,
            validate=lambda r: isinstance(_json_seguro(r).get("chaves"), dict))
        chaves = _json_seguro(raw).get("chaves") or {}
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("aprofundamento das lacunas indisponível: %s", exc)
        return 0
    novas = []
    for f in wikis:
        lang = re.search(r"//(\w\w)\.", f["url"]).group(1)
        kws = {w for c in chaves.get(lang) or [] for w in web_search._keywords(str(c))}
        if not kws:
            continue
        titulo = unquote(f["url"].split("/wiki/", 1)[1]).replace("_", " ")
        try:
            texto = web_search._wiki_article(lang, titulo)[0]
        except Exception as exc:  # noqa: BLE001 — rede fora: segue sem aprofundar
            log.warning("artigo completo de %s indisponível: %s", f["url"], exc)
            continue
        trecho = web_search._dense_passage(texto, kws, limite, bonus_numero=False)
        if trecho and len(trecho) > 150:
            novas.append({"titulo": f"{f.get('titulo', titulo)} (aprofundado nas lacunas)", "url": f["url"] + "#lacunas",
                          "trecho": trecho, "cortes_marcados": True})
    if not novas:
        return 0
    entrou = adicionar_fontes(banco, topic, novas, web_facts, atualizar_lacunas=True)
    log.info("lacunas aprofundadas no artigo completo: %d fonte(s), %d evidência(s) a mais", len(novas), len(entrou))
    return len(entrou)


def adicionar_fontes(banco: dict, topic: str, novas: list[dict] | None, web_facts: list[dict],
                     atualizar_lacunas: bool = False) -> list[dict]:
    """Fontes achadas depois (busca focada no conserto): extrai, confere e acrescenta ao banco com
    ids novos. Devolve as evidências acrescentadas. `web_facts` (as fontes do job) recebe as novas."""
    urls = {f.get("url") for f in web_facts}
    novas = [f for f in novas or [] if f.get("url") not in urls and str(f.get("trecho", "")).strip()]
    if not novas:
        return []
    base = len(web_facts)
    web_facts.extend(novas)
    fontes = _fontes_do_banco(web_facts)
    extra = _extrair_novas(topic, web_facts, fontes, base, banco.get("historia"))
    if not extra:
        return []
    inicio = len(banco["evidencias"]) + 1
    julgar_sustentacao(extra[0], web_facts, topic)
    novas_ev = _numerar([_status(e) for e in extra[0]], inicio)
    if atualizar_lacunas and extra[0]:
        banco["lacunas"] = extra[1]  # o que as fontes novas ainda não respondem (o resto deixou de ser lacuna)
    banco["fontes"] = fontes
    banco["evidencias"] = marcar_conflitos(banco["evidencias"] + novas_ev, sujeitos_do_tema(topic),
                                           confirmar=lambda pares: mesma_medida_ia(pares, topic))
    log.info("banco de evidências: +%d evidência(s) de %d fonte(s) nova(s)", len(novas_ev), len(novas))
    return novas_ev


# ---------------------------------------------------------------- consulta
def por_id(banco: dict | None) -> dict[str, dict]:
    return {e["id"]: e for e in (banco or {}).get("evidencias", [])}


def so_oficiais(banco: dict | None) -> dict | None:
    """24/11 (regra do dono): o roteiro só usa evidência OFICIAL e verificada — trecho provado numa fonte
    oficial/acadêmica. Wikipédia, Grokipedia, turismo e imprensa de fonte única ficam fora do banco que o
    escritor, o editor e os auditores veem (na rodada de Svalbard eram elas que traziam "Marrocos e Líbano",
    "arenito", "Juízo Final")."""
    if not banco:
        return banco
    return {**banco, "evidencias": [e for e in banco.get("evidencias", []) if e.get("status") == "oficial"]}


def usaveis(banco: dict | None) -> list[dict]:
    return [e for e in (banco or {}).get("evidencias", []) if e["status"] in STATUS_USAVEIS]


# ---------------------------------------------------------------- revisão das afirmações (25/11)
# Catálogo de falhas de Svalbard: a afirmação do banco dizia mais que o trecho ("serve como protetor natural
# mantendo as sementes na temperatura necessária", "Hemisfério Sul" por "Southern regions", "para garantir" por
# "to contribute to securing", "elevou o total para" por "added to the more than") e o escritor repetia o exagero.
# O juiz de sustentação (cascata, sem raciocínio) aprovou. Aqui o JUIZ fixo, com raciocínio, relê cada afirmação
# utilizável contra o MESMO trecho que o escritor vê e decide: fiel, corrigir (frase nova fiel) ou descartar.
# Uma vez por evidência: a decisão fica em cache pelo texto da afirmação e do trecho (e pelas regras).
LOTE_REVISAO = 10
TRECHO_REVISAO = 600
DECISOES_REVISAO = ("fiel", "corrigir", "descartar")

_PROMPT_REVISAO = """Você revisa as AFIRMAÇÕES de um banco de evidências de um vídeo (tema: {tema}). Cada AFIRMAÇÃO
(português) deveria dizer EXATAMENTE o que o TRECHO da fonte (no idioma original) diz — nem mais, nem menos forte.
Só o trecho conta, não o que você sabe. Leia com rigor; para cada item decida:

"fiel": a afirmação diz o que o trecho diz (tradução, resumo e arredondamento no sentido certo valem).
"corrigir": a afirmação diz MAIS ou OUTRA COISA que o trecho. Dê "afirmacao_corrigida": UMA frase em português do
  Brasil que diz exatamente o que o trecho diz sobre o mesmo assunto. Corrija quando houver:
  - FINALIDADE, CAUSA ou CONSEQUÊNCIA que o trecho não afirma ("foi criado para", "para garantir", "o que garante",
    "por isso", "sem depender de") — "It allows" é "permite", não "foi criado para permitir";
  - EFEITO ou AÇÃO atribuídos ao sujeito que o trecho não diz que ele faz, muitas vezes num gerúndio ("mantendo X",
    "permitindo Y", "evitando Z"): "X serves as a protectant, as the seeds need to be kept cold" diz que X serve de
    proteção PORQUE as sementes precisam ficar frias — NÃO diz que X mantém as sementes frias;
  - TERMO MAIS FORTE ou mais amplo que o do trecho: "helping ensure" é "ajuda a garantir", "contribute to securing"
    é "contribuir para garantir", "Southern regions" é "regiões do sul" (não "Hemisfério Sul"), "mostly" é "em
    sua maioria", "many" não vira "todos";
  - RESSALVA ou MODO perdidos: mantenha "em muitos aspectos", "pode", "teria de", "seria", "cerca de",
    "aproximadamente" (condicional/necessidade não vira fato consumado: "would have to be" é "teria de ser");
  - TEMPO ou SENTIDO trocados: "added to the more than 1.4 million already" é "somou-se às mais de 1,4 milhão
    que já estavam lá" (não "elevou o total para mais de 1,4 milhão"); data/época do trecho fica a do trecho;
  - PALAVRAS A MAIS: qualificador, detalhe ou conclusão que o trecho não tem ("vitais", "sem transferência de
    posse") — tire;
  - SUJEITO trocado ou perdido: mantenha quem faz/diz o quê; citação ou opinião é atribuída a quem a diz
    ("segundo X, ...").
  A frase corrigida não acrescenta nada (nenhum número, nome ou dado que não esteja no trecho) e fica completa e
  compreensível sozinha.
"descartar": o trecho é texto de interface/navegação do site ("see the tab in the menu above", "click here") e
  não sobra fato; OU é fragmento sem sujeito/atribuição que não dá uma afirmação completa; OU o trecho não diz o
  que a afirmação diz e não há fato aproveitável. Se o trecho tem um fato real junto com texto de interface,
  prefira "corrigir" ficando só com o fato — a frase corrigida NUNCA leva instrução de navegação ("veja/consulte a
  aba", "no menu acima", "clique").
Na dúvida entre "fiel" e "corrigir", corrija: a frase corrigida fiel nunca prejudica.

{itens}

Responda SÓ JSON: {{"revisoes": [{{"n": 1, "decisao": "fiel|corrigir|descartar", "afirmacao_corrigida": "",
"motivo": "o que a afirmação diz a mais (ou por que descartar)"}}]}}"""


def _trecho_do_escritor(banco: dict) -> dict[str, str]:
    """{id: trecho} — o MESMO trecho que o escritor e a conferência leem (redacao.mapa_do_conhecimento: a citação
    verificada da melhor autoridade)."""
    from .redacao import mapa_do_conhecimento
    return {m["id"]: m["trecho"] for m in mapa_do_conhecimento(banco)}


def _regras_revisao() -> str:
    import inspect
    return _impressao(1, _PROMPT_REVISAO, DECISOES_REVISAO, inspect.getsource(_revisao_valida))


def _revisao_valida(d, trecho: str) -> dict | None:
    """Decisão aproveitável do juiz, já com a trava de código: correção com número que o trecho não tem (ou
    vazia) não vale — fica a afirmação original (devolve None)."""
    if not isinstance(d, dict):
        return None
    # 26/11: "fiel: corrigir" / "Corrigir" — forma e palavras aprendidas em src/decisoes.py
    from .decisoes import ler_lote
    decisao = ler_lote([d.get("decisao")], DECISOES_REVISAO, ("fiel",), "revisao")[0] or ""
    if decisao not in DECISOES_REVISAO:
        return None
    motivo = re.sub(r"\s+", " ", str(d.get("motivo") or "")).strip()[:300]
    if decisao != "corrigir":
        return {"decisao": decisao, "motivo": motivo}
    nova = re.sub(r"\s+", " ", str(d.get("afirmacao_corrigida") or "")).strip()
    if len(nova.split()) < 3:
        return None
    ok, _ = numeros_conferidos(nova, numeros(nova), trecho)
    if not ok:
        log.info("revisão: correção recusada (número fora do trecho): %s", nova[:160])
        return None
    return {"decisao": "corrigir", "afirmacao_corrigida": nova, "motivo": motivo}


def _revisar_lote(itens: list[dict], topic: str) -> dict[int, dict]:
    """{n (1-based): decisão válida}. Juiz fixo (raciocínio); fora do ar → a cascata forte. Sem resposta: {}."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    texto = "\n".join(f"{n}) AFIRMAÇÃO: {it['afirmacao']}\n   TRECHO: \"{it['trecho'][:TRECHO_REVISAO]}\""
                      for n, it in enumerate(itens, 1))
    pedido = [{"role": "user", "content": _PROMPT_REVISAO.format(tema=topic, itens=texto)}]
    valido = lambda r: '"revisoes"' in (r or "")  # noqa: E731
    try:
        try:
            raw = complete(pedido, max_tokens=6000, temperature=0.1, validate=valido, juiz=True)
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("juiz da revisão das afirmações sem resposta (%s) — revisa a cascata", str(exc)[:120])
            raw = complete(pedido, max_tokens=6000, temperature=0.1, validate=valido, model_filter=is_strong_model)
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("revisão das afirmações indisponível (%s): ficam como estão", exc)
        return {}
    try:
        lista = _json(raw).get("revisoes") or []
    except ValueError:
        return {}
    out: dict[int, dict] = {}
    for d in lista if isinstance(lista, list) else []:
        if not isinstance(d, dict) or not str(d.get("n", "")).strip().isdigit():
            continue
        n = int(str(d["n"]).strip())
        if 1 <= n <= len(itens):
            v = _revisao_valida(d, itens[n - 1]["trecho"])
            if v:
                out[n] = v
    return out


def revisar_afirmacoes(banco: dict | None, topic: str, ids: set[str] | None = None) -> dict | None:
    """25/11: cada afirmação utilizável relida pelo juiz fixo contra o trecho que o escritor vê, antes do escritor.
    "corrigir" troca a afirmação (a original fica em `afirmacao_original`); "descartar" tira a evidência das
    utilizáveis (status "nao_verificado" com o motivo); "fiel" só marca. Sem decisão do juiz, nada muda.
    Devolve um banco novo (o recebido não é alterado)."""
    if not banco or not banco.get("evidencias"):
        return banco
    from . import disk_cache
    trechos = _trecho_do_escritor(banco)
    regras = _regras_revisao()
    # 25/11 (Svalbard 1019: 200 evidências, 3–4 min por lote = ~45 min): só as que o roteiro cita (`ids`) —
    # é nelas que a distorção da afirmação chega ao vídeo; o resto do banco nunca é dito
    alvos = [e for e in usaveis(banco) if trechos.get(e["id"]) and (ids is None or e["id"] in ids)]
    decisoes: dict[str, dict] = {}
    faltam: list[dict] = []
    for e in alvos:
        chave = [regras, e["afirmacao"], trechos[e["id"]]]
        guardado = disk_cache.get("revisao_afirmacao", chave)
        if isinstance(guardado, dict) and guardado.get("decisao") in DECISOES_REVISAO:
            decisoes[e["id"]] = guardado
        else:
            faltam.append({"id": e["id"], "afirmacao": e["afirmacao"], "trecho": trechos[e["id"]], "chave": chave})
    _contar_cache("revisao", len(decisoes), len(faltam))
    for k in range(0, len(faltam), LOTE_REVISAO):
        lote = faltam[k:k + LOTE_REVISAO]
        for n, d in _revisar_lote(lote, topic).items():
            it = lote[n - 1]
            decisoes[it["id"]] = d
            disk_cache.put("revisao_afirmacao", it["chave"], d)
    novas = []
    cont = {"fiel": 0, "corrigir": 0, "descartar": 0}
    for e in banco["evidencias"]:
        d = decisoes.get(e["id"])
        if not d:
            novas.append(e)
            continue
        cont[d["decisao"]] += 1
        if d["decisao"] == "fiel":
            novas.append({**e, "revisada": "fiel"})
        elif d["decisao"] == "corrigir":
            nova = d["afirmacao_corrigida"]
            log.info("revisão %s: %s → %s", e["id"], e["afirmacao"], nova)
            nums = numeros(nova)
            novas.append({**e, "afirmacao_original": e["afirmacao"], "afirmacao": nova, "revisada": "corrigida",
                          "motivo_revisao": d.get("motivo", ""), "numeros": sorted(nums),
                          "tipo": "quantitativo" if nums else "qualitativo"})
        else:
            log.info("revisão %s: descartada (%s): %s", e["id"], d.get("motivo", ""), e["afirmacao"])
            novas.append({**e, "status": "nao_verificado", "status_original": e["status"], "revisada": "descartada",
                          "motivo": f"descartada na revisão da afirmação: {d.get('motivo', '')}".strip()})
    log.info("revisão das afirmações: %d revisadas de %d, %d fiéis, %d corrigidas, %d descartadas",
             sum(cont.values()), len(alvos), cont["fiel"], cont["corrigir"], cont["descartar"])
    return {**banco, "evidencias": novas}


def numeros_da_evidencia(e: dict) -> set[str]:
    """Números que a evidência sustenta: os da afirmação e os dos trechos verificados (lidos como
    fonte, com vírgula de milhar do inglês)."""
    out = set(e.get("numeros") or [])
    # 20/11: número por extenso na afirmação ("Dez trabalhadores"): a afirmação já foi julgada contra o trecho
    if any(c.get("verificado") for c in e.get("citacoes", [])):
        from .fatos import extenso_para_algarismos
        out |= numeros(extenso_para_algarismos(e.get("afirmacao", "")))
    for c in e.get("citacoes", []):
        if c.get("verificado"):
            out |= numeros_fonte(c.get("trecho", ""))
    return out


def bloco_para_escritor(banco: dict, limite_trecho: int = 220, ids: set[str] | None = None) -> str:
    """O material que o roteirista lê: cada evidência com status e o trecho que a prova.
    `ids` limita às evidências pedidas (o auditor só precisa das citadas); `limite_trecho` 0 tira o trecho."""
    linhas = []
    for e in banco.get("evidencias", []):
        if e["status"] == "nao_verificado" or (ids is not None and e["id"] not in ids):
            continue
        c = next((c for c in e["citacoes"] if c["verificado"]), None)
        rotulo = e["status"] + (", hipotese" if e.get("hipotese") else ", estimado" if e.get("estimado") else "")
        if e["status"] == "contraditorio":
            rotulo += f", diverge de {', '.join(e.get('conflitos', []))}"
        linhas.append(f"{e['id']} [{rotulo}] {e['afirmacao']}"
                      + (f"\n    trecho ({c['dominio']}): \"{c['trecho'][:limite_trecho]}\"" if c and limite_trecho else "")
                      + (_linha_de_ordem(c, e) if c and limite_trecho else ""))
    if banco.get("lacunas"):
        linhas.append("\nPERGUNTAS QUE AS FONTES NÃO RESPONDEM (não responda; não invente): "
                      + "; ".join(banco["lacunas"]))
    return "\n".join(linhas)


def _linha_de_ordem(c: dict, e: dict | None = None) -> str:
    """21/11 (E86): trecho com palavra de ordem ("first", "depois") leva junto a referência que a fonte dá,
    para quem escreve e quem audita não ler "primeiro" como primeiro de tudo. Com a conferência da ordem em código
    (src/ordem.py), a linha diz se a ordem está comprovada ou INDETERMINADA."""
    o = (e or {}).get("ordem") or {}
    if o.get("comprovada"):
        return (f"\n    ordem COMPROVADA pela fonte ({', '.join(o.get('marcas') or [])}); referência: "
                f"\"{str(o.get('referente', ''))[:160]}\"")
    if o.get("indeterminada"):
        return ("\n    ordem INDETERMINADA: a fonte não comprova em que ordem isto aconteceu em relação aos outros passos "
                f"({str(o.get('motivo', ''))[:160]}) — conte o acontecimento sem primeiro/depois/em seguida/por fim")
    ctx = c.get("contexto") or {}
    if not ctx.get("marcadores"):
        return ""
    ref = ctx.get("referencia_temporal")
    if ctx.get("contiguidade") == "nao_garantida":
        aviso = "recorte sem marca de corte: a ordem em relação ao resto da fonte não está comprovada"
    elif ctx.get("corte_antes"):
        aviso = "há texto pulado logo antes: a referência da ordem não está no material"
    else:
        aviso = f"frase anterior na fonte: \"{str(ctx.get('anterior', ''))[:160]}\""
    data = (f"; data mais próxima antes na fonte: {ref['expressao']}" if ref and not ref.get("no_trecho") else "")
    return f"\n    ordem ({', '.join(ctx['marcadores'])}): {aviso}{data}"


def minimo_para(max_minutes: float) -> int:
    """Evidências verificadas mínimas para a duração: ~2 por minuto de história (5 min -> 10)."""
    return max(5, round(max_minutes * 2))


def exigir_suficiente(banco: dict | None, minimo: int = 5) -> dict:
    """Pesquisa sem base não vira vídeo: menos de `minimo` evidências verificadas reprova com motivo."""
    if not banco:
        raise PesquisaInsuficiente("banco de evidências indisponível (extração forte não respondeu ou sem fontes)")
    n = len(usaveis(banco))
    if n < minimo:
        nv = sum(1 for e in banco.get("evidencias", []) if e["status"] == "nao_verificado")
        raise PesquisaInsuficiente(f"pesquisa insuficiente: {n} evidência(s) verificada(s) (mínimo {minimo}); "
                                   f"{nv} sem trecho provado na fonte")
    return banco
