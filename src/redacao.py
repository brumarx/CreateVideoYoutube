"""Redação do Fractal (modo de roteiro "redacao", 19/11): o roteirista lê o banco de evidências
(src/evidencias.py) e conta a história com liberdade; a auditoria confere cada afirmação contra
as evidências, sem confundir construção narrativa com erro factual.

Regra: liberdade para contar a história, nenhuma liberdade para inventar a realidade.

Contrato do roteiro (o mesmo dict de sempre, com metadados a mais):
    scenes[i]["narration"]  texto falado (o que vai para a voz, a legenda e as imagens)
    scenes[i]["frases"]     [{texto, tipo, evidencias}] — a narração frase a frase, com:
        tipo        "factual"     — afirma algo sobre o mundo (data, número, lugar, técnica...)
                    "explicativa" — relação de causa/consequência ou explicação de fenômeno
                    "narrativa"   — abertura, transição, pergunta retórica, frase de impacto
        evidencias  ["E3", "E7"] — ids do banco (obrigatório em factual e explicativa)
    script["_banco"]        o banco de evidências (fica gravado no script.json do job)
    script["_redacao"]      True

Os ids NUNCA vão para o texto falado. Toda etapa que mexe na narração depois (corte de pergunta
no fim, ortografia, encurtamento) é seguida de `sincronizar`, que realinha os metadados frase a
frase; frase nova sem metadado é auditada como factual."""
from __future__ import annotations

import difflib
import json
import logging
import math
import re
from typing import Callable

from .evidencias import FORCA, STATUS_USAVEIS, adicionar_fontes, bloco_para_escritor, numeros_da_evidencia, por_id, usaveis
from .fatos import _pares, estima_antes, extenso_para_algarismos, numeros, temperaturas
from .fontes import dobrar
from .ordem import ordem_da_frase

log = logging.getLogger("redacao")

TIPOS = ("factual", "explicativa", "narrativa")
PALAVRAS_POR_MINUTO = 135  # ritmo medido da voz (edge-tts Antonio)
NOTA_MINIMA = 7           # qualidade narrativa (0-10) abaixo disso o roteiro não sai
NOTA_ALVO = 8             # abaixo disso: uma passada de edição com o parecer (fica a melhor versão)
# tokens reservados para a resposta: com o pedido, cabe nos ~8 mil por pedido do Groq grátis
RESPOSTA_AUDITOR = 3000      # decisões de até FRASES_POR_BLOCO frases (com justificativa) + nota
# 675 palavras faladas + metadados por frase cabem em ~3 mil tokens; com o banco só com as afirmações, o
# pedido inteiro cabe no Groq e o gpt-oss-120b escreve (job 997: com 7 mil, sobrou o Llama 70B)
RESPOSTA_ESCRITOR = 4500
MAX_PERDA = 0.35          # a edição não pode tirar mais que isto das palavras (vira outro vídeo)
_FRASE = re.compile(r"(?<=[.!?])\s+")
# afirmação escondida em frase "narrativa": superlativo, absoluto, recorde
_ABSOLUTO = re.compile(r"\b(maior(es)?|menor(es)?|únic[oa]s?|unic[oa]s?|primeir[oa]s?|últim[oa]s?|recorde|"
                       r"(o|a|os|as) mais \w+|(o|a|os|as) menos \w+|jamais|nunca|sempre|todos os|todas as|"
                       r"(mais|menos) [\wÀ-ÿ]+ d[oa]s? (mundo|planeta|terra|história|historia|universo|país|pais|"
                       r"continente|europa|ásia|asia|américa|america|áfrica|africa))\b", re.I)
_CONECTIVOS = re.compile(r"^(mas|só que|so que|por isso|então|entao|e foi|agora|isso|foi aí|foi ai|ou seja|"
                         r"é por isso|acontece que|no entanto|quando|imagine|pense|só|e |só que|porque|"
                         r"para entender|o segredo|a resposta|e o|e a|aí|ai)\b", re.I)


def _obj(raw: str | None) -> dict:
    """O objeto JSON da resposta ({} se não houver)."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class RoteiroReprovado(Exception):
    """Reprovação justificada da redação (motivo na mensagem)."""


# ---------------------------------------------------------------- metadados
def frases_de(script: dict) -> list[tuple[int, int, dict]]:
    return [(i, j, f) for i, sc in enumerate(script.get("scenes", [])) for j, f in enumerate(sc.get("frases") or [])]


def _norm(texto: str) -> str:
    return " ".join(re.findall(r"\w+", dobrar(texto)))


def sincronizar(script: dict) -> dict:
    """Realinha `frases` com a `narration` atual de cada cena: frase igual (ou quase igual, ex.:
    acento corrigido) herda tipo e evidências; frase nova fica "factual" sem evidência e a
    auditoria cobra (ou é narrativa pura, que não precisa)."""
    cenas = []
    for sc in script.get("scenes", []):
        antigas = list(sc.get("frases") or [])
        novas = []
        for texto in [t.strip() for t in _FRASE.split(str(sc.get("narration", ""))) if t.strip()]:
            n = _norm(texto)
            par = next((f for f in antigas if _norm(f.get("texto", "")) == n), None)
            if par is None and antigas:
                melhor = max(antigas, key=lambda f: difflib.SequenceMatcher(None, _norm(f.get("texto", "")), n).ratio())
                if difflib.SequenceMatcher(None, _norm(melhor.get("texto", "")), n).ratio() >= 0.75:
                    par = melhor
            if par is not None:
                novas.append({**par, "texto": texto})
            else:
                novas.append({"texto": texto, "tipo": _tipo_sem_metadado(texto), "evidencias": []})
        cenas.append({**sc, "frases": novas})
    return {**script, "scenes": cenas}


def _tipo_sem_metadado(texto: str) -> str:
    return "factual" if afirmacao_escondida(texto, set()) else "narrativa"


def afirmacao_escondida(texto: str, nomes_conhecidos: set[str]) -> str | None:
    """Motivo se a frase dita "narrativa" afirma algo verificável: número (algarismo ou por
    extenso), superlativo/absoluto, ou nome próprio. None se é construção narrativa pura
    ("Mas viver ali não é nada simples.")."""
    t = extenso_para_algarismos(texto)
    if numeros(t):
        return "tem número"
    m = _ABSOLUTO.search(texto)
    if m:
        return f"afirmação absoluta ({m.group(0)!r})"
    palavras = re.findall(r"\b[A-ZÀ-Ý][\wÀ-ÿ-]+", texto)
    proprios = [w for w in palavras[1:] if dobrar(w) not in nomes_conhecidos]
    if proprios:
        return f"nome próprio ({proprios[0]})"
    return None


def _nomes_conhecidos(script: dict, topic: str) -> set[str]:
    """Nomes que não precisam de evidência numa frase narrativa: os do tema, o nome do canal no
    bordão e os que já aparecem em frase factual sustentada do próprio roteiro."""
    base = " ".join([topic] + [f["texto"] for _, _, f in frases_de(script) if f.get("tipo") != "narrativa"
                               and f.get("evidencias")])
    return {dobrar(w) for w in re.findall(r"[\wÀ-ÿ-]+", base)} | {"e", "ai", "mente"}


# ---------------------------------------------------------------- auditoria em código
def auditar_codigo(script: dict, banco: dict, topic: str = "") -> tuple[dict, list[dict]]:
    """Conferência determinística, frase a frase. Conserta sozinha o que é seguro (liga número à
    evidência que o tem; marca estimativa) e devolve (roteiro, problemas). Problema:
    {tipo, termo (a frase), motivo, cena, frase}."""
    ev = por_id(banco)
    nomes = _nomes_conhecidos(script, topic)
    problemas: list[dict] = []
    cenas = []
    for i, sc in enumerate(script.get("scenes", [])):
        frases = []
        for j, f in enumerate(sc.get("frases") or []):
            f = {**f, "evidencias": [x for x in f.get("evidencias") or [] if isinstance(x, str)]}
            texto = f["texto"]
            nums = numeros(extenso_para_algarismos(texto))
            if f.get("tipo") == "narrativa":
                motivo = afirmacao_escondida(texto, nomes)
                if motivo:
                    f["tipo"] = "factual"
                    f["reclassificada"] = motivo
            if f.get("tipo") not in TIPOS:
                f["tipo"] = "factual"
            if f["tipo"] != "narrativa":
                citadas = [ev[x] for x in f["evidencias"] if x in ev]
                validas = [e for e in citadas if e["status"] in STATUS_USAVEIS]
                contra = [e for e in citadas if e["status"] == "contraditorio"]
                if nums:
                    cobertos = set().union(*(numeros_da_evidencia(e) for e in validas), set())
                    faltam = nums - cobertos
                    if faltam and _APROX.search(texto):
                        # 19/11: arredondado para a fala ("cerca de 300 mil" para 303.863): vale se uma
                        # evidência verificada tem o valor a até 5% e a frase diz que é aproximado
                        casados = set()
                        for n in sorted(faltam):
                            dona = _perto(n, validas) or _perto(n, usaveis(banco))
                            if dona:
                                casados.add(n)
                                if dona["id"] not in f["evidencias"]:
                                    f["evidencias"].append(dona["id"])
                                    validas.append(dona)
                        # "300 mil" é lido como 300 e 300000: casado o valor com escala, o 300 sem escala também
                        faltam -= casados | {x for x in faltam if _escala_de(x, casados)}
                    if faltam:
                        # o número existe em outra evidência verificada: liga a frase a ela — 20/11: só se for a
                        # MESMA medida (unidade e sinal) e se a evidência citada não for a divergente daquele valor
                        # ("8 graus negativos" citando a E34 contraditória era ligado a outra evidência com um 8)
                        for n in sorted(faltam):
                            if any(n in numeros_da_evidencia(e) for e in contra):
                                continue
                            dona = next((e for e in usaveis(banco) if n in numeros_da_evidencia(e)
                                         and _mesma_medida(n, texto, e)), None)
                            if dona:
                                validas.append(dona)
                                f["evidencias"].append(dona["id"])
                        faltam = nums - set().union(*(numeros_da_evidencia(e) for e in validas), set())
                    arredondados: list[tuple[str, dict, float]] = []
                    if faltam:
                        # 20/11 (job 998): "64 graus negativos" para o recorde de −64,4 °C. Arredondar valor com
                        # casa decimal vale só com a aproximação dita e no sentido certo ("mais de 64");
                        # sem isso é número que a evidência não traz — e o motivo diz qual é o valor dela
                        for n in sorted(faltam):
                            par = _arredonda(n, validas) or _arredonda(
                                n, [e for e in usaveis(banco) if _mesma_medida(n, texto, e, arredondado=True)])
                            if not par:
                                continue
                            dona, valor = par
                            if _aproximacao_coerente(texto, n, valor):
                                faltam = faltam - {n}
                                if dona["id"] not in f["evidencias"]:
                                    f["evidencias"].append(dona["id"])
                                    validas.append(dona)
                            else:
                                arredondados.append((n, dona, valor))
                        for n, dona, valor in arredondados:
                            faltam = faltam - {n}
                            problemas.append(_p("numero_arredondado", texto, i, j, _motivo_arredondado(n, dona, valor)))
                    if faltam:
                        # 20/11: "1,84 km" para 1.840 m e "anos 1880" para 1882 são o mesmo fato em outra forma
                        for n in sorted(faltam):
                            dona = _outra_forma(n, texto, validas) or _outra_forma(n, texto, usaveis(banco))
                            if dona:
                                faltam = faltam - {n}
                                if dona["id"] not in f["evidencias"]:
                                    f["evidencias"].append(dona["id"])
                                    validas.append(dona)
                    if faltam:
                        so_contra = {n for n in faltam if any(n in numeros_da_evidencia(e) for e in contra)
                                     or any(n in numeros_da_evidencia(x) for x in ev.values()
                                            if x["status"] == "contraditorio")}
                        if so_contra and not _apresenta_divergencia(texto, so_contra, ev):
                            problemas.append(_p("numero_contraditorio", texto, i, j,
                                                f"o valor {', '.join(sorted(so_contra))} diverge entre as fontes; "
                                                "diga os dois valores e que as fontes divergem, ou não use"))
                        resto = faltam - so_contra
                        if resto:
                            problemas.append(_p("numero_sem_evidencia", texto, i, j,
                                                f"número {', '.join(sorted(resto))} não está em nenhuma evidência verificada: "
                                                "tire o número; se uma evidência sustenta a ideia sem número, diga-a "
                                                "assim; não ponha outro número no lugar"))
                    elif not arredondados:
                        texto = _marca_estimativa(texto, validas, nums)
                        f["texto"] = texto
                if validas and not any(p["cena"] == i and p["frase"] == j for p in problemas):
                    sinal = _sinal_invertido(texto, validas)
                    if sinal:
                        problemas.append(_p("sinal_invertido", texto, i, j, sinal))
                    elif all(e.get("hipotese") for e in validas) and not _HEDGE.search(texto):
                        hip = next(e for e in validas if e.get("hipotese"))
                        problemas.append(_p("hipotese_como_fato", texto, i, j,
                                            f"a evidência {hip['id']} é uma hipótese da fonte, não um fato: diga como "
                                            "hipótese (\"acredita-se\", \"uma das explicações\", \"pode\") sem afirmar"))
                    elif _falta_atribuicao(texto, validas):
                        problemas.append(_p("fonte_unica_sem_atribuicao", texto, i, j,
                                            "recorde/extremo de uma fonte só dito como consenso: diga de onde vem "
                                            "(\"segundo os registros\", \"a menor temperatura já registrada\", \"de acordo "
                                            "com ...\") sem mudar o valor"))
                if validas:
                    f["confianca"] = max((e["status"] for e in validas), key=lambda st: FORCA.get(st, -1))
                if not validas and not (nums and contra and _apresenta_divergencia(texto, nums, ev)):
                    if not any(p["cena"] == i and p["frase"] == j for p in problemas):
                        motivo = ("cita evidência não verificada" if any(e["status"] == "nao_verificado" for e in citadas)
                                  else f"frase {f['tipo']} sem evidência" + (f" ({f['reclassificada']})" if f.get("reclassificada") else ""))
                        problemas.append(_p("sem_evidencia", texto, i, j, motivo))
                f["evidencias"] = list(dict.fromkeys(f["evidencias"]))
            # 21/11 (E27): ordem relativa só com evidência citada que a comprova (vale para toda frase, inclusive a
            # narrativa — "Depois, veio o maior desafio" também afirma uma sequência)
            if not any(p["cena"] == i and p["frase"] == j for p in problemas):
                provas = [ev[x] for x in f["evidencias"] if x in ev and ev[x]["status"] in STATUS_USAVEIS]
                motivo_ordem = ordem_da_frase(f["texto"], provas)
                if motivo_ordem:
                    problemas.append(_p("ordem_nao_comprovada", f["texto"], i, j, motivo_ordem))
            frases.append(f)
        cenas.append({**sc, "frases": frases, "narration": " ".join(x["texto"] for x in frases)})
    return {**script, "scenes": cenas}, problemas


_APROX = re.compile(r"\b(cerca de|aproximadamente|quase|mais de|menos de|em torno de|por volta de|perto de|uns|umas)\b", re.I)


def _escala_de(x: str, valores: set[str]) -> bool:
    try:
        v = float(x)
        return any(abs(float(y) - v * esc) < 1e-6 * max(1.0, float(y)) for y in valores for esc in (1e3, 1e6, 1e9))
    except ValueError:
        return False


def _perto(n: str, evidencias: list[dict]) -> dict | None:
    """Evidência com um valor a até 5% de `n` (n >= 100: arredondamento de valor grande, não de dígito)."""
    try:
        v = float(n)
    except ValueError:
        return None
    if v < 100:
        return None
    for e in evidencias:
        for x in numeros_da_evidencia(e):
            try:
                if abs(float(x) - v) <= 0.05 * float(x):
                    return e
            except ValueError:
                continue
    return None


def _medidas(texto: str) -> set[tuple[str, str]]:
    return _pares(extenso_para_algarismos(texto))


def _mesma_medida(n: str, texto: str, e: dict, arredondado: bool = False) -> bool:
    """A evidência traz o valor `n` na mesma unidade (e sinal, para temperatura) em que a frase o diz.
    Número sem unidade na frase: só vale para quem não tem unidade conhecida no valor (data, contagem)
    — e nunca para o arredondamento, que sem unidade casaria qualquer valor parecido."""
    da_frase = {(v, u) for v, u in _medidas(texto) if v.lstrip("-") == n}
    fontes = [e["afirmacao"]] + [c.get("trecho", "") for c in e.get("citacoes", []) if c.get("verificado")]
    da_ev = set().union(*(_medidas(t) for t in fontes))
    if not da_frase:
        return not arredondado and not any(v.lstrip("-") == n for v, _ in da_ev)
    for v, u in da_frase:
        for ve, ue in da_ev:
            if ue != u:
                continue
            try:
                a, b = float(v), float(ve)
            except ValueError:
                continue
            if (a < 0) != (b < 0) and a != 0 and b != 0:
                continue
            if abs(a) == abs(b) or (arredondado and int(abs(a)) in (math.floor(abs(b)), math.ceil(abs(b)))):
                return True
    return False


_CONVERSOES = {("km", "m"): 1000.0, ("m", "km"): 0.001}


def _outra_forma(n: str, texto: str, evidencias: list[dict]) -> dict | None:
    """Evidência que traz o MESMO fato em outra forma: a medida em outra unidade, arredondada como na fala
    ("1,84 km" = 1.840 m; "1,67 km" = 1.669 m), ou a década ("anos 1880", "década de 1880") que contém um
    ano da evidência. Nada de número novo: o valor tem de sair da evidência por conta exata."""
    t = extenso_para_algarismos(texto)
    decada = re.search(r"\b(?:anos|d[ée]cada de)\s+(" + re.escape(n) + r")\b", t, re.I)
    if decada and re.fullmatch(r"1\d{2}0|20\d0|[1-9]0", n):
        # "anos 1880" ou "anos 60" (a década de qualquer século que a evidência trouxer)
        for e in evidencias:
            for x in numeros_da_evidencia(e):
                if re.fullmatch(r"1\d{3}|20\d{2}", x) and (int(n) <= int(x) <= int(n) + 9 if len(n) == 4
                                                              else int(x) % 100 // 10 * 10 == int(n)):
                    return e
    da_frase = [(float(v), u) for v, u in _medidas(texto) if v.lstrip("-") == n]
    for v, u in da_frase:
        casas = len(n.split(".")[1]) if "." in n else 0
        for e in evidencias:
            fontes = [e["afirmacao"]] + [c.get("trecho", "") for c in e.get("citacoes", []) if c.get("verificado")]
            for ve, ue in set().union(*(_medidas(x) for x in fontes)):
                fator = _CONVERSOES.get((ue, u))
                if fator and round(float(ve) * fator, casas) == round(v, casas):
                    return e
    return None


def _arredonda(n: str, evidencias: list[dict]) -> tuple[dict, float] | None:
    """(evidência, valor) quando `n` (inteiro) é o arredondamento, para cima ou para baixo, de um valor
    com casa decimal que a evidência traz: 64 para −64,4 (o sinal não entra: números são lidos sem ele)."""
    if not re.fullmatch(r"\d+", n):
        return None
    alvo = int(n)
    for e in evidencias:
        for x in sorted(numeros_da_evidencia(e)):
            if "." not in x:
                continue
            try:
                v = abs(float(x))
            except ValueError:
                continue
            if alvo in (math.floor(v), math.ceil(v)):
                return e, v
    return None


# 20/11: três coisas diferentes antes de um número — não confundir.
#   sinal:        "menos 64", "−64" (e "64 negativos/abaixo de zero", lido em fatos.temperaturas)
#   aproximação:  "cerca de 64"           -> o valor real está a menos de 1 do dito
#   comparação:   "mais de 64", "menos de 65", "abaixo de 65", "quase 65" -> desigualdade verdadeira e
#                 justa (o limite é o inteiro vizinho do valor real), nunca um número qualquer
_APROXIMACAO = ("cerca de", "aproximadamente", "em torno de", "por volta de", "perto de", "uns", "umas", "na casa de")
_MAIOR_QUE = ("pouco mais de", "mais de", "acima de", "além de", "superior a", "passou de", "passa de", "ultrapassou")
_MENOR_QUE = ("pouco menos de", "menos de", "abaixo de", "inferior a", "quase")
_MARCA_ANTES = re.compile(r"\b(" + "|".join(sorted(_APROXIMACAO + _MAIOR_QUE + _MENOR_QUE, key=len, reverse=True))
                          + r")\s+((?:[−–-]|menos\s+)\s*)?$", re.I)


def relacao_do_numero(t: str, ini: int) -> tuple[str, str]:
    """Que relação a frase afirma com o número que começa em `ini` (texto já com algarismos):
    ("=", "") valor dito seco; ("≈", palavra) aproximação; (">", palavra) / ("<", palavra) comparação;
    ("?", palavra) comparação com sinal explícito no meio ("mais de −64": ambígua, não vale)."""
    m = _MARCA_ANTES.search(t[:ini])
    if not m:
        return "=", ""
    palavra = re.sub(r"\s+", " ", m.group(1).lower())
    if palavra in _APROXIMACAO:
        return "≈", palavra
    if m.group(2):
        return "?", palavra
    return (">" if palavra in _MAIOR_QUE else "<"), palavra


def _aproximacao_coerente(texto: str, n: str, valor: float) -> bool:
    """`n` (inteiro) dito no lugar de `valor` (grandeza da evidência, com decimal) vale só com a relação
    DITA e verdadeira: aproximação a menos de 1; comparação no sentido certo e com o inteiro vizinho
    ("mais de 64" e "menos de 65" para 64,4). Valor seco, comparação falsa ou folgada, não.
    O sinal é conferido à parte (_sinal_invertido): "menos de 65 graus" não é −65."""
    t = extenso_para_algarismos(texto)
    for m in re.finditer(r"\d+(?:[.,]\d+)*", t):
        if m.group() != n:
            continue
        rel, palavra = relacao_do_numero(t, m.start())
        k = int(n)
        if rel == "<" and palavra != "quase" and any(v == "-" + n for v, _ in temperaturas(t)):
            # 20/11: "menos de 65 graus abaixo de zero" para −64,4 °C é verdade na matemática e confuso para
            # quem ouve (menos frio? menos de 65?): limite superior de temperatura negativa não descreve o valor
            continue
        if rel == "≈" and abs(valor - k) < 1:
            return True
        if rel == ">" and k < valor < k + 1:
            return True
        if rel == "<" and k - 1 < valor < k:
            return True
    return False


def _motivo_arredondado(n: str, dona: dict, valor: float) -> str:
    exato = format(valor, "g").replace(".", ",")
    # 20/11: sugere o que soa claro (valor exato ou "aproximadamente" com o inteiro mais próximo), não comparação
    return (f"o número {n} arredonda o valor {exato} da evidência {dona['id']} (\"{dona['afirmacao'][:110]}\"): "
            f"diga o valor exato ({exato}) ou \"aproximadamente {round(valor)}\"; não troque por outro número")


def _p(tipo: str, frase: str, cena: int, idx: int, motivo: str) -> dict:
    return {"tipo": tipo, "termo": frase, "motivo": motivo, "cena": cena, "frase": idx}


def _apresenta_divergencia(texto: str, nums: set[str], ev: dict[str, dict]) -> bool:
    """A frase traz os dois lados de um conflito (o valor e o da evidência divergente)."""
    nums_frase = numeros(extenso_para_algarismos(texto))
    for e in ev.values():
        if e["status"] != "contraditorio" and not e.get("conflitos"):
            continue
        if not nums & numeros_da_evidencia(e):
            continue
        for outro in e.get("conflitos", []):
            if outro in ev and nums_frase & numeros_da_evidencia(ev[outro]) - numeros_da_evidencia(e):
                return True
    return False


def _marca_estimativa(texto: str, validas: list[dict], nums: set[str]) -> str:
    """Valor que a FONTE dá como estimativa ganha "cerca de"/"estima-se". 20/11: precisão e
    corroboração são coisas diferentes — valor exato de fonte única (o recorde medido de −64,4 °C)
    fica exato; a limitação de fonte única vai na atribuição (_falta_atribuicao) e em "confianca"."""
    alvo = set()
    for e in validas:
        if e.get("estimado") and not e.get("hipotese"):  # hipótese se diz como hipótese, não com "cerca de"
            alvo |= nums & set(e.get("numeros") or [])
    if alvo and not any(not e.get("estimado") and alvo <= set(e.get("numeros") or []) for e in validas):
        return estima_antes(texto, alvo)
    return texto


_EXTREMO = re.compile(r"\b(recordes?|(?:maior|menor|maiores|menores) do (?:mundo|planeta)|mais \w+ do (?:mundo|planeta)|"
                      r"mais baix[ao]s? (?:já )?registrad|mais alt[ao]s? (?:já )?registrad|únic[oa]s? (?:no|do) mundo)", re.I)
_ATRIBUICAO = re.compile(r"\b(segundo|de acordo com|conforme|pelos registros|nos registros|os registros|"
                         r"registrad[oa]s?|registrou|medid[oa]s?|mediu|apontam?|indicam?)\b", re.I)


_HEDGE = re.compile(r"\b(acredita-se|acreditam|acredita|hip[óo]tese|teoria|suspeita-se|suspeitam|pode|podem|poderia|"
                    r"poderiam|talvez|provavelmente|sugere|sugerem|prop[õo]e|prop[õo]em|propuseram|especula\w*|"
                    r"sup[õo]e-se|seria|seriam|defende|defendem|explica[çc][ãa]o (?:proposta|possível)|uma das explica[çc][õo]es)\b",
                    re.I)


def _falta_atribuicao(texto: str, validas: list[dict]) -> bool:
    """Recorde, extremo ou superlativo sustentado só por fonte única e dito sem dizer de onde vem:
    seria apresentar como consenso o que tem uma fonte só."""
    return (bool(validas) and all(e["status"] == "fonte_unica" for e in validas)
            and bool(_EXTREMO.search(texto)) and not _ATRIBUICAO.search(texto))


def _temps_da_evidencia(e: dict) -> set[tuple[str, str]]:
    out = set(temperaturas(e["afirmacao"]))
    for c in e.get("citacoes", []):
        if c.get("verificado"):
            out |= temperaturas(c.get("trecho", ""))
    return out


def _sinal_invertido(texto: str, validas: list[dict]) -> str | None:
    """20/11: temperatura com o sinal trocado — a evidência diz −64,4 °C e a frase diz "64,4 °C" ou
    "mais de 64 °C" (sem "negativos", "abaixo de zero", "menos" ou "−"). Compara pela grandeza (valor
    exato ou arredondamento de menos de 1 grau) na mesma escala. Motivo ou None."""
    for valor, escala in temperaturas(extenso_para_algarismos(texto)):
        v = float(valor)
        for e in validas:
            for ve, ee in _temps_da_evidencia(e):
                x = float(ve)
                if ee != escala or abs(abs(x) - abs(v)) >= 1 or x == 0 or v == 0:
                    continue
                if (x < 0) == (v < 0):
                    break  # sinal certo para esta grandeza
                comparacao = re.search(r"\b(menos de|abaixo de)\s+" + re.escape(valor.lstrip("-").replace(".", ",")),
                                       extenso_para_algarismos(texto), re.I)
                aviso = (f" (\"{comparacao.group(1)} {valor.lstrip('-')}\" é comparação, não sinal negativo)"
                         if comparacao else "")
                return (f"sinal trocado{aviso}: a evidência {e['id']} diz {ve.replace('.', ',')} {ee} e a frase diz "
                        f"{valor.replace('.', ',')} {escala}; diga \"negativos\", \"abaixo de zero\" ou o sinal de menos "
                        "— nunca inverta o sinal nem troque o número")
            else:
                continue
            break
    return None


# ---------------------------------------------------------------- qualidade (código)
def parece_lista(script: dict) -> str | None:
    """Roteiro que enumera fatos em vez de contar: quase tudo factual e quase nenhuma ligação
    entre as frases. Motivo ou None."""
    todas = [f for _, _, f in frases_de(script)]
    if len(todas) < 6:
        return None
    factuais = [f for f in todas if f.get("tipo") == "factual"]
    ligadas = [f for f in todas if _CONECTIVOS.match(f.get("texto", "").strip()) or f.get("tipo") != "factual"]
    if len(factuais) / len(todas) >= 0.85 and len(ligadas) / len(todas) < 0.2:
        return (f"parece lista de fatos: {len(factuais)} de {len(todas)} frases são fato solto, quase sem "
                "explicação, transição ou fio narrativo")
    return None


def _palavras(script: dict) -> int:
    return sum(len(str(sc.get("narration", "")).split()) for sc in script.get("scenes", []))


# ---------------------------------------------------------------- escritor
_PROMPT_ESCRITOR = """Você é o roteirista do canal "{canal}" (YouTube Shorts, português do Brasil).
Tema: {tema}

BANCO DE EVIDÊNCIAS (pesquisa verificada: cada item tem a frase da fonte que o prova):
{banco}

Sua tarefa: ler, entender e CONTAR A MELHOR HISTÓRIA possível sobre o tema com este material.
Não é para percorrer E1, E2, E3 transformando cada item numa cena. Escolha o fato mais
surpreendente para abrir, crie curiosidade, organize uma progressão (o que é → como funciona →
por que → consequência ou virada), explique o difícil com palavras simples, use comparações que
ajudem a imaginar a escala, transições naturais e, quando o assunto permitir, suspense. Termine
entregando a resposta da curiosidade criada no começo. Tem que parecer escrito por alguém que
pesquisou e entendeu — nada de relatório, enciclopédia, lista ou tom de IA.

LIMITES (inegociáveis):
- Toda afirmação sobre o mundo (número, data, lugar, técnica, causa, recorde, superlativo) vem
  das evidências. Nenhum fato novo disfarçado de narrativa; nenhuma relação de causa que as
  evidências não sustentem; nenhuma resposta às perguntas que as fontes não respondem.
- Comparação e analogia só com o que as evidências dizem (não invente medida para comparar).
- Evidência "contraditorio": não escolha um valor — diga os dois e que as fontes divergem, ou não use.
- "estimado": diga como aproximação ("cerca de"). Valor medido/exato fica EXATO, mesmo de fonte única.
- "hipotese": diga como hipótese ("acredita-se", "uma das explicações"), nunca como fato.
- "fonte_unica" com recorde ou superlativo: diga de onde vem ("segundo os registros", "a menor já
  registrada"); não apresente como consenso.
- Números em algarismos; número grande (mil ou mais) arredondado com "cerca de" (303.863 → "cerca de
  300 mil"). Cada número UMA vez.
- Decimal (−64,4 °C): o valor da evidência, ou o arredondamento dito e no lado certo ("mais de 64 graus
  negativos"). Mantenha o sinal (negativos, abaixo de zero) e diga o que o número é (recorde, média).
- Número que nenhuma evidência traz: não diga; use a descrição sem número que uma evidência sustente
  ("frio extremo"); nunca outro número no lugar.
- Título "como/por que" pede o processo explicado; datas só as que mudam a história (sem linha do tempo).
- Conclusão que entrega algo: a resposta à pergunta do começo ou uma virada concreta das evidências —
  nada de "é um exemplo de como a engenharia supera desafios" ou "lugar único e fascinante".
- Sem saudação inicial e sem pedir like/inscrição (entram depois).
{linha_canal}
FORMATO: entre {cenas_min} e {cenas_max} cenas (a história manda; cada cena 1 a 3 frases), no total
entre {min_pal} e {max_pal} palavras faladas. Cada frase com:
- "tipo": "factual" (afirma algo sobre o mundo), "explicativa" (explica causa, consequência ou
  funcionamento) ou "narrativa" (abertura, transição, pergunta retórica, impacto — sem afirmar fato);
- "evidencias": ids que sustentam a frase (obrigatório em factual e explicativa; vazio em narrativa).
Os ids ficam SÓ no campo "evidencias", nunca no texto.
{feedback}
Responda SÓ JSON:
{{"titulo": "...", "descricao": "2 a 3 frases", "tags": ["..."],
  "cenas": [{{"frases": [{{"texto": "...", "tipo": "narrativa", "evidencias": []}},
                         {{"texto": "...", "tipo": "factual", "evidencias": ["E2"]}}]}}]}}"""


def _valido_escritor(raw: str) -> bool:
    cenas = _obj(raw).get("cenas")
    return bool(cenas) and all(isinstance(c, dict) and isinstance(c.get("frases"), list) and c["frases"] for c in cenas)


def de_json(data: dict, banco: dict) -> dict:
    """Resposta do escritor → roteiro no contrato do pipeline (narration montada das frases)."""
    cenas = []
    for c in data.get("cenas") or []:
        frases = []
        for f in c.get("frases") or []:
            if not isinstance(f, dict) or not str(f.get("texto", "")).strip():
                continue
            texto = re.sub(r"\s*\[\s*E\d+(\s*[,;]\s*E\d+)*\s*\]", "", str(f["texto"])).strip()
            tipo = str(f.get("tipo", "")).strip().lower()
            frases.append({"texto": texto, "tipo": tipo if tipo in TIPOS else "factual",
                           "evidencias": [str(x).strip() for x in f.get("evidencias") or [] if str(x).strip()]})
        if frases:
            cenas.append({"narration": " ".join(f["texto"] for f in frases), "frases": frases})
    return {"title": str(data.get("titulo", "")).strip(), "description": str(data.get("descricao", "")).strip(),
            "tags": [str(t) for t in data.get("tags") or []][:15], "scenes": cenas,
            "_banco": banco, "_redacao": True}
    # 19/11 (job 996): sem "_fatos_verificados" — a regra antiga de título COPIADO da narração saiu
    # "Permafrost: 303.863 habitantes". O título sai do roteiro final com a checagem de números e nomes
    # da narração (script_gen.packaging_from_final) e o auditor confere a coerência


def defeitos(script: dict, banco: dict, topic: str, min_pal: int, max_pal: int, cenas_min: int, cenas_max: int
             ) -> tuple[dict, list[str], list[dict]]:
    """Defeitos que pedem nova tentativa ao escritor: factuais (auditoria em código), forma e
    estilo. Devolve (roteiro conferido, textos de defeito para o pedido, problemas factuais)."""
    from .script_gen import _echo_problem, _ending_problem, _filler_phrase, _hype_problem, _repetition_problem
    script, problemas = auditar_codigo(script, banco, topic)
    lista = [f"a frase \"{p['termo'][:90]}\": {p['motivo']}" for p in problemas]
    n_pal, n_cenas = _palavras(script), len(script["scenes"])
    if not min_pal <= n_pal <= max_pal:
        lista.append(f"tem {n_pal} palavras; precisa ficar entre {min_pal} e {max_pal}")
    if not cenas_min <= n_cenas <= cenas_max:
        lista.append(f"tem {n_cenas} cenas; use entre {cenas_min} e {cenas_max}")
    from .fatos import numero_repetido
    for d in (parece_lista(script), _repetition_problem(script), _echo_problem(script), _ending_problem(script),
              _hype_problem(script, 2), _filler_phrase(script, topic), numero_repetido(script, 1)):
        if d:
            lista.append(str(d))
    # 20/11: o que o editor travaria (linha do tempo, fato repetido) já volta ao escritor nas tentativas dele
    lista += travas_editoriais(script)
    return script, lista, problemas


def escrever(channel, topic: str, banco: dict, *, min_minutes: float, max_minutes: float,
             cenas_ref: int, feedback: str = "", tentativas: int = 3) -> dict:
    """O roteirista escreve a história a partir do banco. Até `tentativas` pedidos, cada um com
    os defeitos do anterior; fica a versão com menos defeitos (os factuais seguem para a
    auditoria e a edição por cena, que reformulam em vez de cortar)."""
    from .providers import complete
    from .script_gen import current_date_rule, recent_mistakes
    # 19/11: meta em palavras pela voz real (~135/min); a duração do áudio decide depois. O mínimo tem
    # folga: história bem contada um pouco mais curta passa; enchimento para chegar lá, não
    min_pal = round(min_minutes * PALAVRAS_POR_MINUTO * 0.9)
    max_pal = round(max_minutes * PALAVRAS_POR_MINUTO)
    # cenas acompanham a duração (uma cena a cada 15-20 s); 12 do yaml é só referência
    cenas_min = max(5, min(cenas_ref - 4, round(min_minutes * 3)))
    cenas_max = max(cenas_ref + 4, round(max_minutes * 4))
    linha = str(getattr(channel, "niche", "") or "")
    pedido = _PROMPT_ESCRITOR.format(
        canal=getattr(channel, "channel_title", "") or channel.name, tema=topic, banco=bloco_para_escritor(banco, 0),
        linha_canal=(f"Linha do canal: {linha}\n" if linha else "") + current_date_rule(),
        cenas_min=cenas_min, cenas_max=cenas_max, min_pal=min_pal, max_pal=max_pal,
        feedback=(f"\nPARECER DA REVISÃO ANTERIOR (corrija):\n{feedback}\n" if feedback else ""))
    pedido += recent_mistakes(channel.name)
    from .providers import ProvedoresIndisponiveis, is_strong_model
    melhor, menos = None, 10**6
    correcao = ""
    infra: ProvedoresIndisponiveis | None = None
    for n in range(tentativas):
        try:
            # 19/11 (job 997): só modelo forte escreve; sem ele, espera/retoma (não cai no Llama 70B)
            from . import metricas
            with metricas.etapa("escrita"):
                raw = complete([{"role": "user", "content": pedido + correcao}], max_tokens=RESPOSTA_ESCRITOR,
                               temperature=0.7, validate=_valido_escritor, model_filter=is_strong_model)
        except ProvedoresIndisponiveis as exc:
            infra = exc
            break
        except RuntimeError as exc:
            log.warning("escritor indisponível (tentativa %d/%d): %s", n + 1, tentativas, exc)
            continue
        script = de_json(_obj(raw), banco)
        script, lista, _ = defeitos(script, banco, topic, min_pal, max_pal, cenas_min, cenas_max)
        log.info("redação, tentativa %d/%d: %d cenas, %d palavras, %d defeito(s)%s", n + 1, tentativas,
                 len(script["scenes"]), _palavras(script), len(lista), (": " + " | ".join(lista[:4])) if lista else "")
        # forma (tamanho, cenas, lista, repetição) pesa 3x: frase sem prova a auditoria conserta depois;
        # roteiro curto ou com cara de lista não se conserta frase a frase
        peso = sum(1 if x.startswith("a frase") else 3 for x in lista)
        if not min_pal <= _palavras(script) <= max_pal:
            peso += 100  # fora da meta de duração só se nenhuma tentativa ficar dentro
        if peso < menos:
            melhor, menos = script, peso
        if not lista:
            break
        correcao = "\n\nO roteiro anterior tinha estes defeitos; corrija TODOS:\n" + "\n".join(f"- {x}" for x in lista)
    if melhor is None:
        if infra is not None:
            raise infra
        raise RoteiroReprovado("escritor indisponível: nenhum modelo capaz respondeu com o roteiro")
    return melhor


# ---------------------------------------------------------------- auditoria por IA
_PROMPT_AUDITOR = """Você é o auditor de uma redação. Tema: {tema}

EVIDÊNCIAS (cada uma com o trecho da fonte que a prova):
{banco}
{contexto_antes}
FRASES A JULGAR — [tipo; evidências citadas]:
{frases}
{contexto_depois}
Dê uma decisão para CADA frase numerada acima — todas, inclusive as corretas:
- factual: "sustentada" (o que afirma está nas evidências: mesmo sujeito, medida, valor, unidade e
  sinal), "parcial" (só parte está), "sem_suporte" (nada sustenta) ou "contradiz";
- explicativa: "sustentada" (coerente com as evidências, sem ir além), "excede" (conclusão ou causa
  que as evidências não permitem), "sem_suporte" ou "contradiz";
- narrativa: "narrativa_ok" (abertura, transição, pergunta, impacto — não precisa de fonte) ou
  "afirmacao_oculta" (esconde um fato verificável sem evidência).
Em cada decisão: "evidencias" (ids que você conferiu) e "justificativa" (uma frase que diga o porquê).
{pedido_qualidade}
Responda SÓ JSON:
{{"frases": [{{"n": 1, "decisao": "...", "evidencias": ["E2"], "justificativa": "..."}}],
  "qualidade": {{"nota": 8, "problemas": ["..."]}}}}"""

_SO_FRASES = """A HISTÓRIA é avaliada à parte (leitura de espectador e editor): julgue só as frases numeradas;
em "qualidade" responda {{"nota": 0, "problemas": []}}."""

DECISOES_AUDITOR = {"sustentada": None, "narrativa_ok": None, "parcial": "excede", "excede": "excede",
                    "sem_suporte": "sem_suporte", "contradiz": "contradiz", "afirmacao_oculta": "afirmacao_oculta"}
FRASES_POR_BLOCO = 12   # uma decisão com justificativa por frase: bloco maior arrisca resposta cortada
CONTEXTO_BLOCO = 2      # frases antes e depois do bloco, só para leitura


_PROMPT_ESPECTADOR = """Você assiste ao vídeo como ESPECTADOR atento e confere como CHECADOR. Tema: {tema}

EVIDÊNCIAS (o que as fontes comprovam):
{banco}

ROTEIRO, na ordem em que é falado (frases numeradas):
{frases}

1) Liste de 3 a 6 conclusões que o público tira do vídeo — inclusive as que não estão escritas, mas que a
   sequência das frases leva a concluir.
2) Confira cada conclusão, e cada sequência de frases, contra as evidências. Aponte SÓ o que leva o público
   a concluir algo que as evidências não sustentam ou contradizem:
   - "combinacao": frases verdadeiras que juntas sugerem algo falso (o tempo ou a medida de um trecho dito
     junto do ponto de partida de outro trajeto parece valer para o trajeto inteiro);
   - "comparacao": comparação apresentada de forma enganosa;
   - "sujeito_trocado": informação verdadeira associada a outro sujeito;
   - "parcial_como_total": parte (distância, tempo, quantidade) apresentada como o total;
   - "causal": relação de causa que as evidências não comprovam;
   - "sentido_alterado": explicação que muda o significado do fato original;
   - "ordem": a ordem das informações leva a uma interpretação errada.
Responda SÓ JSON:
{{"conclusoes_do_publico": ["..."],
  "enganos": [{{"frases": [7, 8], "tipo": "combinacao", "o_publico_conclui": "...", "por_que_engana": "...",
               "como_corrigir": "..."}}]}}"""

TIPOS_ENGANO = ("combinacao", "comparacao", "sujeito_trocado", "parcial_como_total", "causal", "sentido_alterado", "ordem")

_PROMPT_EDITOR = """Você é o EDITOR do vídeo (a checagem de fatos é feita à parte: aqui só a HISTÓRIA).
Tema: {tema}. Título: "{titulo}".
Narração, cena por cena:
{cenas}

Dê de 0 a 10 a cada critério e aponte os problemas com as cenas onde estão. Seja exigente: 8 ou mais só
para o que alguém assistiria até o fim.
- "abertura": prende a atenção logo no começo;
- "progressao": cada cena leva a história adiante (do que é, para como funciona, por que, consequência);
- "novidade": cada cena traz informação nova (aponte as cenas que não acrescentam);
- "sem_repeticao": nenhum fato ou ideia dito duas vezes (aponte as cenas que repetem);
- "narrativa": é uma história com fio, não uma lista de curiosidades ou de datas;
- "titulo": o desenvolvimento entrega o que o título promete (título "como…" pede o processo explicado);
- "conclusao": a conclusão acrescenta algo ou responde à curiosidade do começo — não repete a abertura;
- "linguagem": português brasileiro natural, claro e envolvente, bom de ouvir.
Responda SÓ JSON:
{{"criterios": {{"abertura": 8, "progressao": 8, "novidade": 8, "sem_repeticao": 8, "narrativa": 8, "titulo": 8,
                "conclusao": 8, "linguagem": 8}},
  "problemas": [{{"criterio": "novidade", "cenas": [3], "descricao": "..."}}]}}"""

CRITERIOS_EDITORIAIS = ("abertura", "progressao", "novidade", "sem_repeticao", "narrativa", "titulo", "conclusao", "linguagem")
CRITERIOS_TRAVA = ("progressao", "sem_repeticao", "narrativa", "titulo", "conclusao")  # 4 ou menos: não sai assim
NOTA_COM_TRAVA = 6.5
EDICOES_MAX = 2  # rodadas de edição por cena (fatos + história) antes de reprovar


def _valido_auditor(raw: str) -> bool:
    data = _obj(raw)
    return isinstance(data.get("frases"), list) and isinstance(data.get("qualidade"), dict)


def _decisoes_validas(data: dict, esperados: set[int]) -> dict[int, dict]:
    """{n: decisão} das frases do bloco com decisão conhecida e justificativa de verdade."""
    out = {}
    for r in data.get("frases") or []:
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        decisao = str(r.get("decisao", "")).strip().lower()
        just = str(r.get("justificativa", "")).strip()
        if n in esperados and decisao in DECISOES_AUDITOR and len(just.split()) >= 3:
            out[n] = {"decisao": decisao, "justificativa": just[:300],
                      "evidencias": [str(x) for x in r.get("evidencias") or [] if isinstance(x, str)]}
    return out


def _hash_banco(banco: dict) -> str:
    import hashlib
    return hashlib.sha1(json.dumps([(e["id"], e["afirmacao"], e["status"], e.get("hipotese"), e.get("estimado"),
                                     sorted(e.get("conflitos") or []))
                                    for e in banco.get("evidencias", [])], ensure_ascii=False).encode()).hexdigest()


def _chave_frase(lista: list, k: int, hb: str) -> str:
    """O que a decisão de uma frase depende: o texto, o tipo, as evidências citadas, as frases vizinhas e o banco."""
    import hashlib
    f = lista[k][2]
    viz = [lista[x][2]["texto"] if 0 <= x < len(lista) else "" for x in (k - 1, k + 1)]
    return hashlib.sha1(json.dumps([f["texto"], f.get("tipo"), sorted(f.get("evidencias") or []), viz, hb],
                                   ensure_ascii=False).encode()).hexdigest()


def auditar_ia(script: dict, banco: dict, topic: str, previas: dict | None = None) -> tuple[list[dict], dict] | None:
    """20/11: TODA frase recebe uma decisão estruturada (decisão + evidências conferidas + justificativa).
    Roteiro grande vai em blocos de até FRASES_POR_BLOCO frases (e que caibam num pedido do Groq grátis,
    sem cortar evidência nem resposta), cada um com as frases vizinhas como contexto; frase que volta
    sem decisão válida é perguntada de novo, sozinha no bloco dela; e um juiz final dá a nota da
    história inteira. Devolve (problemas, qualidade com "decisoes") ou None — sem decisão para todas as
    frases, ou sem modelo forte, o roteiro não sai."""
    from .providers import ProvedoresIndisponiveis, _too_big_for, complete, is_strong_model
    lista = frases_de(script)
    if not lista:
        return [], {"nota": 0, "problemas": ["roteiro vazio"], "decisoes": []}

    def linha(k: int) -> str:
        f = lista[k][2]
        return f"{k + 1}) [{f.get('tipo')}; {', '.join(f.get('evidencias') or []) or '-'}] {f['texto']}"

    # 20/11: depois de uma edição por cena, frase idêntica com as mesmas vizinhas e o mesmo banco tem a mesma
    # decisão — só as frases que mudaram (ou cujas vizinhas mudaram) voltam ao auditor. A leitura de espectador
    # e o editor relêem SEMPRE o roteiro inteiro
    hb = _hash_banco(banco)
    previas = {} if previas is None else previas
    chaves = [_chave_frase(lista, k, hb) for k in range(len(lista))]
    pendentes = [k for k in range(len(lista)) if chaves[k] not in previas]

    def pedido_de(nums: list[int], bloco_unico: bool) -> str:
        ini, fim = min(nums), max(nums) + 1
        citadas = {x for k in nums for x in lista[k][2].get("evidencias") or []}
        citadas |= {c for e in por_id(banco).values() if e["id"] in citadas for c in e.get("conflitos") or []}
        antes = [lista[k][2]["texto"] for k in range(max(0, ini - CONTEXTO_BLOCO), ini)]
        depois = [lista[k][2]["texto"] for k in range(fim, min(len(lista), fim + CONTEXTO_BLOCO))]
        return _PROMPT_AUDITOR.format(
            tema=topic, banco=bloco_para_escritor(banco, 200, ids=citadas) or "(nenhuma citada)",
            contexto_antes=("\nCONTEXTO ANTES (não julgue): " + " ".join(antes) + "\n") if antes else "",
            frases="\n".join(linha(k) if k in nums else f"   (contexto, não julgue) {lista[k][2]['texto']}"
                             for k in range(ini, fim)),
            contexto_depois=("\nCONTEXTO DEPOIS (não julgue): " + " ".join(depois) + "\n") if depois else "",
            pedido_qualidade=_SO_FRASES)

    def cabe(p: str) -> bool:
        return not _too_big_for("groq", [{"content": p}], RESPOSTA_AUDITOR)

    blocos = [pendentes] if pendentes else []
    if pendentes and (len(pendentes) > FRASES_POR_BLOCO or not cabe(pedido_de(pendentes, True))):
        blocos = [pendentes[k:k + FRASES_POR_BLOCO] for k in range(0, len(pendentes), FRASES_POR_BLOCO)]
        while any(not cabe(pedido_de(b, False)) and len(b) > 2 for b in blocos):
            novos = []
            for b in blocos:
                novos += [b[:len(b) // 2], b[len(b) // 2:]] if (not cabe(pedido_de(b, False)) and len(b) > 2) else [b]
            blocos = novos
    unico = len(blocos) == 1

    def chamar(p: str, validar) -> dict | None:
        try:
            return _obj(complete([{"role": "user", "content": p}], max_tokens=RESPOSTA_AUDITOR, temperature=0.1,
                                 validate=validar, model_filter=is_strong_model))
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("auditor indisponível: %s", exc)
            return None

    decisoes: dict[int, dict] = {k + 1: previas[chaves[k]] for k in range(len(lista)) if chaves[k] in previas}
    from . import metricas
    metricas.somar("frases_reaproveitadas", len(decisoes))
    metricas.somar("frases_julgadas", len(pendentes))
    with metricas.etapa("auditoria_factual"):
        for b in blocos:
            r = chamar(pedido_de(b, unico), _valido_auditor)
            if r is None:
                return None
            esperados = {k + 1 for k in b}
            decisoes.update(_decisoes_validas(r, esperados))
            faltam = sorted(esperados - set(decisoes))
            if faltam:
                log.warning("auditor: %d frase(s) sem decisão válida no bloco %d-%d — perguntando de novo",
                            len(faltam), min(esperados), max(esperados))
                r2 = chamar(pedido_de([n - 1 for n in faltam], False), _valido_auditor)
                if r2 is not None:
                    decisoes.update(_decisoes_validas(r2, set(faltam)))
    sem = [n for n in range(1, len(lista) + 1) if n not in decisoes]
    if sem:
        log.warning("auditor: frase(s) %s sem decisão depois de perguntar de novo — o roteiro não sai assim", sem)
        return None
    for k in range(len(lista)):
        previas[chaves[k]] = decisoes[k + 1]
    log.info("auditoria factual: %d frase(s) julgadas em %d bloco(s), %d reaproveitada(s) (frase, evidências, "
             "vizinhas e banco iguais)", len(pendentes), len(blocos), len(lista) - len(pendentes))
    with metricas.etapa("auditoria_semantica"):
        leitura = leitura_espectador(script, banco, topic)
    if leitura is None:
        return None
    with metricas.etapa("auditoria_editorial"):
        editorial = avaliar_editorial(script, topic)
    if editorial is None:
        return None
    problemas, registro = [], []
    for n in range(1, len(lista) + 1):
        i, j, f = lista[n - 1]
        d = decisoes[n]
        tipo = DECISOES_AUDITOR[d["decisao"]]
        if f.get("tipo") == "narrativa" and tipo not in (None, "afirmacao_oculta"):
            tipo = None  # narrativa não precisa de fonte: só a afirmação escondida conta
        if f.get("tipo") != "narrativa" and d["decisao"] == "narrativa_ok":
            tipo = "sem_suporte"  # frase factual "aprovada como narrativa" não foi conferida contra evidência
        registro.append({"n": n, "cena": i, "frase": j, "tipo": f.get("tipo"), **d, "problema": tipo})
        if tipo:
            problemas.append(_p(tipo, f["texto"], i, j, d["justificativa"]))
    for eng in leitura["enganos"]:
        # o problema vai para a ÚLTIMA frase da sequência (é ela que fecha a conclusão errada); a edição a
        # reescreve vendo a anterior
        n = max(eng["frases"])
        i, j, f = lista[n - 1]
        outras = [lista[k - 1][2]["texto"][:80] for k in eng["frases"] if k != n]
        problemas.append(_p("leitura_enganosa", f["texto"], i, j,
                            f"leitura enganosa ({eng['tipo']})" + (f" junto com \"{' / '.join(outras)}\"" if outras else "")
                            + f": o público conclui que {eng['o_publico_conclui']}, mas {eng['por_que_engana']}. "
                            f"Corrija: {eng['como_corrigir']}"))
    factual = {"frases": len(lista), "sustentadas": sum(1 for d in registro if d["problema"] is None),
               "enganos": leitura["enganos"], "conclusoes_do_publico": leitura["conclusoes"]}
    factual["nota"] = max(0.0, round(10 * factual["sustentadas"] / max(1, len(lista)) - 2 * len(leitura["enganos"]), 1))
    return problemas, {**editorial, "decisoes": registro, "blocos": len(blocos), "factual": factual,
                       "frases_julgadas": len(pendentes), "frases_reaproveitadas": len(lista) - len(pendentes)}


def leitura_espectador(script: dict, banco: dict, topic: str) -> dict | None:
    """20/11: frases verdadeiras uma a uma podem, juntas, levar o público a uma conclusão falsa (o tempo de um
    trecho dito ao lado do ponto de partida de outro trajeto parece o tempo da viagem inteira). Uma leitura do roteiro INTEIRO como espectador:
    o que o público conclui e onde isso não é sustentado. {"conclusoes", "enganos"} ou None (sem leitura
    válida, o roteiro não sai)."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    lista = frases_de(script)
    citadas = {x for _, _, f in lista for x in f.get("evidencias") or []}
    pedido = _PROMPT_ESPECTADOR.format(
        tema=topic, banco=bloco_para_escritor(banco, 200, ids=citadas) or "(nenhuma citada)",
        frases="\n".join(f"{k}) {f['texto']}" for k, (_, _, f) in enumerate(lista, 1)))

    def valido(raw: str) -> bool:
        d = _obj(raw)
        return isinstance(d.get("conclusoes_do_publico"), list) and len(d["conclusoes_do_publico"]) >= 2 \
            and isinstance(d.get("enganos"), list)
    for _ in range(2):
        try:
            d = _obj(complete([{"role": "user", "content": pedido}], max_tokens=2000, temperature=0.1,
                              validate=valido, model_filter=is_strong_model))
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("leitura de espectador indisponível: %s", exc)
            continue
        enganos = []
        for e in d.get("enganos") or []:
            try:
                frs = sorted({int(x) for x in e.get("frases") or []})
            except (TypeError, ValueError):
                continue
            textos = [str(e.get(k, "")) for k in ("o_publico_conclui", "por_que_engana", "como_corrigir")]
            if frs and all(1 <= x <= len(lista) for x in frs) and e.get("tipo") in TIPOS_ENGANO \
                    and all(len(t.split()) >= 3 for t in textos[:2]):
                enganos.append({"frases": frs, "tipo": e["tipo"], "o_publico_conclui": textos[0][:300],
                                "por_que_engana": textos[1][:300], "como_corrigir": textos[2][:300]})
        log.info("leitura de espectador: %d conclusão(ões) do público, %d engano(s)%s", len(d["conclusoes_do_publico"]),
                 len(enganos), "".join(f" | {e['tipo']} {e['frases']}: {e['o_publico_conclui'][:80]}" for e in enganos))
        return {"conclusoes": [str(x)[:200] for x in d["conclusoes_do_publico"]][:6], "enganos": enganos}
    return None


def _anos_e_medidas_por_cena(script: dict) -> dict[str, set[int]]:
    from .fatos import _pares
    onde: dict[str, set[int]] = {}
    for i, _, f in frases_de(script):
        if f.get("tipo") == "narrativa":
            continue
        t = extenso_para_algarismos(f["texto"])
        chaves = set(re.findall(r"\b(1\d{3}|20\d{2})\b", t)) | {f"{v} {u}" for v, u in _pares(t) if u != "ano"}
        for c in chaves:
            onde.setdefault(c, set()).add(i)
    return onde


def travas_editoriais(script: dict) -> list[str]:
    """Problemas de história que o código enxerga sozinho, em qualquer tema: o mesmo fato (ano ou medida)
    dito em cenas diferentes, e o desenvolvimento que vira linha do tempo."""
    travas = []
    repetidos = sorted(c for c, cenas in _anos_e_medidas_por_cena(script).items() if len(cenas) > 1)
    if repetidos:
        travas.append("o mesmo fato aparece em mais de uma cena (" + ", ".join(repetidos[:4]) + "): diga uma vez só")
    factuais = [f for _, _, f in frases_de(script) if f.get("tipo") != "narrativa"]
    com_ano = [f for f in factuais if re.search(r"\b(1\d{3}|20\d{2})\b", f["texto"])]
    anos = {a for f in com_ano for a in re.findall(r"\b(1\d{3}|20\d{2})\b", f["texto"])}
    if factuais and len(anos) >= 5 and len(com_ano) / len(factuais) >= 0.45:
        travas.append(f"o desenvolvimento virou linha do tempo ({len(anos)} anos em {len(com_ano)} de {len(factuais)} "
                      "frases): conte o processo e fique só com as datas que mudam a história")
    return travas


def cenas_das_travas(script: dict) -> list[dict]:
    """As travas do código com as cenas onde estão (1, 2, …), para a edição localizada."""
    alvos = []
    for c, cenas in sorted(_anos_e_medidas_por_cena(script).items()):
        if len(cenas) > 1:
            alvos.append({"criterio": "sem_repeticao", "cenas": sorted(x + 1 for x in cenas),
                          "descricao": f"\"{c}\" aparece nas cenas {', '.join(str(x + 1) for x in sorted(cenas))}: fica "
                                       "em UMA só (onde é essencial); nas outras, tire e ponha informação nova"})
    if any("linha do tempo" in t for t in travas_editoriais(script)):
        com_ano = sorted({i + 1 for i, _, f in frases_de(script)
                          if f.get("tipo") != "narrativa" and re.search(r"\b(1\d{3}|20\d{2})\b", f["texto"])})
        alvos.append({"criterio": "progressao", "cenas": com_ano,
                      "descricao": "o desenvolvimento virou linha do tempo: no máximo 3 anos no roteiro todo; conte o "
                                   "processo (o problema, como funciona, como foi feito, o resultado) e fique só com "
                                   "as datas que mudam a história"})
    return alvos


_NOMES_PROBLEMA = {"sem_evidencia": "afirmação sem evidência", "sem_suporte": "a evidência citada não sustenta",
                   "numero_sem_evidencia": "número que nenhuma evidência traz", "excede": "diz mais do que a evidência",
                   "leitura_enganosa": "leitura enganosa", "hipotese_como_fato": "hipótese dita como fato",
                   "ordem_nao_comprovada": "ordem entre acontecimentos sem prova"}


def motivo_factual(p: dict) -> str:
    """O problema da auditoria factual/semântica como o editor o lê: a frase, o tipo e o motivo do auditor."""
    return (f"FACTUAL — {_NOMES_PROBLEMA.get(p['tipo'], p['tipo'].replace('_', ' '))} na frase \"{p['termo'][:200]}\": "
            f"{p['motivo'][:400]}")


def alvos_da_edicao(script: dict, qualidade: dict, problemas: list[dict] | None = None,
                    maximo: int | None = None) -> dict[int, list[str]]:
    """Cena (índice 0) → motivos da edição. 21/11: os problemas factuais e semânticos da auditoria entram
    primeiro e TODOS (nenhuma reprovação do auditor fica de fora); depois as travas do código e os problemas do
    editor pelo critério de nota mais baixa, até o limite de cenas (metade do roteiro, no mínimo 3) — edição
    que toca tudo é reescrita."""
    n_cenas = len(script.get("scenes", []))
    maximo = maximo or max(3, n_cenas // 2)
    alvos: dict[int, list[str]] = {}
    for p in problemas or []:
        i = p.get("cena")
        if isinstance(i, int) and 0 <= i < n_cenas:
            alvos.setdefault(i, []).append(motivo_factual(p))
    if qualidade.get("nota", 0) >= NOTA_ALVO and qualidade.get("aprovado", True) and not qualidade.get("travas"):
        return alvos  # a história já está no alvo: a edição só corrige os fatos
    crit = qualidade.get("criterios") or {}
    padrao = {"abertura": [1], "conclusao": [n_cenas]}
    itens = [(-1.0, a) for a in qualidade.get("alvos_travas") or []]
    for a in qualidade.get("alvos") or []:
        cenas = a.get("cenas") or padrao.get(a.get("criterio"), [])
        itens.append((float(crit.get(a.get("criterio"), 10)), {**a, "cenas": cenas}))
    for k in CRITERIOS_TRAVA + ("abertura",):
        if crit.get(k, 10) <= 4 and k in padrao and not any(a.get("criterio") == k for _, a in itens):
            itens.append((float(crit[k]), {"criterio": k, "cenas": padrao[k], "descricao": f"{k} com nota {crit[k]:.0f}"}))
    for _, a in sorted(itens, key=lambda x: x[0]):
        for c in a["cenas"]:
            try:
                i = int(c) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < n_cenas and (i in alvos or len(alvos) < maximo):
                alvos.setdefault(i, []).append(f"HISTÓRIA — {a.get('criterio')}: {a.get('descricao', '')}".strip())
    return alvos


def avaliar_editorial(script: dict, topic: str) -> dict | None:
    """20/11: a HISTÓRIA, separada dos fatos. O editor (IA forte) dá nota por critério e aponta as cenas; a
    nota é a média calculada pelo código, e trava em NOTA_COM_TRAVA quando um critério essencial fica em 4 ou
    menos ou quando o código vê repetição de fato ou linha do tempo. {"nota", "problemas", "criterios",
    "travas", "aprovado"} ou None."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    cenas = "\n".join(f"[{k}] {sc.get('narration', '')}" for k, sc in enumerate(script.get("scenes", []), 1))
    pedido = _PROMPT_EDITOR.format(tema=topic, titulo=script.get("title", ""), cenas=cenas)

    def valido(raw: str) -> bool:
        c = _obj(raw).get("criterios")
        return isinstance(c, dict) and all(isinstance(c.get(k), (int, float)) for k in CRITERIOS_EDITORIAIS)
    try:
        d = _obj(complete([{"role": "user", "content": pedido}], max_tokens=1500, temperature=0.1,
                          validate=valido, model_filter=is_strong_model))
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("editor indisponível: %s", exc)
        return None
    crit = {k: max(0.0, min(10.0, float(d["criterios"][k]))) for k in CRITERIOS_EDITORIAIS}
    problemas = [f"{p.get('criterio', '')} (cenas {', '.join(str(x) for x in p.get('cenas') or [])}): {p.get('descricao', '')}"
                 for p in d.get("problemas") or [] if isinstance(p, dict) and str(p.get("descricao", "")).strip()]
    alvos = [{"criterio": str(p.get("criterio", "")), "cenas": [c for c in p.get("cenas") or [] if isinstance(c, (int, float, str))],
              "descricao": str(p.get("descricao", ""))[:300]}
             for p in d.get("problemas") or [] if isinstance(p, dict) and str(p.get("descricao", "")).strip()]
    travas = travas_editoriais(script) + [f"{k} com nota {crit[k]:.0f}" for k in CRITERIOS_TRAVA if crit[k] <= 4]
    nota = round(sum(crit.values()) / len(crit), 1)
    if travas:
        nota = min(nota, NOTA_COM_TRAVA)
    return {"nota": nota, "problemas": (travas + problemas)[:10], "criterios": crit, "travas": travas,
            "alvos": alvos, "alvos_travas": cenas_das_travas(script), "aprovado": not travas and nota >= NOTA_MINIMA}


# ---------------------------------------------------------------- edição por cena (única correção)
_PROMPT_EDICAO = """Você faz a EDIÇÃO LOCALIZADA de um roteiro pronto (YouTube, português do Brasil). Tema: {tema}. Título: "{titulo}".
Você NÃO reescreve o roteiro: edita SÓ as cenas marcadas com >>, para corrigir o problema apontado em cada uma,
e devolve só as cenas que mudou. As outras ficam exatamente como estão.

ROTEIRO COMPLETO (contexto; [tipo; evidências] antes de cada frase):
{roteiro}

EVIDÊNCIAS CITADAS NAS CENAS A EDITAR E NAS VIZINHAS (com o trecho da fonte):
{citadas}
{outras}
CENAS A EDITAR E O PROBLEMA DE CADA UMA:
{alvos}

Para cada cena a editar:
- entenda a FUNÇÃO dela na história, o que as cenas anteriores já explicaram e o que as seguintes vão explicar;
- corrija SÓ o problema apontado e preserve a informação que a cena precisa entregar ao resto da história;
- toda frase factual cita as evidências que a sustentam e diz só o que elas dizem: nada de fato, número, nome,
  causa ou detalhe que nenhuma evidência traz; frase de ligação sem fato é "narrativa";
- não repita fato que outra cena já diz; a cena editada continua ligada à anterior e à seguinte;
- ordem entre acontecimentos (primeiro, depois, em seguida, antes disso, por fim) só com evidência citada de
  "ordem COMPROVADA"; sem ela, conte o acontecimento sem afirmar a ordem;
- números em algarismos; decimal da evidência fica exato ("−64,4 °C") ou com aproximação no sentido certo
  ("mais de 64"); número que nenhuma evidência traz sai (descreva sem número só se uma evidência sustenta a
  descrição). Nunca ponha outro número no lugar.
Problema FACTUAL (reprovação do auditor): a frase apontada NÃO pode ficar como está. Se uma evidência sustenta
o que ela quer dizer, reescreva para dizer EXATAMENTE isso e cite os ids (hipótese dita como hipótese,
estimativa como estimativa, divergência entre fontes dita como divergência); se nenhuma sustenta, vire frase
narrativa que mantém a ligação sem afirmar nada, ou tire a frase. Leitura enganosa: reescreva a sequência
para que o público conclua só o que as evidências sustentam.
Problema de HISTÓRIA: corrija sem perder os fatos que a cena entrega e sem criar fato novo sem evidência.
Se a correção EXIGIR mudar uma cena não marcada (ex.: o fato repetido sai de uma cena e a outra precisa da
ligação), pode editá-la, dizendo por quê em "justificativa". {regra_titulo}
Responda SÓ JSON:
{{"cenas": [{{"cena": 3, "funcao": "o que a cena faz na história", "preserva": "o que foi mantido",
   "corrige": "o que mudou e por quê", "justificativa": "só para cena não marcada",
   "frases": [{{"texto": "...", "tipo": "factual|explicativa|narrativa", "evidencias": ["E2"]}}]}}],
 "titulo": ""}}"""


def editar_cenas(script: dict, alvos: dict[int, list[str]], banco: dict, topic: str, *,
                 titulo_motivo: str = "", problemas: list[dict] | None = None,
                 prioritarias: set[str] | None = None) -> tuple[dict, dict]:
    """20/11: edição LOCALIZADA (1 chamada). O editor vê o roteiro inteiro, as evidências das cenas a editar e
    das vizinhas e o motivo de cada cena; devolve só as cenas que mudou. Cena fora dos alvos só entra com
    justificativa (e no máximo tantas quanto os alvos); cena editada que não passa na conferência em código
    volta a ser a original — a edição nunca troca um problema de história por um erro de fato.
    21/11: é a ÚNICA correção do roteiro — fatos (problemas da auditoria factual e semântica, em `problemas`)
    e história no mesmo pedido. Frase reprovada que volta igual fica registrada em "factuais_sem_correcao"
    (a auditoria seguinte a reprova de novo). `prioritarias`: evidências achadas pela busca para as frases
    reprovadas — entram com trecho mesmo quando o pedido precisa encolher. Devolve (roteiro, registro)."""
    from .providers import ProvedoresIndisponiveis, _too_big_for, complete, is_strong_model
    cenas = script.get("scenes", [])
    registro = {"alvos": {str(i + 1): m for i, m in sorted(alvos.items())}, "cenas_editadas": [], "revertidas": [],
                "extras": [], "recusadas": [], "titulo": None, "detalhes": [], "factuais_sem_correcao": []}
    if not alvos:
        return script, registro

    def fr(f: dict) -> str:
        return f"[{f.get('tipo')}; {','.join(f.get('evidencias') or []) or '-'}] {f['texto']}"
    roteiro = "\n".join(f"{'>>' if i in alvos else '  '} CENA {i + 1}:\n" + "\n".join(f"     {fr(f)}" for f in sc.get("frases") or [])
                        for i, sc in enumerate(cenas))
    perto = {x for i in alvos for x in (i - 1, i, i + 1) if 0 <= x < len(cenas)}
    ids_citados = {e for i in perto for f in cenas[i].get("frases") or [] for e in f.get("evidencias") or []} \
        | set(prioritarias or ())
    usados = {e for _, _, f in frases_de(script) for e in f.get("evidencias") or []}
    nao_usadas = {e["id"] for e in usaveis(banco)} - usados
    itens = "\n".join(f"CENA {i + 1}: " + " | ".join(m) for i, m in sorted(alvos.items()))
    regra_titulo = (f"O título tem um problema ({titulo_motivo}): proponha outro em \"titulo\", fiel ao que o roteiro "
                    "entrega." if titulo_motivo else "Deixe \"titulo\" vazio (o título não está em questão).")

    def montar(com_outras: bool) -> str:
        return _PROMPT_EDICAO.format(
            tema=topic, titulo=script.get("title", ""), roteiro=roteiro, alvos=itens, regra_titulo=regra_titulo,
            citadas=bloco_para_escritor(banco, 220, ids=ids_citados) or "(nenhuma)",
            outras=("\nEVIDÊNCIAS DO BANCO AINDA NÃO USADAS NO ROTEIRO (material para informação nova):\n"
                    + bloco_para_escritor(banco, 0, ids=nao_usadas) + "\n") if com_outras and nao_usadas else "")
    pedido = montar(True)
    if _too_big_for("groq", [{"content": pedido}], RESPOSTA_ESCRITOR):
        pedido = montar(False)

    def valido(raw: str) -> bool:
        return isinstance(_obj(raw).get("cenas"), list)
    from . import metricas
    try:
        with metricas.etapa("edicao_cenas"):
            raw = complete([{"role": "user", "content": pedido}], max_tokens=RESPOSTA_ESCRITOR, temperature=0.4,
                           validate=valido, model_filter=is_strong_model)
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("edição por cena indisponível: %s — o roteiro fica como está", exc)
        registro["erro"] = str(exc)[:200]
        return script, registro
    data = _obj(raw)
    novas = [dict(sc, frases=[dict(f) for f in sc.get("frases") or []]) for sc in cenas]
    extras_max = len(alvos)
    for c in data.get("cenas") or []:
        if not isinstance(c, dict):
            continue
        try:
            i = int(c.get("cena")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(cenas):
            continue
        frases = [{"texto": str(f.get("texto", "")).strip(), "tipo": str(f.get("tipo", "factual")).lower(),
                   "evidencias": [str(x) for x in f.get("evidencias") or []]}
                  for f in c.get("frases") or [] if isinstance(f, dict) and str(f.get("texto", "")).strip()]
        if not frases or [f["texto"] for f in frases] == [f["texto"] for f in cenas[i].get("frases") or []]:
            continue
        just = str(c.get("justificativa", "")).strip()
        if i not in alvos:
            if not just or len(registro["extras"]) >= extras_max:
                registro["recusadas"].append({"cena": i + 1, "motivo": "cena não marcada sem justificativa"
                                              if not just else "cenas extras demais"})
                continue
            registro["extras"].append({"cena": i + 1, "justificativa": just[:300]})
        teste, prob = auditar_codigo({"scenes": [{"frases": frases}]}, banco, topic)
        if prob:
            registro["revertidas"].append({"cena": i + 1, "motivo": prob[0]["motivo"][:200]})
            log.warning("edição da cena %d não passou na conferência (%s) — fica a original", i + 1, prob[0]["motivo"][:120])
            continue
        frases = teste["scenes"][0]["frases"]
        novas[i] = {**cenas[i], "frases": frases, "narration": " ".join(f["texto"] for f in frases)}
        registro["cenas_editadas"].append(i + 1)
        registro["detalhes"].append({"cena": i + 1, **{k: str(c.get(k, ""))[:300] for k in ("funcao", "preserva", "corrige")},
                                     "antes": cenas[i].get("narration", ""), "depois": novas[i]["narration"]})
    saida = {**script, "scenes": novas}
    textos_novos = {(i, _norm(f["texto"])) for i, sc in enumerate(novas) for f in sc.get("frases") or []}
    for p in problemas or []:
        if isinstance(p.get("cena"), int) and (p["cena"], _norm(p["termo"])) in textos_novos:
            registro["factuais_sem_correcao"].append({"cena": p["cena"] + 1, "tipo": p["tipo"], "frase": p["termo"][:160]})
    if registro["factuais_sem_correcao"]:
        log.warning("edição: %d frase(s) reprovada(s) ficaram iguais — a auditoria seguinte confere de novo",
                    len(registro["factuais_sem_correcao"]))
    titulo = str(data.get("titulo") or "").strip()
    if titulo_motivo and titulo and titulo != script.get("title"):
        saida["title"] = titulo
        registro["titulo"] = {"antes": script.get("title"), "depois": titulo}
    registro["cenas_editadas"].sort()
    metricas.somar("cenas_modificadas", len(registro["cenas_editadas"]))
    log.info("edição por cena: alvos %s; editadas %s; revertidas %s; extras %s; recusadas %s",
             sorted(i + 1 for i in alvos), registro["cenas_editadas"], [r["cena"] for r in registro["revertidas"]],
             [r["cena"] for r in registro["extras"]], [r["cena"] for r in registro["recusadas"]])
    return saida, registro


def _hash_versao(script: dict, banco: dict) -> str:
    """Identidade de uma versão auditada: o texto (frases com tipo e evidências, título) e o banco contra o
    qual foi conferida. Aprovação vale só para a versão com o mesmo hash."""
    import hashlib
    corpo = [script.get("title", "")] + [[f["texto"], f.get("tipo"), sorted(f.get("evidencias") or [])]
                                         for _, _, f in frases_de(script)]
    return hashlib.sha1(json.dumps([corpo, _hash_banco(banco)], ensure_ascii=False).encode()).hexdigest()[:12]


def _auditar_versao(script: dict, banco: dict, topic: str, previas: dict) -> tuple[dict, list[dict], dict]:
    """Auditoria completa de UMA versão: código + decisões por frase + leitura de espectador + editor."""
    script = sincronizar(script)
    script, problemas = auditar_codigo(script, banco, topic)
    # o descanso curto das chaves é esperado dentro da cascata (providers.complete); o longo sobe como
    # ProvedoresIndisponiveis (o pipeline guarda o trabalho e retoma). None aqui = resposta inválida
    # dos modelos fortes: uma 2ª tentativa e, sem auditoria, reprova (nunca aprova sem auditor)
    ia = auditar_ia(script, banco, topic, previas)
    if ia is None:
        ia = auditar_ia(script, banco, topic, previas)
    if ia is None:
        _registra_texto(script, "auditor indisponível")
        raise RoteiroReprovado("auditor forte indisponível — roteiro não sai sem auditoria")
    problemas_ia, qualidade = ia
    vistos = {(p["cena"], p["frase"]) for p in problemas}
    problemas += [p for p in problemas_ia if (p["cena"], p["frase"]) not in vistos]
    return script, problemas, qualidade


def auditar_e_corrigir(script: dict, banco: dict, topic: str, *, web_facts: list[dict] | None = None,
                       buscar_evidencia: Callable[[list[dict], str], list[dict]] | None = None,
                       min_palavras: int = 0) -> tuple[dict, dict]:
    """Auditoria (código + IA forte) e correção LOCALIZADA em rodadas, com versões identificadas.

    Cada versão do roteiro (v1 = a escrita; depois cada edição) passa pela auditoria INTEIRA — factual (frase a
    frase; só as frases que mudaram são julgadas de novo), semântica (leitura de espectador do roteiro todo) e
    editorial — e recebe as próprias notas; nada é herdado de outra versão. 21/11: uma correção só, a edição
    por cena: os problemas factuais e semânticos e os editoriais vão juntos, cada um na sua cena, e o editor
    vê o roteiro inteiro (até EDICOES_MAX rodadas). Frase sem evidência → antes da edição, busca de
    evidência para ela. As travas em código (números, unidades, evidência, sinal, contradição) continuam: a
    cena editada que não passa nelas volta a ser a original, e a versão seguinte é auditada do zero. No fim
    fica a MELHOR versão sem problema factual; ela só sai aprovada pela própria auditoria — e, se o banco
    mudou depois dela, é auditada de novo. Devolve (roteiro, relatório) ou levanta RoteiroReprovado."""
    from . import metricas
    palavras_inicio = _palavras(script)
    piso = max(min_palavras, palavras_inicio * (1 - MAX_PERDA))
    relatorio: dict = {"rodadas": [], "edicoes": 0, "versoes": [], "registro_edicoes": []}
    previas: dict = {}
    versoes: list[tuple[dict, dict, list[dict], dict]] = []  # (registro, roteiro, problemas, qualidade)
    origem, mudou = "escrita", []
    for _ in range(EDICOES_MAX + 1):
        script, problemas, qualidade = _auditar_versao(script, banco, topic, previas)
        v = {"versao": len(versoes) + 1, "origem": origem, "cenas_modificadas": mudou, "hash": _hash_versao(script, banco),
             "palavras": _palavras(script), "problemas_factuais": len(problemas),
             "nota_factual": (qualidade.get("factual") or {}).get("nota"),
             "enganos": len((qualidade.get("factual") or {}).get("enganos") or []),
             "nota_editorial": qualidade["nota"], "criterios": qualidade.get("criterios"),
             "travas": qualidade.get("travas") or [],
             "aprovado": not problemas and bool(qualidade.get("aprovado", qualidade["nota"] >= NOTA_MINIMA)),
             "frases_julgadas": qualidade.get("frases_julgadas"), "frases_reaproveitadas": qualidade.get("frases_reaproveitadas")}
        versoes.append((v, script, problemas, qualidade))
        relatorio["rodadas"].append({"versao": v["versao"], "problemas": problemas, "qualidade": qualidade})
        log.info("versão %d (%s, cenas %s): %d problema(s) factual(is); factual %s, editorial %.1f%s%s", v["versao"],
                 origem, mudou or "-", len(problemas), v["nota_factual"], v["nota_editorial"],
                 f"; travas: {'; '.join(v['travas'])}" if v["travas"] else "", " — APROVADA" if v["aprovado"] else "")
        if v["aprovado"] and qualidade["nota"] >= NOTA_ALVO:
            break
        if relatorio["edicoes"] >= EDICOES_MAX:
            break
        novas_ids: set[str] = set()
        sem_suporte = [p for p in problemas if p["tipo"] in ("sem_evidencia", "sem_suporte", "numero_sem_evidencia", "excede")]
        if sem_suporte and buscar_evidencia and web_facts is not None:
            antes = {e["id"] for e in banco.get("evidencias", [])}
            with metricas.etapa("busca_evidencia"):
                try:
                    novas = buscar_evidencia(sem_suporte, topic)
                except Exception as exc:  # noqa: BLE001 — busca extra nunca derruba a auditoria
                    log.warning("busca de evidência para as frases apontadas falhou: %s", exc)
                    novas = []
                adicionar_fontes(banco, topic, novas, web_facts)
            script["_banco"] = banco
            novas_ids = {e["id"] for e in banco.get("evidencias", []) if e["id"] not in antes and e["status"] != "nao_verificado"}
        alvos = alvos_da_edicao(script, qualidade, problemas)
        crit = qualidade.get("criterios") or {}
        titulo_motivo = next((a["descricao"] for a in qualidade.get("alvos") or [] if a.get("criterio") == "titulo"), "") \
            if crit.get("titulo", 10) < NOTA_MINIMA else ""
        if not alvos and not titulo_motivo:
            log.warning("edição: a auditoria não aponta cena nenhuma — nada a editar localmente")
            break
        while True:
            relatorio["edicoes"] += 1
            editado, reg = editar_cenas(script, alvos, banco, topic, titulo_motivo=titulo_motivo, problemas=problemas,
                                        prioritarias=novas_ids)
            reg["da_versao"] = v["versao"]
            reg["evidencias_novas"] = sorted(novas_ids)
            relatorio["registro_edicoes"].append(reg)
            if reg.get("cenas_editadas") or reg.get("titulo") or relatorio["edicoes"] >= EDICOES_MAX \
                    or not (reg.get("revertidas") or reg.get("factuais_sem_correcao")):
                break
            # nada mudou porque a conferência em código recusou as cenas ou o editor devolveu a frase reprovada
            # igual: o texto é o da versão já auditada, então a 2ª rodada vai direto ao editor, com o motivo
            # (sem auditar de novo o mesmo texto)
            for r in reg.get("revertidas") or []:
                alvos.setdefault(r["cena"] - 1, []).append(
                    f"A EDIÇÃO ANTERIOR DESTA CENA FOI RECUSADA pela conferência em código: {r['motivo']}")
            for r in reg.get("factuais_sem_correcao") or []:
                alvos.setdefault(r["cena"] - 1, []).append(
                    f"A EDIÇÃO ANTERIOR DEVOLVEU IGUAL a frase reprovada \"{r['frase']}\": ela não pode ficar assim")
        if not reg.get("cenas_editadas") and not reg.get("titulo"):
            log.warning("edição: nenhuma cena mudou — fica a versão %d", v["versao"])
            break
        if _palavras(editado) < piso:
            _registra_texto(editado, "edição esvaziou")
            log.warning("edição tiraria demais da história (%d -> %d palavras) — descartada",
                        palavras_inicio, _palavras(editado))
            break
        origem, mudou = "edicao", reg["cenas_editadas"]
        script = editado
    relatorio["versoes"] = [x[0] for x in versoes]
    metricas.anotar("versoes", [{k: x[0][k] for k in ("versao", "origem", "cenas_modificadas", "problemas_factuais",
                                                         "nota_factual", "nota_editorial", "aprovado")} for x in versoes])
    limpas = [x for x in versoes if not x[2]]
    if not limpas:
        ultimo = versoes[-1]
        _registra_texto(ultimo[1], "sem evidência depois da edição")
        raise RoteiroReprovado("afirmações sem evidência depois de {} edição(ões) por cena: {}".format(
            relatorio["edicoes"], "; ".join(f"{p['tipo']}: {p['termo'][:80]}" for p in ultimo[2])[:500]))
    # a melhor versão sem problema factual: aprovada antes de reprovada, depois a maior nota editorial; no
    # empate, a mais recente (é a que tem mais correções)
    v, escolhido, _, qualidade = max(limpas, key=lambda x: (x[0]["aprovado"], x[3]["nota"], x[0]["versao"]))
    if v["hash"] != _hash_versao(escolhido, banco):
        # o banco mudou (busca de evidência) depois da auditoria dessa versão: a aprovação dela não vale mais
        log.warning("versão %d foi auditada com outro banco — auditoria completa de novo", v["versao"])
        escolhido, problemas, qualidade = _auditar_versao(escolhido, banco, topic, previas)
        v = {**v, "reauditada": True, "problemas_factuais": len(problemas),
             "nota_factual": (qualidade.get("factual") or {}).get("nota"),
             "nota_editorial": qualidade["nota"],
             "aprovado": not problemas and bool(qualidade.get("aprovado", qualidade["nota"] >= NOTA_MINIMA))}
        relatorio["versoes"].append(v)
    if v["versao"] != versoes[-1][0]["versao"]:
        log.info("fica a versão %d (editorial %.1f): a(s) seguinte(s) não ficou(aram) melhor(es)", v["versao"], v["nota_editorial"])
    relatorio["versao_final"] = v["versao"]
    relatorio["qualidade"] = qualidade
    if not v["aprovado"]:
        _registra_texto(escolhido, "qualidade baixa")
        raise RoteiroReprovado(f"qualidade narrativa {qualidade['nota']:.1f}/10 (mínimo {NOTA_MINIMA}) ou com trava "
                               f"editorial depois de {relatorio['edicoes']} edição(ões): " + "; ".join(qualidade["problemas"])[:300])
    return escolhido, relatorio


def _registra_texto(script: dict, motivo: str) -> None:
    """Roteiro reprovado fica no log, frase a frase com tipo e evidências (diagnóstico do dia sem vídeo)."""
    log.warning("roteiro reprovado (%s):\n%s", motivo, "\n".join(
        f"  [{f.get('tipo')}; {','.join(f.get('evidencias') or []) or '-'}] {f['texto']}" for _, _, f in frases_de(script)))


def _chave_rascunho(topic: str, banco: dict) -> list:
    import hashlib
    ids = json.dumps([(e["id"], e["afirmacao"], e["status"]) for e in banco.get("evidencias", [])], ensure_ascii=False)
    from . import disk_cache
    # a versão do código entra na chave: regra ou meta de duração mudou, o rascunho antigo não volta
    return [1, disk_cache.versao_do_codigo("redacao.py"), topic, hashlib.sha1(ids.encode()).hexdigest()]


def guardar_rascunho(topic: str, banco: dict, script: dict) -> None:
    """19/11: o roteiro escrito (e depois o auditado) fica em cache pelo tema + banco: se os provedores
    caírem depois, a retomada do job não escreve de novo (nem perde o que já foi auditado)."""
    from . import disk_cache
    disk_cache.put("roteiro_redacao", _chave_rascunho(topic, banco), {k: v for k, v in script.items() if k != "_banco"})


def rascunho(topic: str, banco: dict) -> dict | None:
    from . import disk_cache
    salvo = disk_cache.get("roteiro_redacao", _chave_rascunho(topic, banco), ttl_days=3)
    return {**salvo, "_banco": banco} if salvo else None


def conferencia_final(script: dict, banco: dict, topic: str = "") -> dict:
    """Depois das etapas que mexem no texto (corte do fim, ortografia, encurtamento): realinha os
    metadados e confere de novo, em código. Frase que perdeu a sustentação sai e fica no log."""
    script, problemas = auditar_codigo(sincronizar(script), banco, topic)
    if not problemas:
        return script
    tirar = {(p["cena"], p["frase"]) for p in problemas}
    log.warning("conferência final: %d frase(s) sem evidência depois do acabamento — saem: %s", len(tirar),
                " | ".join(p["termo"][:60] for p in problemas))
    cenas = []
    for i, sc in enumerate(script["scenes"]):
        frases = [f for j, f in enumerate(sc["frases"]) if (i, j) not in tirar]
        if frases:
            cenas.append({**sc, "frases": frases, "narration": " ".join(f["texto"] for f in frases)})
    return {**script, "scenes": cenas}


def roteiro_insuficiente(script: dict, min_palavras: int) -> str | None:
    """Critério de tamanho do modo redação: só a duração (sem mínimo de números)."""
    n = _palavras(script)
    return None if n >= min_palavras else f"roteiro final curto demais: {n} palavras (mínimo {min_palavras} para a duração)"
