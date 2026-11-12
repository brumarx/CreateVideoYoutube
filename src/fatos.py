"""Fatos com ORIGEM: cada fato verificado tem ID, as fontes que o sustentam e um status
(oficial, confirmado, secundário). O escritor marca em cada frase os IDs que usou e o código
confere número por número; a narração final é ajustada pelo status do fato, não por listas
feitas à mão para um tema (11/11: Túnel Seikan, 20 rodadas de remendo).

Status:
- oficial: alguma fonte que sustenta o fato é oficial (domínio de governo, instituição
  pública ou de ensino, ou marcada como oficial no arquivo de fontes do job);
- confirmado: sustentado por 2 ou mais fontes de domínios diferentes;
- secundario: uma fonte só, não oficial. Na narração vira "estima-se".
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("fatos")

_DOMINIO_OFICIAL = re.compile(
    r"(^|\.)((gov|gob|gouv|govt|gv|mil|edu)(\.[a-z]{2})?|(go|ac)\.[a-z]{2}|int|europa\.eu|un\.org)$")
_ID = re.compile(r"\s*\[\s*(F\d+(?:\s*[,;]\s*F\d+)*)\s*\]")
_FRASES = re.compile(r"(?<=[.!?])\s+")
LIMITE_BLOCO = 16000
LIMITE_POR_FONTE = 3000  # uma Wikipédia de 5 mil caracteres não empurra as outras fontes para fora do bloco
STATUS_ORDEM = {"oficial": 0, "confirmado": 1, "secundario": 2}
# valor de planejamento/orçamento escrito como custo final (linguagem do português, não do tema)
_PLANEJAMENTO = re.compile(r"planejament|orçament|orcament|previst|estimad|projetad", re.I)



def dominio(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def dominio_raiz(url: str) -> str:
    """Domínio de quem publica: pt.wikipedia.org e en.wikipedia.org são a mesma fonte
    (wikipedia.org); www.mlit.go.jp vira mlit.go.jp; g1.globo.com vira globo.com."""
    partes = dominio(url).split(".")
    if len(partes) >= 3 and len(partes[-1]) == 2 and partes[-2] in {"co", "com", "org", "net", "gov", "go", "ac", "edu", "gob", "gouv"}:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:])


def classificar_fonte(item: dict) -> str:
    """'oficial' ou 'secundaria'. A marca explícita do arquivo de fontes vale primeiro."""
    if "oficial" in item:
        return "oficial" if item["oficial"] else "secundaria"
    return "oficial" if _DOMINIO_OFICIAL.search(dominio(item.get("url", ""))) else "secundaria"


def canon(numero: str) -> str:
    """'23,30', '23,3' e '23.3' viram o mesmo valor; '6.900' (milhar) vira 6900."""
    n = numero.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", n):
        n = n.replace(".", "")
    n = n.replace(".", "").replace(",", ".") if "," in n else n
    try:
        return format(float(n), "g")
    except ValueError:
        return numero


_ESCALAS = {"trilhão": 1e12, "trilhões": 1e12, "trillion": 1e12, "bilhão": 1e9, "bilhões": 1e9, "billion": 1e9,
            "milhão": 1e6, "milhões": 1e6, "million": 1e6, "mil": 1e3, "thousand": 1e3,
            "兆": 1e12, "億": 1e8, "万": 1e4}
_ESCALA = re.compile(r"(\d+(?:[.,]\d+)*)\s*(trilhões|trilhão|trillion|bilhões|bilhão|billion|milhões|milhão|million|"
                     r"thousand|mil|兆|億|万)", re.I)


def numeros(texto: str) -> set[str]:
    """Números do texto, iguais em qualquer formato e idioma: além do número escrito, o valor
    absoluto quando há escala ("538,4 bilhões" e "5.384億" valem os dois 538400000000)."""
    texto = milhar_com_espaco(texto or "")
    out = {canon(m) for m in re.findall(r"\d+(?:[.,]\d+)*", texto)}
    for m in _ESCALA.finditer(texto):
        bruto = m.group(1)
        leituras = {canon(bruto)}
        if re.fullmatch(r"\d{1,3}(,\d{3})+", bruto):
            leituras.add(bruto.replace(",", ""))  # vírgula de milhar (en, ja): "5,384億"
        escala = _ESCALAS.get(m.group(2).lower() if m.group(2).isascii() else m.group(2))
        for leitura in leituras:
            try:
                out.add(format(round(float(leitura) * escala), "d"))
            except (TypeError, ValueError):
                continue
    return out


def bloco_numerado(web_facts: list[dict] | None) -> str:
    """Fontes numeradas [S1], [S2]... para a extração citar de onde tirou cada fato."""
    linhas = [f"[S{i + 1}] {f.get('titulo', '')} ({dominio(f.get('url', ''))}): {str(f.get('trecho', ''))[:LIMITE_POR_FONTE]}"
              for i, f in enumerate(web_facts or [])]
    return "\n".join(linhas)[:LIMITE_BLOCO]


_MILHAR_ESPACO = re.compile(r"(?<![\d.,])(\d{1,3})((?:[ \u00a0\u202f]\d{3})+)(?![\d.,]|\s*\d)")


def milhar_com_espaco(texto: str) -> str:
    """"101 000 m³" (milhar separado por espaço, padrão francês/ISO) vira "101.000 m³": senão vira
    dois números, "101" e "000" (job 887)."""
    return _MILHAR_ESPACO.sub(lambda m: m.group(1) + re.sub(r"[ \u00a0\u202f]", ".", m.group(2)), texto or "")


def atribuir(fatos_brutos: list[dict], web_facts: list[dict], outliers: list[str] | None = None) -> list[dict]:
    """Fatos da extração → fatos com ID, fontes que de fato contêm seus números e status.
    Fato cujo número não está em fonte nenhuma é descartado."""
    fontes = list(web_facts or [])
    nums_fonte = [numeros(f.get("trecho", "") + " " + f.get("titulo", "")) for f in fontes]
    # número que alguma fonte apresenta como planejamento/orçamento/previsão (ex.: "base de
    # planejamento: 538,4 bilhões") — vale mesmo se o fato veio de outra fonte que diz "custo total"
    planejamento = set()
    for f in fontes:
        for frase in _FRASES.split(f.get("trecho", "")):
            if _PLANEJAMENTO.search(frase):
                planejamento |= numeros(frase)
    saida: list[dict] = []
    for bruto in fatos_brutos:
        texto = milhar_com_espaco(str(bruto.get("texto", "")).strip())
        if not texto:
            continue
        citadas = []
        for ref in bruto.get("fontes") or []:
            m = re.search(r"\d+", str(ref))
            if m and 1 <= int(m.group()) <= len(fontes):
                citadas.append(int(m.group()) - 1)
        nums = numeros(texto)
        if nums:
            # sustentam o fato as fontes que têm TODOS os números dele (a citação do modelo não basta)
            apoio = [i for i in range(len(fontes)) if nums <= nums_fonte[i]]
            if not apoio:
                log.info("fato descartado (número fora das fontes): %r", texto[:80])
                continue
        else:
            apoio = citadas
        tipos = {classificar_fonte(fontes[i]) for i in apoio}
        dominios = {dominio_raiz(fontes[i].get("url", "")) for i in apoio}
        status = "oficial" if "oficial" in tipos else ("confirmado" if len(dominios) >= 2 else "secundario")
        saida.append({"id": f"F{len(saida) + 1}", "texto": texto, "status": status,
                      "fontes": [fontes[i].get("url", "") for i in apoio],
                      "planejamento": sorted(nums & planejamento)})
    return remover_conflitos(saida, outliers)


# unidades de medida (vocabulário do português, não de um tema)
_UNIDADES = {
    "km": "km", "quilômetro": "km", "quilômetros": "km", "quilometros": "km", "metro": "m", "metros": "m", "m": "m",
    "tonelada": "t", "toneladas": "t", "ano": "ano", "anos": "ano", "dia": "dia", "dias": "dia", "hora": "h", "horas": "h",
    "iene": "iene", "ienes": "iene", "dólar": "usd", "dólares": "usd", "dolares": "usd", "real": "brl", "reais": "brl",
    "euro": "eur", "euros": "eur", "%": "%", "graus": "grau", "pessoas": "pessoa", "operários": "pessoa",
    "trabalhadores": "pessoa", "mortos": "pessoa", "mortes": "pessoa", "vítimas": "pessoa",
}
_NUM_UNIDADE = re.compile(r"(\d+(?:[.,]\d+)*)\s*(?:(?:mil|milhões|milhão|bilhões|bilhão)\s*(?:de\s*)?)?([%a-zà-ú]+)?", re.I)
_PARADAS = {"para", "pelo", "pela", "como", "mais", "este", "esta", "isso", "seus", "suas", "sobre", "entre", "cerca",
            "teve", "tinha", "foram", "durante", "total", "aproximadamente"}


_PES = {"pé", "pés", "pes", "ft", "feet", "foot"}


def _pares(texto: str) -> set[tuple[str, str]]:
    out = set()
    for m in _NUM_UNIDADE.finditer(texto):
        bruta = (m.group(2) or "").lower()
        if bruta in _PES:
            # pés viram metros para comparar: "85 pés" e "26 metros" são a mesma medida (job 872)
            try:
                out.add((str(round(float(canon(m.group(1))) * 0.3048, 1)), "m"))
            except ValueError:
                pass
            continue
        unidade = _UNIDADES.get(bruta)
        if unidade:
            out.add((canon(m.group(1)), unidade))
    return out


def mede_o_que_ja_foi_dito(fato: str, narracao: str) -> bool:
    """O fato traz uma medida (valor e unidade, com arredondamento) que a narração já diz."""
    ditos = _pares(narracao)
    return any(any(u == u2 and _algum_igual({n}, {n2}) for n2, u2 in ditos) for n, u in _pares(fato))


def _palavras(texto: str) -> set[str]:
    import unicodedata
    fold = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return {w[:5] for w in re.findall(r"[a-z]{4,}", fold)} - {w[:5] for w in _PARADAS}


def _mesma_coisa(a: str, b: str, tema: set[str]) -> bool:
    """Os dois fatos medem a MESMA coisa: quase todas as palavras de conteúdo do mais curto (fora o
    vocabulário do tema) estão no outro. Uma palavra em comum não basta: "seção submarina... 23,3 km
    de extensão" e "53,85 km de extensão total" dividem "extensão", mas são medidas diferentes."""
    pa, pb = _palavras(a) - tema, _palavras(b) - tema
    menor, maior = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    return bool(menor) and len(menor & maior) / len(menor) >= 0.7


def _forca(fato: dict) -> tuple[int, int]:
    """Quem vence um conflito: status melhor; depois, mais publicadores independentes."""
    return (-STATUS_ORDEM[fato["status"]], len({dominio_raiz(u) for u in fato.get("fontes", [])}))


def _algum_igual(xs: set[str], ys: set[str]) -> bool:
    """Algum valor igual nos dois lados, aceitando arredondamento de até 2% ("25,9 m" e "26 metros"
    são a mesma medida, job 866; "53,85 km" e "65,23 km" não)."""
    def num(x: str) -> float | None:
        try:
            return float(x)
        except ValueError:
            return None
    for x in xs:
        for y in ys:
            if x == y:
                return True
            a, b = num(x), num(y)
            if a is not None and b is not None and max(abs(a), abs(b)) > 0 and abs(a - b) / max(abs(a), abs(b)) <= 0.02:
                return True
    return False


def sem_medida_repetida(fatos: list[dict]) -> list[dict]:
    """Para o escritor: um fato por medida. Fatos com o mesmo valor (arredondamento de até 2%) na
    mesma unidade dizem a mesma coisa ("26 metros", "25,9 metros", "26 metros em três etapas", job
    867: três cenas repetindo o desnível); fica o mais forte, na ordem original."""
    ordem = sorted(fatos, key=_forca, reverse=True)
    vistos: list[set[tuple[str, str]]] = []
    ficam: set[str] = set()
    for f in ordem:
        pares = {(n, u) for n, u in _pares(f["texto"]) if u}
        repetido = pares and any(
            all(any(u == u2 and _algum_igual({n}, {n2}) for n2, u2 in v) for n, u in pares) for v in vistos)
        if not repetido:
            ficam.add(f["id"])
            vistos.append(pares)
    return [f for f in fatos if f["id"] in ficam]


def remover_conflitos(fatos: list[dict], outliers: list[str] | None = None) -> list[dict]:
    """Dois fatos que dão valores DIFERENTES para a mesma medida (mesma unidade e mesmo assunto):
    fica o mais forte (status melhor; empate no status, mais publicadores independentes). Ex.:
    "65,23 km de extensão" (só Wikipédia pt) sai quando há "53,85 km de extensão" oficial ou em
    mais fontes. As fontes que só sustentavam o valor perdedor são fora da curva: vão para
    `outliers` (URLs), para saírem da lista do job. Empate total: os dois ficam."""
    if not fatos:
        return fatos
    contagem: dict[str, int] = {}
    for f in fatos:
        for w in _palavras(f["texto"]):
            contagem[w] = contagem.get(w, 0) + 1
    tema = {w for w, n in contagem.items() if len(fatos) >= 4 and n * 2 >= len(fatos)}
    fora: set[str] = set()
    for a in fatos:
        for b in fatos:
            if a is b or _forca(a) <= _forca(b):
                continue
            pa, pb = _pares(a["texto"]), _pares(b["texto"])
            mesma_medida = {u for _, u in pa} & {u for _, u in pb}
            if not mesma_medida or not _mesma_coisa(a["texto"], b["texto"], tema):
                continue
            if not _algum_igual({n for n, u in pb if u in mesma_medida}, {n for n, u in pa if u in mesma_medida}):
                fora.add(b["id"])
                log.warning("fato %s descartado: contradiz %s, mais bem sustentado (%r × %r)", b["id"], a["id"],
                            b["texto"][:60], a["texto"][:60])
                if outliers is not None:
                    for url in b.get("fontes", []):
                        if url not in a.get("fontes", []) and url not in outliers:
                            outliers.append(url)
                            log.warning("fonte fora da curva, sai do job: %s", url)
    if not fora:
        return fatos
    restantes = [f for f in fatos if f["id"] not in fora]
    return [{**f, "id": f"F{i + 1}"} for i, f in enumerate(restantes)]


def lista_para_escritor(fatos: list[dict]) -> str:
    return "\n".join(f"{f['id']}: {f['texto']}" for f in fatos)


_FRONTEIRAS = (": ", "; ", ", e ", ", mas ", ", enquanto ")
# aproximação já escrita antes do número: nada a acrescentar
_JA_APROXIMADO = re.compile(r"(cerca de|aproximadamente|em torno de|por volta de|quase|mais de|menos de|estima|calcula|aponta)\s*$",
                            re.I)
_MESES = r"janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
# variar para não repetir: quantidade ganha a aproximação antes do número; data e outros casos, a oração
_APROXIMACOES = ("cerca de", "aproximadamente", "em torno de")
_ORACOES = ("estima-se que", "calcula-se que", "aponta-se que")


def _eh_data(frase: str, inicio: int, numero: str) -> bool:
    antes = frase[max(0, inicio - 14):inicio].lower()
    depois = frase[inicio + len(numero):inicio + len(numero) + 16].lower()
    valor = numero.replace(".", "").replace(",", "")
    return (bool(re.search(rf"({_MESES})\s+de\s*$", antes)) or bool(re.match(rf"\s*de\s+({_MESES})\b", depois))
            or (valor.isdigit() and len(valor) == 4 and 1500 <= int(valor) <= 2100))


def estima_antes(frase: str, nums_alvo: set[str], vez: int = 0) -> str:
    """Marca o número de fonte secundária como aproximado, variando a palavra (`vez` alterna):
    quantidade → "cerca de 50 trens", "aproximadamente 34 operários"; data ou outro caso →
    "calcula-se que o museu fechou em 2006", no começo da oração do número."""
    if re.search(r"estima|calcula-se|aponta-se", frase, re.I):
        return frase
    alvo = next((m for m in re.finditer(r"\d+(?:[.,]\d+)*", frase) if canon(m.group()) in nums_alvo), None)
    if alvo is not None and not _eh_data(frase, alvo.start(), alvo.group()):
        if _JA_APROXIMADO.search(frase[:alvo.start()]):
            return frase
        palavra = _APROXIMACOES[vez % len(_APROXIMACOES)]
        # "exatamente cerca de 85 pés" (job 865): número aproximado não é exato
        antes = re.sub(r"\b(exatamente|precisamente|justamente|exatos|exatas)\s+$", "", frase[:alvo.start()], flags=re.I)
        depois = re.sub(r"^(\d+(?:[.,]\d+)*\s*\w*)\s+exat[oa]s\b", r"\1", frase[alvo.start():], flags=re.I)
        frase = antes + depois
        alvo = re.compile(r"\d+(?:[.,]\d+)*").search(frase, len(antes))
        if re.search(r"\b(de|em|a|para|por|com|sobre)\s+$", frase[:alvo.start()], re.I) and palavra.endswith(" de"):
            palavra = "aproximadamente"  # "elevação de em torno de 26 metros" (job 889)
        if alvo.start() == 0:
            palavra = palavra[0].upper() + palavra[1:]
        return frase[:alvo.start()] + palavra + " " + frase[alvo.start():]
    oracao = _ORACOES[vez % len(_ORACOES)]
    posicao = alvo.start() if alvo is not None else 0
    corte = max((frase.rfind(f, 0, posicao) + len(f) for f in _FRONTEIRAS if frase.rfind(f, 0, posicao) >= 0),
                default=0) if posicao else 0
    if corte == 0:
        m = re.match(r"(E|Mas|Então|Porém)\s+", frase)
        if m:
            resto = frase[m.end():]
            return frase[:m.end()] + oracao + " " + resto[0].lower() + resto[1:]
        return oracao[0].upper() + oracao[1:] + " " + frase[0].lower() + frase[1:]
    resto = frase[corte:]
    return frase[:corte] + oracao + " " + resto[0].lower() + resto[1:]


def _melhor_por_numero(n: str, fatos: list[dict]) -> dict | None:
    candidatos = [f for f in fatos if n in numeros(f["texto"])]
    return min(candidatos, key=lambda f: STATUS_ORDEM[f["status"]]) if candidatos else None


_LIVRE = re.compile(r"coment[áa]rio|inscrev|compartilh|curt[ai]|deixe (sua|seu|um)|me conta|mente curiosa", re.I)


_PERGUNTA_LIVRE = {"você", "voce", "vocês", "teria", "coragem", "acha", "pensa", "imagina", "imaginar", "confiaria",
                   "atravessaria", "toparia", "faria", "conhece", "conhecia", "sabia", "deixe", "comentários", "comente",
                   "isso", "disso", "nisso", "nessa", "nesse", "nessas", "nesses", "essa", "esse", "essas", "esses", "sabendo", "mesmo", "coisa", "possível"}


def _premissa_nova(frase: str, resto: str) -> bool:
    """Pergunta que traz três ou mais palavras de conteúdo que o resto da narração não diz: carrega uma
    premissa ("Você confiaria num elevador movido só por chuva e gravidade?", job 863) e vai para a
    checagem por citação; "Você teria coragem de atravessar esse túnel?" não."""
    base = {w[:5] for w in re.findall(r"\w+", resto.lower()) if len(w) >= 5}
    novas = [w for w in re.findall(r"\w+", frase.lower())
             if len(w) >= 5 and w not in _PERGUNTA_LIVRE and w[:5] not in base]
    return len(novas) >= 3


def _alem_dos_fatos(frase: str, citados: list[dict]) -> bool:
    """Frase com três ou mais palavras de conteúdo que nenhum fato citado tem."""
    base = {w[:5] for f in citados for w in re.findall(r"\w+", f["texto"].lower()) if len(w) >= 5}
    return len([w for w in re.findall(r"\w+", frase.lower()) if len(w) >= 5 and w[:5] not in base]) >= 3


_EXTENSO = re.compile(
    r"\b(vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzent[oa]s|trezent[oa]s|"
    r"quatrocent[oa]s|quinhent[oa]s|seiscent[oa]s|setecent[oa]s|oitocent[oa]s|novecent[oa]s|mil|milhão|milhões|"
    r"bilhão|bilhões)\b", re.I)


def numero_por_extenso(texto: str) -> list[str]:
    """Números grandes escritos por extenso ("vinte e seis metros", "um milhão e oitocentos mil"):
    escapam da conferência por número (job 873: 367 palavras e nenhum algarismo). "Mil" depois de
    algarismo ("1,8 mil") é escala, não extenso."""
    achados = []
    for m in _EXTENSO.finditer(texto or ""):
        if m.group(1).lower() in ("mil", "milhão", "milhões", "bilhão", "bilhões") and re.search(r"\d\s*$", texto[:m.start()]):
            continue
        achados.append(m.group(0))
    return achados


_UNID = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
         "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14,
         "quatorze": 14, "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19,
         "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70,
         "oitenta": 80, "noventa": 90, "cem": 100, "cento": 100, "duzentos": 200, "duzentas": 200,
         "trezentos": 300, "trezentas": 300, "quatrocentos": 400, "quatrocentas": 400, "quinhentos": 500,
         "quinhentas": 500, "seiscentos": 600, "seiscentas": 600, "setecentos": 700, "setecentas": 700,
         "oitocentos": 800, "oitocentas": 800, "novecentos": 900, "novecentas": 900}
_ESCALA_EXT = {"mil": 1000, "milhão": 10**6, "milhões": 10**6, "bilhão": 10**9, "bilhões": 10**9}
_ALT_NUM = "|".join(sorted(set(_UNID) | set(_ESCALA_EXT), key=len, reverse=True))
_SEQ_NUM = re.compile(r"\b(?:" + _ALT_NUM + r")(?:\s+(?:e\s+)?(?:" + _ALT_NUM + r"))*\b", re.I)


def _valor_extenso(palavras: list[str]) -> int:
    total = corrente = 0
    for w in palavras:
        if w in _ESCALA_EXT:
            total += (corrente or 1) * _ESCALA_EXT[w]
            corrente = 0
        else:
            corrente += _UNID[w]
    return total + corrente


def _escreve(v: int) -> str:
    for escala, nome_1, nome_n in ((10**9, "bilhão", "bilhões"), (10**6, "milhão", "milhões")):
        if v >= escala and v % (escala // 10) == 0:
            q = v / escala
            txt = f"{q:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{txt} {nome_1 if q < 2 else nome_n}"
    if v >= 10000 and v % 1000 == 0:
        return f"{v // 1000} mil"
    return str(v)


def extenso_para_algarismos(texto: str) -> str:
    """Número por extenso vira algarismo ("vinte e seis metros" -> "26 metros", "cem mil toneladas"
    -> "100 mil toneladas"), para a conferência por número checar (job 877: o escritor insistiu no
    extenso nas 3 tentativas e todas as frases saíram). Não afrouxa nada: o algarismo passa pela
    mesma conferência. Números pequenos (até 9) e "um/uma" artigo ficam como estão."""
    def troca(m: re.Match) -> str:
        palavras = [w.lower() for w in re.findall(r"\w+", m.group(0)) if w.lower() != "e"]
        v = _valor_extenso(palavras)
        if v < 10 and not any(w in _ESCALA_EXT for w in palavras):
            return m.group(0)
        if re.search(r"\d\s*$", texto[:m.start()]) and palavras[0] in _ESCALA_EXT:
            return m.group(0)  # "1,8 milhão": escala depois de algarismo
        novo = _escreve(v)
        if novo.endswith(("ão", "ões")) and not re.match(r"\s+de\b", texto[m.end():]):
            novo += " de"  # "um milhão e oitocentos mil metros" -> "1,8 milhão de metros"
        return novo
    return milhar_com_espaco(_SEQ_NUM.sub(troca, texto or ""))


def fato_repetido(script: dict) -> str | None:
    """O mesmo fato ([F#]) em duas frases com o mesmo número: dado repetido (job 874: "1,8 milhão de
    metros cúbicos" nas cenas 3 e 9)."""
    vistos: dict[tuple[str, str], int] = {}
    for sc in script.get("scenes", []):
        for frase in _FRASES.split(sc.get("narration", "")):
            ids = {i.strip() for m in _ID.finditer(frase) for i in re.split(r"[,;]", m.group(1))}
            for n in numeros(_ID.sub("", frase)):
                for i in ids:
                    vistos[(i, n)] = vistos.get((i, n), 0) + 1
    repetidos = [f"{i} ({n})" for (i, n), q in vistos.items() if q > 1]
    return f"o mesmo fato em mais de uma frase: {', '.join(repetidos[:3])}" if repetidos else None


NUMERO_MAX_FRASES = 2  # o mesmo número em mais frases que isso soa repetido ("26 metros" 5x, job 865)


def numero_repetido(script: dict) -> str | None:
    """Número (fora ano) dito em mais de NUMERO_MAX_FRASES frases do roteiro."""
    contagem: dict[str, int] = {}
    for sc in script.get("scenes", []):
        for frase in _FRASES.split(_ID.sub("", sc.get("narration", ""))):
            for m in re.finditer(r"\d+(?:[.,]\d+)*", frase):
                if _eh_data(frase, m.start(), m.group()):
                    continue
                chave = canon(m.group())
                chave = next((k for k in contagem if _algum_igual({k}, {chave})), chave)
                contagem[chave] = contagem.get(chave, 0) + 1
    pior = max(contagem.items(), key=lambda kv: kv[1], default=("", 0))
    return f"o número {pior[0]} aparece em {pior[1]} frases" if pior[1] > NUMERO_MAX_FRASES else None


def _frase_livre(frase: str, resto: str | None = None) -> bool:
    """Frase que não afirma fato: pergunta (sem premissa nova, quando o resto da narração é dado),
    exclamação curta (saudação) ou convite ao público."""
    f = frase.strip()
    if f.endswith("?"):
        return resto is None or not _premissa_nova(f, resto)
    return (f.endswith("!") and len(f.split()) <= 6) or bool(_LIVRE.search(f))


def frases_sem_marca(script: dict) -> list[str]:
    """Frases que afirmam algo e não trazem a marca [F#] do escritor (job 876: 10 frases sem marca,
    9 sem fonte, o roteiro esvaziou). Pergunta, saudação e convite não precisam de marca."""
    texto = " ".join(str(sc.get("narration", "")) for sc in script.get("scenes", []))
    return [f.strip() for f in re.split(r"(?<=[.!?])\s+", texto)
            if f.strip() and not _ID.search(f) and not _frase_livre(_ID.sub("", f))]


MAX_SEM_MARCA = 1


MIN_NUMEROS_FINAL = 4   # Fractal: dado concreto é o vídeo; com menos de 4 números distintos não tem base
MIN_PALAVRAS_FINAL = 100  # ~46 s de voz; abaixo disso as remoções deixaram só o esqueleto


def _raizes(frase: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w+", frase.lower()) if len(w) >= 5}


_SOLTA = re.compile(r"\b(é|foi|era) (um|uma) (deles|delas)\b", re.I)


def remover_redundantes(script: dict) -> dict:
    """Texto final sem repetição (job 879: depois das trocas por fato, 1914, 1,8 milhão e 26 metros
    vinham 3 vezes e "o sistema triplo desce o nível do lago" 2 vezes). Fora do gancho (1ª cena),
    sai a frase cujos números TODOS já foram ditos fora do gancho, e a frase sem número cujas
    palavras de conteúdo já estão quase todas (80%) numa frase anterior. Pergunta e convite ficam.
    Não reescreve nada: só tira frase."""
    cenas = [dict(sc) for sc in script.get("scenes", [])]
    ditos: set[str] = set()
    anteriores: list[set[str]] = []
    removidas = []
    for i, sc in enumerate(cenas):
        ficam = []
        for f in re.split(r"(?<=[.!?])\s+", str(sc.get("narration", "")).strip()):
            if not f.strip():
                continue
            nums, raizes = numeros(f), _raizes(f)
            if i > 0 and not _frase_livre(f):
                if nums and nums <= ditos:
                    removidas.append(f)
                    continue
                if not nums and raizes and any(len(raizes & a) >= 0.8 * len(raizes) for a in anteriores):
                    removidas.append(f)
                    continue
                # referência solta ("Gatún é um deles", job 879): a lista a que se refere não foi dita
                ultima = ficam[-1] if ficam else (cenas[i - 1]["narration"].strip() if i else "")
                if _SOLTA.search(f) and not ultima.endswith("?"):
                    removidas.append(f)
                    continue
            if i > 0:
                ditos |= nums
            anteriores.append(raizes)
            ficam.append(f)
        sc["narration"] = " ".join(ficam)
    if removidas:
        log.warning("texto final: %d frase(s) repetida(s) removida(s): %s", len(removidas),
                    "; ".join(f[:60] for f in removidas))
    return {**script, "scenes": [sc for sc in cenas if sc["narration"].strip()]}


_ANAFORA = re.compile(r"^(isso|isto|esse|essa|esses|essas|este|esta|ele|ela|eles|elas|l[áa]|ali|a[íi])\b", re.I)
_SAUDACAO = re.compile(r"^[^.!?]{0,40}!\s*")


def gancho_sem_referencia(script: dict, topic: str = "") -> dict:
    """Gancho que começa com pronome ("Isso acontece nas eclusas", job 886: a frase a que ele se
    referia foi removida na validação) ganha antes dele a frase do texto que tem o número do tema
    (ou o primeiro número). Só muda a ordem de frases já validadas."""
    cenas = [dict(sc) for sc in script.get("scenes", [])]
    if len(cenas) < 2:
        return script
    primeira = str(cenas[0].get("narration", ""))
    saud = _SAUDACAO.match(primeira)
    resto = primeira[saud.end():] if saud else primeira
    if not _ANAFORA.match(resto.strip()):
        return script
    alvo_nums = numeros(topic)
    candidatas = [(i, f) for i, sc in enumerate(cenas[1:], 1)
                  for f in re.split(r"(?<=[.!?])\s+", str(sc.get("narration", ""))) if numeros(f) and not f.endswith("?")]
    escolha = next(((i, f) for i, f in candidatas if numeros(f) & alvo_nums), candidatas[0] if candidatas else None)
    if not escolha:
        return script
    i, frase = escolha
    cenas[i]["narration"] = " ".join(x for x in re.split(r"(?<=[.!?])\s+", cenas[i]["narration"]) if x != frase)
    cenas[0]["narration"] = ((saud.group(0) if saud else "") + frase + " " + resto.strip()).strip()
    log.warning("gancho começava com pronome solto — a frase %r subiu para a abertura", frase[:60])
    return {**script, "scenes": [sc for sc in cenas if sc["narration"].strip()]}


def completar_com_fatos(script: dict, fatos: list[dict] | None) -> dict:
    """Texto final que as remoções esvaziaram (jobs 880-884: o escritor enfeitava, a validação
    cortava, sobravam 70-130 palavras e 1 número) é completado com fatos verificados ainda não
    ditos, cada um com o próprio texto do fato (já validado: nada novo entra). Oficial/confirmado
    primeiro, com número primeiro; fato cuja medida a narração já diz fica de fora. Entram antes
    da última cena (a da pergunta final) até o texto deixar de ser magro."""
    if not fatos or not roteiro_magro(script):
        return script
    cenas = [dict(sc) for sc in script.get("scenes", [])]
    narr = " ".join(str(sc.get("narration", "")) for sc in cenas)
    usados = {i for sc in cenas for i in sc.get("fatos") or []}
    ordem = sorted(sem_medida_repetida(fatos), key=lambda f: (f.get("status") == "secundario", not numeros(f["texto"])))
    novos = []
    for f in ordem:
        if f["id"] in usados or mede_o_que_ja_foi_dito(f["texto"], narr) or (numeros(f["texto"]) and numeros(f["texto"]) <= numeros(narr)):
            continue
        if _raizes(f["texto"]) and any(len(_raizes(f["texto"]) & _raizes(x)) >= 0.8 * len(_raizes(f["texto"]))
                                       for x in re.split(r"(?<=[.!?])\s+", narr)):
            continue  # já dito com outras palavras
        novos.append({"narration": f["texto"].strip(), "fatos": [f["id"]]})
        narr += " " + f["texto"]
        teste = {**script, "scenes": cenas[:-1] + novos + cenas[-1:]}
        if not roteiro_magro(teste):
            break
    if not novos:
        return script
    log.warning("texto final magro completado com %d fato(s) verificado(s): %s", len(novos),
                "; ".join(n["narration"][:50] for n in novos))
    return {**script, "scenes": cenas[:-1] + novos + cenas[-1:] if len(cenas) > 1 else cenas + novos}


def roteiro_magro(script: dict) -> str | None:
    """Motivo para reprovar o texto final que as remoções esvaziaram (job 863: 9 de 11 afirmações sem
    fonte saíram e sobraram 4 cenas, 63 palavras e nenhum número); None se tem corpo."""
    texto = " ".join(str(sc.get("narration", "")) for sc in script.get("scenes", []))
    n_num, n_pal = len(numeros(texto)), len(texto.split())
    if n_num < MIN_NUMEROS_FINAL or n_pal < MIN_PALAVRAS_FINAL:
        return (f"roteiro final ficou magro depois das remoções: {n_pal} palavras e {n_num} número(s) "
                f"(mínimo {MIN_PALAVRAS_FINAL} e {MIN_NUMEROS_FINAL})")
    return None


def conferir_frases(script: dict, fatos: list[dict]) -> dict:
    """Depois do escritor: lê as marcas [F#] de cada frase, confere os números contra os fatos
    citados (ou, sem marca certa, contra o fato que tem aquele número), tira a frase cujo número
    não está em fato nenhum, põe "estima-se" quando o fato é secundário e apaga as marcas.
    Frase sem número e sem marca (pergunta, gancho, convite) fica como está."""
    if not fatos:
        return script
    por_id = {f["id"]: f for f in fatos}
    removidas = estimadas = 0
    pendentes: list[str] = []
    # o escritor usou as marcas? sem nenhuma marca no roteiro, a regra de "frase sem marca sai"
    # apagaria tudo: aí só os números são conferidos
    com_marcas = any(_ID.search(sc.get("narration", "")) for sc in script.get("scenes", []))
    frases_limpas = [re.sub(r"\s+([.!?,;:])", r"\1", _ID.sub("", f)).strip()
                     for sc in script.get("scenes", []) for f in _FRASES.split(sc.get("narration", "")) if f.strip()]
    cenas = []
    for sc in script.get("scenes", []):
        saida, usados = [], []
        for frase in _FRASES.split(sc.get("narration", "")):
            if not frase.strip():
                continue
            ids = [i.strip() for m in _ID.finditer(frase) for i in re.split(r"[,;]", m.group(1))]
            limpa = re.sub(r"\s+([.!?,;:])", r"\1", _ID.sub("", frase)).strip()
            nums = numeros(limpa)
            citados = [por_id[i] for i in ids if i in por_id]
            if numero_por_extenso(limpa):
                # número por extenso não dá para conferir contra os fatos: a frase sai
                removidas += 1
                log.warning("frase com número por extenso (não conferível) — removida: %r", limpa[:90])
                continue
            if com_marcas and not citados and not nums and not _frase_livre(
                    limpa, " ".join(x for x in frases_limpas if x != limpa)):
                # afirmação sem fato citado: não sai direto — vai para a verificação por citação nas
                # fontes (fact_check.verificar_sem_fato); verdade que o escritor não marcou fica
                pendentes.append(limpa)
            if nums:
                if not nums <= set().union(*(numeros(f["texto"]) for f in citados), set()):
                    # a marca não cobre o número: vale o fato que tem o número (mais oficial primeiro)
                    achados = [_melhor_por_numero(n, fatos) for n in nums]
                    if any(c is None for c in achados):
                        removidas += 1
                        log.warning("frase sem fato para o número — removida: %r", limpa[:90])
                        continue
                    citados = list({c["id"]: c for c in achados if c is not None}.values())
            if citados and not _frase_livre(limpa) and _alem_dos_fatos(limpa, citados):
                # frase que diz mais do que o fato ("A gravidade enche cada câmara sem bombas", job 865;
                # "1,8 milhão de m³ — daria uma muralha ao redor de uma cidade", job 867): o número
                # confere, o resto não — vai para a checagem por citação. Vale também para a frase sem
                # marca casada pelo número (12/11: antes só a marcada passava por aqui)
                pendentes.append(limpa)
            secundarios = [f for f in citados if f["status"] == "secundario"]
            alvo = set().union(*(numeros(f["texto"]) for f in secundarios), set()) & nums
            if alvo:
                novo = estima_antes(limpa, alvo, estimadas)
                estimadas += novo != limpa
                limpa = novo
            usados += [f["id"] for f in citados]
            saida.append(limpa)
        cenas.append({**sc, "narration": " ".join(saida), "fatos": list(dict.fromkeys(usados))})
    log.info("conferência por fato: %d frase(s) removida(s), %d aproximada(s), %d sem marca para verificar",
             removidas, estimadas, len(pendentes))
    out = {**script, "scenes": [sc for sc in cenas if sc["narration"].strip()]}
    if pendentes:
        out["_sem_fato"] = pendentes
    return out


_CUSTO_FINAL = [
    (re.compile(r"^O custo total (chegou a|foi de|ficou em|era de|de)\b"), "O orçamento estimado era de"),
    (re.compile(r"\bo custo total (chegou a|foi de|ficou em|era de|de)\b"), "o orçamento estimado era de"),
    (re.compile(r"\b(um|seu) custo total de\b"), r"\1 orçamento estimado de"),
    (re.compile(r"\b(custou|consumiu)\b"), "teve orçamento estimado de"),
]
_CONVERSAO = re.compile(r",?\s*(o )?(equivalente|cerca) a (US\$\s*)?[\d.,]+\s*(mil|milhões|milhão|bilhões|bilhão)?"
                        r"\s*(de )?(d[óo]lares|euros|reais)", re.I)


def ajustar_texto_final(script: dict) -> dict:
    """Depois da validação, sobre o texto final (o conserto e o corte reescrevem frases sem marca):
    conversão de moeda sem fato sai da frase; valor que só existe como planejamento/orçamento vira
    "orçamento estimado"; e a mesma conferência por número de conferir_frases (número sem fato tira
    a frase, fato secundário põe "estima-se")."""
    # proporção em palavras que contradiz a conta sai (vale mesmo sem lista de fatos)
    erradas = set(proporcao_errada(script))
    if erradas:
        log.warning("proporção que não bate com a conta — frase(s) removida(s): %s", " | ".join(x[:70] for x in erradas))
        script = {**script, "scenes": [{**sc, "narration": " ".join(
            fr for fr in _FRASES.split(sc.get("narration", "")) if fr.strip() and fr.strip() not in erradas)}
            for sc in script.get("scenes", [])]}
    fatos = script.get("_fatos") or []
    if not fatos:
        return script
    todos = set().union(*(numeros(f["texto"]) for f in fatos))
    planejamento = {n for f in fatos for n in f.get("planejamento", [])} | set().union(
        *(numeros(f["texto"]) for f in fatos if _PLANEJAMENTO.search(f["texto"])), set())
    cenas = []
    for sc in script.get("scenes", []):
        frases = []
        for frase in _FRASES.split(sc.get("narration", "")):
            m = _CONVERSAO.search(frase)
            if m and not numeros(m.group(0)) <= todos:
                frase = (frase[:m.start()] + frase[m.end():]).replace(" .", ".")
            if numeros(frase) & planejamento:
                for padrao, troca in _CUSTO_FINAL:
                    frase = padrao.sub(troca, frase)
            frases.append(frase)
        cenas.append({**sc, "narration": " ".join(f for f in frases if f.strip())})
    return conferir_frases({**script, "scenes": cenas}, fatos)


def _raiz(palavra: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", palavra.lower()).encode("ascii", "ignore").decode()


_LIGACAO = {"de", "do", "da", "dos", "das", "e", "o", "a", "os", "as", "em", "no", "na", "um", "uma", "que", "com",
            "por", "para", "ao", "à", "se"}


def numero_fora_de_contexto(texto: str, narracao: str) -> list[str]:
    """Números do título/miniatura cujo CONTEXTO (as palavras logo depois do número) não aparece ao
    lado do mesmo número na narração. "53,85 km sob o mar" com a narração dizendo "53,85 km de
    extensão" e "23,3 km sob o leito marinho" volta ["53,85 km sob o mar"]. Lista vazia = ok."""
    erros = []
    # a janela de contexto fica DENTRO da frase: "53,85 km de extensão. Sob o mar..." não vale para 53,85
    frases_narr = [re.findall(r"\d+(?:[.,]\d+)*|[^\W\d_]+", fr) for fr in _FRASES.split(narracao)]
    for m in re.finditer(r"(\d+(?:[.,]\d+)*)\s*([^\W\d_]+(?:\s+[^\W\d_]+){0,4})?", texto):
        numero, resto = m.group(1), (m.group(2) or "")
        palavras = [w for w in resto.split() if _raiz(w) not in _LIGACAO][1:3]  # a 1ª é a unidade ("km")
        if not palavras:
            continue
        ok = False
        for tokens_narr in frases_narr:
            for i, tok in enumerate(tokens_narr):
                if not tok[0].isdigit() or canon(tok) != canon(numero):
                    continue
                vizinhas = [_raiz(w) for w in tokens_narr[i + 1:i + 7]]
                if all(any(v.startswith(_raiz(p)[:3]) or _raiz(p).startswith(v[:3]) for v in vizinhas if len(v) >= 3)
                       for p in palavras):
                    ok = True
                    break
            if ok:
                break
        if not ok:
            erros.append(m.group(0).strip())
    return erros


def frases_com_numero(narracao: str, limite: int = 12) -> list[str]:
    """Cada número da narração com as palavras que vêm logo depois DENTRO da mesma frase
    ("53,85 km de extensão", "23,3 km sob o leito marinho"): o que o título pode citar."""
    out: list[str] = []
    for frase in _FRASES.split(narracao):
        for m in re.finditer(r"\d+(?:[.,]\d+)*(?:\s+[^\W\d_]+){0,5}", frase):
            trecho = m.group(0).strip().rstrip(",;:")
            if trecho not in out:
                out.append(trecho)
    return out[:limite]


def _palavras_seguidas(texto: str) -> list[str]:
    import unicodedata
    fold = unicodedata.normalize("NFKD", (texto or "").lower()).encode("ascii", "ignore").decode()
    return re.findall(r"\d+(?:[.,]\d+)*|[a-z]+", fold)


def copiado_do_texto(trecho: str, narracao: str) -> bool:
    """O trecho é cópia da narração: as mesmas palavras, na mesma ordem e seguidas (maiúsculas e
    pontuação não contam). "23,3 KM SOB O MAR" com a narração "Sob o mar ficam 23,3 km" é falso."""
    alvo, texto = _palavras_seguidas(trecho), _palavras_seguidas(narracao)
    if not alvo:
        return False
    n = len(alvo)
    return any(texto[i:i + n] == alvo for i in range(len(texto) - n + 1))


def titulo_copiado(titulo: str, narracao: str, assunto: str = "") -> bool:
    """Título "Assunto: trecho" — cada parte tem de ser cópia da narração; a primeira também pode ser o
    nome do assunto do tema validado (título da Wikipédia), mesmo que a narração não o escreva. O
    trecho não pode repetir as palavras do assunto ("Vão central: O vão central mede...")."""
    partes = [p.strip() for p in re.split(r"[:—–|]", titulo or "") if p.strip()]
    if not partes:
        return False
    nome = _palavras_seguidas(assunto)
    if nome and _palavras_seguidas(partes[0]) != nome:
        return False  # assunto conhecido: o título abre com o nome dele ("Vão central mede 1991 metros" não diz qual ponte)
    primeira_ok = copiado_do_texto(partes[0], narracao) or (bool(nome) and _palavras_seguidas(partes[0]) == nome)
    if not primeira_ok or not all(copiado_do_texto(p, narracao) for p in partes[1:]):
        return False
    if len(partes) >= 2:
        a = {w for w in _palavras_seguidas(partes[0]) if len(w) > 3}
        b = set(_palavras_seguidas(partes[1]))
        if a and len(a & b) / len(a) >= 0.5:
            return False
    return True


TITULO_MAX = 60  # título acima de 60 caracteres rende menos (views reais, 11/2026)


def titulo_com_assunto(titulo: str, assunto: str) -> str:
    """Título que não abre com o nome do assunto validado ganha o nome na frente ("Ponte Akashi
    Kaikyō: Vão central mede 1991 metros"); se passar de TITULO_MAX, fica só o nome."""
    titulo = (titulo or "").strip()
    if not assunto or not titulo:
        return titulo
    partes = [p.strip() for p in re.split(r"[:—–|]", titulo) if p.strip()]
    if partes and _palavras_seguidas(partes[0]) == _palavras_seguidas(assunto):
        return titulo
    trecho = " — ".join(partes) if partes else titulo
    novo = f"{assunto}: {trecho[:1].upper()}{trecho[1:]}"
    return novo if len(novo) <= TITULO_MAX else assunto


# proporção dita em palavras precisa bater com a conta parte/total da mesma unidade
_PROPORCOES = [
    (re.compile(r"\b(a\s+)?maior\s+parte\b|\bmaioria\b", re.I), lambda r: r > 0.5),
    (re.compile(r"\bquase\s+tod[oa]s?\b", re.I), lambda r: r >= 0.8),
    (re.compile(r"\bmenos\s+da\s+metade\b|\bminoria\b", re.I), lambda r: r < 0.5),
    (re.compile(r"\b(cerca\s+de\s+|quase\s+)?(a\s+)?metade\b", re.I), lambda r: 0.4 <= r <= 0.6),
]


def proporcao_errada(script: dict) -> list[str]:
    """Frases com proporção em palavras ("a maior parte", "a maioria", "metade", "quase todo",
    "menos da metade") que contradizem a conta entre o número da cena (a parte) e o maior número
    da mesma unidade no roteiro (o total). Ex.: 23,3 de 53,85 km é 43%, não "a maior parte"."""
    todos: dict[str, float] = {}
    for sc in script.get("scenes", []):
        for n, u in _pares(sc.get("narration", "")):
            todos[u] = max(todos.get(u, 0.0), float(n))
    erradas = []
    for sc in script.get("scenes", []):
        pares_cena = [(float(n), u) for n, u in _pares(sc.get("narration", ""))]
        for frase in _FRASES.split(sc.get("narration", "")):
            regra = next((teste for padrao, teste in _PROPORCOES if padrao.search(frase)), None)
            if regra is None:
                continue
            pares = [(n, u) for n, u in _pares(frase)] and [(float(n), u) for n, u in _pares(frase)] or pares_cena
            for parte, unidade in pares:
                total = todos.get(unidade, 0.0)
                if 0 < parte < total and not regra(parte / total):
                    erradas.append(frase.strip())
                    break
    return erradas



_CONECTIVOS_FINAIS = {"de", "do", "da", "dos", "das", "e", "o", "a", "os", "as", "em", "no", "na", "um", "uma", "que",
                      "com", "por", "para", "ao", "sob", "sobre", "entre", "se", "mas", "ou", "ficam", "fica"}


def termina_solto(texto: str) -> bool:
    """Texto curto (miniatura, título) que termina em preposição/artigo/conjunção ou verbo de
    ligação está cortado no meio: "23,3 km ficam sob"."""
    palavras = _palavras_seguidas(texto)
    return bool(palavras) and palavras[-1] in _CONECTIVOS_FINAIS



def _tem_conteudo(frase: str) -> bool:
    """Frase que carrega fato: número ou nome próprio fora do começo da frase."""
    if re.search(r"\d", frase):
        return True
    palavras = frase.split()
    return any(p[:1].isupper() and len(p.strip(".,;:!?")) > 2 for p in palavras[1:])


def encurtar_roteiro(script: dict, max_palavras: int) -> dict:
    """Short longo demais: tira FRASES (nunca reescreve) até caber em `max_palavras`. Saem primeiro
    as de enchimento (sem número e sem nome próprio), das mais longas para as mais curtas; nunca a
    primeira frase de cada cena, pergunta, nem a última cena (fechamento e convite). Se só com
    enchimento não couber, saem as de conteúdo das cenas do meio, da mais longa para a mais curta."""
    cenas = [[f for f in _FRASES.split(sc.get("narration", "")) if f.strip()] for sc in script.get("scenes", [])]
    total = sum(len(f.split()) for fr in cenas for f in fr)
    if total <= max_palavras or len(cenas) < 3:
        return script

    def candidatas(com_conteudo: bool) -> list[tuple[int, int, int]]:
        out = []
        for i, frases in enumerate(cenas[:-1]):
            for j, f in enumerate(frases):
                if j == 0 or f.strip().endswith("?") or _tem_conteudo(f) != com_conteudo:
                    continue
                out.append((len(f.split()), i, j))
        return sorted(out, reverse=True)

    tirar: set[tuple[int, int]] = set()
    for com_conteudo in (False, True):
        for tamanho, i, j in candidatas(com_conteudo):
            if total <= max_palavras:
                break
            tirar.add((i, j))
            total -= tamanho
        if total <= max_palavras:
            break
    log.warning("Short longo: %d frase(s) tirada(s), ficou com %d palavras (limite %d)", len(tirar), total, max_palavras)
    novas = [{**sc, "narration": " ".join(f for j, f in enumerate(cenas[i]) if (i, j) not in tirar)}
             for i, sc in enumerate(script.get("scenes", []))]
    return {**script, "scenes": [sc for sc in novas if sc["narration"].strip()]}



def termina_em_fronteira(trecho: str, narracao: str) -> bool:
    """O trecho copiado termina onde a frase da narração termina (pontuação ou fim da frase):
    "53,85 km que atravessa" (a narração continua "o mar...") é recusado."""
    alvo = _palavras_seguidas(trecho)
    if not alvo:
        return False
    for frase in re.split(r"(?<=[.!?:;,])\s+", narracao):
        palavras = _palavras_seguidas(frase)
        n = len(alvo)
        for i in range(len(palavras) - n + 1):
            if palavras[i:i + n] == alvo and i + n == len(palavras):
                return True
    return False
