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
from pathlib import Path
from typing import Callable

from .evidencias import FORCA, STATUS_USAVEIS, adicionar_fontes, bloco_para_escritor, numeros_da_evidencia, por_id, usaveis
from .fatos import _pares, estima_antes, extenso_para_algarismos, numeros, temperaturas
from .fontes import dobrar
from .ordem import ordem_da_frase

log = logging.getLogger("redacao")

TIPOS = ("factual", "explicativa", "narrativa")
PALAVRAS_POR_MINUTO = 135  # ritmo medido da voz (edge-tts Antonio)
NOTA_MINIMA = 6.5           # qualidade narrativa (0-10) abaixo disso o roteiro não sai
FAIXA_DUVIDA = (NOTA_MINIMA - 0.5, NOTA_MINIMA + 0.3)  # 26/11: nota editorial nesta faixa ganha LEITURAS_EXTRA (mediana de 5, não de 3)
LEITURAS_EXTRA = 2
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
    # 25/11 (Svalbard 1020: "Comenta SEMENTE se…" reprovada como nome próprio): a palavra do chamado ao espectador
    # não é afirmação
    texto = _COMENTA.sub("Comenta", texto)
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
                if nums and validas and not any(p["cena"] == i and p["frase"] == j for p in problemas):
                    faixa = faixa_de_fontes_divergentes(texto, citadas, banco)
                    if faixa:
                        problemas.append(_p("divergencia_como_faixa", texto, i, j, faixa))
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


_FAIXA = re.compile(r"(?:\b(?:entre|de)\s+)?(?:(?:cerca de|aproximadamente|uns|umas)\s+)?([−–-]?\s?\d[\d.,]*)"
                    r"\s*(?:°\s?C|%|[a-zà-ÿ]{1,12})?\s+(?:a|e|até)\s+([−–-]?\s?\d[\d.,]*)\s*(°\s?C|%|[a-zà-ÿ]+)", re.I)


def faixa_de_fontes_divergentes(texto: str, citadas: list[dict], banco: dict) -> str | None:
    """23/11 (Svalbard v2 do job 1005: "cerca de 120 a 130 metros", com 120 de uma fonte e 130 de outra): dois
    valores de fontes DIFERENTES para a mesma medida ditos como faixa ("entre X e Y", "X a Y") parecem um
    intervalo confirmado. Só é faixa se UMA evidência traz os dois valores; dita a divergência ("as fontes
    divergem", "segundo uma… outra…"), passa. Devolve o motivo ou None."""
    from .fatos import _pares
    t = extenso_para_algarismos(texto)
    if _DIVERGENCIA_DITA.search(t):
        return None
    for m in _FAIXA.finditer(t):
        trecho = t[m.start():m.end()]
        if not any(u != "ano" for _, u in _pares(trecho)):
            continue  # "entre 2017 e 2019", "de 3 a 5 vezes": não é medida
        a, b = numeros(m.group(1)), numeros(m.group(2))
        if not a or not b or a == b:
            continue
        candidatas = citadas + [e for e in usaveis(banco) if e not in citadas]
        if any(a & numeros_da_evidencia(e) and b & numeros_da_evidencia(e) for e in candidatas):
            continue  # uma fonte dá a faixa: legítima
        # a mesma medida (unidade e sinal) nas duas fontes: 120 METROS de uma e 130 METROS de outra
        # (numa faixa a unidade vem só depois do 2º número: "100 e 120 metros" vale para os dois)
        so_a, so_b = f"{m.group(1)} {m.group(3)}", f"{m.group(2)} {m.group(3)}"
        donas_a = [e["id"] for e in candidatas if a & numeros_da_evidencia(e) and _mesma_medida(min(a), so_a, e)]
        donas_b = [e["id"] for e in candidatas if b & numeros_da_evidencia(e) and _mesma_medida(min(b), so_b, e)]
        if donas_a and donas_b:
            return (f"\"{trecho.strip()}\" junta como faixa valores de fontes diferentes ({min(a)}: "
                    f"{', '.join(donas_a[:3])}; {min(b)}: {', '.join(donas_b[:3])}) e nenhuma fonte dá essa faixa — "
                    "diga que as fontes trazem valores diferentes, identificando cada uma, ou use um valor só")
    return None


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
# 22/11 (Svalbard, jobs 999-1001): o escritor e o editor trocavam a escala do que a fonte diz ("bancos danificados"
# → "perderam coleções inteiras"; "umidade na primavera" → "sempre trazia umidade"; um episódio → "infiltrações
# recorrentes"), e a edição trocava um exagero por outro. Precisão é parte do fato: quantidade, frequência,
# certeza, extensão, causa, tempo e medida ficam no nível da evidência.
_REGRA_PRECISAO = """PRECISÃO (o fato inclui a ESCALA que a fonte dá; mais forte que ela é exagero, mesmo com o resto certo):
- quantificador: todos / a maioria / alguns / nenhum — só o que a fonte diz ("alguns bancos" não vira "os bancos");
- frequência: sempre / frequentemente / às vezes / uma vez — um episódio não vira "recorrente", "sempre" ou "toda vez";
- certeza: confirmado / provável / possível — estimativa e hipótese continuam estimativa e hipótese;
- extensão: inteiro / parcial / total — "danificado" não vira "perdido por inteiro", "parte" não vira "tudo";
- causa: só a relação causal que a fonte afirma; acontecer junto ou depois não é causar;
- tempo: o fato vale para a data ou o período da fonte (o número de 2025 não é "hoje" sem a data);
- natureza do mecanismo: o que depende de máquina (refrigeração, bomba) não é "natural";
- divergência entre fontes é divergência ("uma fonte diz X, outra Y"), nunca faixa ("entre X e Y") nem média;
- unidades e medidas exatamente como na evidência (comprimento do túnel não é profundidade da sala).
Sem sustentação para a escala: diga a versão mais restrita que a evidência sustenta, ou tire a informação.
Nunca troque um exagero por outro equivalente com outras palavras."""
# a mesma regra no pedido do escritor, que precisa caber no limite por pedido do Groq grátis
_REGRA_PRECISAO_CURTA = """- Escala como na fonte (todos/alguns, sempre/uma vez, inteiro/parcial, causa, data, medida); divergência não é faixa."""

# marcadores de escala que a evidência citada precisa trazer (em português ou no idioma da fonte); usados na
# conferência em código da edição: escala → (marcadores na frase, sustentação na evidência)
_PRECISAO: dict[str, tuple[re.Pattern, re.Pattern]] = {
    "quantificador": (re.compile(r"\b(tod[oa]s (os|as)|nenhum[a]?|ninguém|a maioria|a maior parte)\b", re.I),
                      re.compile(r"\b(all|every|none|no one|nobody|most|majority|tod[oa]s|nenhum|maioria|maior parte)\b", re.I)),
    "frequencia": (re.compile(r"\b(sempre|nunca|jamais|frequentemente|constantemente|regularmente|recorrentes?|"
                              r"repetidamente|toda vez|todo ano|todos os anos|ano após ano|anualmente|com frequência|muitas vezes|"
                              r"várias vezes|seguidamente|crônic[oa]s?|permanentemente|permanentes?)\b", re.I),
                   re.compile(r"\b(always|never|frequent\w*|constant\w*|regular\w*|recurr\w*|repeated\w*|every (year|time|"
                              r"spring|summer|winter)|each year|annual\w*|sempre|nunca|frequente\w*|recorrent\w*|todo ano|anual\w*|"
                              r"chronic\w*|permanent\w*|crônic\w*)\b", re.I)),
    "certeza": (re.compile(r"\b(comprovadamente|certamente|com certeza|sem dúvida|garantidamente|definitivamente|"
                           r"está provado|provou que)\b", re.I),
                re.compile(r"\b(proven|proved|confirmed|certain\w*|guarantee\w*|definitely|comprovad\w*|confirmad\w*|garant\w*)\b", re.I)),
    # 23/11 (job 1006): "parcialmente danificado" virou "destruído"
    "extensao": (re.compile(r"\b(inteir[oa]s?|totalmente|completamente|por completo|integralmente|destru[íi]d[oa]s?|"
                            r"destruiu|destruíram|destruição|arrasad[oa]s?|aniquilad[oa]s?)\b", re.I),
                 re.compile(r"\b(entire\w*|whole|complete\w*|total\w*|fully|wholly|inteir\w*|complet\w*|integral\w*|"
                            r"destr\w*|wiped out|razed|annihilat\w*)\b", re.I)),
    "natureza": (re.compile(r"\bnaturalmente\b", re.I), re.compile(r"\b(natural\w*)\b", re.I)),
    "causa": (re.compile(r"\b(causou|causaram|provocou|provocaram|por causa d[eoa]s?|graças a|fez com que|levou a|"
                         r"resultou)\b", re.I),
              re.compile(r"\b(caus\w*|because|due to|led to|lead to|result\w*|owing to|provoc\w*|por causa|levou|graças|"
                         r"devido|em razão|por conta|em consequência|consequência)\b", re.I)),
}
_DIVERGENCIA_DITA = re.compile(r"\b(segundo uma|uma fonte|outra fonte|outras fontes|as fontes divergem|divergem|"
                               r"varia conforme|dependendo da fonte|há registros de)\b", re.I)


def marcadores_sem_suporte(texto: str, evidencias: list[dict]) -> dict[str, str]:
    """Escala de precisão → palavra da frase que a evidência citada não sustenta (nem a afirmação verificada
    nem os trechos). Vazio se a frase não usa marcador de escala ou se a evidência traz um equivalente."""
    base = " ".join([e.get("afirmacao", "") for e in evidencias]
                    + [c.get("trecho", "") for e in evidencias for c in e.get("citacoes") or [] if c.get("verificado")])
    out = {}
    for cat, (marca, apoio) in _PRECISAO.items():
        m = marca.search(texto)
        if m and not apoio.search(base):
            out[cat] = m.group(0)
    return out


# ---------------------------------------------------------------- mapa do conhecimento e plano da história (23/11)
# Svalbard (jobs 1001-1006): o escritor recebia ~85 evidências e as transformava em frases; o roteiro nascia
# lista e a edição remendava frase a frase, trocando um exagero por outro. Agora o material vira primeiro um
# MAPA (natureza e autoridade de cada evidência, o trecho que a prova), depois um PLANO (ângulo, blocos com
# função, poucas evidências por bloco e, para cada uma, o que comprova e o que NÃO comprova) — e só então o
# escritor conta a história do plano, vendo só as evidências dele.
_ENCICLOPEDIA = re.compile(r"wikipedia\.org|grokipedia|wikiwand|britannica|enciclop", re.I)
_CIENTIFICA = re.compile(r"\.edu\b|\.edu\.|\.ac\.[a-z]{2}\b|universit|scholar|doi\.org|springer|nature\.com|"
                         r"sciencedirect|wiley|pubmed|ncbi|arxiv|journal", re.I)
_INSTITUCIONAL = re.compile(r"\.gov\b|\.gov\.|\.gob\.|\.gouv\.|regjeringen|government|governo|europa\.eu|\bun\.org|"
                            r"fao\.org|unesco|who\.int|worldbank|\.int\b|oecd", re.I)
AUTORIDADES = ("primária", "institucional", "científica", "imprensa/outra", "enciclopédia")


def autoridade_da_citacao(c: dict) -> str:
    """Autoridade da fonte de UMA citação (para qualquer tema): a página oficial da organização responsável
    (classe "oficial" que não é universidade nem enciclopédia) é "primária"; governo e organismos oficiais,
    "institucional"; universidade e periódico, "científica"; enciclopédia (Wikipédia, Grokipedia…) serve
    para descobrir, não pesa como fonte primária."""
    dominio = f"{c.get('dominio', '')} {c.get('url', '')}"
    if c.get("classe") == "enciclopedia" or _ENCICLOPEDIA.search(dominio):
        return "enciclopédia"
    if _CIENTIFICA.search(dominio):
        return "científica"
    if c.get("classe") == "oficial":
        return "primária"
    if _INSTITUCIONAL.search(dominio):
        return "institucional"
    return "imprensa/outra"


def natureza_da_evidencia(e: dict) -> str:
    if e["status"] == "contraditorio":
        return "contraditória"
    if e.get("hipotese"):
        return "hipótese"
    if e.get("estimado"):
        return "estimativa"
    return {"corroborado": "fato confirmado", "oficial": "fonte primária/oficial",
            "fonte_unica": "fonte única", "nao_verificado": "não verificada"}.get(e["status"], e["status"])


def mapa_do_conhecimento(banco: dict) -> list[dict]:
    """Cada evidência utilizável: o que afirma, natureza, a melhor autoridade entre as fontes que a provam,
    os domínios (páginas do mesmo domínio contam uma vez) e o trecho. Não verificada fica fora."""
    mapa = []
    for e in usaveis(banco):
        cits = [c for c in e.get("citacoes") or [] if c.get("verificado")] or list(e.get("citacoes") or [])
        auts = sorted({autoridade_da_citacao(c) for c in cits}, key=AUTORIDADES.index) or ["imprensa/outra"]
        melhor = next((c for c in cits if autoridade_da_citacao(c) == auts[0]), cits[0] if cits else {})
        mapa.append({"id": e["id"], "afirmacao": e["afirmacao"], "natureza": natureza_da_evidencia(e),
                     "autoridade": auts[0], "so_enciclopedia": auts == ["enciclopédia"],
                     "fontes": sorted({c.get("dominio", "") for c in cits if c.get("dominio")}),
                     "conflitos": list(e.get("conflitos") or []), "trecho": str(melhor.get("trecho") or "")})
    return mapa


def bloco_do_mapa(mapa: list[dict], ids: set[str] | None = None, limite_trecho: int = 200,
                  plano: dict | None = None) -> str:
    linhas = []
    for m in mapa:
        if ids is not None and m["id"] not in ids:
            continue
        extra = (", diverge de " + ", ".join(m["conflitos"])) if m["conflitos"] else ""
        extra += " (só enciclopédia: não use como fato central se houver fonte melhor)" if m["so_enciclopedia"] else ""
        linha = f"{m['id']} [{m['natureza']}; autoridade {m['autoridade']}: {', '.join(m['fontes'][:3])}{extra}] {m['afirmacao']}"
        if limite_trecho and m["trecho"]:
            linha += f"\n    trecho: \"{m['trecho'][:limite_trecho]}\""
        lim = ((plano or {}).get("evidencias") or {}).get(m["id"])
        if lim:
            linha += f"\n    comprova: {lim.get('comprova', '')[:220]}\n    NÃO comprova: {lim.get('nao_comprova', '')[:220]}"
        linhas.append(linha)
    return "\n".join(linhas)


_PROMPT_PLANO = """Você planeja a HISTÓRIA de um vídeo (YouTube, {min_min:g} a {max_min:g} min, português do Brasil). Tema: {tema}
Ainda NÃO escreva o roteiro: entenda o material e decida o que contar e como.

MAPA DO CONHECIMENTO (cada item: natureza, autoridade da fonte, domínios e o trecho que o prova):
{mapa}

Como ler o mapa:
- AUTORIDADE: "primária" (a organização responsável documentando o próprio projeto: basta, se o trecho diz
  diretamente o que se quer afirmar) > "institucional" (governo/organismo oficial, na competência dele) >
  "científica" > "imprensa/outra" > "enciclopédia" (serve para descobrir; fato central só com fonte melhor,
  quando existir). Autoridade não é infalibilidade: a fonte tem de dizer DIRETAMENTE o que vai ser afirmado.
- Quantidade de páginas não decide a verdade: páginas que repetem o mesmo relatório são uma fonte só.
- Valores diferentes para a mesma medida: veja se medem a mesma coisa, a data, a definição; se não der para
  resolver, atribua cada valor à sua fonte ou deixe de fora. Nunca transforme em faixa.
- Estimativa é estimativa, hipótese é hipótese, parcial é parcial, um episódio não é regra, a característica
  de uma coisa não passa para outra.

PLANEJE:
0) O GANCHO (a 1ª frase do vídeo, até 12 palavras): escolha UMA fórmula desta lista e o fato MAIS ESTRANHO do
   material que a sustenta (verdadeiro e entregue no vídeo; o fato, não o contexto):
{formulas}
1) O ÂNGULO: a pergunta que a abertura desperta e a resposta que a conclusão entrega — só com o que o material
   sustenta (sem apoio, escolha outro ângulo dentro do tema). E o PONTO DE VISTA: uma frase com a leitura do
   canal sobre a história, apoiada nos fatos (sem número novo, sem exagero), dita no fechamento.
2) De 5 a 9 BLOCOS, em ordem, cada um com uma FUNÇÃO na história (por exemplo: abertura, o problema que existe,
   como funciona, por que ali, como guarda e protege, o que guarda de fato, como entra e sai, o que já
   aconteceu, os limites, conclusão — use só os que o material sustenta), o que o bloco conta e as evidências
   de que ele precisa: NO MÁXIMO 3 por bloco. Um exemplo concreto vale mais que uma lista de países, datas,
   números, siglas ou tratados — o bloco explica UMA coisa, não acumula tudo o que se sabe sobre ela.
3) Para CADA evidência usada: o que ela comprova e o que NÃO comprova (a escala exata: parte ou todo, uma vez ou
   sempre, possível ou aconteceu, causa ou coincidência, de quem é a característica, que medida e unidade).
Não use todas as evidências: escolha o que explica a história; o resto fica de fora.
Responda SÓ JSON:
{{"gancho": {{"formula": "nome_da_formula", "evidencia": "E3", "frase": "até 12 palavras"}},
  "angulo": {{"pergunta": "...", "resposta": "...", "ponto_de_vista": "..."}},
  "blocos": [{{"funcao": "abertura", "conta": "...", "evidencias": ["E1"]}}],
  "evidencias": {{"E1": {{"comprova": "...", "nao_comprova": "..."}}}},
  "nao_exagerar": ["..."]}}"""


EVID_POR_BLOCO = 4  # o pedido diz 3; o código corta no 4º

# 25/11 (grupo A das melhorias; referência: projeto-canais docs/ganchos.md): os 3 primeiros segundos decidem se a
# pessoa fica. O plano escolhe UMA fórmula, nunca a dos últimos vídeos do canal, e o gancho sai de um fato aprovado
FORMULAS_GANCHO = {
    "numero_absurdo": "um número do material que parece impossível",
    "inversao": "o contrário do que todo mundo pensa",
    "comparacao_de_tempo": "antes × agora, ou quanto tempo algo leva",
    "voce_fez_sem_saber": "algo que o espectador faz sem saber o porquê",
    "impossivel_real": "o que parece impossível e existe",
    "objeto_comum_com_segredo": "uma coisa comum com um segredo",
    "forma_estranha": "um formato ou lugar estranho",
    "permanencia": "algo que dura muito mais do que se imagina",
    "origem_literal": "de onde veio, ao pé da letra",
    "durabilidade": "o que resiste a quase tudo",
    "movimento_invisivel": "algo acontecendo agora sem ninguém ver",
    "proibido": "o que é proibido ou só pouca gente pode fazer",
    "mito_derrubado": "um mito que o material desmente",
    "escala_humana": "um tamanho traduzido para o corpo ou o dia a dia",
    "coincidencia_historica": "duas coisas que aconteceram juntas",
    "pergunta_que_ninguem_fez": "uma pergunta óbvia que ninguém faz",
    "recorde": "o maior, o menor, o primeiro (só se a fonte disser)",
    "contagem_regressiva": "um prazo ou contagem",
    "consequencia_inesperada": "um efeito que ninguém esperava",
    "o_que_acontece_se": "o que acontece se…?",
    "lugar_secreto": "um lugar que quase ninguém vê por dentro",
    "quem_decide": "quem tem a chave ou a última palavra",
}
GANCHOS_SEM_REPETIR = 3  # fórmulas dos últimos vídeos do canal que o plano não pode repetir
_GANCHOS_USADOS = Path(__file__).resolve().parent.parent / "data" / "ganchos_usados.json"


def ganchos_recentes(canal: str) -> list[str]:
    try:
        return list(json.loads(_GANCHOS_USADOS.read_text()).get(canal) or [])[-GANCHOS_SEM_REPETIR:]
    except (OSError, ValueError):
        return []


def registrar_gancho(canal: str, formula: str) -> None:
    if not canal or formula not in FORMULAS_GANCHO:
        return
    try:
        dados = json.loads(_GANCHOS_USADOS.read_text()) if _GANCHOS_USADOS.exists() else {}
    except (OSError, ValueError):
        dados = {}
    dados[canal] = (list(dados.get(canal) or []) + [formula])[-20:]
    _GANCHOS_USADOS.parent.mkdir(parents=True, exist_ok=True)
    _GANCHOS_USADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=1))


def bloco_formulas(evitar: list[str]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in FORMULAS_GANCHO.items() if k not in evitar)


def _plano_valido(d: dict, validos: set[str]) -> dict | None:
    blocos = [b for b in d.get("blocos") or [] if isinstance(b, dict) and str(b.get("conta", "")).strip()]
    if len(blocos) < 4 or not isinstance(d.get("angulo"), dict):
        return None
    for b in blocos:
        # 23/11 (1º plano de Svalbard: blocos com 11 evidências viraram cenas-lista): no máximo EVID_POR_BLOCO
        b["evidencias"] = [x for x in b.get("evidencias") or [] if x in validos][:EVID_POR_BLOCO]
    usados = {x for b in blocos for x in b["evidencias"]}
    lim = {k: v for k, v in (d.get("evidencias") or {}).items() if k in usados and isinstance(v, dict)}
    if len(usados) < 4 or len(lim) < len(usados) * 0.8:
        return None
    g = d.get("gancho") if isinstance(d.get("gancho"), dict) else {}
    gancho = ({"formula": str(g.get("formula")), "evidencia": str(g.get("evidencia") or ""),
               "frase": str(g.get("frase") or "")[:160]}
              if g.get("formula") in FORMULAS_GANCHO and str(g.get("evidencia") or "") in validos else None)
    return {"angulo": d["angulo"], "blocos": blocos, "evidencias": lim, "gancho": gancho,
            "nao_exagerar": [str(x)[:200] for x in d.get("nao_exagerar") or []][:10]}


def planejar_historia(topic: str, banco: dict, *, min_minutes: float, max_minutes: float,
                      canal: str = "") -> dict | None:
    """O plano da história a partir do mapa (uma chamada, guardada em cache pelo tema + banco + código).
    None se nenhum modelo forte devolver um plano válido (o escritor volta a ler o banco inteiro)."""
    import hashlib
    from . import disk_cache, metricas
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    mapa = mapa_do_conhecimento(banco)
    validos = {m["id"] for m in mapa}
    pedido = _PROMPT_PLANO.format(tema=topic, mapa=bloco_do_mapa(mapa, limite_trecho=180),
                                  min_min=min_minutes, max_min=max_minutes,
                                  formulas=bloco_formulas(ganchos_recentes(canal)))
    chave = [1, hashlib.sha1(pedido.encode()).hexdigest()]
    salvo = disk_cache.get("plano_historia", chave, ttl_days=7)
    if salvo:
        return salvo
    try:
        with metricas.etapa("plano_historia"):
            raw = complete([{"role": "user", "content": pedido}], max_tokens=6000, temperature=0.3,
                           validate=lambda r: _plano_valido(_obj(r), validos) is not None, model_filter=is_strong_model)
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("plano da história indisponível: %s — o escritor lê o banco inteiro", exc)
        return None
    plano = _plano_valido(_obj(raw), validos)
    disk_cache.put("plano_historia", chave, plano)
    log.info("plano da história: %s → %s; %d blocos: %s", plano["angulo"].get("pergunta", "")[:120],
             plano["angulo"].get("resposta", "")[:120], len(plano["blocos"]),
             " | ".join(f"{b.get('funcao')} {b['evidencias']}" for b in plano["blocos"]))
    return plano


def material_do_plano(plano: dict, banco: dict) -> str:
    ids = {x for b in plano["blocos"] for x in b["evidencias"]}
    blocos = "\n".join(f"{k}. [{b.get('funcao', '')}] {b.get('conta', '')} (evidências: {', '.join(b['evidencias']) or '-'})"
                       for k, b in enumerate(plano["blocos"], 1))
    g = plano.get("gancho") or {}
    return (f"PLANO DA HISTÓRIA (siga o ângulo e a ordem; cada bloco vira uma ou mais cenas com a FUNÇÃO dele):\n"
            + (f"Gancho (1ª frase, fórmula {g.get('formula')}, fato {g.get('evidencia')}): {g.get('frase', '')}\n"
               if g.get("frase") else "")
            + f"Pergunta da abertura: {plano['angulo'].get('pergunta', '')}\n"
            f"Resposta da conclusão: {plano['angulo'].get('resposta', '')}\n"
            + (f"Ponto de vista (dito no fechamento): {plano['angulo']['ponto_de_vista']}\n"
               if plano["angulo"].get("ponto_de_vista") else "")
            + f"{blocos}\n"
            + ("Não exagerar: " + "; ".join(plano["nao_exagerar"]) + "\n" if plano.get("nao_exagerar") else "")
            + "\nEVIDÊNCIAS DO PLANO (a base da história; cada uma com o trecho, o que comprova e o que NÃO comprova):\n"
            + bloco_do_mapa(mapa_do_conhecimento(banco), ids=ids, limite_trecho=400, plano=plano)
            + _outros_fatos(banco, ids))


def _outros_fatos(banco: dict, ids: set[str]) -> str:
    """25/11 (catálogo Svalbard): o escritor só via o bloco do plano e escrevia fato que existe no banco citando o
    id errado (ICARDA/E94, "três janelas"/E74). Os outros fatos aprovados vão junto, só a afirmação."""
    from .evidencias import usaveis
    resto = {e["id"] for e in usaveis(banco)} - ids
    if not resto:
        return ""
    return ("\n\nOUTROS FATOS APROVADOS (use só se a história pedir; cite o id de cada um que usar):\n"
            + bloco_para_escritor(banco, 0, ids=resto))


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
    lista += fato_em_duas_cenas(script) + narrativa_que_afirma(script) + abertura_e_fechamento(script)
    # 25/11 (grupo B): chavões de "texto com cara de IA" e abertura genérica (src/qualidade_texto.py, em código)
    from .qualidade_texto import defeitos_de_texto
    lista += defeitos_de_texto(script)
    return script, lista, problemas


_PEDE_LIKE = re.compile(r"\b(deixa (o|seu) like|deixe (o|seu) like|se inscrev|inscreva-se|ative o sininho|"
                        r"ativa o sininho)\b", re.I)
_COMENTA = re.compile(r"\b[Cc]omenta(r)? [A-ZÁÉÍÓÚÂÊÔÃÕÇ]{3,}\b")
GANCHO_MAX_PALAVRAS = 16  # o pedido diz 12; folga para "Em 2008," e afins


def abertura_e_fechamento(script: dict) -> list[str]:
    """25/11 (grupo A): gancho curto e não genérico; fim com pergunta ao espectador ou "Comenta PALAVRA";
    nunca pedido de like/inscrição no texto (o canal põe o dele)."""
    frases = [f["texto"].strip() for _, _, f in frases_de(script)]
    if not frases:
        return []
    out = []
    primeira = frases[0]
    # abertura genérica: qualidade_texto.cara_de_ia (conhece o bordão de cada canal)
    if len(primeira.split()) > GANCHO_MAX_PALAVRAS:
        out.append(f"a 1ª frase tem {len(primeira.split())} palavras: o gancho tem até 12")
    if not (frases[-1].endswith("?") or _COMENTA.search(frases[-1])):
        out.append("o fim não chama o espectador: feche com uma pergunta a ele ou \"Comenta PALAVRA se…\"")
    pede = next((f for f in frases if _PEDE_LIKE.search(f)), None)
    if pede:
        out.append(f"a frase \"{pede[:80]}\" pede like/inscrição: tire (o canal põe o dele)")
    return out


def fato_em_duas_cenas(script: dict) -> list[str]:
    """25/11 (catálogo Svalbard: sem_repeticao 2 a 5 em toda versão): o mesmo fato aprovado citado em duas cenas é
    o mesmo fato contado duas vezes."""
    onde: dict[str, list[int]] = {}
    for i, _, f in frases_de(script):
        for e in f.get("evidencias") or []:
            if i + 1 not in onde.setdefault(e, []):
                onde[e].append(i + 1)
    return [f"o fato {e} aparece nas cenas {', '.join(map(str, c))}: conte uma vez só" for e, c in onde.items() if len(c) > 1]


_ALEGA = re.compile(r"\b(funciona|funcionou|garante|garantiu|prova|provou|total|totalmente|sempre|já|porque|por isso|"
                    r"graças|salvou|protege|resolve|resolveu)\b", re.I)


def narrativa_que_afirma(script: dict) -> list[str]:
    """25/11 (catálogo: "A transparência é total", "O seguro já funcionou", "Ela funciona porque…"): narrativa sem
    número nem nome, mas afirmando. Pergunta é livre."""
    out = []
    for _, _, f in frases_de(script):
        t = f["texto"].strip()
        if f.get("tipo") == "narrativa" and not t.endswith("?"):
            m = _ALEGA.search(t)
            if m:
                out.append(f"a frase narrativa \"{t[:90]}\" afirma algo (\"{m.group(0)}\"): ou vira fato com id, ou sai")
    return out


# ---------------------------------------------------------------- escrita livre + conferência na hora (25/11)
# 24/11 dois extremos: IA escrevendo livre contava bem (editorial 7,0) mas completava com o que sabe ("baixa
# umidade", "transgênicas"); montando só frases da fonte isoladas, os fatos ficavam limpos e a história virava
# lista de fragmentos (editorial 4,1). Agora a IA escreve livre, com tom de história, e ANTES da auditoria cada
# frase factual é conferida contra o TRECHO das evidências que cita: a que diz mais do que o trecho é reescrita
# na hora pela IA com o motivo (só aquela frase, no lugar dela) e conferida de novo; a que ainda excede sai.
_PROMPT_ESCRITOR = """Você é o roteirista do canal "{canal}" (YouTube, português do Brasil). Tema: {tema}

{material}

Sua tarefa: CONTAR COMO HISTÓRIA o que as evidências contam — tom de quem conta um caso a um amigo, com
começo curioso, tensão e resposta — sem inventar nada. Você escolhe as evidências, a ordem e as palavras.
- Cada evidência é um FATO APROVADO, conferido contra a fonte. Cada frase factual/explicativa reconta um fato
  (ou dois do mesmo assunto) e cita o id de TODO fato que usa: pode dizer menos, nunca mais — a frase é conferida
  contra o fato e o excesso é cortado. Nada de causa, finalidade, comparação ou detalhe que você "sabe";
  mantenha as ressalvas do fato ("ajuda a", "cerca de").
- NUNCA ligue fatos com "por isso", "porque", "graças a", "o que garante", salvo se o fato diz a causa.
- Narrativa (abertura, ligação, pergunta) não afirma: sem número, nome, "funciona", "garante", "já".
- 1ª frase = gancho (até 12 palavras, o fato mais estranho; nunca "você sabia"/"neste vídeo"). O fim volta ao
  começo, diz o ponto de vista e fecha com pergunta ao espectador ou "Comenta PALAVRA se…"; sem pedir like.
- Prefira o que faz história (o que aconteceu, por que ali, como funciona) ao institucional.
REGRAS DE PRECISÃO (inegociáveis):
- Evidência "contraditorio": diga os dois valores e que as fontes divergem, ou não use.
- "estimado": diga como aproximação ("cerca de"). Valor medido/exato fica EXATO, mesmo de fonte única.
- "hipotese": diga como hipótese ("acredita-se", "uma das explicações"), nunca como fato.
- "fonte única" com recorde ou superlativo: diga de onde vem ("segundo os registros", "a menor já
  registrada"); não apresente como consenso.
- Números em algarismos; número grande (mil ou mais) arredondado com "cerca de" (303.863 → "cerca de
  300 mil"). Cada número UMA vez.
- Decimal (−64,4 °C): o valor da evidência, ou o arredondamento dito e no lado certo ("mais de 64 graus
  negativos"). Mantenha o sinal (negativos, abaixo de zero) e diga o que o número é (recorde, média).
- Número que nenhuma evidência traz: não diga. Nunca outro número no lugar do que a evidência traz.
- Datas só as que mudam a história.
{regra_precisao}
- Sem saudação inicial e sem pedir like/inscrição (entram depois).
{linha_canal}
FORMATO: entre {cenas_min} e {cenas_max} cenas (cada cena 1 a 3 frases), no total entre {min_pal} e {max_pal}
palavras faladas (mire em ~{alvo_pal}); com evidência de menos, fique mais curto — nunca invente. Cada fato
UMA vez (o mesmo id não volta). Cada frase com:
- "tipo": "factual" (afirma algo sobre o mundo), "explicativa" (explica causa ou funcionamento) ou
  "narrativa" (abertura, transição, pergunta — sem afirmar fato);
- "evidencias": ids cujo TRECHO sustenta a frase (obrigatório em factual e explicativa; vazio em narrativa).
Os ids ficam SÓ no campo "evidencias", nunca no texto.
{feedback}
Responda SÓ JSON:
{{"titulo": "até 60 caracteres, só com o que o roteiro diz", "descricao": "2 a 3 frases", "tags": ["..."],
  "cenas": [{{"frases": [{{"texto": "...", "tipo": "narrativa", "evidencias": []}},
                         {{"texto": "...", "tipo": "factual", "evidencias": ["E2"]}}]}}]}}"""

_PROMPT_CONFERE = """Confira cada FRASE de um roteiro contra os FATOS que ela cita (cada fato é uma afirmação já
conferida contra a fonte; o trecho original vem junto só para tirar dúvida de sentido). Só os fatos e trechos contam,
não o que você sabe. A frase anterior aparece só para entender a quem a frase se refere.
"sustentada": tudo o que a frase afirma está nos fatos citados — recontar com outras palavras, resumir, juntar dois
fatos citados SEM criar relação entre eles e arredondar no sentido certo valem; não reprove estilo nem tom.
"excede": a frase afirma algo que os fatos não dizem — diga exatamente o quê. Contam como "excede" (25/11):
- DEDUÇÃO: conclusão, consequência, finalidade ou causa que o trecho não diz ("por tempo suficiente para
  reparo", "por isso", "o que garante", "para que") — mesmo que pareça lógica;
- TEMPO TROCADO: dado de uma data/estudo/época dito como atual ("hoje", presente) ou de outra data;
- TERMO MAIS FORTE: palavra mais ampla ou absoluta que a do trecho ("regiões do sul" → "Hemisfério Sul",
  "muitos" → "todos", "ajuda" → "garante", "pode" → "vai");
- SENTIDO INVERTIDO: o trecho diz que algo NÃO bastava/não era e a frase usa como se bastasse;
- JUNÇÃO: dois trechos combinados numa relação (causa, comparação) que nenhum deles afirma.
{itens}
Responda SÓ JSON: {{"decisoes": [{{"n": 1, "decisao": "sustentada", "o_que_excede": ""}}]}}"""

_PROMPT_REESCREVE = """Estas frases de um roteiro de vídeo (tema: {tema}) dizem mais do que a fonte. Reescreva cada
uma para dizer SÓ o que o TRECHO diz (pode dizer menos, nunca mais), no mesmo lugar da história: mantenha a
ligação com a frase anterior e com a seguinte, o tom falado e o tamanho parecido. Se nada da frase cabe no
trecho, responda texto vazio (a frase sai). Sem dedução, sem "hoje" para dado de outra época, sem termo mais forte
que o do trecho.
{itens}
Responda SÓ JSON: {{"frases": [{{"n": 1, "texto": "..."}}]}}"""

CONFERE_POR_CHAMADA = 12  # frases por pedido (decisão + motivo de cada uma; mais que isso arrisca resposta cortada)
TRECHO_CONFERE = 600


def _trechos_das(ids: list[str], mapa: dict[str, dict]) -> str:
    """25/11: o FATO APROVADO (afirmação em português já conferida contra a fonte) é a régua; o trecho original
    vai junto só para dúvida de sentido. Português × português: o juiz rígido não reprova tradução."""
    return "\n".join(f"   FATO {x}: \"{mapa[x]['afirmacao']}\""
                     + (f"\n      (trecho original: \"{mapa[x]['trecho'][:TRECHO_CONFERE]}\")" if mapa[x].get("trecho") else "")
                     for x in ids[:4] if x in mapa) or "   (nenhuma evidência válida citada)"


# 25/11 (Svalbard 1017: "Por isso a engenharia adiciona refrigeração"): conector de causa na frase só passa se
# algum trecho citado também afirma causa; o juiz ainda confere se é ESSA causa
_CONECTOR_CAUSAL = re.compile(r"\b(por isso|por causa diss[oa]|portanto|consequentemente|daí que|graças a|"
                              r"o que garante|o que permite|o que faz com que|é por isso|é assim que|por esse motivo|"
                              r"por essa razão|porque|garantindo|assegurando)\b", re.I)
_CAUSA_NO_TRECHO = re.compile(r"\b(because|therefore|thus|hence|as a result|due to|owing to|consequently|so that|"
                              r"in order to|that is why|which is why|reason|porque|por isso|portanto|devido|graças|"
                              r"por causa|razão|motivo|para que|a fim de|fordi|derfor)\b", re.I)


def causa_sem_trecho(texto: str, ids: list[str], mapa: dict[str, dict]) -> str | None:
    m = _CONECTOR_CAUSAL.search(texto)
    if not m:
        return None
    trechos = " ".join(f"{mapa[x].get('trecho') or ''} {mapa[x].get('afirmacao') or ''}" for x in ids if x in mapa)
    if _CAUSA_NO_TRECHO.search(trechos):
        return None
    return f'DEDUÇÃO: "{m.group(0)}" liga dois fatos numa causa que nenhum trecho citado afirma'


_PROMPT_SEGUNDA_OPINIAO = """Um auditor REPROVOU as frases abaixo de um roteiro de vídeo. Antes de a reprovação valer,
releia com calma cada uma: a FRASE, o MOTIVO da reprovação e os FATOS que ela cita (afirmações já conferidas
contra a fonte; o trecho original vem junto só para tirar dúvida de sentido). Só os fatos e trechos contam.
Para cada frase decida:
- "confirma": a reprovação está certa — a frase afirma algo que os fatos não dizem (dedução, causa, finalidade,
  termo mais forte, tempo trocado, número ou sujeito diferente), ou é narrativa que esconde um fato verificável;
- "retira": o auditor errou — a frase diz o mesmo que os fatos, com outras palavras ou resumindo (ou é narrativa
  pura, sem fato), e o motivo aponta algo que não está na frase ou que o próprio fato diz.
Na dúvida, "confirma". Na justificativa cite a palavra da frase e a do fato que decidem.
{itens}
Responda SÓ JSON: {{"revisao": [{{"n": 1, "decisao": "confirma", "justificativa": "..."}}]}}"""


def segunda_opiniao(itens: list[dict], banco: dict) -> dict[int, str]:
    """26/11 (pedido do dono: "o auditor pode parar e pensar — opa, isso está certo? — antes de reprovar"): a
    leitura do público já tem 3 leituras com votação, mas a auditoria frase a frase e a conferência julgavam UMA
    vez. Cada reprovação volta ao juiz fixo (com raciocínio) com o motivo e os fatos; ele confirma ou retira.
    itens: [{"texto", "evidencias", "motivo", "anterior"?}]. Devolve {índice: justificativa} das RETIRADAS.
    Sem resposta, nada é retirado (a reprovação vale)."""
    from .decisoes import ler_lote
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    if not itens:
        return {}
    mapa = {m["id"]: m for m in mapa_do_conhecimento(banco)}
    retiradas: dict[int, str] = {}
    for ini in range(0, len(itens), CONFERE_POR_CHAMADA):
        lote = itens[ini:ini + CONFERE_POR_CHAMADA]
        texto = "\n".join(f"{n}) " + (f"(anterior: {it['anterior'][:160]})\n   " if it.get("anterior") else "")
                          + f"FRASE: {it['texto']}\n   MOTIVO DA REPROVAÇÃO: {it['motivo'][:300]}\n"
                          + _trechos_das(it.get("evidencias") or [], mapa)
                          for n, it in enumerate(lote, 1))
        pedido = [{"role": "user", "content": _PROMPT_SEGUNDA_OPINIAO.format(itens=texto)}]
        valido = lambda r: isinstance(_obj(r).get("revisao"), list)  # noqa: E731
        try:
            try:
                raw = complete(pedido, max_tokens=4000, temperature=0.1, validate=valido, juiz=True)
            except ProvedoresIndisponiveis:
                raise
            except RuntimeError as exc:
                log.warning("juiz da 2ª opinião sem resposta (%s) — a cascata", str(exc)[:120])
                raw = complete(pedido, max_tokens=4000, temperature=0.1, validate=valido, model_filter=is_strong_model)
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("2ª opinião indisponível (%s) — as reprovações valem", exc)
            continue
        revs = [r for r in _obj(raw).get("revisao") or [] if isinstance(r, dict) and str(r.get("n", "")).isdigit()]
        for r, d in zip(revs, ler_lote([r.get("decisao") for r in revs], ("confirma", "retira"), ("retira",),
                                       "segunda_opiniao")):
            n = int(r["n"])
            just = str(r.get("justificativa") or "").strip()
            if d == "retira" and 1 <= n <= len(lote) and len(just.split()) >= 4:
                retiradas[ini + n - 1] = just[:300]
    return retiradas


def conferir_contra_trechos(itens: list[dict], banco: dict) -> dict[int, str | None]:
    """itens: [{"texto", "evidencias", "anterior"}]. Devolve {índice: None (sustentada) | o que excede}.
    Sem resposta da conferência a frase conta como não conferida (nunca passa por falta de juiz).
    25/11 (Svalbard 1017): quem confere é o JUIZ fixo, com raciocínio — sem ele o Nemotron deixou passar
    "por isso" sem causa no trecho e o fundo dos bancos de genes virando o fundo do cofre. Juiz fora do ar →
    a cascata sem raciocínio."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    mapa = {m["id"]: m for m in mapa_do_conhecimento(banco)}
    saida: dict[int, str | None] = {}
    do_juiz: list[int] = []
    for ini in range(0, len(itens), CONFERE_POR_CHAMADA):
        lote = itens[ini:ini + CONFERE_POR_CHAMADA]
        texto = "\n".join(f"{n}) " + (f"(anterior: {it['anterior'][:160]})\n   " if it.get("anterior") else "")
                          + f"FRASE: {it['texto']}\n{_trechos_das(it['evidencias'], mapa)}"
                          for n, it in enumerate(lote, 1))
        decisoes: dict[int, dict] = {}
        pedido = [{"role": "user", "content": _PROMPT_CONFERE.format(itens=texto)}]
        try:
            try:
                raw = complete(pedido, max_tokens=4000, temperature=0.1, validate=lambda r: '"decisoes"' in r,
                               juiz=True)
            except ProvedoresIndisponiveis:
                raise
            except RuntimeError as exc:
                log.warning("juiz da conferência sem resposta (%s) — confere a cascata", str(exc)[:120])
                raw = complete(pedido, max_tokens=4000, temperature=0.1, validate=lambda r: '"decisoes"' in r,
                               model_filter=is_strong_model)
            decisoes = {int(d["n"]): d for d in _obj(raw).get("decisoes") or []
                        if isinstance(d, dict) and str(d.get("n", "")).isdigit()}
        except RuntimeError as exc:
            log.warning("conferência contra os trechos indisponível (%s)", exc)
        from .decisoes import ler_lote
        lidas = dict(zip(decisoes, ler_lote([d.get("decisao") for d in decisoes.values()], ("sustentada", "excede"),
                                            ("sustentada",), "conferencia")))
        for n, it in enumerate(lote, 1):
            d = decisoes.get(n) or {}
            ok = lidas.get(n) == "sustentada"
            saida[ini + n - 1] = (causa_sem_trecho(it["texto"], it.get("evidencias") or [], mapa) if ok
                                  else (str(d.get("o_que_excede") or "").strip()
                                        or ("diz mais do que o trecho" if d else "não conferida")))
            if not ok and d:
                do_juiz.append(ini + n - 1)
    # 26/11: a reprovação do juiz passa pela 2ª opinião; retirada, só a trava de causa em código continua valendo
    if do_juiz:
        retiradas = segunda_opiniao([{**itens[k], "motivo": saida[k]} for k in do_juiz], banco)
        for pos, just in retiradas.items():
            k = do_juiz[pos]
            log.info("conferência: reprovação retirada na 2ª opinião — %s (%s)", itens[k]["texto"][:80], just[:120])
            saida[k] = causa_sem_trecho(itens[k]["texto"], itens[k].get("evidencias") or [], mapa)
    return saida


def _itens_factuais(script: dict) -> list[tuple[int, int, dict]]:
    out = []
    for i, j, f in frases_de(script):
        if f.get("tipo") == "narrativa":
            continue
        ant = script["scenes"][i]["frases"][j - 1]["texto"] if j else (
            script["scenes"][i - 1]["frases"][-1]["texto"] if i and script["scenes"][i - 1].get("frases") else "")
        out.append((i, j, {"texto": f["texto"], "evidencias": list(f.get("evidencias") or []), "anterior": ant}))
    return out


def revisar_citadas(ids: set[str], banco: dict, topic: str) -> None:
    """25/11: antes da conferência, as afirmações que as frases citam são relidas pelo juiz contra o trecho
    (evidencias.revisar_afirmacoes; cache por evidência). Muda o banco NO LUGAR: o plano, a conferência e a
    auditoria passam a ver a afirmação revisada; a descartada deixa de valer como prova."""
    from . import evidencias as ev
    if not ids or not banco.get("evidencias"):
        return
    novo = ev.revisar_afirmacoes(banco, topic, ids=ids)
    if novo is not None and novo is not banco:
        banco["evidencias"][:] = novo["evidencias"]


def conferir_na_escrita(script: dict, banco: dict, topic: str) -> tuple[dict, list[str]]:
    """Confere cada frase factual contra os trechos; reescreve na hora as que excedem (uma vez) e confere de novo;
    as que ainda excedem saem. Devolve (roteiro, motivos do que saiu)."""
    from .providers import complete, is_strong_model
    alvos = _itens_factuais(script)
    if not alvos:
        return script, []
    revisar_citadas({e for _, _, it in alvos for e in it["evidencias"]}, banco, topic)
    veredito = conferir_contra_trechos([it for _, _, it in alvos], banco)
    ruins = [(alvos[k], motivo) for k, motivo in veredito.items() if motivo]
    if not ruins:
        log.info("conferência na escrita: %d frases factuais, todas sustentadas", len(alvos))
        return script, []
    mapa = {m["id"]: m for m in mapa_do_conhecimento(banco)}
    cenas = [dict(sc, frases=[dict(f) for f in sc["frases"]]) for sc in script["scenes"]]

    def seguinte(i: int, j: int) -> str:
        if j + 1 < len(cenas[i]["frases"]):
            return cenas[i]["frases"][j + 1]["texto"]
        return cenas[i + 1]["frases"][0]["texto"] if i + 1 < len(cenas) and cenas[i + 1]["frases"] else ""

    itens = "\n".join(f"{n}) anterior: {it['anterior'][:160]}\n   FRASE: {it['texto']}\n   seguinte: {seguinte(i, j)[:160]}"
                      f"\n   O QUE EXCEDE: {motivo}\n{_trechos_das(it['evidencias'], mapa)}"
                      for n, ((i, j, it), motivo) in enumerate(ruins, 1))
    novas: dict[int, str] = {}
    try:
        raw = complete([{"role": "user", "content": _PROMPT_REESCREVE.format(tema=topic, itens=itens)}],
                       max_tokens=4000, temperature=0.3, validate=lambda r: '"frases"' in r,
                       model_filter=is_strong_model)
        novas = {int(d["n"]): str(d.get("texto") or "").strip() for d in _obj(raw).get("frases") or []
                 if isinstance(d, dict) and str(d.get("n", "")).isdigit()}
    except RuntimeError as exc:
        log.warning("reescrita das frases que excedem indisponível (%s)", exc)
    reescritas = [(n, ruins[n - 1]) for n in range(1, len(ruins) + 1) if len(novas.get(n, "").split()) >= 3]
    segunda = conferir_contra_trechos([{**it, "texto": novas[n]} for n, ((_, _, it), _) in reescritas], banco) \
        if reescritas else {}
    fora: set[tuple[int, int]] = {(i, j) for (i, j, _), _ in ruins}
    for k, (n, ((i, j, it), _)) in enumerate(reescritas):
        if segunda.get(k) is None:
            cenas[i]["frases"][j]["texto"] = novas[n]
            fora.discard((i, j))
    motivos = [f"\"{it['texto'][:80]}\": {m}" for (i, j, it), m in ruins if (i, j) in fora]
    saida = []
    for i, sc in enumerate(cenas):
        frases = [f for j, f in enumerate(sc["frases"]) if (i, j) not in fora]
        if any(f.get("tipo") != "narrativa" for f in frases):
            saida.append({**sc, "frases": frases, "narration": " ".join(f["texto"] for f in frases)})
    log.info("conferência na escrita: %d frases factuais, %d excediam, %d corrigidas, %d cortadas",
             len(alvos), len(ruins), len(ruins) - len(motivos), len(motivos))
    return {**script, "scenes": saida}, motivos


RITMO_VIDEOS = 3
RITMO_CARACTERES = 450


def ritmo_de_referencia(canal: str) -> str:
    """25/11 (grupo A; referência: projeto-canais auditoria.referencias_de_voz): as narrações dos vídeos do canal
    com mais visualizações (data/queue.db video_performance + data/published_scripts) para o escritor imitar o
    RITMO — nunca as frases nem os fatos (fato só do banco, conferido)."""
    import sqlite3
    raiz = Path(__file__).resolve().parent.parent / "data"
    try:
        con = sqlite3.connect(f"file:{raiz / 'queue.db'}?mode=ro", uri=True, timeout=5)
        linhas = con.execute("SELECT job_id FROM video_performance WHERE channel = ? AND views_current > 0 "
                             "ORDER BY views_current DESC LIMIT 12", (canal,)).fetchall()
        con.close()
    except sqlite3.Error:
        return ""
    trechos = []
    for (job,) in linhas:
        try:
            narr = json.loads((raiz / "published_scripts" / canal / f"{job}.json").read_text()).get("narration") or ""
        except (OSError, ValueError):
            continue
        if len(narr.split()) >= 40:
            trechos.append(narr[:RITMO_CARACTERES].rsplit(" ", 1)[0] + "…")
        if len(trechos) >= RITMO_VIDEOS:
            break
    if not trechos:
        return ""
    return ("\n\nRITMO DOS VÍDEOS QUE MAIS FUNCIONARAM NESTE CANAL (imite o ritmo e o tom; NUNCA as frases nem os "
            "fatos — fato só das evidências acima):\n" + "\n".join(f"- {t}" for t in trechos))


def escrever(channel, topic: str, banco: dict, *, min_minutes: float, max_minutes: float,
             cenas_ref: int, feedback: str = "", tentativas: int = 3) -> dict:
    """O roteirista conta a história a partir do banco; cada versão passa pela conferência contra os trechos
    (corrige ou corta o que excede) e pelos defeitos em código. Até `tentativas` pedidos, cada um com os
    defeitos do anterior (inclusive o que precisou ser cortado); fica a versão com menos defeitos."""
    from .providers import complete
    from .script_gen import current_date_rule, recent_mistakes
    # 19/11: meta em palavras pela voz real (~135/min); a duração do áudio decide depois
    min_pal = round(min_minutes * PALAVRAS_POR_MINUTO * 0.9)
    max_pal = round(max_minutes * PALAVRAS_POR_MINUTO)
    cenas_min = max(5, min(cenas_ref - 4, round(min_minutes * 3)))
    cenas_max = max(cenas_ref + 4, round(max_minutes * 4))
    linha = str(getattr(channel, "niche", "") or "")
    plano = planejar_historia(topic, banco, min_minutes=min_minutes, max_minutes=max_minutes,
                              canal=getattr(channel, "name", ""))
    if plano and plano.get("gancho"):
        registrar_gancho(getattr(channel, "name", ""), plano["gancho"]["formula"])
    material = (material_do_plano(plano, banco) if plano else
                "EVIDÊNCIAS (fontes oficiais verificadas: cada item tem o trecho da fonte que o prova):\n"
                + bloco_para_escritor(banco, 0))  # sem plano: o banco inteiro não cabe com trechos
    pedido = _PROMPT_ESCRITOR.format(
        canal=getattr(channel, "channel_title", "") or channel.name, tema=topic, material=material,
        linha_canal=(f"Linha do canal: {linha}\n" if linha else "") + current_date_rule(),
        cenas_min=cenas_min, cenas_max=cenas_max, min_pal=min_pal, max_pal=max_pal,
        alvo_pal=min(max_pal, round(min_pal * 1.25)),
        regra_precisao=_REGRA_PRECISAO_CURTA,
        feedback=(f"\nPARECER DA REVISÃO ANTERIOR (corrija):\n{feedback}\n" if feedback else ""))
    pedido += recent_mistakes(channel.name)
    # 25/11 (grupo A): o ritmo dos vídeos que mais funcionaram no canal; sai se o pedido não couber no Groq
    from .providers import _too_big_for
    ritmo = ritmo_de_referencia(channel.name)
    if ritmo and not _too_big_for("groq", [{"content": pedido + ritmo}], RESPOSTA_ESCRITOR):
        pedido += ritmo
    from .providers import ProvedoresIndisponiveis, is_strong_model
    melhor, menos = None, 10**6
    correcao = ""
    infra: ProvedoresIndisponiveis | None = None
    for n in range(tentativas):
        try:
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
        script, cortadas = conferir_na_escrita(script, banco, topic)
        script, lista, _ = defeitos(script, banco, topic, min_pal, max_pal, cenas_min, cenas_max)
        lista += [f"a frase {m} — a fonte não diz isso (frase cortada)" for m in cortadas]
        log.info("redação, tentativa %d/%d: %d cenas, %d palavras, %d defeito(s)%s", n + 1, tentativas,
                 len(script["scenes"]), _palavras(script), len(lista), (": " + " | ".join(lista[:4])) if lista else "")
        # forma (tamanho, cenas, lista, repetição) pesa 3x; frase cortada pesa 1 (já saiu do roteiro)
        peso = sum(1 if x.startswith("a frase") else 3 for x in lista)
        if not min_pal <= _palavras(script) <= max_pal:
            peso += 100
        if peso < menos:
            melhor, menos = script, peso
        if not lista:
            break
        correcao = "\n\nO roteiro anterior tinha estes defeitos; corrija TODOS:\n" + "\n".join(f"- {x}" for x in lista)
    if melhor is None:
        if infra is not None:
            raise infra
        raise RoteiroReprovado("escritor indisponível: nenhum modelo capaz respondeu com o roteiro")
    if plano:
        melhor["_plano"] = plano
    return escrever_fechamento(melhor, plano, topic, max_pal)


_PROMPT_FECHAMENTO = """Escreva o FECHAMENTO de um vídeo (tema: {tema}). O roteiro abaixo já contou os fatos.
São 2 ou 3 frases faladas, nesta ordem:
1. volta à pergunta da abertura e a responde com o que o vídeo JÁ mostrou (sem fato novo);
2. o ponto de vista, dito como opinião ("para mim", "o que mais me impressiona"), sobre o que o vídeo mostrou;
3. uma pergunta direta ao espectador OU "Comenta PALAVRA se…" (PALAVRA em maiúsculas, uma palavra do tema).
Proibido: número, data, nome que o roteiro não citou, pedir like ou inscrição, e estas palavras (mesmo que a
pergunta da abertura use alguma delas — então responda com outro verbo): {proibidas}. A opinião fala do que VOCÊ sente diante do que o vídeo
mostrou ("para mim, o que mais impressiona é a ideia de uma cópia de segurança para o mundo inteiro"), nunca
explica causa nem avalia resultado.
Pergunta da abertura: {pergunta}
Ponto de vista do plano: {ponto}
ROTEIRO:
{roteiro}
Responda SÓ JSON: {{"frases": ["...", "..."]}}"""


# a lista do pedido sai da MESMA regex que confere (Svalbard 26/11: "protege" estava na regex e fora do pedido;
# a pergunta da abertura usava "protege" e o fechamento foi recusado 3× pela mesma palavra)
_PALAVRAS_ALEGA = ", ".join(f'"{p}"' for p in _ALEGA.pattern.split("(", 1)[1].split(")", 1)[0].split("|"))
_OPINIAO = re.compile(r"\b(para mim|pra mim|me impressiona|eu acho|na minha opinião|o que mais me)\b", re.I)


def escrever_fechamento(script: dict, plano: dict | None, topic: str, max_pal: int, tentativas: int = 3) -> dict:
    """26/11 (Svalbard 1021–1023: conclusão 4–5 em toda versão; o roteiro terminava num dado de capacidade):
    o fechamento é escrito à parte, só narrativa — retoma a pergunta da abertura, ponto de vista como opinião,
    pergunta ao espectador. Conferido em código (sem número, sem nome novo, sem afirmação); recusado, uma 2ª
    tentativa com o motivo; sem fechamento válido, fica o roteiro como veio. A narrativa final antiga da
    última cena sai."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    cenas = script.get("scenes") or []
    if not cenas:
        return script
    angulo = (plano or {}).get("angulo") or {}
    roteiro = "\n".join(f"{i + 1}. {sc.get('narration') or ' '.join(f['texto'] for f in sc.get('frases') or [])}"
                        for i, sc in enumerate(cenas))
    pedido = _PROMPT_FECHAMENTO.format(tema=topic, pergunta=angulo.get("pergunta") or "-",
                                       ponto=angulo.get("ponto_de_vista") or "-", roteiro=roteiro,
                                       proibidas=_PALAVRAS_ALEGA)
    ultima = list(cenas[-1].get("frases") or [])
    while ultima and ultima[-1].get("tipo") == "narrativa":
        ultima.pop()
    if not ultima:
        return script
    nomes = _nomes_conhecidos(script, topic)
    correcao, recusadas = "", []
    for _ in range(tentativas):
        try:
            raw = complete([{"role": "user", "content": pedido + correcao}], max_tokens=800, temperature=0.5,
                           validate=lambda r: isinstance(_obj(r).get("frases"), list), model_filter=is_strong_model)
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("fechamento indisponível (%s) — fica o fim do roteiro", exc)
            return script
        frases = [" ".join(str(t).split()) for t in _obj(raw).get("frases") or [] if str(t).strip()][:3]
        motivo = None
        if len(frases) < 2:
            motivo = "menos de 2 frases"
        elif not (frases[-1].endswith("?") or _COMENTA.search(frases[-1])):
            motivo = "não termina chamando o espectador"
        for t in frases:
            achado = afirmacao_escondida(t, nomes)
            if achado and achado.startswith("afirmação absoluta") and _OPINIAO.search(t):
                achado = None  # "para mim, o mais bonito…" é opinião dita como opinião
            achado = achado or (f"afirma ({m.group(0)})" if (m := _ALEGA.search(_COMENTA.sub("", t)))
                                else None) or ("pede like" if _PEDE_LIKE.search(t) else None)
            motivo = motivo or (f"a frase \"{t}\": {achado}" if achado else None)
        novas = ultima + [{"texto": t, "tipo": "narrativa", "evidencias": [], "fechamento": True} for t in frases]
        novo = {**script, "scenes": cenas[:-1] + [{**cenas[-1], "frases": novas,
                                                   "narration": " ".join(f["texto"] for f in novas)}]}
        if not motivo and _palavras(novo) > max_pal * 1.1:
            motivo = f"passaria do tamanho ({_palavras(novo)} palavras): encurte"
        if not motivo:
            log.info("fechamento escrito à parte: %s", " | ".join(frases))
            return novo
        log.warning("fechamento recusado (%s)", motivo)
        recusadas.append(motivo)
        correcao = ("\n\nFechamentos anteriores recusados:\n" + "\n".join(f"- {m}" for m in recusadas)
                    + "\nEscreva de novo sem nenhum desses problemas: troque a palavra apontada por outra, não a repita.")
    log.warning("fechamento sem versão válida — fica o fim do roteiro")
    return script


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
em "qualidade" responda {"nota": 0, "problemas": []}."""

DECISOES_AUDITOR = {"sustentada": None, "narrativa_ok": None, "parcial": "excede", "excede": "excede",
                    "sem_suporte": "sem_suporte", "contradiz": "contradiz", "afirmacao_oculta": "afirmacao_oculta"}
META_BLOCO = 4  # 26/11: pedaço do bloco que voltou sem decisão duas vezes
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
3) Dê a GRAVIDADE de cada ponto, com bom senso de espectador comum (não de advogado):
   - "erro_factual": a sequência afirma algo que as evidências contradizem ou não sustentam;
   - "enganosa": um espectador comum, lendo naturalmente, concluiria algo falso — diga em "por_que_razoavel"
     por que essa conclusão é a leitura natural;
   - "ambiguidade": dá para entender errado, mas a leitura natural está certa; a redação pode deixar mais claro;
   - "improvavel": só uma leitura forçada chegaria a essa conclusão;
   - "estilo": preferência de redação, não engano.
Responda SÓ JSON:
{{"conclusoes_do_publico": ["..."],
  "enganos": [{{"frases": [7, 8], "tipo": "combinacao", "gravidade": "enganosa", "o_publico_conclui": "...",
               "por_que_engana": "...", "por_que_razoavel": "...", "como_corrigir": "..."}}]}}"""

GRAVIDADES_BLOQUEIAM = ("erro_factual", "enganosa")
GRAVIDADES = GRAVIDADES_BLOQUEIAM + ("ambiguidade", "improvavel", "estilo")

TIPOS_ENGANO = ("combinacao", "comparacao", "sujeito_trocado", "parcial_como_total", "causal", "sentido_alterado", "ordem")

_PROMPT_EDITOR = """Você é o EDITOR do vídeo (a checagem de fatos é feita à parte: aqui só a HISTÓRIA).
Título: "{titulo}".
Narração, cena por cena:
{cenas}

Avalie SÓ o que está escrito neste roteiro: como ele conta (ordem, ligação entre cenas, repetição, clareza,
abertura, conclusão). NÃO cobre fatos, detalhes, acontecimentos ou etapas que não estão no roteiro — o
roteiro só pode dizer o que as fontes verificadas sustentam, e o que falta não é defeito da história.
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


def _decisao_normalizada(valor) -> str:
    """26/11 (Svalbard 1026: "factual: sustentada" em todas as frases — 4 pedidos perdidos e o job parado):
    só a forma (ver src/decisoes.py, que também aprende as palavras desconhecidas)."""
    from .decisoes import normalizar
    return normalizar(valor)


def _decisoes_validas(data: dict, esperados: set[int]) -> dict[int, dict]:
    """{n: decisão} das frases do bloco com decisão conhecida e justificativa de verdade.
    26/11 (Svalbard 1025: bloco 13-24 sem decisão 4 vezes): resposta numerada de 1 a len(bloco), sem nenhum
    número do bloco, é lida pela posição."""
    out = {}
    itens = [r for r in data.get("frases") or [] if isinstance(r, dict)]
    ordem = sorted(esperados)
    try:
        ns = [int(r.get("n")) for r in itens]
    except (TypeError, ValueError):
        ns = []
    renumera = bool(ns) and not set(ns) & esperados and set(ns) <= set(range(1, len(ordem) + 1))
    from .decisoes import ler_lote
    lidas = dict(zip(map(id, itens), ler_lote([r.get("decisao") for r in itens], DECISOES_AUDITOR,
                                              ("sustentada", "narrativa_ok"), "auditor")))
    for r in itens:
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if renumera:
            n = ordem[n - 1]
        decisao = lidas[id(r)]
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
                faltam = sorted(esperados - set(decisoes))
                if faltam:
                    log.warning("auditor: resposta sem decisão válida (amostra: %s) — pedaços de até %d frases",
                                json.dumps((r2 or {}).get("frases", [])[:2], ensure_ascii=False)[:300], META_BLOCO)
                    for ini in range(0, len(faltam), META_BLOCO):
                        parte = faltam[ini:ini + META_BLOCO]
                        r3 = chamar(pedido_de([n - 1 for n in parte], False), _valido_auditor)
                        if r3 is not None:
                            decisoes.update(_decisoes_validas(r3, set(parte)))
    sem = [n for n in range(1, len(lista) + 1) if n not in decisoes]
    if sem:
        log.warning("auditor: frase(s) %s sem decisão depois de perguntar de novo — o roteiro não sai assim", sem)
        return None
    # 26/11: cada reprovação NOVA do auditor passa pela 2ª opinião do juiz fixo antes de valer
    reprovadas = [k for k in pendentes if DECISOES_AUDITOR.get(decisoes[k + 1]["decisao"])]
    if reprovadas:
        with metricas.etapa("segunda_opiniao"):
            itens = [{"texto": lista[k][2]["texto"], "evidencias": list(lista[k][2].get("evidencias") or []),
                      "anterior": lista[k - 1][2]["texto"] if k else "",
                      "motivo": f"{decisoes[k + 1]['decisao']}: {decisoes[k + 1]['justificativa']}"} for k in reprovadas]
            retiradas = segunda_opiniao(itens, banco)
        for pos, just in retiradas.items():
            k = reprovadas[pos]
            narrativa = lista[k][2].get("tipo") == "narrativa"
            log.info("auditor: reprovação retirada na 2ª opinião (%s) — %s (%s)", decisoes[k + 1]["decisao"],
                     lista[k][2]["texto"][:80], just[:120])
            decisoes[k + 1] = {**decisoes[k + 1], "decisao": "narrativa_ok" if narrativa else "sustentada",
                               "justificativa": f"2ª opinião: {just}", "retirada_de": decisoes[k + 1]["decisao"]}
        log.info("auditor: %d reprovação(ões), %d retirada(s) na 2ª opinião", len(reprovadas), len(retiradas))
    for k in range(len(lista)):
        previas[chaves[k]] = decisoes[k + 1]
    log.info("auditoria factual: %d frase(s) julgadas em %d bloco(s), %d reaproveitada(s) (frase, evidências, "
             "vizinhas e banco iguais)", len(pendentes), len(blocos), len(lista) - len(pendentes))
    with metricas.etapa("auditoria_semantica"):
        leitura = leitura_espectador(script, banco, topic)
    if leitura is None:
        # 23/11: sem nenhuma leitura válida do juiz fixo a versão fica NÃO JULGADA (nunca aprovada); não
        # repete a auditoria inteira nem pede a outro modelo
        leitura = {"conclusoes": [], "enganos": [], "alertas": [], "leituras": 0,
                   "nao_julgada": "nenhuma leitura de espectador válida"}
    with metricas.etapa("auditoria_editorial"):
        editorial = avaliar_editorial(script, topic)
    if editorial is None:
        editorial = {"nota": 0.0, "criterios": {}, "travas": [], "problemas": ["avaliação editorial não julgada"],
                     "alvos": [], "alvos_travas": [], "leituras": [], "aprovado": False,
                     "nao_julgada": "nenhuma avaliação editorial válida"}
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
    evs = por_id(banco)
    for eng in leitura["enganos"]:
        # o problema fica ancorado na ÚLTIMA frase da sequência (compatibilidade), mas leva TODAS as frases que
        # produzem a conclusão errada, com as evidências de cada uma (23/11, Svalbard job 1004: só a última ia
        # ao editor, e ele reescreveu duas vezes a frase seguinte e deixou intacta a que causava o engano)
        n = max(eng["frases"])
        i, j, f = lista[n - 1]
        sequencia = [{"n": k, "cena": lista[k - 1][0], "frase": lista[k - 1][1], "texto": lista[k - 1][2]["texto"],
                      "tipo": lista[k - 1][2].get("tipo"),
                      "evidencias": [{"id": x, "afirmacao": evs[x]["afirmacao"]}
                                     for x in lista[k - 1][2].get("evidencias") or [] if x in evs]}
                     for k in eng["frases"]]
        outras = [x["texto"][:80] for x in sequencia if x["n"] != n]
        prob = _p("leitura_enganosa", f["texto"], i, j,
                  f"leitura enganosa ({eng['tipo']})" + (f" junto com \"{' / '.join(outras)}\"" if outras else "")
                  + f": o público conclui que {eng['o_publico_conclui']}, mas {eng['por_que_engana']}. "
                  f"Corrija: {eng['como_corrigir']}")
        prob.update(sequencia=sequencia, engano=eng["tipo"], publico_conclui=eng["o_publico_conclui"],
                    por_que_engana=eng["por_que_engana"], como_corrigir=eng["como_corrigir"],
                    cenas=sorted({x["cena"] for x in sequencia}))
        problemas.append(prob)
    factual = {"frases": len(lista), "sustentadas": sum(1 for d in registro if d["problema"] is None),
               "enganos": leitura["enganos"], "conclusoes_do_publico": leitura["conclusoes"],
               "alertas": leitura.get("alertas") or [], "leituras_espectador": leitura.get("leituras")}
    factual["nota"] = max(0.0, round(10 * factual["sustentadas"] / max(1, len(lista)) - 2 * len(leitura["enganos"]), 1))
    nao_julgada = [x for x in (leitura.get("nao_julgada"), editorial.get("nao_julgada")) if x]
    if nao_julgada:
        editorial = {**editorial, "aprovado": False}  # ausência de julgamento nunca vira aprovação
    return problemas, {**editorial, "nao_julgada": nao_julgada, "decisoes": registro, "blocos": len(blocos), "factual": factual,
                       "frases_julgadas": len(pendentes), "frases_reaproveitadas": len(lista) - len(pendentes)}


def leitura_espectador(script: dict, banco: dict, topic: str) -> dict | None:
    """20/11: frases verdadeiras uma a uma podem, juntas, levar o público a uma conclusão falsa (o tempo de um
    trecho dito ao lado do ponto de partida de outro trajeto parece o tempo da viagem inteira). Uma leitura do roteiro INTEIRO como espectador:
    o que o público conclui e onde isso não é sustentado. {"conclusoes", "enganos"} ou None (sem leitura
    válida, o roteiro não sai)."""
    from .providers import ProvedoresIndisponiveis, complete
    lista = frases_de(script)
    citadas = {x for _, _, f in lista for x in f.get("evidencias") or []}
    pedido = _PROMPT_ESPECTADOR.format(
        tema=topic, banco=bloco_para_escritor(banco, 200, ids=citadas) or "(nenhuma citada)",
        frases="\n".join(f"{k}) {f['texto']}" for k, (_, _, f) in enumerate(lista, 1)))

    def valido(raw: str) -> bool:
        d = _obj(raw)
        return isinstance(d.get("conclusoes_do_publico"), list) and len(d["conclusoes_do_publico"]) >= 2 \
            and isinstance(d.get("enganos"), list)
    leituras: list[dict] = []
    for _ in range(LEITURAS):  # 23/11: o mesmo pedido, o mesmo modelo (juiz fixo), independentes
        try:
            d = _obj(complete([{"role": "user", "content": pedido}], max_tokens=SAIDA_ESPECTADOR, temperature=0.1,
                              validate=valido, juiz=True))
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("leitura de espectador sem resposta válida: %s", exc)
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
                # 23/11: sem gravidade dita conta como "enganosa" (rigor); "enganosa" sem dizer por que é a
                # leitura natural não bloqueia sozinha — vira ambiguidade (alerta para revisão humana)
                grav = str(e.get("gravidade") or "enganosa").strip().lower()
                grav = grav if grav in GRAVIDADES else "enganosa"
                razao = str(e.get("por_que_razoavel", "")).strip()
                if grav == "enganosa" and len(razao.split()) < 3 and "gravidade" in e:
                    grav = "ambiguidade"
                enganos.append({"frases": frs, "tipo": e["tipo"], "gravidade": grav, "o_publico_conclui": textos[0][:300],
                                "por_que_engana": textos[1][:300], "por_que_razoavel": razao[:300],
                                "como_corrigir": textos[2][:300]})
        log.info("leitura de espectador %d: %d conclusão(ões) do público, %d ponto(s)%s", len(leituras) + 1,
                 len(d["conclusoes_do_publico"]), len(enganos),
                 "".join(f" | {e['gravidade']} {e['tipo']} {e['frases']}: {e['o_publico_conclui'][:80]}" for e in enganos))
        leituras.append({"conclusoes": [str(x)[:200] for x in d["conclusoes_do_publico"]][:6], "enganos": enganos})
    if not leituras:
        return None
    # só "erro_factual"/"enganosa" votam para bloquear; ambiguidade vira alerta; improvável e estilo saem
    graves = [[e for e in x["enganos"] if e["gravidade"] in GRAVIDADES_BLOQUEIAM] for x in leituras]
    grupos = agrupar_enganos(graves)
    bloqueantes = [g for g in grupos if g["votos"] >= 2]
    ambiguos = agrupar_enganos([[e for e in x["enganos"] if e["gravidade"] == "ambiguidade"] for x in leituras])
    alertas = [g for g in grupos if g["votos"] < 2] + ambiguos
    descartados = sum(1 for x in leituras for e in x["enganos"] if e["gravidade"] in ("improvavel", "estilo"))
    if descartados:
        log.info("leitura de espectador: %d ponto(s) improvável(is) ou de estilo fora da auditoria", descartados)
    nao_julgada = "" if len(leituras) == LEITURAS else f"só {len(leituras)} de {LEITURAS} leituras de espectador válidas"
    log.info("leitura de espectador: %d leitura(s); %d interpretação(ões) enganosa(s) em 2+ leituras (bloqueiam), "
             "%d em uma só (alerta para revisão humana)%s%s", len(leituras), len(bloqueantes), len(alertas),
             "".join(f" | BLOQUEIA {g['tipo']} {g['frases']} ({g['votos']}/{len(leituras)})" for g in bloqueantes)
             + "".join(f" | alerta {g['tipo']} {g['frases']}" for g in alertas),
             f" — NÃO JULGADA ({nao_julgada})" if nao_julgada else "")
    return {"conclusoes": leituras[0]["conclusoes"], "enganos": bloqueantes, "alertas": alertas,
            "leituras": len(leituras), "nao_julgada": nao_julgada}


def _mesma_interpretacao(grupo: dict, e: dict) -> bool:
    """Duas leituras apontam a MESMA interpretação enganosa quando falam das mesmas frases (metade ou mais em
    comum) ou de frases em comum com o mesmo tipo de engano."""
    fs = set(e["frases"])
    comum = grupo["frases"] & fs
    if not comum:
        return False
    return len(comum) / len(grupo["frases"] | fs) >= 0.5 or e["tipo"] in grupo["tipos"]


def agrupar_enganos(leituras: list[list[dict]]) -> list[dict]:
    """Junta as interpretações enganosas das leituras independentes: a mesma interpretação (ver
    _mesma_interpretacao) conta UM voto por leitura, nunca duplicada. Cada grupo: as frases de todas as
    leituras que a apontaram, o tipo mais citado, o texto da primeira e "votos" (quantas leituras)."""
    grupos: list[dict] = []
    for n, enganos in enumerate(leituras):
        for e in enganos:
            g = next((g for g in grupos if _mesma_interpretacao(g, e)), None)
            if g is None:
                grupos.append({"frases": set(e["frases"]), "tipos": [e["tipo"]], "leituras": {n}, "itens": [e]})
            else:
                g["frases"] |= set(e["frases"])
                g["tipos"].append(e["tipo"])
                g["leituras"].add(n)
                g["itens"].append(e)
    saida = []
    for g in grupos:
        primeiro = g["itens"][0]
        saida.append({**primeiro, "frases": sorted(g["frases"]), "tipo": max(set(g["tipos"]), key=g["tipos"].count),
                      "tipos": sorted(set(g["tipos"])), "votos": len(g["leituras"])})
    return saida


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


def _repetidos_em_frases(script: dict) -> set[str]:
    """Anos e medidas ditos em mais de uma frase não narrativa do roteiro (na mesma cena ou em cenas diferentes)."""
    from .fatos import _pares
    contagem: dict[str, int] = {}
    for _, _, f in frases_de(script):
        if f.get("tipo") == "narrativa":
            continue
        t = extenso_para_algarismos(f["texto"])
        for c in set(re.findall(r"\b(1\d{3}|20\d{2})\b", t)) | {f"{v} {u}" for v, u in _pares(t) if u != "ano"}:
            contagem[c] = contagem.get(c, 0) + 1
    return {c for c, n in contagem.items() if n > 1}


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
                   "ordem_nao_comprovada": "ordem entre acontecimentos sem prova",
                   "divergencia_como_faixa": "valores divergentes de fontes diferentes ditos como faixa"}


def motivo_leitura(p: dict) -> str:
    """Leitura enganosa de uma SEQUÊNCIA de frases como o editor a lê: todas as frases (cena, tipo, evidências
    com o que cada uma comprova), o que o público conclui, por que engana e a sugestão do auditor."""
    frases = "; ".join(
        f"[cena {x['cena'] + 1}, {x.get('tipo')}] \"{x['texto'][:220]}\" — evidências: "
        + (" | ".join(f"{e['id']}: {e['afirmacao'][:160]}" for e in x["evidencias"]) or "nenhuma")
        for x in p["sequencia"])
    return (f"FACTUAL — LEITURA ENGANOSA ({p.get('engano', '')}) produzida pelo CONJUNTO destas {len(p['sequencia'])} "
            f"frase(s): {frases}. O público conclui que {p.get('publico_conclui', '')[:300]}; isso engana porque "
            f"{p.get('por_que_engana', '')[:300]}. O que as fontes comprovam é só o que as evidências acima dizem. "
            "Descubra qual(is) frase(s) produzem a conclusão errada e mude SÓ essa(s), dizendo exatamente o que a "
            "evidência dela comprova (as outras ficam iguais; nada de afirmação nova; informação verdadeira fica). "
            f"Sugestão do auditor: {p.get('como_corrigir', '')[:300]}")


def motivo_factual(p: dict) -> str:
    """O problema da auditoria factual/semântica como o editor o lê: a frase, o tipo e o motivo do auditor."""
    if p.get("sequencia"):
        return motivo_leitura(p)
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
        for c in p.get("cenas") or []:  # leitura enganosa: as outras cenas da sequência também vão ao editor
            if c != i and 0 <= c < n_cenas:
                alvos.setdefault(c, []).append(f"FACTUAL — esta cena faz parte da LEITURA ENGANOSA descrita na cena "
                                               f"{i + 1} (\"{p['termo'][:120]}\"): veja lá e mude aqui só se a frase "
                                               "desta cena contribui para a conclusão errada")
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


LEITURAS = 3             # 23/11: avaliações independentes por versão (editorial e leitura de espectador)
SAIDA_EDITOR = 6000      # o Nemotron raciocina dentro da saída (+4096 em providers); cortada → o dobro, 1 vez
SAIDA_ESPECTADOR = 16000  # job 1005: a leitura passou de 8 mil tokens e era cortada com 4 mil


def _mediana(valores: list[float]) -> float:
    v = sorted(valores)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def avaliar_editorial(script: dict, topic: str) -> dict | None:
    """20/11: a HISTÓRIA, separada dos fatos. O editor (IA forte) dá nota por critério e aponta as cenas; a
    nota é a média calculada pelo código, e trava em NOTA_COM_TRAVA quando um critério essencial fica em 4 ou
    menos ou quando o código vê repetição de fato ou linha do tempo.
    23/11: o mesmo texto recebeu 6,0 / 6,6 / 7,2 / 7,2 / 7,5 em cinco leituras iguais (mesmo pedido, modelo e
    parâmetros: o raciocínio do modelo varia). Agora são LEITURAS avaliações independentes no JUIZ FIXO
    (providers.JUIZ, sem cascata) e cada critério fica com a MEDIANA delas; menos de LEITURAS válidas → a
    versão fica "nao_julgada" e não pode ser aprovada. {"nota", "problemas", "criterios", "travas",
    "aprovado", "leituras", "nao_julgada"} ou None (nenhuma leitura válida)."""
    from .providers import ProvedoresIndisponiveis, complete
    cenas = "\n".join(f"[{k}] {sc.get('narration', '')}" for k, sc in enumerate(script.get("scenes", []), 1))
    pedido = _PROMPT_EDITOR.format(tema=topic, titulo=script.get("title", ""), cenas=cenas)

    def valido(raw: str) -> bool:
        c = _obj(raw).get("criterios")
        return isinstance(c, dict) and all(isinstance(c.get(k), (int, float)) for k in CRITERIOS_EDITORIAIS)
    leituras = []

    def ler(n: int) -> None:
        for _ in range(n):  # o mesmo pedido, o mesmo modelo, o mesmo roteiro — independentes
            try:
                leituras.append(_obj(complete([{"role": "user", "content": pedido}], max_tokens=SAIDA_EDITOR,
                                              temperature=0.1, validate=valido, juiz=True)))
            except ProvedoresIndisponiveis:
                raise
            except RuntimeError as exc:
                log.warning("leitura editorial sem resposta válida: %s", exc)

    def criterios() -> tuple[list[dict], dict]:
        notas = [{k: max(0.0, min(10.0, float(d["criterios"][k]))) for k in CRITERIOS_EDITORIAIS} for d in leituras]
        return notas, {k: _mediana([n[k] for n in notas]) for k in CRITERIOS_EDITORIAIS}
    ler(LEITURAS)
    if not leituras:
        return None
    notas_leitura, crit = criterios()
    # 26/11 (Svalbard 1024: v4 limpa com 6,8 / 6,4 / 7,1): o mesmo texto varia ±0,5 entre leituras; perto do
    # mínimo, 3 leituras deixam a sorte decidir. Na faixa de dúvida, mais LEITURAS_EXTRA — mediana de 5
    perto = FAIXA_DUVIDA[0] <= round(sum(crit.values()) / len(crit), 1) < FAIXA_DUVIDA[1]
    if perto and len(leituras) >= LEITURAS and not travas_editoriais(script):
        log.info("editorial perto do mínimo (%.1f): mais %d leituras", sum(crit.values()) / len(crit), LEITURAS_EXTRA)
        ler(LEITURAS_EXTRA)
        notas_leitura, crit = criterios()
    problemas, alvos, vistos = [], [], set()
    for d in leituras:
        for p in d.get("problemas") or []:
            if not (isinstance(p, dict) and str(p.get("descricao", "")).strip()):
                continue
            cenas_p = [c for c in p.get("cenas") or [] if isinstance(c, (int, float, str))]
            chave = (str(p.get("criterio", "")), tuple(str(c) for c in cenas_p))
            if chave in vistos:  # o mesmo problema visto em mais de uma leitura entra uma vez
                continue
            vistos.add(chave)
            problemas.append(f"{chave[0]} (cenas {', '.join(chave[1])}): {p.get('descricao', '')}")
            alvos.append({"criterio": chave[0], "cenas": cenas_p, "descricao": str(p.get("descricao", ""))[:300]})
    travas = travas_editoriais(script) + [f"{k} com nota {crit[k]:.0f}" for k in CRITERIOS_TRAVA if crit[k] <= 4]
    nota = round(sum(crit.values()) / len(crit), 1)
    if travas:
        nota = min(nota, NOTA_COM_TRAVA)
    medias = [round(sum(n.values()) / len(n), 1) for n in notas_leitura]
    nao_julgada = "" if len(leituras) >= LEITURAS else f"só {len(leituras)} de {LEITURAS} avaliações editoriais válidas"
    log.info("editorial: %d leitura(s) %s → mediana por critério %s = %.1f%s", len(leituras),
             " / ".join(f"{m:.1f}" for m in medias), {k: v for k, v in crit.items()}, nota,
             f" — NÃO JULGADA ({nao_julgada})" if nao_julgada else "")
    return {"nota": nota, "problemas": (travas + problemas)[:10], "criterios": crit, "travas": travas,
            "alvos": alvos, "alvos_travas": cenas_das_travas(script), "leituras": medias, "nao_julgada": nao_julgada,
            "aprovado": not travas and nota >= NOTA_MINIMA and not nao_julgada}


# ---------------------------------------------------------------- histórico de reprovações do job
# 22/11 (Svalbard): a cada rodada o editor via só os problemas da versão atual; corrigia um exagero e escrevia
# outro igual com outras palavras, e um problema já resolvido voltava (v2 5 → v3 7 problemas). O histórico vive
# dentro de UMA chamada de auditar_e_corrigir (um job, um tema): nada passa para outro job.
_MOTIVO_DIVERGENCIA = re.compile(r"diverg|faixa|intervalo|discrepân|discrepan|fontes (dão|dizem|trazem) valores", re.I)
_NATUREZA_PRESENTE = re.compile(r"\bnatura(l|is|lmente)\b|pela (própria )?natureza", re.I)


def _escalas_presentes(texto: str) -> set[str]:
    cats = {c for c, (marca, _) in _PRECISAO.items() if marca.search(texto)}
    if _NATUREZA_PRESENTE.search(texto):
        cats.add("natureza")
    return cats


def _frase_do_problema(script: dict, p: dict) -> dict:
    try:
        return script["scenes"][p["cena"]]["frases"][p["frase"]]
    except (KeyError, IndexError, TypeError):
        return {"texto": p.get("termo", ""), "evidencias": []}


def registrar_historico(historico: list[dict], versao: int, script: dict, problemas: list[dict], banco: dict,
                        qualidade: dict | None = None) -> None:
    """Acrescenta as reprovações da versão `versao` (factuais, semânticas e editoriais) ao histórico do job e
    marca as que VOLTARAM (o mesmo problema numa versão anterior: mesma frase, ou mesma cena com a mesma
    evidência e o mesmo tipo de erro ou a mesma escala de exagero)."""
    evs = {e["id"]: e for e in banco.get("evidencias", [])}
    novas = []
    itens = []
    for p in problemas:
        itens.append(p)
        for x in p.get("sequencia") or []:  # leitura enganosa: as outras frases do conjunto também
            if (x["cena"], x["frase"]) != (p.get("cena"), p.get("frase")):
                itens.append({**p, "cena": x["cena"], "frase": x["frase"], "termo": x["texto"]})
    for p in itens:
        f = _frase_do_problema(script, p)
        ids = [x for x in f.get("evidencias") or [] if x in evs]
        texto = p.get("termo") or f.get("texto", "")
        novas.append({"versao": versao, "cena": p.get("cena"), "frase": texto, "tipo": p["tipo"],
                      "motivo": p.get("motivo", ""), "evidencias": ids,
                      "escalas": marcadores_sem_suporte(texto, [evs[x] for x in ids]),
                      "marcas": sorted(_escalas_presentes(texto)),
                      "numeros": sorted(numeros(extenso_para_algarismos(texto))),
                      "tentativas": [], "recusadas": [], "voltou_de": None, "voltou": 0})
    for a in (qualidade or {}).get("alvos") or []:
        for c in a.get("cenas") or [None]:
            novas.append({"versao": versao, "cena": (c - 1) if isinstance(c, int) else None, "frase": "",
                          "tipo": f"editorial:{a.get('criterio', '')}", "motivo": str(a.get("descricao", ""))[:300],
                          "evidencias": [], "escalas": {}, "marcas": [], "numeros": [], "tentativas": [],
                          "recusadas": [], "voltou_de": None, "voltou": 0})
    for n in novas:
        for h in historico:
            if h["versao"] >= versao:
                continue
            mesmo = (n["frase"] and _norm(n["frase"]) == _norm(h["frase"])) or (
                n["cena"] == h["cena"] and n["cena"] is not None and (
                    (n["tipo"].startswith("editorial:") and n["tipo"] == h["tipo"])
                    or (set(n["evidencias"]) & set(h["evidencias"])
                        and (n["tipo"] == h["tipo"] or set(n["escalas"]) & set(h["escalas"])))))
            if mesmo:
                n["voltou_de"] = h["versao"] if n["voltou_de"] is None else n["voltou_de"]
                h["voltou"] += 1
    historico.extend(novas)


def registrar_tentativas(historico: list[dict], versao: int, editado: dict, registro: dict) -> None:
    """O que a edição fez com cada reprovação da versão `versao`: a cena reescrita (tentativa) ou o motivo da
    recusa da conferência em código."""
    editadas = set(registro.get("cenas_editadas") or [])
    recusas = {r["cena"]: r["motivo"] for r in registro.get("revertidas") or []}
    for h in historico:
        if h["versao"] != versao or h["cena"] is None:
            continue
        if h["cena"] + 1 in editadas:
            h["tentativas"].append(editado["scenes"][h["cena"]].get("narration", "")[:300])
        if h["cena"] + 1 in recusas:
            h["recusadas"].append(recusas[h["cena"] + 1][:200])


def bloco_historico(historico: list[dict], banco: dict, maximo: int = 14) -> str:
    """O histórico como o editor o lê, no INÍCIO do pedido: frase, cena, motivo, o que a evidência permite
    afirmar, correções já tentadas e o que voltou."""
    if not historico:
        return ""
    evs = {e["id"]: e for e in banco.get("evidencias", [])}
    # factuais e semânticos primeiro (os que voltaram, depois os mais recentes); editoriais no fim, no máximo 4
    factuais = sorted((h for h in historico if not h["tipo"].startswith("editorial:")),
                      key=lambda h: (-h["voltou"] - (1 if h["voltou_de"] else 0), -h["versao"]))
    editoriais = sorted((h for h in historico if h["tipo"].startswith("editorial:")),
                        key=lambda h: (-h["voltou"], -h["versao"]))
    itens = (factuais[:maximo] + editoriais[:4])[:maximo + 4]
    linhas = ["", "HISTÓRICO DE REPROVAÇÕES DESTE ROTEIRO (leia ANTES de editar; vale para TODAS as cenas):",
              "O mesmo erro dito com outras palavras é o MESMO erro: se a evidência não sustenta a escala (todos, "
              "inteiro, sempre, recorrente, natural, causou, a faixa entre dois valores), nenhum sinônimo dela entra; "
              "diga a versão mais restrita que a evidência sustenta ou tire a informação."]
    for h in itens:
        onde = f"cena {h['cena'] + 1}" if h["cena"] is not None else "roteiro"
        linhas.append(f"- [v{h['versao']}, {onde}] {h['tipo']}" + (f" — frase: \"{h['frase'][:200]}\"" if h["frase"] else ""))
        linhas.append(f"  motivo: {h['motivo'][:350]}")
        if h["escalas"]:
            linhas.append("  exagero sem sustentação: " + "; ".join(f"{c} ('{w}')" for c, w in h["escalas"].items()))
        for x in h["evidencias"][:3]:
            e = evs[x]
            c = next((c for c in e.get("citacoes") or [] if c.get("verificado")), None)
            linhas.append(f"  a evidência permite afirmar: {x}: {e.get('afirmacao', '')[:200]}"
                          + (f" | trecho: \"{c['trecho'][:180]}\"" if c else ""))
        for t in h["tentativas"]:
            linhas.append(f"  correção já tentada: \"{t[:220]}\"")
        for r in h["recusadas"]:
            linhas.append(f"  edição recusada pela conferência: {r}")
        if h["voltou_de"]:
            linhas.append(f"  VOLTOU (já reprovado na v{h['voltou_de']}): reescreva mais restrito ou tire a frase.")
        elif h["voltou"]:
            linhas.append("  ESTE PROBLEMA VOLTOU na versão seguinte: a correção anterior não resolveu.")
    return "\n".join(linhas) + "\n"


def exagero_na_edicao(frases: list[dict], originais: list[dict], cena: int, banco: dict,
                      historico: list[dict]) -> str | None:
    """Motivo para recusar a cena editada, ou None. Recusa (1) a frase nova que traz de volta a escala de
    exagero de uma reprovação desta cena ou destas evidências; (2) a frase nova com marcador de escala que as
    evidências citadas não sustentam e que a cena original não tinha (exagero novo); (3) a mesma combinação de
    números reprovada como leitura enganosa, sem dizer que é divergência entre fontes."""
    evs = {e["id"]: e for e in banco.get("evidencias", [])}
    antigos = {_norm(f["texto"]) for f in originais}
    cats_antigas = set().union(*[_escalas_presentes(f["texto"]) for f in originais]) if originais else set()
    for f in frases:
        if f.get("tipo") == "narrativa" or _norm(f["texto"]) in antigos:
            continue
        ids = [x for x in f.get("evidencias") or [] if x in evs]
        presentes = _escalas_presentes(f["texto"])
        nums = numeros(extenso_para_algarismos(f["texto"]))
        sem = marcadores_sem_suporte(f["texto"], [evs[x] for x in ids])
        for h in historico:
            if h["tipo"].startswith("editorial:"):
                continue
            ligada = h["cena"] == cena or bool(set(h["evidencias"]) & set(ids))
            if not ligada:
                continue
            # só a escala que a evidência NÃO sustentava: "permafrost natural como reserva" é fato; o
            # "naturalmente congeladas" reprovado fica para o histórico no pedido (a palavra não distingue os dois)
            # 24/11 (job 1008): a escala só VOLTA se as evidências da frase nova também não a sustentam — o
            # "nunca correram risco" (E21) reprovado bloqueava "vazamentos recorrentes" (E38: "recurrent minor leaks")
            volta = set(h["escalas"]) & presentes & set(sem)
            if volta:
                c = sorted(volta)[0]
                return (f"volta o exagero reprovado na v{h['versao']} ({c}): \"{f['texto'][:120]}\" — a frase "
                        f"reprovada era \"{h['frase'][:100]}\"; diga só o que a evidência sustenta")
            # 23/11 (job 1002): só quando a reprovação foi por divergência dita como faixa — a mesma trava para
            # qualquer leitura enganosa com dois números recusou correções de ordem e de combinação
            if (h["cena"] == cena and len(h["numeros"]) >= 2 and h["tipo"] in ("leitura_enganosa", "excede")
                    and _MOTIVO_DIVERGENCIA.search(h["motivo"])
                    and set(h["numeros"]) <= nums and not _DIVERGENCIA_DITA.search(f["texto"])):
                return (f"volta a combinação de números {', '.join(h['numeros'])} reprovada na v{h['versao']} como "
                        f"{h['tipo'].replace('_', ' ')}: diga a divergência como divergência ou use um valor só")
        novos = {c: w for c, w in sem.items() if c not in cats_antigas}
        if novos:
            c, w = next(iter(novos.items()))
            return (f"exagero novo: '{w}' ({c}) sem sustentação nas evidências citadas "
                    f"({', '.join(ids) or 'nenhuma'}) — diga a versão mais restrita ou tire")
    return None


# ---------------------------------------------------------------- edição por cena (única correção)
_PROMPT_EDICAO = """Você faz a EDIÇÃO LOCALIZADA de um roteiro pronto (YouTube, português do Brasil). Tema: {tema}. Título: "{titulo}".
Você NÃO reescreve o roteiro: edita SÓ as cenas marcadas com >>, para corrigir o problema apontado em cada uma,
e devolve só as cenas que mudou. As outras ficam exatamente como estão.
{historico}
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
- corrigir um fato NÃO é acrescentar dado: restrinja ou tire o que excede; a cena não vira lista de números,
  nomes ou datas, não antecipa o que uma cena seguinte conta e não repete o que outra já contou;
- ordem entre acontecimentos (primeiro, depois, em seguida, antes disso, por fim) só com evidência citada de
  "ordem COMPROVADA"; sem ela, conte o acontecimento sem afirmar a ordem;
- números em algarismos; decimal da evidência fica exato ("−64,4 °C") ou com aproximação no sentido certo
  ("mais de 64"); número que nenhuma evidência traz sai (descreva sem número só se uma evidência sustenta a
  descrição). Nunca ponha outro número no lugar.
- 25/11: sem DEDUÇÃO (consequência, finalidade ou causa que o trecho não diz: "ganhando tempo para o reparo",
  "por isso"), sem "hoje" para dado de outra época, sem termo mais forte que o do trecho ("regiões do sul" não é
  "Hemisfério Sul"), sem inverter o sentido do trecho e sem juntar dois trechos numa relação que nenhum afirma.
  Frase reprovada por dedução: TIRE a dedução, não reescreva a mesma ideia com outras palavras.
{regra_precisao}
Problema FACTUAL — ANTES de reescrever: ache a parte EXATA da frase que vai além da evidência, o trecho da fonte
que sustenta o resto, e escreva a formulação MAIS RESTRITA que continua interessante. Corrigir é tirar ou
restringir: não complete a frase com número, nome, país, data, causa ou curiosidade só para ela ficar mais
cheia. Nunca: dano parcial vira destruição; possível vira acontecido; causa que a fonte não dá; caso específico
vira regra; duas medidas divergentes viram faixa; característica de uma coisa passa para outra.
Problema FACTUAL (reprovação do auditor): a frase apontada NÃO pode ficar como está. Se uma evidência sustenta
o que ela quer dizer, reescreva para dizer EXATAMENTE isso e cite os ids (hipótese dita como hipótese,
estimativa como estimativa, divergência entre fontes dita como divergência); se nenhuma sustenta, vire frase
narrativa que mantém a ligação sem afirmar nada, ou tire a frase. Leitura enganosa: o problema traz TODAS as
frases do conjunto, com as evidências de cada uma; mude só a(s) que levam à conclusão errada, para que o público
conclua só o que as evidências sustentam — sem apagar informação verdadeira e sem afirmação nova.
Problema de HISTÓRIA: corrija sem perder os fatos que a cena entrega e sem criar fato novo sem evidência.
PRESERVAR A HISTÓRIA: a edição anterior da cena piorou a história; o texto anterior vem como referência da
função da cena — se ele tinha erro factual, a correção fica e só o que piorou é reformulado.
Se a correção EXIGIR mudar uma cena não marcada (ex.: o fato repetido sai de uma cena e a outra precisa da
ligação), pode editá-la, dizendo por quê em "justificativa". {regra_titulo}
Responda SÓ JSON:
{{"cenas": [{{"cena": 3, "funcao": "o que a cena faz na história", "preserva": "o que foi mantido",
   "corrige": "o que mudou e por quê", "reprovado": "a parte exata que ia além da evidência (problema factual)",
   "trecho": "o trecho da fonte que sustenta a versão nova", "justificativa": "só para cena não marcada",
   "frases": [{{"texto": "...", "tipo": "factual|explicativa|narrativa", "evidencias": ["E2"]}}]}}],
 "titulo": ""}}"""


def nome_novo_na_correcao(frases: list[dict], originais: list[dict], motivos: list[str], banco: dict) -> str | None:
    """23/11 (job 1006): ao corrigir a cena 6, o editor a completou com "regenerar as coleções no Marrocos e no
    Líbano" — nomes que nem a cena nem as evidências citadas traziam. Cena editada SÓ por problema factual não
    ganha nome próprio que não estava nela nem no texto das evidências que a frase cita (número inventado a
    conferência de números já recusa). Corrigir é restringir, não completar."""
    if not motivos or not all(m.startswith("FACTUAL") for m in motivos):
        return None
    evs = por_id(banco)
    antes = dobrar(" ".join(f["texto"] for f in originais)).lower()
    novos = []
    for f in frases:
        fonte = dobrar(" ".join(evs[x]["afirmacao"] + " " + " ".join(str(c.get("trecho") or "") for c in evs[x].get("citacoes") or [])
                                for x in f.get("evidencias") or [] if x in evs)).lower()
        # a 1ª palavra da frase fica de fora (maiúscula de começo de frase não é nome)
        novos += [n for n in _nomes(f["texto"]) if n not in antes and n not in fonte]
    if novos:
        return ("a correção pôs nome que nem a cena nem as evidências citadas trazem (" + ", ".join(sorted(set(novos))[:5])
                + "): corrigir é restringir ou tirar o que excede, sem completar a frase com detalhe novo")
    return None


def conferir_edicao(novas: list[dict], cenas: list[dict], registro: dict, banco: dict) -> None:
    """25/11 (Svalbard 1017): a frase que a EDIÇÃO escreve passa pela mesma conferência contra os trechos da
    escrita — antes a edição reescrevia "garante a operação" e "por isso" sem ninguém conferir. Frase nova que
    excede sai da cena; cena que fica sem frase volta a ser a original. Mexe em `novas` e no registro."""
    alvos = []
    for i in registro["cenas_editadas"]:
        k = i - 1
        antes = {_norm(f["texto"]) for f in cenas[k].get("frases") or []}
        frases = novas[k]["frases"]
        for j, f in enumerate(frases):
            if f.get("tipo") == "narrativa" or _norm(f["texto"]) in antes:
                continue
            ant = frases[j - 1]["texto"] if j else (novas[k - 1]["frases"][-1]["texto"]
                                                    if k and novas[k - 1].get("frases") else "")
            alvos.append((k, j, {"texto": f["texto"], "evidencias": list(f.get("evidencias") or []), "anterior": ant}))
    if not alvos:
        return
    revisar_citadas({e for _, _, it in alvos for e in it["evidencias"]}, banco, "")
    veredito = conferir_contra_trechos([it for _, _, it in alvos], banco)
    fora: dict[int, set[int]] = {}
    for n, motivo in veredito.items():
        if motivo:
            k, j, it = alvos[n]
            fora.setdefault(k, set()).add(j)
            registro.setdefault("cortadas_na_conferencia", []).append(
                {"cena": k + 1, "frase": it["texto"][:160], "motivo": motivo[:200]})
            log.warning("edição da cena %d: frase nova excede o trecho (%s) — sai: %s", k + 1, motivo[:120],
                        it["texto"][:100])
    for k, js in fora.items():
        restam = [f for j, f in enumerate(novas[k]["frases"]) if j not in js]
        if not any(f.get("tipo") != "narrativa" for f in restam):
            novas[k] = cenas[k]
            registro["cenas_editadas"].remove(k + 1)
            registro["detalhes"] = [d for d in registro["detalhes"] if d["cena"] != k + 1]
            registro["revertidas"].append({"cena": k + 1, "motivo": "toda frase nova excedia o trecho"})
        else:
            novas[k] = {**novas[k], "frases": restam, "narration": " ".join(f["texto"] for f in restam)}
    log.info("conferência da edição: %d frase(s) nova(s), %d excediam", len(alvos), sum(1 for m in veredito.values() if m))


def editar_cenas(script: dict, alvos: dict[int, list[str]], banco: dict, topic: str, *,
                 titulo_motivo: str = "", problemas: list[dict] | None = None,
                 prioritarias: set[str] | None = None, historico: list[dict] | None = None) -> tuple[dict, dict]:
    """20/11: edição LOCALIZADA (1 chamada). O editor vê o roteiro inteiro, as evidências das cenas a editar e
    das vizinhas e o motivo de cada cena; devolve só as cenas que mudou. Cena fora dos alvos só entra com
    justificativa (e no máximo tantas quanto os alvos); cena editada que não passa na conferência em código
    volta a ser a original — a edição nunca troca um problema de história por um erro de fato.
    21/11: é a ÚNICA correção do roteiro — fatos (problemas da auditoria factual e semântica, em `problemas`)
    e história no mesmo pedido. Frase reprovada que volta igual fica registrada em "factuais_sem_correcao"
    (a auditoria seguinte a reprova de novo). `prioritarias`: evidências achadas pela busca para as frases
    reprovadas — entram com trecho mesmo quando o pedido precisa encolher. `historico`: as reprovações das
    versões anteriores deste job (ver registrar_historico) — vão no início do pedido, e a conferência em código
    recusa a cena que traz de volta o mesmo exagero (mesma escala de precisão) ou cria um novo. Devolve
    (roteiro, registro)."""
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
            historico=bloco_historico(historico or [], banco), regra_precisao=_REGRA_PRECISAO,
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
        motivo = exagero_na_edicao(frases, cenas[i].get("frases") or [], i, banco, historico or [])
        if motivo:
            registro["revertidas"].append({"cena": i + 1, "motivo": motivo})
            log.warning("edição da cena %d não passou na conferência (%s) — fica a original", i + 1, motivo[:160])
            continue
        candidata = {**cenas[i], "frases": frases, "narration": " ".join(f["texto"] for f in frases)}
        # 22/11: a conferência acima vê a cena sozinha; a repetição só aparece no roteiro inteiro. Edição que
        # passa a dizer de novo um fato (ano/medida) que outra frase já diz volta a ser a original
        motivo = nome_novo_na_correcao(frases, cenas[i].get("frases") or [], alvos.get(i) or [], banco)
        if motivo:
            registro["revertidas"].append({"cena": i + 1, "motivo": motivo})
            log.warning("edição da cena %d não passou na conferência (%s) — fica a original", i + 1, motivo[:160])
            continue
        com_ela = {"scenes": novas[:i] + [candidata] + novas[i + 1:]}
        repetidos = _repetidos_em_frases(com_ela) - _repetidos_em_frases({"scenes": novas})
        # 23/11 (Svalbard v3): "o maior backup de sementes" posto logo antes de "a maior coleção de sementes"
        repetidos |= _superlativos_repetidos(com_ela) - _superlativos_repetidos({"scenes": novas})
        if repetidos:
            motivo = f"repete fato que outra frase do roteiro já diz ({', '.join(sorted(repetidos)[:4])}): diga uma vez só"
            registro["revertidas"].append({"cena": i + 1, "motivo": motivo})
            log.warning("edição da cena %d não passou na conferência (%s) — fica a original", i + 1, motivo[:120])
            continue
        novas[i] = candidata
        registro["cenas_editadas"].append(i + 1)
        registro["detalhes"].append({"cena": i + 1, **{k: str(c.get(k, ""))[:300]
                                                        for k in ("funcao", "preserva", "corrige", "reprovado", "trecho")},
                                     "antes": cenas[i].get("narration", ""), "depois": novas[i]["narration"]})
    conferir_edicao(novas, cenas, registro, banco)
    saida = {**script, "scenes": novas}
    textos_novos = {(i, _norm(f["texto"])) for i, sc in enumerate(novas) for f in sc.get("frases") or []}
    for p in problemas or []:
        seq = p.get("sequencia") or [{"cena": p.get("cena"), "texto": p["termo"]}]
        if isinstance(p.get("cena"), int) and all((x["cena"], _norm(x["texto"])) in textos_novos for x in seq):
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


def limpar_em_codigo(script: dict) -> dict:
    """25/11 (Svalbard 1022: editorial travado em sem_repeticao 4 — E26 nas cenas 3 e 8, "caixa-preta" de novo
    na narrativa da cena 9 com "o permafrost garante o frio"): fato_em_duas_cenas e narrativa_que_afirma só
    voltavam ao escritor; a melhor tentativa seguia com o defeito e a edição nem passava por eles. Antes de cada
    auditoria, em código: sai a frase factual cujos fatos TODOS já foram contados numa cena anterior e a
    narrativa (não pergunta, não abertura, não "Comenta PALAVRA") que afirma. Só sai se a cena continua com
    outra frase factual — nunca some uma cena."""
    vistos: set[str] = set()
    cenas = []
    tirou = []
    for i, sc in enumerate(script.get("scenes") or []):
        frases = list(sc.get("frases") or [])
        da_cena: set[str] = set()
        manter = []
        for j, f in enumerate(frases):
            t = f["texto"].strip()
            evs = set(f.get("evidencias") or [])
            repetida = f.get("tipo") != "narrativa" and evs and evs <= vistos
            afirma = (f.get("tipo") == "narrativa" and (i or j) and not f.get("fechamento") and not t.endswith("?")
                      and not _COMENTA.search(t) and _ALEGA.search(t))
            if repetida or afirma:
                if any(g.get("tipo") != "narrativa" for g in manter + frases[j + 1:]):
                    tirou.append((f, "fato já contado" if repetida else "narrativa que afirma"))
                    continue
            manter.append(f)
            da_cena |= evs
        vistos |= da_cena
        cenas.append({**sc, "frases": manter, "narration": " ".join(f["texto"] for f in manter)}
                     if len(manter) != len(frases) else sc)
    if not tirou:
        return script
    log.info("limpeza em código: %d frase(s) saem: %s", len(tirou),
             " | ".join(f"{m}: {f['texto'][:70]}" for f, m in tirou))
    return {**script, "scenes": cenas}


def _auditar_versao(script: dict, banco: dict, topic: str, previas: dict) -> tuple[dict, list[dict], dict]:
    """Auditoria completa de UMA versão: código + decisões por frase + leitura de espectador + editor."""
    script = limpar_em_codigo(sincronizar(script))
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


# ---------------------------------------------------------------- versões: comparação e seleção (23/11)
# Svalbard (job 1003): a edição corrigiu fatos (6 → 2 problemas) e a história caiu 7,2 → 5,6. Reavaliadas
# lado a lado, v1 → v3 perderam 2 pontos em narrativa, título e conclusão: a edição corrigia o fato COPIANDO
# o dado da evidência (lista país a país, "32 variedades… 881 amostras… 396 acessos… 82 países"), trocou a
# abertura narrativa por uma frase que repete a seguinte ("maior backup" + "maior coleção") e pôs a Síria pela
# terceira vez na conclusão. Ninguém comparava a versão nova com a anterior: cada versão era auditada sozinha
# e no fim ficava a de maior nota. Agora cada versão fica guardada inteira (texto, cenas, evidências, auditoria
# factual, semântica e editorial com os 8 critérios, hash); depois de cada edição a versão nova é comparada
# com a anterior critério a critério e cena a cena; a cena que corrigiu fato mas piorou a história volta para
# a rodada seguinte com o texto anterior e o pedido de reformulação localizada (o fato corrigido não volta);
# e a escolha final segue a prioridade fato > leitura enganosa > coerência > nota, podendo montar uma versão
# com as cenas seguras de cada versão (auditada inteira de novo, sem rodada de edição a mais).

DIFERENCA_CRITERIO = 1.0  # o editor (IA) varia ~0,5 por critério entre duas leituras do mesmo texto
REAUDITORIAS_MAX = 2      # versões antigas reauditadas (banco mudou) antes de desistir da escolha
# 24/11 (job 1007): a conferência olhava as 4 palavras seguintes e tomou "primeiro teste… guerra" e "primeiro
# depósito… guerra" pela mesma afirmação (recusou por isso a correção de "destruiu em Alepo"). Agora compara o
# OBJETO: a palavra que o superlativo/ordinal qualifica.
_SUPERLATIVO = re.compile(r"\b(maior(?:es)?|menor(?:es)?|[úu]nic[oa]s?|primeir[oa]s?|[úu]ltim[oa]s?|recordes?)\b", re.I)
_PALAVRA_CHEIA = re.compile(r"[a-zà-ÿ]{4,}")
_ORDINAL = ("prime", "ultim")
_ALCANCE = re.compile(r"\b(do mundo|do planeta|mundial|global|da história|da terra)\b", re.I)
_VAZIAS = {"real", "reais", "grande", "grandes", "mais", "vez", "este", "esta", "essa", "esse", "aqui"}


def _superlativos(texto: str) -> list[dict]:
    """Cada superlativo/ordinal da frase: a raiz, o OBJETO (a 1ª palavra cheia que ele qualifica: "primeiro
    teste", "maior coleção"), o complemento ("de sementes"), o alcance (do mundo, do planeta, mundial) e as
    palavras de contexto da frase."""
    t = texto.lower()
    achados = []
    for m in _SUPERLATIVO.finditer(t):
        resto = t[m.end():m.end() + 80].split(",")[0].split(";")[0]
        palavras = [dobrar(w) for w in _PALAVRA_CHEIA.findall(resto) if dobrar(w) not in _VAZIAS]
        if not palavras:
            continue
        comp = re.search(r"\b(?:de|da|do|das|dos)\s+([a-zà-ÿ]{4,})", resto)
        achados.append({"raiz": dobrar(m.group(1))[:5], "objeto": palavras[0],
                        "complemento": dobrar(comp.group(1)) if comp else "",
                        "alcance": bool(_ALCANCE.search(resto)),
                        # contexto: a frase inteira (o sujeito costuma vir antes: "o Sudão fez o primeiro depósito")
                        "contexto": {dobrar(w) for w in _PALAVRA_CHEIA.findall(t) if not _SUPERLATIVO.fullmatch(w)}
                        - _VAZIAS - {palavras[0]}})
    return achados


def mesma_afirmacao(a: dict, b: dict) -> bool:
    """Ordinal (primeiro, último): mesmo objeto E o contexto confirma (outra palavra em comum: "primeiro depósito
    do Sudão" duas vezes). Superlativo (maior, menor, único, recorde): mesmo objeto, ou o mesmo complemento com
    alcance nos dois ("o maior backup de sementes do planeta" e "a maior coleção de sementes do mundo" falam da
    mesma coisa: o cofre como o maior de sementes do mundo)."""
    if a["raiz"] != b["raiz"]:
        return False
    if a["raiz"] in _ORDINAL:
        return a["objeto"] == b["objeto"] and bool(a["contexto"] & b["contexto"])
    if a["objeto"] == b["objeto"]:
        return True
    return bool(a["complemento"]) and a["complemento"] == b["complemento"] and a["alcance"] and b["alcance"]


def _superlativos_repetidos(script: dict) -> set[str]:
    """A mesma afirmação de superlativo/ordinal em duas frases do roteiro (ver mesma_afirmacao)."""
    por_frase = [_superlativos(f["texto"]) for _, _, f in frases_de(script)]
    repetidos = set()
    for i, xs in enumerate(por_frase):
        for ys in por_frase[i + 1:]:
            for a in xs:
                for b in ys:
                    if mesma_afirmacao(a, b):
                        repetidos.add(f"{a['raiz']}… {a['objeto'] if a['objeto'] == b['objeto'] else a['complemento']}")
    return repetidos


def _nomes(texto: str) -> set[str]:
    """Nomes próprios e siglas da frase (palavra com maiúscula fora do começo)."""
    palavras = re.findall(r"[\wÀ-ÿ]+", texto)
    return {dobrar(p) for p in palavras[1:] if p[:1].isupper() and len(p) >= 3}


def _nomes_da_cena(sc: dict) -> set[str]:
    return {n for f in sc.get("frases") or [] for n in _nomes(f["texto"])}


def nomes_levados_pela_edicao(antes: dict, depois: dict, cenas: list[int]) -> dict[int, set[str]]:
    """Cena editada (1, 2, …) → nomes que a edição PÔS nela e que outra cena já traz (a Síria contada na
    cena 6 que a edição pôs também na conclusão). Sinal de repetição para a comparação, não trava."""
    a, d = antes.get("scenes", []), depois.get("scenes", [])
    saida = {}
    for c in cenas:
        i = c - 1
        if not (0 <= i < len(d)):
            continue
        novos = _nomes_da_cena(d[i]) - (_nomes_da_cena(a[i]) if i < len(a) else set())
        outras = {n for j, sc in enumerate(d) if j != i for n in _nomes_da_cena(sc)}
        if novos & outras:
            saida[c] = novos & outras
    return saida


def _chave_cena(sc: dict) -> str:
    return json.dumps([[_norm(f["texto"]), sorted(f.get("evidencias") or [])] for f in sc.get("frases") or []],
                      ensure_ascii=False)


def instantaneo(numero: int, origem: str, mudou: list[int], script: dict, problemas: list[dict], qualidade: dict,
                banco: dict) -> dict:
    """Uma versão guardada INTEIRA: roteiro, cenas, evidências por cena, auditoria factual (problemas por
    frase), semântica (leituras enganosas e conclusões do público), editorial (nota, 8 critérios, travas,
    cenas apontadas) e o hash. É o que a comparação e a seleção usam — nada é recalculado de memória."""
    import copy
    enganos = [p for p in problemas if p.get("tipo") == "leitura_enganosa"]
    factuais = [p for p in problemas if p.get("tipo") != "leitura_enganosa"]
    crit = {k: float(v) for k, v in (qualidade.get("criterios") or {}).items() if isinstance(v, (int, float))}
    travas = list(qualidade.get("travas") or [])
    nota = float(qualidade.get("nota", 0) or 0)
    coer = [crit[k] for k in CRITERIOS_TRAVA if k in crit]
    fat = qualidade.get("factual") or {}
    return {
        "versao": numero, "origem": origem, "cenas_modificadas": list(mudou), "hash": _hash_versao(script, banco),
        "roteiro": copy.deepcopy({"title": script.get("title", ""), "scenes": script.get("scenes", [])}),
        "cenas": [sc.get("narration", "") for sc in script.get("scenes", [])],
        "evidencias_por_cena": [sorted({e for f in sc.get("frases") or [] for e in f.get("evidencias") or []})
                                for sc in script.get("scenes", [])],
        "factual": {"problemas": factuais, "nota": fat.get("nota")},
        "semantica": {"enganos": enganos, "conclusoes": list(fat.get("conclusoes_do_publico") or [])},
        "editorial": {"nota": nota, "criterios": crit, "travas": travas, "alvos": list(qualidade.get("alvos") or []),
                      "aprovado": bool(qualidade.get("aprovado", nota >= NOTA_MINIMA))},
        "coerencia": round(sum(coer) / len(coer), 2) if coer else nota,
        "cenas_com_problema": sorted({c for p in problemas for c in [p.get("cena"), *(p.get("cenas") or [])]
                                      if isinstance(c, int)}),
        "nao_julgada": list(qualidade.get("nao_julgada") or []),
        "alertas": list(fat.get("alertas") or []),
        "aprovado": not problemas and not qualidade.get("nao_julgada")
        and bool(qualidade.get("aprovado", nota >= NOTA_MINIMA)),
    }


def chave_selecao(v: dict) -> tuple:
    """Prioridade da escolha: 1) correção factual (menos problemas de fato), 2) sem leitura enganosa,
    3) aprovada pela própria auditoria, 4) coerência da história (sem trava; média dos critérios essenciais),
    5) nota editorial; no empate, a mais recente. Nota 8 com erro de fato nunca passa à frente de nota 7 sem."""
    return (-len(v["factual"]["problemas"]), -len(v["semantica"]["enganos"]), v["aprovado"],
            not v["editorial"]["travas"], v["coerencia"], v["editorial"]["nota"], v["versao"])


def comparar_versoes(ant: dict, nova: dict) -> dict:
    """O que a edição de `ant` para `nova` fez: problemas factuais resolvidos e novos, leitura enganosa
    (interpretação geral), cada um dos 8 critérios (melhorou/piorou, com tolerância DIFERENCA_CRITERIO para
    a variação do editor), repetição (critério + o que o código vê), abertura, conclusão e título — e, cena
    a cena, qual edição é responsável pela piora."""
    def termos(lista):
        return {_norm(p.get("termo", "")) for p in lista}
    fa, fn = ant["factual"]["problemas"], nova["factual"]["problemas"]
    ea, en = ant["semantica"]["enganos"], nova["semantica"]["enganos"]
    resolvidos = [p for p in fa + ea if _norm(p.get("termo", "")) not in termos(fn + en)]
    novos = [p for p in fn + en if _norm(p.get("termo", "")) not in termos(fa + ea)]
    ca, cn = ant["editorial"]["criterios"], nova["editorial"]["criterios"]
    criterios = {k: {"antes": ca[k], "depois": cn[k], "diferenca": round(cn[k] - ca[k], 1)}
                 for k in CRITERIOS_EDITORIAIS if k in ca and k in cn}
    pioraram = [k for k, d in criterios.items() if d["diferenca"] <= -DIFERENCA_CRITERIO]
    melhoraram = [k for k, d in criterios.items() if d["diferenca"] >= DIFERENCA_CRITERIO]
    editadas = sorted(nova["cenas_modificadas"])
    n_cenas = len(nova["cenas"])
    rep_codigo = sorted(_superlativos_repetidos(nova["roteiro"]) - _superlativos_repetidos(ant["roteiro"]))
    nomes = nomes_levados_pela_edicao(ant["roteiro"], nova["roteiro"], editadas)
    responsaveis: dict[int, list[str]] = {}

    def culpa(c: int, motivo: str) -> None:
        if c in editadas and motivo not in responsaveis.setdefault(c, []):
            responsaveis[c].append(motivo)
    for a in nova["editorial"]["alvos"]:
        if a.get("criterio") in pioraram:
            for c in a.get("cenas") or []:
                try:
                    c = int(c)
                except (TypeError, ValueError):
                    continue
                d = criterios[a["criterio"]]
                culpa(c, f"{a['criterio']} {d['antes']:.0f}→{d['depois']:.0f}: {str(a.get('descricao', ''))[:200]}")
    if "abertura" in pioraram:
        culpa(1, f"abertura {criterios['abertura']['antes']:.0f}→{criterios['abertura']['depois']:.0f}")
    if "conclusao" in pioraram:
        culpa(n_cenas, f"conclusão {criterios['conclusao']['antes']:.0f}→{criterios['conclusao']['depois']:.0f}")
    for c, ns in nomes.items():
        if {"sem_repeticao", "novidade"} & set(pioraram):
            culpa(c, "repete o que outra cena já conta (" + ", ".join(sorted(ns)) + ")")
    for i, sc in enumerate(nova["roteiro"]["scenes"]):
        if any(r.split("… ")[1] in dobrar(sc.get("narration", "")).lower() for r in rep_codigo):
            culpa(i + 1, "repete a mesma afirmação de superlativo (" + ", ".join(rep_codigo) + ")")
    if pioraram and not responsaveis and editadas:
        for c in editadas:  # o editor não localizou: toda cena editada é suspeita
            culpa(c, "piora não localizada pelo editor (" + ", ".join(pioraram) + ")")
    corrigiu = sorted({p["cena"] + 1 for p in resolvidos if isinstance(p.get("cena"), int)} & set(editadas))
    return {
        "de": ant["versao"], "para": nova["versao"], "cenas_editadas": editadas,
        "factual": {"resolvidos": [p.get("termo", "")[:120] for p in resolvidos],
                    "novos": [f"{p.get('tipo')}: {p.get('termo', '')[:120]}" for p in novos],
                    "antes": len(fa) + len(ea), "depois": len(fn) + len(en)},
        "interpretacao": {"enganos_antes": len(ea), "enganos_depois": len(en),
                          "correta": not en, "piorou": len(en) > len(ea) or any(p in en for p in novos)},
        "criterios": criterios, "melhoraram": melhoraram, "pioraram": pioraram,
        "repeticao": {"sem_repeticao": criterios.get("sem_repeticao"), "superlativos": rep_codigo,
                      "nomes": {str(c): sorted(ns) for c, ns in nomes.items()}},
        "abertura": criterios.get("abertura"), "conclusao": criterios.get("conclusao"),
        "titulo": {**(criterios.get("titulo") or {}), "mudou": ant["roteiro"]["title"] != nova["roteiro"]["title"]},
        "nota": {"antes": ant["editorial"]["nota"], "depois": nova["editorial"]["nota"]},
        "corrigiu_fato_em": corrigiu,
        "responsaveis": {str(c): m for c, m in sorted(responsaveis.items())},
    }


def alvos_de_preservacao(comp: dict, ant: dict) -> dict[int, list[str]]:
    """Cena (índice 0) → pedido de reformulação localizada para cada cena cuja edição piorou a história. A
    cena que corrigiu fato leva o texto anterior como REFERÊNCIA DE HISTÓRIA, nunca como texto a restaurar."""
    alvos: dict[int, list[str]] = {}
    for c, motivos in comp["responsaveis"].items():
        c = int(c)
        anterior = ant["cenas"][c - 1] if 0 < c <= len(ant["cenas"]) else ""
        if c in comp["corrigiu_fato_em"]:
            msg = (f"PRESERVAR A HISTÓRIA — a edição anterior desta cena corrigiu o fato, mas piorou a história: "
                   f"{' | '.join(motivos)}. Texto ANTES da correção (só como referência da função da cena; tinha o "
                   f"erro factual, NÃO o traga de volta): «{anterior[:500]}». Reformule só o que piorou: mantenha a "
                   "correção, devolva à cena a função que ela tinha e corrija restringindo ou tirando, não "
                   "acrescentando dados.")
        else:
            msg = (f"PRESERVAR A HISTÓRIA — a edição anterior desta cena piorou a história sem corrigir fato: "
                   f"{' | '.join(motivos)}. Texto anterior da cena (já passou na auditoria): «{anterior[:500]}». "
                   "Volte a ele ou faça melhor, sem problema factual novo.")
        alvos.setdefault(c - 1, []).append(msg)
    return alvos


def compor_versao(versoes: list[dict], banco: dict, topic: str) -> tuple[dict, list[int], int] | None:
    """Uma versão montada com cenas JÁ AUDITADAS: parte da melhor versão (chave_selecao) e troca cada cena com
    problema, ou responsável por piora da história, pelo texto da mesma cena numa versão em que ele passou.
    Texto de cena é seguro só se NENHUMA auditoria que o viu apontou problema nele (o mesmo texto reprovado
    depois não serve mais). Sem opção segura para alguma cena problemática: None. A versão montada é auditada
    inteira como qualquer outra (não é rodada de edição). Devolve (roteiro, versão de origem de cada cena,
    versão base)."""
    if len(versoes) < 2:
        return None
    n = len(versoes[0]["roteiro"]["scenes"])
    if any(len(v["roteiro"]["scenes"]) != n for v in versoes):
        return None
    reprovado: dict[tuple[int, str], bool] = {}
    for v in versoes:
        for i, sc in enumerate(v["roteiro"]["scenes"]):
            k = (i, _chave_cena(sc))
            reprovado[k] = reprovado.get(k, False) or i in v["cenas_com_problema"]
    ordem = sorted(versoes, key=chave_selecao, reverse=True)
    base = ordem[0]
    por_numero = {v["versao"]: v for v in versoes}
    # cenas cuja edição piorou a história (comparação da base com a versão de onde a edição dela saiu)
    piores = {int(c) - 1 for c in (base.get("comparacao") or {}).get("responsaveis", {})}
    cenas, origem = [], []
    for i, sc in enumerate(base["roteiro"]["scenes"]):
        seguro = not reprovado[(i, _chave_cena(sc))]
        if seguro and i not in piores:
            cenas.append(sc)
            origem.append(base["versao"])
            continue
        opcoes = [v for v in ordem if v["versao"] != base["versao"]
                  and not reprovado[(i, _chave_cena(v["roteiro"]["scenes"][i]))]
                  and _chave_cena(v["roteiro"]["scenes"][i]) != _chave_cena(sc)]
        if opcoes:
            cenas.append(opcoes[0]["roteiro"]["scenes"][i])
            origem.append(opcoes[0]["versao"])
        elif seguro:
            cenas.append(sc)  # piorou a história, mas não há outra segura: fica (a seleção decide)
            origem.append(base["versao"])
        else:
            return None  # cena com problema factual sem texto seguro: montar não resolve
    if origem == [base["versao"]] * n:
        return None
    import copy
    montado = copy.deepcopy({**base["roteiro"], "scenes": cenas})
    if any(all(_chave_cena(a) == _chave_cena(b) for a, b in zip(montado["scenes"], v["roteiro"]["scenes"]))
           for v in versoes):
        return None  # igual a uma versão que já existe
    _, prob = auditar_codigo(montado, banco, topic)
    fontes = [por_numero[o]["roteiro"] for o in set(origem)]
    if prob or _repetidos_em_frases(montado) - set().union(*map(_repetidos_em_frases, fontes)) \
            or _superlativos_repetidos(montado) - set().union(*map(_superlativos_repetidos, fontes)):
        return None  # juntar cenas de versões diferentes criou erro ou repetição
    return montado, origem, base["versao"]


def _registra_versao(v: dict) -> None:
    """Cada versão fica no log inteira (diagnóstico: a v2 do job 1003 se perdeu e a queda não pôde ser lida)."""
    log.info("texto da versão %d (%s, hash %s):\n%s", v["versao"], v["origem"], v["hash"], "\n".join(
        f"  {i}. " + " ".join(f"[{f.get('tipo')}; {','.join(f.get('evidencias') or []) or '-'}] {f['texto']}"
                              for f in sc.get("frases") or [])
        for i, sc in enumerate(v["roteiro"]["scenes"], 1)))


def _registra_problemas(versao: int, script: dict, problemas: list[dict], banco: dict) -> None:
    """23/11 (job 1006: 4 problemas factuais da v3 sem registro): toda reprovação fica no log com o texto exato,
    a cena, as evidências citadas com o trecho da fonte e o motivo do auditor."""
    evs = por_id(banco)
    for p in problemas:
        f = _frase_do_problema(script, p)
        cits = []
        for x in f.get("evidencias") or []:
            e = evs.get(x)
            if e:
                c = next((c for c in e.get("citacoes") or [] if c.get("verificado")), (e.get("citacoes") or [{}])[0])
                cits.append(f"{x} ({e['status']}, {c.get('dominio', '')}): \"{str(c.get('trecho') or '')[:220]}\"")
        log.info("reprovação v%d — cena %s, %s: \"%s\"\n    evidências: %s\n    motivo: %s", versao,
                 (p["cena"] + 1) if isinstance(p.get("cena"), int) else "?", p.get("tipo"), p.get("termo", ""),
                 " | ".join(cits) or "nenhuma citada", str(p.get("motivo", ""))[:600])


def _registra_comparacao(c: dict) -> None:
    crit = ", ".join(f"{k} {d['antes']:.0f}→{d['depois']:.0f}" for k, d in c["criterios"].items() if d["diferenca"])
    log.info("comparação v%d → v%d (cenas %s): fatos %d → %d (resolvidos %d, novos %d); leitura enganosa %d → %d; "
             "editorial %.1f → %.1f; melhoraram %s; pioraram %s%s%s", c["de"], c["para"], c["cenas_editadas"],
             c["factual"]["antes"], c["factual"]["depois"], len(c["factual"]["resolvidos"]), len(c["factual"]["novos"]),
             c["interpretacao"]["enganos_antes"], c["interpretacao"]["enganos_depois"], c["nota"]["antes"],
             c["nota"]["depois"], c["melhoraram"] or "-", c["pioraram"] or "-", f" [{crit}]" if crit else "",
             "".join(f"\n  cena {k} piorou a história: {'; '.join(m)[:300]}" for k, m in c["responsaveis"].items()))


# problemas achados pela conferência em código: a edição que os conserta passa de novo por ela (ver editar_cenas)
CORTES_MAX = 2  # 25/11: cortes antes da edição (sem chamada de escrita; cada um custa uma auditoria completa)
TIPOS_DO_CODIGO = frozenset({"numero_arredondado", "numero_contraditorio", "numero_sem_evidencia", "divergencia_como_faixa",
                             "sinal_invertido", "hipotese_como_fato", "fonte_unica_sem_atribuicao", "ordem_nao_comprovada"})


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
    mudou depois dela, é auditada de novo. 23/11: cada versão é guardada inteira (instantaneo), comparada com a
    anterior critério a critério (comparar_versoes) e a cena que piorou a história volta à rodada seguinte
    para reformulação localizada; a escolha segue chave_selecao e pode montar uma versão com as cenas seguras
    de cada versão (compor_versao). Devolve (roteiro, relatório) ou levanta RoteiroReprovado."""
    from . import metricas
    palavras_inicio = _palavras(script)
    piso = max(min_palavras, palavras_inicio * (1 - MAX_PERDA))
    relatorio: dict = {"rodadas": [], "edicoes": 0, "versoes": [], "registro_edicoes": [], "comparacoes": []}
    previas: dict = {}
    historico: list[dict] = []  # 22/11: reprovações de todas as versões deste job (só deste job)
    versoes: list[dict] = []    # 23/11: instantâneo de CADA versão (nenhuma se perde)

    def registrar(texto: dict, problemas: list[dict], qualidade: dict, origem: str, mudou: list[int],
                  de_versao: int | None) -> dict:
        inst = instantaneo(len(versoes) + 1, origem, mudou, texto, problemas, qualidade, banco)
        inst.update(_script=texto, problemas=problemas, qualidade=qualidade, de_versao=de_versao,
                    palavras=_palavras(texto), frases_julgadas=qualidade.get("frases_julgadas"),
                    frases_reaproveitadas=qualidade.get("frases_reaproveitadas"))
        if de_versao is not None:
            inst["comparacao"] = comparar_versoes(versoes[de_versao - 1], inst)
            relatorio["comparacoes"].append(inst["comparacao"])
        versoes.append(inst)
        registrar_historico(historico, inst["versao"], texto, problemas, banco, qualidade)
        relatorio["rodadas"].append({"versao": inst["versao"], "problemas": problemas, "qualidade": qualidade})
        travas = inst["editorial"]["travas"]
        log.info("versão %d (%s, cenas %s): %d problema(s) factual(is); factual %s, editorial %.1f%s%s", inst["versao"],
                 origem, mudou or "-", len(problemas), inst["factual"]["nota"], inst["editorial"]["nota"],
                 f"; travas: {'; '.join(travas)}" if travas else "", " — APROVADA" if inst["aprovado"] else
                 (f" — NÃO JULGADA ({'; '.join(inst['nao_julgada'])})" if inst["nao_julgada"] else ""))
        if inst["alertas"]:
            log.info("versão %d: %d alerta(s) para revisão humana (interpretação vista por uma leitura só): %s",
                     inst["versao"], len(inst["alertas"]), " | ".join(f"{a['tipo']} {a['frases']}: "
                                                                     f"{a['o_publico_conclui'][:100]}" for a in inst["alertas"]))
        _registra_versao(inst)
        _registra_problemas(inst["versao"], texto, problemas, banco)
        if inst.get("comparacao"):
            _registra_comparacao(inst["comparacao"])
        return inst

    origem, mudou, de_versao = "escrita", [], None
    relatorio["cortes"] = 0
    relatorio["trocas"] = 0
    relatorio["registro_trocas"] = []
    teto = EDICOES_MAX
    # 25/11 (Svalbard 1020: dois cortes gastaram as 2 rodadas e a última frase enganosa, que só uma reescrita
    # resolvia — o corte deixaria 273 de 286 palavras —, ficou sem edição): corte e edição têm limites próprios
    for _ in range(EDICOES_MAX + EDICAO_EXTRA + CORTES_MAX + TROCAS_MAX + 1):
        script, problemas, qualidade = _auditar_versao(script, banco, topic, previas)
        atual = registrar(script, problemas, qualidade, origem, mudou, de_versao)
        if atual["aprovado"] and qualidade["nota"] >= NOTA_ALVO:
            break
        # 26/11 (Svalbard 1024: v4 sem fato errado nem leitura enganosa, história 6,8 — e as 2 edições já gastas):
        # versão limpa a menos de 1 ponto do mínimo ganha UMA edição a mais, só de história
        if (not problemas and NOTA_MINIMA - 1 <= qualidade["nota"] < NOTA_MINIMA
                and relatorio["edicoes"] >= teto == EDICOES_MAX):
            teto += EDICAO_EXTRA
            log.info("versão %d limpa com história %.1f: mais %d edição de história", atual["versao"],
                     qualidade["nota"], EDICAO_EXTRA)
        if relatorio["edicoes"] >= teto and relatorio["cortes"] >= CORTES_MAX \
                and relatorio["trocas"] >= TROCAS_MAX:
            break
        # 23/11: a próxima edição parte da versão atual — a não ser que uma anterior tenha estritamente menos
        # problemas de fato e de leitura E história no mínimo tão boa (a edição que piorou os fatos sem melhorar
        # a história não vira ponto de partida; a que melhorou a história segue e tem o fato corrigido nela — se
        # não for corrigido, a anterior continua guardada e vence na seleção)
        base = atual
        melhor = min(versoes, key=lambda x: (len(x["problemas"]), -x["editorial"]["nota"], -x["versao"]))
        if len(melhor["problemas"]) < len(atual["problemas"]) and melhor["editorial"]["nota"] >= atual["editorial"]["nota"]:
            base = melhor
            log.info("a próxima edição parte da versão %d (%d problema(s)), não da %d (%d)", base["versao"],
                     len(base["problemas"]), atual["versao"], len(atual["problemas"]))
        script, problemas, qualidade = base["_script"], base["problemas"], base["qualidade"]
        # 25/11 (catálogo Svalbard): corrigir fato reescrevendo trocava um exagero por outro e desmontava a história
        # (editorial 6,9 → 4,0); o corte puro subiu o editorial de 6,9 para 8,1. Frase reprovada SAI primeiro; a
        # edição seguinte só mexe na história, com a frase nova conferida contra o fato (conferir_edicao)
        # (o problema que o CÓDIGO acha — número arredondado, sinal, atribuição — vai à edição: o conserto é conferido
        # pelo próprio código; o que só a IA julga — excede, sem suporte, leitura enganosa — sai)
        julgados = [p for p in problemas if p["tipo"] not in TIPOS_DO_CODIGO]
        # 25/11 (Svalbard 1021): primeiro a troca mínima pela sugestão do leitor (conferida contra o fato) —
        # o corte tirava a conclusão e derrubava a história; só o que a troca não resolve é cortado depois
        # 26/11 (Svalbard 1023: as duas trocas foram gastas na v1, história 5,5; a v4 chegou a 6,9 com 1 leitura
        # enganosa e não sobrou troca): história abaixo do mínimo vai primeiro à edição (que também recebe os
        # problemas); troca e corte consertam a versão de história boa
        historia_fraca = qualidade["nota"] < NOTA_MINIMA and relatorio["edicoes"] < teto
        if historia_fraca:
            julgados = []
        if julgados and relatorio["trocas"] < TROCAS_MAX:
            relatorio["trocas"] += 1
            trocado, reg_troca = trocar_frases_apontadas(script, julgados, banco, topic)
            reg_troca["da_versao"] = base["versao"]
            relatorio["registro_trocas"].append(reg_troca)
            if trocado and not roteiro_insuficiente(trocado, min_palavras) and _palavras(trocado) >= piso:
                origem, mudou, de_versao = "troca", sorted({x["cena"] for x in reg_troca["trocadas"]}), base["versao"]
                script = trocado
                continue
        if julgados and relatorio["cortes"] < CORTES_MAX:
            cortado = cortar_frases_reprovadas(script, julgados)
            if cortado and not roteiro_insuficiente(cortado, min_palavras) and _palavras(cortado) >= piso:
                relatorio["cortes"] += 1
                log.info("corte antes da edição: %d frase(s) reprovada(s) saem da versão %d", len(julgados), base["versao"])
                origem, mudou, de_versao = "corte", [], base["versao"]
                script = cortado
                continue
        if relatorio["edicoes"] >= teto:
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
        if base.get("comparacao"):
            # a edição que levou a esta versão piorou a história em alguma cena: reformulação localizada dela
            for i, msgs in alvos_de_preservacao(base["comparacao"], versoes[base["de_versao"] - 1]).items():
                alvos.setdefault(i, []).extend(msgs)
        crit = qualidade.get("criterios") or {}
        titulo_motivo = next((a["descricao"] for a in qualidade.get("alvos") or [] if a.get("criterio") == "titulo"), "") \
            if crit.get("titulo", 10) < NOTA_MINIMA else ""
        if not alvos and not titulo_motivo:
            log.warning("edição: a auditoria não aponta cena nenhuma — nada a editar localmente")
            break
        while True:
            relatorio["edicoes"] += 1
            editado, reg = editar_cenas(script, alvos, banco, topic, titulo_motivo=titulo_motivo, problemas=problemas,
                                        prioritarias=novas_ids, historico=historico)
            registrar_tentativas(historico, base["versao"], editado, reg)
            reg["da_versao"] = base["versao"]
            reg["evidencias_novas"] = sorted(novas_ids)
            relatorio["registro_edicoes"].append(reg)
            if reg.get("cenas_editadas") or reg.get("titulo") or relatorio["edicoes"] >= teto \
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
            log.warning("edição: nenhuma cena mudou — fica a versão %d", base["versao"])
            break
        if _palavras(editado) < piso:
            _registra_texto(editado, "edição esvaziou")
            log.warning("edição tiraria demais da história (%d -> %d palavras) — descartada",
                        palavras_inicio, _palavras(editado))
            break
        origem, mudou, de_versao = "edicao", reg["cenas_editadas"], base["versao"]
        script = editado

    # ---- seleção (23/11): prioridade fato > leitura enganosa > aprovada > coerência > nota (chave_selecao)
    reauditorias = 0

    def escolher() -> dict | None:
        """A melhor versão sem problema; a de auditoria vencida (o banco mudou depois dela) é auditada de novo
        inteira e sai da disputa — fica a reauditoria, que só concorre se passar."""
        nonlocal reauditorias
        while True:
            limpas = sorted((x for x in versoes if not x["problemas"] and not x.get("substituida")),
                            key=chave_selecao, reverse=True)
            if not limpas:
                return None
            x = limpas[0]
            if x["hash"] == _hash_versao(x["_script"], banco):
                return x
            if reauditorias >= REAUDITORIAS_MAX:
                x["substituida"] = "auditoria vencida (o banco mudou) e o limite de reauditorias acabou"
                continue
            reauditorias += 1
            log.warning("versão %d foi auditada com outro banco — auditoria completa de novo", x["versao"])
            s2, p2, q2 = _auditar_versao(x["_script"], banco, topic, previas)
            nova = registrar(s2, p2, q2, "reauditoria", [], None)
            nova.update(reauditada=True, reauditada_de=x["versao"])
            x["substituida"] = f"reauditada como versão {nova['versao']}"
            if not nova["aprovado"]:
                log.warning("versão %d não passa mais na auditoria (%d problema(s), editorial %.1f) — fora da escolha",
                            x["versao"], len(p2), nova["editorial"]["nota"])

    final = escolher()
    if (final is None or not final["aprovado"]) and len(versoes) >= 2:
        # nenhuma versão aprovada: uma versão montada com as cenas seguras de cada versão (sem texto novo)
        montagem = compor_versao([x for x in versoes if not x.get("substituida")], banco, topic)
        if montagem:
            montado, origem_cenas, base_n = montagem
            base_s = versoes[base_n - 1]["_script"]
            trocadas = [i + 1 for i, o in enumerate(origem_cenas) if o != base_n]
            log.info("versão montada a partir da %d: cenas %s vêm das versões %s — auditoria completa",
                     base_n, trocadas, [origem_cenas[c - 1] for c in trocadas])
            s3, p3, q3 = _auditar_versao({**base_s, **montado}, banco, topic, previas)
            nova = registrar(s3, p3, q3, "composicao", trocadas, base_n)
            nova["origem_das_cenas"] = origem_cenas
            final = escolher()
    if final is None:
        # 24/11 (Svalbard v6): a frase reprovada cuja correção não tem fonte SAI do roteiro, sozinha — em vez de
        # reprovar o vídeo inteiro por uma frase. Uma auditoria completa da versão cortada decide.
        melhor = max((x for x in versoes if not x.get("substituida")), key=chave_selecao, default=None)
        cortado = cortar_frases_reprovadas(melhor["_script"], melhor["problemas"]) if melhor else None
        curto = roteiro_insuficiente(cortado, min_palavras) if cortado else None
        if cortado and not curto:
            log.info("corte automático: %d frase(s) reprovada(s) saem da versão %d — auditoria completa",
                     len(melhor["problemas"]), melhor["versao"])
            s4, p4, q4 = _auditar_versao(cortado, banco, topic, previas)
            registrar(s4, p4, q4, "corte", [], melhor["versao"])
            final = escolher()
        elif melhor:
            log.warning("corte automático não aplicado: %s", curto or "problema sem frase identificada")
    resumo = ("versao", "origem", "cenas_modificadas", "hash", "palavras", "de_versao", "aprovado", "coerencia",
              "nao_julgada", "alertas",
              "frases_julgadas", "frases_reaproveitadas", "reauditada", "reauditada_de", "substituida", "origem_das_cenas")
    relatorio["versoes"] = [{**{k: x[k] for k in resumo if k in x}, "problemas_factuais": len(x["problemas"]),
                             "nota_factual": x["factual"]["nota"], "enganos": len(x["semantica"]["enganos"]),
                             "nota_editorial": x["editorial"]["nota"], "criterios": x["editorial"]["criterios"],
                             "travas": x["editorial"]["travas"]} for x in versoes]
    relatorio["instantaneos"] = [{k: v for k, v in x.items() if not k.startswith("_") and k not in ("problemas", "qualidade")}
                                 for x in versoes]
    relatorio["selecao"] = [{"versao": x["versao"], "problemas_factuais": len(x["factual"]["problemas"]),
                             "enganos": len(x["semantica"]["enganos"]), "aprovado": x["aprovado"],
                             "coerencia": x["coerencia"], "nota_editorial": x["editorial"]["nota"],
                             "fora": x.get("substituida")} for x in sorted(versoes, key=chave_selecao, reverse=True)]
    relatorio["historico"] = historico
    voltaram = sum(1 for h in historico if h["voltou_de"])
    metricas.anotar("historico_reprovacoes", {"itens": len(historico), "voltaram": voltaram})
    metricas.anotar("versoes", [{k: x[k] for k in ("versao", "origem", "cenas_modificadas", "problemas_factuais",
                                                     "nota_factual", "nota_editorial", "aprovado")}
                                for x in relatorio["versoes"]])
    log.info("seleção de versões: %s", " > ".join(
        f"v{s['versao']} (fatos {s['problemas_factuais']}, enganos {s['enganos']}, coerência {s['coerencia']}, "
        f"editorial {s['nota_editorial']:.1f}{', fora: ' + s['fora'] if s['fora'] else ''})" for s in relatorio["selecao"]))
    if final is None:
        melhor = max(versoes, key=chave_selecao)
        _registra_texto(melhor["_script"], "sem evidência depois da edição")
        raise RoteiroReprovado("afirmações sem evidência depois de {} edição(ões) por cena: {}".format(
            relatorio["edicoes"], "; ".join(f"{p['tipo']}: {p['termo'][:80]}" for p in melhor["problemas"])[:500]))
    if final["versao"] != versoes[-1]["versao"]:
        log.info("fica a versão %d (editorial %.1f): é a primeira na ordem fato > leitura > coerência > nota",
                 final["versao"], final["editorial"]["nota"])
    relatorio["versao_final"] = final["versao"]
    relatorio["qualidade"] = qualidade = final["qualidade"]
    if not final["aprovado"]:
        _registra_texto(final["_script"], "qualidade baixa")
        raise RoteiroReprovado(f"qualidade narrativa {qualidade['nota']:.1f}/10 (mínimo {NOTA_MINIMA}) ou com trava "
                               f"editorial depois de {relatorio['edicoes']} edição(ões): " + "; ".join(qualidade.get("problemas") or [])[:300])
    return final["_script"], relatorio


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


_PROMPT_TROCA = """Um roteiro de vídeo (tema: {tema}) tem frases que um leitor apontou: elas dizem mais do que os
fatos ou levam o público a concluir algo falso. Para CADA problema, troque a MENOR parte possível das frases
indicadas, aplicando a correção sugerida pelo leitor. Regras:
- use SÓ o que os FATOS dizem; nunca acrescente fato, número, causa, nome ou comparação;
- prefira tirar ou suavizar a palavra que exagera ("garante" → "ajuda a", "sem energia" sai) a reescrever a frase;
- não ligue fatos com causa ("por isso", "porque", "graças a") se o fato não diz a causa;
- frase de opinião/conclusão (ponto de vista) continua opinião, dita como opinião ("para mim", "o que impressiona"),
  e só retoma o que o vídeo já mostrou — nunca afirma fato novo;
- mantenha o tom falado, a ligação com a frase anterior e a seguinte e o tamanho parecido;
- combinação enganosa (duas frases juntas levam à conclusão falsa): a troca mais segura costuma ser tirar a
  frase que sustenta a conclusão errada (texto vazio) ou dizer na outra o limite que o fato dá;
- se não há troca segura, texto vazio (a frase sai);
- devolva só o texto falado, sem a etiqueta entre colchetes.
ROTEIRO (frases numeradas):
{roteiro}
PROBLEMAS:
{problemas}
Responda SÓ JSON: {{"trocas": [{{"n": 7, "texto": "frase nova"}}]}}"""

_ETIQUETA = re.compile(r"^(\[[^\]]{1,20}\]\s*)+")
EDICAO_EXTRA = 1  # 26/11: edição de história a mais para a versão limpa a menos de 1 ponto do mínimo
TROCAS_MAX = 2  # 25/11: rodadas de troca mínima pela sugestão do leitor (antes do corte e da edição)


def trocar_frases_apontadas(script: dict, problemas: list[dict], banco: dict, topic: str) -> tuple[dict | None, dict]:
    """25/11 (Svalbard 1021: cortar tirava a conclusão e o editorial caía 6,4 → 5,4; a edição de cena voltava
    4 de 4): cada leitor que aponta o problema já diz como corrigir. A frase apontada (ou as frases da combinação
    enganosa) recebe a troca MÍNIMA que aplica essa sugestão; a frase nova factual é conferida contra os FATOS
    citados e só fica se passar. Devolve (roteiro trocado | None se nada mudou, registro)."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    lista = frases_de(script)
    pos = {(i, j): n for n, (i, j, _) in enumerate(lista, 1)}
    mapa = {m["id"]: m for m in mapa_do_conhecimento(banco)}
    registro: dict = {"trocadas": [], "recusadas": []}
    permitidas: set[int] = set()
    blocos = []
    for k, p in enumerate(problemas, 1):
        ns = sorted({pos[(x["cena"], x["frase"])] for x in p.get("sequencia") or [] if (x["cena"], x["frase"]) in pos}
                    or ({pos[(p["cena"], p["frase"])]} if (p.get("cena"), p.get("frase")) in pos else set()))
        if not ns:
            continue
        permitidas |= set(ns)
        ids = sorted({e for n in ns for e in lista[n - 1][2].get("evidencias") or []})
        blocos.append(f"{k}) frases {ns} — {p.get('motivo') or p.get('termo')}\n{_trechos_das(ids, mapa)}")
    if not blocos:
        return None, registro
    roteiro = "\n".join(f"{n}) [{f.get('tipo') or 'fato'}] {f['texto']}" for n, (_, _, f) in enumerate(lista, 1))
    try:
        raw = complete([{"role": "user", "content": _PROMPT_TROCA.format(tema=topic, roteiro=roteiro,
                                                                         problemas="\n".join(blocos))}],
                       max_tokens=3000, temperature=0.2, validate=lambda r: '"trocas"' in r,
                       model_filter=is_strong_model)
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("troca mínima indisponível (%s)", exc)
        return None, registro
    novas: dict[int, str] = {}
    for d in _obj(raw).get("trocas") or []:
        if isinstance(d, dict) and str(d.get("n", "")).isdigit() and int(d["n"]) in permitidas:
            # 26/11 (Svalbard 1023): o modelo copiava a etiqueta do roteiro numerado ("[factual] Sistemas…")
            novas[int(d["n"])] = _ETIQUETA.sub("", " ".join(str(d.get("texto") or "").split()))
    novas = {n: t for n, t in novas.items() if _norm(t) != _norm(lista[n - 1][2]["texto"])}
    if not novas:
        return None, registro
    # frase nova (não vazia) conferida contra os fatos que a original citava; narrativa: sem afirmação escondida
    conferir = []
    for n, t in novas.items():
        i, j, f = lista[n - 1]
        if not t:
            continue
        if f.get("tipo") == "narrativa":
            achado = afirmacao_escondida(t, _nomes_conhecidos(script, topic))
            if achado:
                registro["recusadas"].append({"n": n, "texto": t[:160], "motivo": f"narrativa afirma: {achado}"[:200]})
                novas[n] = None
            continue
        ant = lista[n - 2][2]["texto"] if n > 1 else ""
        conferir.append((n, {"texto": t, "evidencias": list(f.get("evidencias") or []), "anterior": ant}))
    veredito = conferir_contra_trechos([it for _, it in conferir], banco) if conferir else {}
    for k, (n, it) in enumerate(conferir):
        if veredito.get(k):
            registro["recusadas"].append({"n": n, "texto": it["texto"][:160], "motivo": veredito[k][:200]})
            novas[n] = None
    aceitas = {n: t for n, t in novas.items() if t is not None}
    if not aceitas:
        log.info("troca mínima: nenhuma troca passou na conferência (%d recusada(s))", len(registro["recusadas"]))
        return None, registro
    cenas = [dict(sc, frases=[dict(f) for f in sc.get("frases") or []]) for sc in script["scenes"]]
    tirar: set[tuple[int, int]] = set()
    for n, t in aceitas.items():
        i, j, f = lista[n - 1]
        registro["trocadas"].append({"n": n, "cena": i + 1, "antes": f["texto"][:160], "depois": t[:160]})
        if t:
            cenas[i]["frases"][j]["texto"] = t
        else:
            tirar.add((i, j))
    saida = []
    for i, sc in enumerate(cenas):
        frases = [f for j, f in enumerate(sc["frases"]) if (i, j) not in tirar]
        if any(f.get("tipo") != "narrativa" for f in frases):
            saida.append({**sc, "frases": frases, "narration": " ".join(f["texto"] for f in frases)})
    if not saida:
        return None, registro
    log.info("troca mínima: %d frase(s) trocada(s), %d recusada(s): %s", len(aceitas), len(registro["recusadas"]),
             " | ".join(f"\"{x['antes'][:60]}\" → \"{x['depois'][:60]}\"" for x in registro["trocadas"]))
    return {**script, "scenes": saida}, registro


def cortar_frases_reprovadas(script: dict, problemas: list[dict]) -> dict | None:
    """Tira do roteiro as frases apontadas pelos problemas (cena, frase). None se algum problema não aponta uma
    frase, se não há o que cortar ou se o roteiro ficaria sem cena. Cena que fica vazia sai."""
    alvo = {(p.get("cena"), p.get("frase")) for p in problemas}
    if not alvo or any(not isinstance(c, int) or not isinstance(f, int) for c, f in alvo):
        return None
    cenas = []
    for i, sc in enumerate(script.get("scenes") or []):
        frases = [f for k, f in enumerate(sc.get("frases") or []) if (i, k) not in alvo]
        if any(f.get("tipo") != "narrativa" for f in frases):
            cenas.append({**sc, "frases": frases, "narration": " ".join(f["texto"] for f in frases)})
    if not cenas or sum(len(sc["frases"]) for sc in cenas) == sum(len(sc.get("frases") or []) for sc in script["scenes"]):
        return None
    return {**script, "scenes": cenas}


def roteiro_insuficiente(script: dict, min_palavras: int) -> str | None:
    """Critério de tamanho do modo redação: só a duração (sem mínimo de números)."""
    n = _palavras(script)
    return None if n >= min_palavras else f"roteiro final curto demais: {n} palavras (mínimo {min_palavras} para a duração)"
