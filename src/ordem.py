"""21/11 (E27, E28, E86): ordem cronológica só com prova, decidida em CÓDIGO.

O juiz de sustentação recebeu o aviso explícito "este texto é um RECORTE sem marca de corte: a frase anterior NÃO
comprova a que se refere 'first'" e mesmo assim aprovou "Primeiro, comandos desdobraram a aba" chamando o contexto de
"contínuo". A ordem relativa ("primeiro", "depois", "em seguida", "por fim"...) aponta para OUTRO acontecimento fora
da frase; ela só vale quando a fonte dá a mesma ordem E o acontecimento de referência está comprovadamente ao lado:
  - no próprio trecho ("X was deployed, and then Y"), ou
  - na frase anterior de um texto em que a pesquisa MARCOU toda frase pulada (contiguidade "marcada", sem corte antes).
Recorte antigo sem marca de corte, corte logo antes, ou fonte sem a palavra de ordem: a ordem fica INDETERMINADA.
Nenhuma decisão de IA muda isso. A conferência é por classe de ordem (início / seguinte / anterior / fim), não por
palavra solta, e vale para qualquer assunto.

Fora do alcance (de propósito): intervalo com unidade ("doze horas depois", "meses depois" — quantitativo, conferido
pelos números e datas das evidências) e relação entre dois acontecimentos nomeados ("depois do lançamento", "antes da
torre" — a própria afirmação traz a referência e o juiz confere contra o trecho)."""
from __future__ import annotations

import re

CLASSES = ("inicio", "seguinte", "anterior", "fim")
NOME_CLASSE = {"inicio": "primeiro", "seguinte": "depois/em seguida", "anterior": "antes disso", "fim": "por fim"}

# ------------------------------------------------------------------ ordem na afirmação / na redação (português)
_INICIO_FRASE = r"(?:^|(?<=[.;:!?—])\s*|(?<=\be)\s+|(?<=\bmas)\s+|(?<=,)\s*)"
_RELACIONAL = r"(?!\s+(?:de|do|da|dos|das|que|ao|à|aos|às)\b)"
_NUMERO = (r"\d+(?:[.,]\d+)?|uma?|dois|duas|três|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|"
           r"catorze|quatorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|"
           r"setenta|oitenta|noventa|cem|cento|mil|meia")
# intervalo COM NÚMERO ("doze horas depois", "27 minutos depois"): quantitativo, conferido pelos números da evidência.
# Sem número ("pouco depois", "meses depois", "dias depois") é ordem relativa como "depois" e passa pela trava.
_UNIDADE_ANTES = re.compile(rf"\b(?:{_NUMERO})\s+(?:e\s+(?:{_NUMERO})\s+)?(?:segundos?|minutos?|horas?|dias?|semanas?|"
                            r"meses|mês|anos?|décadas?|séculos?)\s*$", re.I)
# forma verbal conjugada ou particípio imediatamente antes (heurística: terminações de verbo em português)
_VERBO = r"\b[^\W\d_]{2,}(?:ad[oa]s?|id[oa]s?|ou|eu|iu|aram|eram|iram|ava|avam|ia|iam|ará|erá|irá)"
# (classe, padrão, removível): removível = advérbio que dá para tirar sem mudar o acontecimento
_MARCAS_PT: list[tuple[str, re.Pattern, bool]] = [
    ("inicio", re.compile(r"\b(primeiramente|em primeiro lugar|antes de tudo|para começar)\b", re.I), True),
    ("inicio", re.compile(_INICIO_FRASE + r"(primeiro)(?=\s*,|\s+(?:o|a|os|as|um|uma)\b)", re.I), True),
    ("inicio", re.compile(r"\s(primeiro)(?=\s*[.;!?]|\s*$)", re.I), True),
    ("seguinte", re.compile(r"\b(depois disso|após isso|depois de tudo isso|a partir daí|a partir de então|"
                            r"logo em seguida|em seguida|na sequência|posteriormente|pouco depois|instantes depois|"
                            r"(?:dias|semanas|meses|anos) depois|tempos depois)\b" + _RELACIONAL, re.I), True),
    ("seguinte", re.compile(_INICIO_FRASE + r"(depois|a seguir|mais tarde|logo depois|logo após|então depois)\b"
                            + _RELACIONAL, re.I), True),
    ("seguinte", re.compile(r"\s(depois|mais tarde)(?=\s*[,.;!?]|\s*$)", re.I), True),
    # advérbio logo depois do verbo ("foi implantada primeiro para", "abriu depois", "chegou antes"): E86
    ("inicio", re.compile(_VERBO + r"\s+(primeiro)\b(?!\s+(?:de|do|da|dos|das)\b)", re.I), True),
    ("seguinte", re.compile(_VERBO + r"\s+(depois|mais tarde)\b" + _RELACIONAL + r"(?!\s+(?:disso|isso)\b)", re.I), True),
    ("anterior", re.compile(_VERBO + r"\s+(antes)\b" + _RELACIONAL + r"(?!\s+(?:disso|isso)\b)", re.I), True),
    ("fim", re.compile(_VERBO + r"\s+(por último|no fim)\b" + _RELACIONAL, re.I), True),
    ("anterior", re.compile(r"\b(antes disso|antes de tudo isso|anteriormente|previamente)\b", re.I), True),
    ("anterior", re.compile(_INICIO_FRASE + r"(antes)(?=\s*,)", re.I), True),
    ("fim", re.compile(r"\b(por fim|por último|finalmente)\b", re.I), True),
    # a ordem É o fato ("o primeiro passo foi..."): não dá para tirar — sem prova, a afirmação fica bloqueada
    ("inicio", re.compile(r"\b((?:o|a)\s+primeir[oa]\s+(?:passo|etapa|fase|ação|tarefa|operação|movimento|coisa|"
                          r"procedimento|comando)s?)\b", re.I), False),
    ("seguinte", re.compile(r"\b((?:o|a)\s+(?:segund[oa]|terceir[oa]|próxim[oa])\s+(?:passo|etapa|fase|ação|tarefa|"
                            r"operação|movimento|procedimento|comando)s?|(?:passo|etapa|fase)\s+seguinte)\b", re.I), False),
    ("fim", re.compile(r"\b((?:o|a)\s+(?:últim[oa]|derradeir[oa])\s+(?:passo|etapa|fase|ação|tarefa|operação|"
                       r"movimento|procedimento|comando)s?|(?:passo|etapa|fase)\s+final)\b", re.I), False),
]


def marcas_de_ordem(texto: str) -> list[dict]:
    """Marcas de ordem relativa no texto em português: [{classe, texto, ini, fim, removivel}]."""
    texto = str(texto or "")
    achadas: list[dict] = []
    for classe, padrao, removivel in _MARCAS_PT:
        for m in padrao.finditer(texto):
            ini, fim = m.span(1)
            if _UNIDADE_ANTES.search(texto[:ini]):  # "doze horas depois", "pouco antes": intervalo, outra conferência
                continue
            if any(not (fim <= a["ini"] or ini >= a["fim"]) for a in achadas):
                continue
            achadas.append({"classe": classe, "texto": m.group(1), "ini": ini, "fim": fim, "removivel": removivel})
    return sorted(achadas, key=lambda a: a["ini"])


def sem_ordem(texto: str, marcas: list[dict] | None = None) -> str | None:
    """O texto sem as marcas de ordem removíveis (o acontecimento fica, a ordem sai). None se alguma marca é a
    própria ordem (não removível) ou se não sobra frase."""
    marcas = marcas_de_ordem(texto) if marcas is None else marcas
    if any(not m["removivel"] for m in marcas):
        return None
    if len({m["classe"] for m in marcas}) >= 2:  # "X abriu antes, e Y depois": a ordem entre os dois É o fato
        return None
    out = str(texto)
    for m in sorted(marcas, key=lambda a: a["ini"], reverse=True):
        antes, depois = out[:m["ini"]], out[m["fim"]:]
        depois = re.sub(r"^\s*,\s*", " ", depois) if not antes.rstrip().endswith(",") else re.sub(r"^\s*,", "", depois)
        antes = re.sub(r",\s*$", "", antes) if re.match(r"\s*[.;!?]", depois) else antes
        out = (antes.rstrip() + " " + depois.lstrip()).strip() if antes.strip() else depois.lstrip()
    out = re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"\s{2,}", " ", out)).strip(" ,")
    out = re.sub(r"^(e|mas)\s+", "", out, flags=re.I)
    if len(re.findall(r"\w+", out)) < 3:
        return None
    out = out[0].upper() + out[1:]
    return out


# ------------------------------------------------------------------ ordem na fonte (qualquer idioma)
_MARCAS_FONTE: list[tuple[str, re.Pattern]] = [
    ("inicio", re.compile(r"(?:^|[.;:!?]\s+|\band\s+)(first(?:ly)?|initially|at first|to begin(?: with)?|primeiro|"
                          r"primeiramente|inicialmente|primero|en primer lugar|al principio)\b", re.I)),
    ("inicio", re.compile(r"\b(first)(?=\s+\w+(?:ed|ing)\b)", re.I)),
    ("inicio", re.compile(r"\b(the first (?:step|stage|phase|action|task|operation|move|command)s?|"
                          r"(?:o|a) primeir[oa] (?:passo|etapa|fase))\b", re.I)),
    ("seguinte", re.compile(r"\b(then|next|afterwards?|subsequently|later|after (?:that|this)|following (?:that|this)|"
                            r"thereafter|depois|em seguida|a seguir|posteriormente|mais tarde|luego|después|"
                            r"a continuación|posteriormente|the next (?:step|stage|phase|day))\b", re.I)),
    ("anterior", re.compile(r"\b(before (?:that|this)|previously|earlier|prior to (?:that|this)|antes disso|"
                            r"anteriormente|antes de eso)\b", re.I)),
    ("fim", re.compile(r"\b(finally|lastly|at last|eventually|in the end|the (?:final|last) (?:step|stage|phase)|"
                       r"por fim|por último|finalmente|al final)\b", re.I)),
]


def marcas_da_fonte(trecho: str) -> list[tuple[str, int, str]]:
    """(classe, posição, texto) das palavras de ordem no trecho literal da fonte."""
    out = []
    for classe, padrao in _MARCAS_FONTE:
        out += [(classe, m.start(1), m.group(1)) for m in padrao.finditer(str(trecho or ""))]
    return out


def _palavras(texto: str) -> int:
    return len(re.findall(r"[^\W\d_]{2,}", texto))


def avaliar_citacao(afirmacao: str, trecho: str, contexto: dict | None) -> dict:
    """A ordem que a afirmação diz é comprovada por ESTA citação? {exige, comprovada, classes, motivo, referente}.
    `contexto` é o de contexto_da_citacao (sem ele, a vizinhança é desconhecida = não garantida)."""
    marcas = marcas_de_ordem(afirmacao)
    if not marcas:
        return {"exige": False}
    ctx = contexto or {}
    fonte = marcas_da_fonte(trecho)
    classes = sorted({m["classe"] for m in marcas})
    motivos, referente = [], ""
    for classe in classes:
        mesmas = [(p, t) for c, p, t in fonte if c == classe]
        if not mesmas:
            motivos.append(f"a fonte não diz \"{NOME_CLASSE[classe]}\" neste trecho: a ordem foi acrescentada")
            continue
        # a referência dentro do próprio trecho: há um acontecimento antes da palavra de ordem
        if any(_palavras(str(trecho)[:p]) >= 3 for p, _ in mesmas):
            referente = referente or "no próprio trecho"
            continue
        if ctx.get("contiguidade") != "marcada":
            motivos.append(f"\"{mesmas[0][1]}\" refere-se a um acontecimento anterior, e este texto é um recorte sem "
                           "marca de corte: a frase vizinha pode não ser a vizinha real na fonte")
        elif ctx.get("corte_antes") or not str(ctx.get("anterior", "")).strip():
            motivos.append(f"\"{mesmas[0][1]}\" refere-se a um acontecimento anterior que não está no material "
                           "(texto pulado logo antes)")
        else:
            referente = referente or str(ctx["anterior"])[:300]
    return {"exige": True, "comprovada": not motivos, "classes": classes, "motivo": "; ".join(motivos),
            "referente": referente if not motivos else "", "marcas": [m["texto"] for m in marcas]}


def travar_ordem(evidencias: list[dict], log=None) -> dict:
    """Depois do juiz (e das decisões do cache): a ordem de cada evidência é conferida em código.
    - comprovada por uma citação verificada: e["ordem"] = {comprovada: True, classes, referente};
    - não comprovada e removível: a afirmação perde só a ordem (o acontecimento fica, verificado), a versão com a
      ordem vai para "afirmacao_com_ordem" e e["ordem"]["indeterminada"] = True;
    - não comprovada e a ordem é o próprio fato ("o primeiro passo foi"): citações bloqueadas (verificado False,
      decisão "ordem_nao_comprovada" por código, com a decisão da IA guardada ao lado).
    Devolve a contagem {comprovadas, reformuladas, bloqueadas, ia_discordou}."""
    cont = {"comprovadas": 0, "reformuladas": 0, "bloqueadas": 0, "ia_discordou": 0}
    for e in evidencias:
        marcas = marcas_de_ordem(e.get("afirmacao", ""))
        if not marcas:
            continue
        verificadas = [c for c in e.get("citacoes") or [] if c.get("verificado")]
        if not verificadas:
            continue
        avaliacoes = [(c, avaliar_citacao(e["afirmacao"], c.get("trecho", ""), c.get("contexto"))) for c in verificadas]
        boas = [a for _, a in avaliacoes if a.get("comprovada")]
        if boas:
            e["ordem"] = {"comprovada": True, "classes": boas[0]["classes"], "referente": boas[0]["referente"],
                          "marcas": boas[0]["marcas"]}
            cont["comprovadas"] += 1
            continue
        motivo = avaliacoes[0][1]["motivo"]
        for c, a in avaliacoes:
            s = c.get("sustentacao") or {}
            c["ordem"] = {"comprovada": False, "motivo": a["motivo"], "decisao_ia": s.get("decisao")}
            if s.get("por") == "ia" and s.get("decisao") == "sustenta":
                c["ordem"]["ia_discordou"] = True
        cont["ia_discordou"] += any(c["ordem"].get("ia_discordou") for c, _ in avaliacoes)
        nova = sem_ordem(e["afirmacao"], marcas)
        if nova:
            e["afirmacao_com_ordem"] = e["afirmacao"]
            e["afirmacao"] = nova
            e["ordem"] = {"comprovada": False, "indeterminada": True, "removido": [m["texto"] for m in marcas],
                          "motivo": motivo}
            cont["reformuladas"] += 1
            if log:
                log.info("ordem não comprovada, afirmação reformulada sem ordem: %r -> %r (%s)",
                         e["afirmacao_com_ordem"][:70], nova[:70], motivo[:120])
        else:
            for c, a in avaliacoes:
                anterior = c.get("sustentacao")
                c.update(verificado=False, sustentacao={"decisao": "ordem_nao_comprovada", "por": "codigo",
                                                        "justificativa": a["motivo"] or motivo,
                                                        "decisao_anterior": anterior})
            e["ordem"] = {"comprovada": False, "bloqueada": True, "marcas": [m["texto"] for m in marcas],
                          "motivo": motivo}
            cont["bloqueadas"] += 1
            if log:
                log.warning("ordem é o fato e não está comprovada — evidência bloqueada: %r (%s)",
                            e["afirmacao"][:70], motivo[:120])
    return cont


def ordem_da_frase(texto: str, evidencias_citadas: list[dict]) -> str | None:
    """Conferência da REDAÇÃO: a frase afirma uma ordem relativa que nenhuma evidência citada comprova?
    Devolve o motivo ou None. Frase com data/intervalo explícito ancora a ordem (conferida pelos números)."""
    marcas = marcas_de_ordem(texto)
    if not marcas:
        return None
    provadas = set()
    for e in evidencias_citadas:
        o = e.get("ordem") or {}
        if o.get("comprovada"):
            provadas |= set(o.get("classes") or [])
    faltam = [m for m in marcas if m["classe"] not in provadas]
    if not faltam:
        return None
    if _DATA_NA_FRASE.search(texto):
        return None
    ditas = ", ".join(f"\"{m['texto']}\"" for m in faltam)
    return (f"{ditas} afirma uma ordem entre acontecimentos que nenhuma evidência citada comprova (a fonte não dá "
            "essa sequência ou o recorte não garante a vizinhança): diga o acontecimento sem afirmar ordem — sem "
            "primeiro/depois/em seguida/por fim — ou ancore com a data que a evidência traz; se a ordem for o fato, "
            "não use a frase")


_DATA_NA_FRASE = re.compile(r"\b(\d{1,2}\s+de\s+[a-zç]+(?:\s+de\s+\d{4})?|(?:em|no|na)\s+(?:1[5-9]|20)\d{2}|"
                            r"\d+\s+(?:segundos?|minutos?|horas?|dias?|semanas?|meses|anos?)\s+(?:após|depois|antes))\b",
                            re.I)
