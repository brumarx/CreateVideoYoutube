"""22/11 (E82): quantidades com unidade e equivalência entre unidades diferentes.

A fonte diz "more than 0.8 million kilometers (500,000 mi)"; a afirmação pode dizer "mais de 800 mil km" ou "mais
de 500 mil milhas". A conferência de números exige que cada número da afirmação esteja no trecho; quando não está
literalmente, ESTE módulo procura no trecho uma quantidade da mesma grandeza que, convertida, dá o mesmo valor:

- conversão: fator (e deslocamento, na temperatura) da tabela UNIDADES, por grandeza;
- arredondamento e precisão: o valor da fonte, convertido e arredondado na precisão da afirmação, tem de dar o
  valor dela (meia-resolução dos algarismos significativos: "0,8 milhão" = ±50 mil), mais o arredondamento da
  própria fonte até 1%; a afirmação não pode ser mais precisa que a fonte (no máximo um algarismo significativo
  a mais — "804.672 km" a partir de "500,000 mi" é precisão inventada);
- unidade original: guardada em cada conversão aceita (o valor da fonte nunca é substituído);
- sentido do valor: "mais de", "menos de", "cerca de" — "mais de" só casa com "mais de", "menos de" só com
  "menos de"; exato e aproximado casam entre si (a precisão já mede o arredondamento).

Grandezas diferentes (km × kg) nunca casam. Unidade fora da tabela: nada é aceito por conversão. Para acrescentar
uma unidade: uma linha em UNIDADES (grandeza, fator para a unidade de base, formas em pt/en/es)."""
from __future__ import annotations

import math
import re

# unidade: (grandeza, fator para a base, deslocamento, formas). valor_base = valor * fator + deslocamento
UNIDADES: dict[str, tuple[str, float, float, tuple[str, ...]]] = {
    # comprimento (base: metro)
    "km": ("comprimento", 1000.0, 0.0, ("km", "quilômetros?", "quilometros?", "kilometers?", "kilometres?", "kilómetros?", "kilometros?")),
    "m": ("comprimento", 1.0, 0.0, ("m", "metros?", "meters?", "metres?")),
    "cm": ("comprimento", 0.01, 0.0, ("cm", "centímetros?", "centimetros?", "centimeters?", "centimetres?")),
    "mm": ("comprimento", 0.001, 0.0, ("mm", "milímetros?", "milimetros?", "millimeters?", "millimetres?")),
    "nmi": ("comprimento", 1852.0, 0.0, ("milhas? náuticas?", "milhas? nauticas?", "nautical miles?", "millas? náuticas?", "nmi")),
    "mi": ("comprimento", 1609.344, 0.0, ("mi", "milhas?", "miles?", "millas?")),
    "ft": ("comprimento", 0.3048, 0.0, ("ft", "pés", "pé", "feet", "foot", "pies")),
    "in": ("comprimento", 0.0254, 0.0, ("polegadas?", "inch(?:es)?", "pulgadas?")),
    "yd": ("comprimento", 0.9144, 0.0, ("jardas?", "yards?", "yd")),
    # massa (base: quilograma)
    "t": ("massa", 1000.0, 0.0, ("toneladas?", "tonnes?", "metric tons?", "t")),
    "kg": ("massa", 1.0, 0.0, ("kg", "quilos?", "quilogramas?", "kilograms?", "kilogramos?")),
    "g": ("massa", 0.001, 0.0, ("g", "gramas?", "grams?", "gramos?")),
    "lb": ("massa", 0.45359237, 0.0, ("lbs?", "libras?", "pounds?")),
    "oz": ("massa", 0.028349523125, 0.0, ("oz", "onças?", "ounces?", "onzas?")),
    # temperatura (base: kelvin)
    # 26/11: "graus"/"degrees" sem escala é Celsius; "°" sozinho também (salvo coordenada "78° N"); "18C" colado
    "°C": ("temperatura", 1.0, 273.15, ("°\\s?C", "º\\s?C", "graus celsius", "degrees celsius", "grados celsius", "celsius",
                                         "graus?", "degrees?", "grados?", "°(?!\\s?[NSLOEWF]\\b)", "(?<=\\d)C")),
    "°F": ("temperatura", 5 / 9, 273.15 - 32 * 5 / 9, ("°\\s?F", "º\\s?F", "deg\\s?F", "graus fahrenheit", "degrees fahrenheit",
                                                       "fahrenheit", "(?<=\\d)F")),
    "K": ("temperatura", 1.0, 0.0, ("K", "kelvin")),
    # velocidade (base: m/s)
    "km/h": ("velocidade", 1000 / 3600, 0.0, ("km/h", "kph", "quilômetros por hora", "quilometros por hora", "kilometers per hour", "kilómetros por hora")),
    "mph": ("velocidade", 1609.344 / 3600, 0.0, ("mph", "milhas por hora", "miles per hour", "millas por hora")),
    "m/s": ("velocidade", 1.0, 0.0, ("m/s", "metros por segundo", "meters per second", "metres per second")),
    "kn": ("velocidade", 1852 / 3600, 0.0, ("nós", "knots?", "nudos?", "kn")),
    # área (base: m²)
    "km²": ("area", 1e6, 0.0, ("km²", "km2", "quilômetros quadrados", "quilometros quadrados", "square kilometers?", "square kilometres?", "kilómetros cuadrados")),
    "m²": ("area", 1.0, 0.0, ("m²", "m2", "metros quadrados", "square meters?", "square metres?", "metros cuadrados")),
    "mi²": ("area", 1609.344 ** 2, 0.0, ("mi²", "mi2", "milhas quadradas", "square miles?", "millas cuadradas")),
    "ha": ("area", 1e4, 0.0, ("ha", "hectares?", "hectáreas?")),
    "acre": ("area", 4046.8564224, 0.0, ("acres?", "acres")),
    # volume (base: m³)
    "m³": ("volume", 1.0, 0.0, ("m³", "m3", "metros cúbicos", "cubic meters?", "cubic metres?")),
    "L": ("volume", 0.001, 0.0, ("litros?", "liters?", "litres?", "L")),
    "gal": ("volume", 0.003785411784, 0.0, ("galões", "galão", "gallons?", "galones?", "gal")),
    # 26/11 (corpus de todos os vídeos: "24h", "%", "megawatts" não eram lidos)
    # tempo (base: segundo) — duração; ano/dia ficam fora (idade, data e prazo têm leitura própria)
    "h": ("tempo", 3600.0, 0.0, ("horas?", "hours?", "(?<=\\d)h")),
    "min": ("tempo", 60.0, 0.0, ("minutos?", "minutes?", "min")),
    "s": ("tempo", 1.0, 0.0, ("segundos?", "seconds?", "(?<=\\d)s")),
    # potência (base: watt) e energia (base: watt-hora)
    "W": ("potencia", 1.0, 0.0, ("watts?", "W")),
    "kW": ("potencia", 1e3, 0.0, ("quilowatts?", "kilowatts?", "kW")),
    "MW": ("potencia", 1e6, 0.0, ("megawatts?", "MW")),
    "GW": ("potencia", 1e9, 0.0, ("gigawatts?", "GW")),
    "kWh": ("energia", 1e3, 0.0, ("quilowatts?[- ]hora", "kilowatt[- ]hours?", "kWh")),
    "MWh": ("energia", 1e6, 0.0, ("megawatts?[- ]hora", "megawatt[- ]hours?", "MWh")),
    "GWh": ("energia", 1e9, 0.0, ("gigawatts?[- ]hora", "gigawatt[- ]hours?", "GWh")),
    # proporção (base: fração)
    "%": ("proporcao", 0.01, 0.0, ("%", "por cento", "percent", "por ciento")),
}

_ESCALAS = (("trilhões|trilhão|trillion|billones|billón", 1e12), ("bilhões|bilhão|billion|mil millones", 1e9),
            ("milhões|milhão|million|millones|millón", 1e6), ("mil|thousand", 1e3))
_COMPARADOR = ((">", r"mais de|acima de|superior a|more than|over|above|in excess of|más de|por encima de"),
               ("<", r"menos de|abaixo de|inferior a|less than|under|below|fewer than|menos de|por debajo de"),
               ("~", r"cerca de|aproximadamente|em torno de|por volta de|quase|uns|umas|about|around|approximately|"
                     r"roughly|nearly|almost|some|alrededor de|unos|unas|casi|~"))

_FORMAS = sorted(((forma, u) for u, (_, _, _, fs) in UNIDADES.items() for forma in fs), key=lambda x: -len(x[0]))
_RE_UNIDADE = "|".join(f"(?P<u{k}>{f})" for k, (f, _) in enumerate(_FORMAS))
_RE_ESCALA = "|".join(e for e, _ in _ESCALAS)
_RE_COMP = "|".join(c for _, c in _COMPARADOR)
# sinal: "−3", "menos 3" (não "menos de 3", que é comparação), "minus 3"; depois: "3 graus negativos"
_RE_SINAL = r"(?<![\w.,])[−–-]\s?|\bmenos\s+(?!de\b)|\bminus\s+"
_RE_NEGATIVO = r"\s+(?:negativos?|abaixo de zero|below zero|bajo cero|below freezing)\b"
_QUANTIDADE = re.compile(
    rf"(?:(?P<comp>{_RE_COMP})\s+)?(?P<sinal>{_RE_SINAL})?(?:(?:cerca de|aproximadamente|uns|umas|quase|por volta de|em torno de|about|around)\s+)?(?P<num>\d+(?:[.,]\d+|[\s  ]\d{3}(?!\d))*)\s*"
    rf"(?:(?P<escala>{_RE_ESCALA})(?![^\W\d_])\s*(?:de\s+)?)?(?:{_RE_UNIDADE})(?![^\W\d_²³/])(?P<neg>{_RE_NEGATIVO})?", re.I)
# 26/11 (Svalbard, "between minus 3 and 4°C"): numa faixa o 1º número vem sem unidade; a unidade (e, na
# temperatura, o sinal) do 2º valem para ele — e o sinal do 1º vale para o 2º quando o 2º não tem sinal
_FAIXA_ANTES = re.compile(rf"(?P<sinal>{_RE_SINAL})?(?<![\w.,])(?P<num>\d{{1,3}}(?:[.,]\d+)?)\s*(?:°\s*C?\s*)?"
                          r"(?:a|e|and|to|até|y|[−–-])\s*$", re.I)
_INGLES = re.compile(r"\b(the|and|of|was|were|is|are|with|from|than|it|its|weighs?|about|over|under)\b", re.I)
_PT_ES = re.compile(r"\b(de|do|da|dos|das|que|com|em|no|na|um|uma|el|los|las|del|con|una|é|foi|são|tem)\b", re.I)


def _ler_numero(bruto: str, ingles: bool) -> tuple[float, int] | None:
    """(valor, algarismos significativos). Inglês: vírgula de milhar e ponto decimal; senão o contrário. Espaço
    entre grupos de 3 algarismos é milhar nos dois."""
    n = re.sub(r"[\s  ](?=\d{3}\b)", "", bruto.strip())
    if ingles:
        n = n.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", n) else n.replace(",", ".")
    else:
        n = n.replace(".", "") if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", n) else n
        n = n.replace(",", ".")
    try:
        valor = float(n)
    except ValueError:
        return None
    if "." in n:
        sig = len(n.replace(".", "").lstrip("0")) or 1          # "0.8" -> 1; "6.50" -> 3
    else:
        sig = len(n.strip("0").lstrip("0")) or 1                # "500000" -> 1 (zeros finais não contam)
    return valor, sig


def quantidades(texto: str) -> list[dict]:
    """Cada valor com unidade da tabela: {valor, unidade, grandeza, base, comparador, sig, resolucao_base, texto,
    numero}. `numero` é o algarismo como escrito (com escala aplicada em `valor`)."""
    # vírgula de milhar ("500,000 mi") é do inglês; sem palavra de nenhum idioma ("4,400 pounds"), também
    ingles = bool(_INGLES.search(texto or "")) or not _PT_ES.search(texto or "")
    out = []
    for m in _QUANTIDADE.finditer(texto or ""):
        lido = _ler_numero(m.group("num"), ingles)
        if not lido:
            continue
        valor, sig = lido
        escala = 1.0
        if m.group("escala"):
            escala = next(f for e, f in _ESCALAS if re.fullmatch(e, m.group("escala"), re.I))
        valor *= escala
        unidade = next(_FORMAS[int(k[1:])][1] for k, v in m.groupdict().items() if k.startswith("u") and v)
        grandeza = UNIDADES[unidade][0]
        comp = "="
        if m.group("comp"):
            comp = next(c for c, r in _COMPARADOR if re.fullmatch(r, m.group("comp"), re.I))
        negativo = bool(m.group("sinal")) or bool(m.group("neg"))
        faixa = None if m.group("comp") or m.group("escala") else _FAIXA_ANTES.search(texto[:m.start()])
        if faixa:
            lido1 = _ler_numero(faixa.group("num"), ingles)
            if lido1:
                temperatura = grandeza == "temperatura"
                neg1 = temperatura and (bool(faixa.group("sinal")) or bool(m.group("neg")))
                negativo = negativo or (temperatura and bool(faixa.group("sinal")))
                out.append(_quantidade(-lido1[0] if neg1 else lido1[0], lido1[1], unidade, "=",
                                       faixa.group(0).strip(), faixa.group("num")))
        if negativo:
            valor = -valor
        out.append(_quantidade(valor, sig, unidade, comp, m.group(0).strip(), m.group("num")))
    return out


def _quantidade(valor: float, sig: int, unidade: str, comp: str, texto: str, numero: str) -> dict:
    grandeza, fator, desloc, _ = UNIDADES[unidade]
    expoente = math.floor(math.log10(abs(valor))) if valor else 0
    resolucao = 10 ** (expoente - sig + 1)                       # passo do último algarismo significativo
    return {"valor": valor, "unidade": unidade, "grandeza": grandeza, "base": valor * fator + desloc,
            "comparador": comp, "sig": sig, "resolucao_base": resolucao * fator, "texto": texto, "numero": numero}


def _comparadores_casam(a: str, f: str) -> bool:
    return a == f or {a, f} <= {"=", "~"}


DESVIO_MAXIMO = 0.05   # arredondar 457,6 °F para "500 °F" é 9%: não é a mesma medida dita de outro jeito


def equivalente(afirmacao: dict, fonte: dict) -> dict | None:
    """A quantidade da afirmação é a da fonte em OUTRA unidade? Na mesma unidade não há conversão: o número tem de
    estar no trecho (−300 °C não é "−272 °C arredondado")."""
    if afirmacao["unidade"] == fonte["unidade"] or afirmacao["grandeza"] != fonte["grandeza"]:
        return None
    if not _comparadores_casam(afirmacao["comparador"], fonte["comparador"]):
        return None
    if afirmacao["grandeza"] == "temperatura" and afirmacao["base"] < 0:
        return None                                              # abaixo do zero absoluto
    if afirmacao["sig"] > fonte["sig"] + 1:
        return None                                              # precisão que a fonte não tem
    _, fator_a, desloc_a, _ = UNIDADES[afirmacao["unidade"]]
    _, fator_f, _, _ = UNIDADES[fonte["unidade"]]
    convertido = (fonte["base"] - desloc_a) / fator_a           # o valor da fonte na unidade da afirmação
    diferenca = abs(afirmacao["valor"] - convertido)
    # o valor convertido, arredondado na precisão da afirmação, tem de dar o valor dela; o arredondamento da
    # própria fonte entra até 1% do valor dela ("−233 °C (−388 °F)": a fonte arredondou a conversão). Zero à
    # direita não prova imprecisão da fonte: "500,000 mi" não vira "qualquer coisa entre 450 e 550 mil"
    res_a = afirmacao["resolucao_base"] / fator_a
    margem_fonte = min(fonte["resolucao_base"] / 2, 0.01 * abs(fonte["valor"]) * fator_f) / fator_a
    tolerancia = res_a / 2 + margem_fonte
    relativo = diferenca / abs(convertido) if convertido else (0.0 if diferenca == 0 else 1.0)
    if diferenca > tolerancia * (1 + 1e-9) or relativo > DESVIO_MAXIMO:
        return None
    return {"afirmacao": afirmacao["texto"], "fonte": fonte["texto"], "unidade_original": fonte["unidade"],
            "unidade_afirmacao": afirmacao["unidade"], "grandeza": fonte["grandeza"],
            "valor_convertido": round(convertido, 4), "diferenca_relativa": round(relativo, 4)}


def por_conversao(afirmacao: str, trecho: str, faltando: set[str], numeros) -> tuple[set[str], list[dict]]:
    """Dos números da afirmação que faltam no trecho, os que são uma quantidade equivalente a uma do trecho.
    `numeros` é a função que lê os números de um texto (fatos.numeros), para devolver os mesmos rótulos."""
    cobertos: set[str] = set()
    conversoes: list[dict] = []
    da_fonte = quantidades(trecho)
    for q in quantidades(afirmacao):
        rotulos = numeros(q["texto"]) & faltando
        if not rotulos:
            continue
        casou = next((r for r in (equivalente(q, f) for f in da_fonte) if r), None)
        if casou:
            cobertos |= rotulos
            conversoes.append(casou)
    return cobertos, conversoes


def unidades_trocadas(afirmacao: str, trecho: str) -> list[dict]:
    """O número da afirmação está no trecho, mas em OUTRA unidade da mesma grandeza (e não na dela): "500 mil km"
    onde a fonte diz "500,000 miles"; "21,3 metros" onde ela diz "21.3 feet". A conferência literal de números
    não vê unidade — esta vê."""
    da_fonte = quantidades(trecho)
    out = []
    for q in quantidades(afirmacao):
        mesmas = [f for f in da_fonte if f["grandeza"] == q["grandeza"] and math.isclose(f["valor"], q["valor"], rel_tol=1e-9)]
        if mesmas and not any(f["unidade"] == q["unidade"] for f in mesmas):
            out.append({"afirmacao": q["texto"], "fonte": mesmas[0]["texto"], "unidade_afirmacao": q["unidade"],
                        "unidade_fonte": mesmas[0]["unidade"]})
    return out
