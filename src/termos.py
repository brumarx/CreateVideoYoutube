"""22/11 (E74): tradução de termos técnicos conferida em código depois do juiz.

A fonte dizia "the first (port) side of the mirror deployed on 7 January and the second (starboard) side ... on 8
January"; a evidência disse "O primeiro lado (boreste) ... em 7 de janeiro" — boreste é starboard. O juiz aprovou
porque a frase parecia certa e "starboard" também estava no trecho. Duas camadas, nenhuma depende de a IA acertar:

1. GLOSSÁRIO de conceitos que se excluem (lados de uma embarcação ou veículo, esquerda/direita, pontos cardeais,
   apsides de órbita, eletrodos, faixas do espectro, sentido de órbita, curvatura de lente, refração do olho...).
   Cada conceito tem as formas em pt/en/es. Conflito quando a afirmação usa um conceito que o trecho não usa e o
   trecho usa outro do mesmo grupo; ou, com os dois conceitos no trecho, quando a afirmação liga o conceito a
   uma ÂNCORA (número, data, ordinal) que o trecho liga ao outro ("first ... port ... 7 January" × "primeiro
   lado (boreste) ... 7 de janeiro"). Para outra área: um grupo novo em GRUPOS — nada aqui é específico de náutica.
2. JUIZ (src/evidencias.py): para cada termo técnico traduzido, devolve original (copiado do trecho), tradução
   (copiada da afirmação) e se o sentido técnico é o mesmo. "Não" com os dois termos conferidos literalmente
   bloqueia — vale para qualquer termo, inclusive os que o glossário não conhece.

Bloqueio: a citação deixa de ser verificada ("traducao_incorreta"), com o termo original, a tradução proposta e a
frase da fonte em que o termo aparece. A evidência não é reescrita (o código não escolhe a tradução certa)."""
from __future__ import annotations

import re

from .fatos import numeros
from .fontes import dobrar

# grupo: {conceito: [formas (regex sobre texto sem acento e minúsculo)]}. Forma com "?" no início é AMBÍGUA fora do
# grupo ("port" = porto; "right" = certo; "direito" = lei): só conta quando outro conceito do grupo aparece no
# mesmo texto ou quando a forma vem com o contexto escrito nela.
GRUPOS: dict[str, dict[str, list[str]]] = {
    "lado de embarcação ou veículo": {
        "bombordo": [r"bombordo", r"babor", r"port[- ]?side", r"\(port\)", r"to port\b",
                      r"port[- ](?:wing|bow|quarter|engine|panel|boom|array|hull|beam|door|hatch|thruster|pallet)s?", r"?port"],
        "estibordo": [r"estibordo", r"boreste", r"estribor", r"starboard"],
    },
    "esquerda/direita": {
        "esquerda": [r"esquerd[oa]s?", r"izquierd[oa]s?", r"left[- ]hand", r"?left"],
        "direita": [r"?direit[oa]s?", r"derech[oa]s?", r"right[- ]hand", r"?right"],
    },
    "ponto cardeal": {
        "norte": [r"norte", r"north(?:ern|erly|wards?|bound)?", r"setentrional"],
        "sul": [r"sul", r"sur", r"south(?:ern|erly|wards?|bound)?", r"meridional"],
        "leste": [r"leste", r"oriente", r"east(?:ern|erly|wards?|bound)?", r"oriental"],
        "oeste": [r"oeste", r"west(?:ern|erly|wards?|bound)?", r"ocidental", r"occidental", r"poente"],
    },
    "ponto da órbita terrestre": {"apogeu": [r"apogeus?", r"apogeos?", r"apogees?"], "perigeu": [r"perigeus?", r"perigeos?", r"perigees?"]},
    "ponto da órbita solar": {"afélio": [r"afelio", r"aphelion"], "periélio": [r"perielio", r"perihelio", r"perihelion"]},
    "sentido de órbita ou rotação": {"prógrado": [r"prograd[oa]s?", r"prograde"], "retrógrado": [r"retrograd[oa]s?", r"retrograde"]},
    "eletrodo": {"ânodo": [r"anodos?", r"anodes?"], "cátodo": [r"catodos?", r"cathodes?"]},
    "faixa do espectro": {"infravermelho": [r"infravermelh[oa]s?", r"infrarroj[oa]s?", r"infrared"],
                          "ultravioleta": [r"ultravioletas?", r"ultraviolet"]},
    "curvatura": {"côncavo": [r"concav[oa]s?", r"concave"], "convexo": [r"convex[oa]s?", r"convex"]},
    "refração do olho": {"miopia": [r"miopia", r"myopia", r"nearsighted(?:ness)?", r"miope"],
                         "hipermetropia": [r"hipermetropia", r"hyperopia", r"farsighted(?:ness)?", r"hipermetrope"]},
    "vaso sanguíneo": {"artéria": [r"arterias?", r"arter(?:y|ies)"], "veia": [r"veias?", r"venas?", r"veins?"]},
}

_ORDINAIS = {"primeir": 1, "first": 1, "primer": 1, "segund": 2, "second": 2, "terceir": 3, "third": 3, "tercer": 3,
             "quart": 4, "fourth": 4, "cuart": 4, "quint": 5, "fifth": 5}
_ORDINAL = re.compile(r"\b(primeir[oa]s?|primer[oa]?|first|segund[oa]s?|second|terceir[oa]s?|tercer[oa]?|third|"
                      r"quart[oa]s?|cuart[oa]s?|fourth|quint[oa]s?|fifth)\b")
# parênteses NÃO separam: "the first (port) side ... on 7 January" é uma oração só
_ORACAO = re.compile(r"[;,:]|\s(?:and|e|y|while|whereas|enquanto|mientras)\s|(?<=[.!?])\s")


def _compilar():
    out = {}
    for grupo, conceitos in GRUPOS.items():
        for conceito, formas in conceitos.items():
            for f in formas:
                ambigua = f.startswith("?")
                out.setdefault(grupo, []).append((conceito, re.compile(rf"(?<![a-z]){f.lstrip('?')}(?![a-z])"), ambigua))
    return out


_PADROES = _compilar()


def _conceitos(texto: str) -> dict[str, list[tuple[str, int, int, str]]]:
    """{grupo: [(conceito, início, fim, forma achada)]} no texto já sem acento."""
    achados: dict[str, list[tuple[str, int, int, str]]] = {}
    for grupo, padroes in _PADROES.items():
        firmes = [(c, m.start(), m.end(), m.group()) for c, p, amb in padroes if not amb for m in p.finditer(texto)]
        ambiguos = [(c, m.start(), m.end(), m.group()) for c, p, amb in padroes if amb for m in p.finditer(texto)]
        ambiguos = [a for a in ambiguos if not any(f[1] <= a[1] < f[2] for f in firmes)]
        presentes = {c for c, *_ in firmes} | {c for c, *_ in ambiguos}
        # forma ambígua só conta se OUTRO conceito do grupo aparece no texto ("port ... starboard", "right ... left")
        validos = firmes + [a for a in ambiguos if presentes - {a[0]}]
        if validos:
            achados[grupo] = sorted(set(validos), key=lambda x: x[1])
    return achados


def _oracoes(texto: str) -> list[tuple[int, int]]:
    cortes = [0] + [m.end() for m in _ORACAO.finditer(texto)] + [len(texto)]
    return [(a, b) for a, b in zip(cortes, cortes[1:]) if b > a]


def _ancoras(pedaco: str) -> set[str]:
    out = {f"n:{n}" for n in numeros(pedaco)}
    out |= {f"o:{v}" for m in _ORDINAL.finditer(pedaco) for k, v in _ORDINAIS.items() if m.group().startswith(k)}
    return out


def _ligacoes(texto: str, achados: dict) -> list[tuple[str, str, set[str], str]]:
    """(grupo, conceito, âncoras da oração, oração) para cada conceito achado."""
    out = []
    for a, b in _oracoes(texto):
        pedaco = texto[a:b]
        anc = _ancoras(pedaco)
        for grupo, lista in achados.items():
            for conceito, ini, _, _ in lista:
                if a <= ini < b:
                    out.append((grupo, conceito, anc, pedaco.strip()))
    return out


def _frase_com(texto_original: str, forma: str) -> str:
    for frase in re.split(r"(?<=[.!?])\s+", texto_original):
        if forma in dobrar(frase):
            return frase.strip()
    return texto_original.strip()[:300]


def conflitos(afirmacao: str, trecho: str) -> list[dict]:
    """Termos do glossário traduzidos para o conceito errado. [] quando não há conflito (ou não há termo do glossário)."""
    a, t = dobrar(afirmacao), dobrar(trecho)
    ca, ct = _conceitos(a), _conceitos(t)
    out = []
    for grupo, lista in ca.items():
        if grupo not in ct:
            continue
        da_fonte = {c for c, *_ in ct[grupo]}
        forma_fonte = {c: f for c, _, _, f in ct[grupo]}
        for conceito, _, _, forma in lista:
            if conceito not in da_fonte:
                outro = sorted(da_fonte)[0]
                out.append({"grupo": grupo, "original": forma_fonte[outro], "traducao": forma, "conceito_fonte": outro,
                            "conceito_afirmacao": conceito, "contexto": _frase_com(trecho, forma_fonte[outro]),
                            "motivo": f"a fonte diz \"{forma_fonte[outro]}\" ({outro}); a afirmação traduz como "
                                      f"\"{forma}\" ({conceito})"})
    # os dois conceitos estão no trecho: a afirmação liga o conceito à mesma âncora que o trecho?
    lig_a, lig_t = _ligacoes(a, ca), _ligacoes(t, ct)
    for grupo, conceito, anc_a, _ in lig_a:
        if any(x["grupo"] == grupo for x in out):
            continue
        for anc in sorted(anc_a):
            da_ancora = {c for g, c, an, _ in lig_t if g == grupo and anc in an}
            if da_ancora and conceito not in da_ancora:
                outro = sorted(da_ancora)[0]
                oracao_t = next(o for g, c, an, o in lig_t if g == grupo and c == outro and anc in an)
                forma = next(f for c, _, _, f in ca[grupo] if c == conceito)
                forma_t = next(f for c, _, _, f in ct[grupo] if c == outro)
                out.append({"grupo": grupo, "original": forma_t, "traducao": forma, "conceito_fonte": outro,
                            "conceito_afirmacao": conceito, "contexto": _frase_com(trecho, forma_t),
                            "motivo": f"no trecho, \"{oracao_t}\" liga {_ancora_legivel(anc)} a \"{forma_t}\" ({outro}); "
                                      f"a afirmação liga a \"{forma}\" ({conceito})"})
                break
    return out


def _ancora_legivel(anc: str) -> str:
    tipo, valor = anc.split(":", 1)
    return f"o {valor}º" if tipo == "o" else f"o número {valor}"


def _registro(c: dict, termo: dict, por: str) -> None:
    c.setdefault("termos", []).append({**termo, "por": por})


def travar_termos(evidencias: list[dict], log=None) -> dict[str, int]:
    """Depois do juiz (e das decisões do cache): bloqueia a citação com termo técnico traduzido para outro sentido
    — pelo glossário (código) ou pela conferência de termos do juiz (literal nos dois lados). Conta os bloqueios."""
    cont = {"bloqueadas_codigo": 0, "bloqueadas_ia": 0}
    for e in evidencias:
        for c in e.get("citacoes") or []:
            c["termos"] = []  # refeito a cada conferência (idempotente)
            achados = conflitos(e["afirmacao"], c.get("trecho", ""))
            termos_ia = [t for t in ((c.get("sustentacao") or {}).get("termos") or []) if isinstance(t, dict)]
            for t in termos_ia:
                _registro(c, {k: t.get(k) for k in ("original", "traducao", "contexto", "mesmo_sentido", "justificativa")}, "ia")
            ruins_ia = [t for t in termos_ia if t.get("mesmo_sentido") is False]
            for t in achados:
                _registro(c, {**t, "mesmo_sentido": False}, "codigo")
            if not (achados or ruins_ia) or not c.get("verificado"):
                continue
            por = "codigo" if achados else "ia"
            motivo = achados[0]["motivo"] if achados else (
                f"tradução técnica muda o sentido: \"{ruins_ia[0].get('original')}\" virou \"{ruins_ia[0].get('traducao')}\""
                f" — {ruins_ia[0].get('justificativa', '')}".rstrip(" —"))
            c["verificado"] = False
            c["sustentacao"] = {"decisao": "traducao_incorreta", "por": por, "justificativa": motivo,
                                "decisao_anterior": c.get("sustentacao")}
            cont[f"bloqueadas_{por}"] += 1
            if log:
                log.warning("evidência %r: %s", e["afirmacao"][:70], motivo)
    return cont
