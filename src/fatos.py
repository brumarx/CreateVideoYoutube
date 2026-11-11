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
LIMITE_BLOCO = 9000
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


def numeros(texto: str) -> set[str]:
    return {canon(m) for m in re.findall(r"\d+(?:[.,]\d+)*", texto or "")}


def bloco_numerado(web_facts: list[dict] | None) -> str:
    """Fontes numeradas [S1], [S2]... para a extração citar de onde tirou cada fato."""
    linhas = [f"[S{i + 1}] {f.get('titulo', '')} ({dominio(f.get('url', ''))}): {f.get('trecho', '')}"
              for i, f in enumerate(web_facts or [])]
    return "\n".join(linhas)[:LIMITE_BLOCO]


def atribuir(fatos_brutos: list[dict], web_facts: list[dict]) -> list[dict]:
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
        texto = str(bruto.get("texto", "")).strip()
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
    return remover_conflitos(saida)


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


def _pares(texto: str) -> set[tuple[str, str]]:
    out = set()
    for m in _NUM_UNIDADE.finditer(texto):
        unidade = _UNIDADES.get((m.group(2) or "").lower())
        if unidade:
            out.add((canon(m.group(1)), unidade))
    return out


def _palavras(texto: str) -> set[str]:
    import unicodedata
    fold = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return {w[:5] for w in re.findall(r"[a-z]{4,}", fold)} - {w[:5] for w in _PARADAS}


def remover_conflitos(fatos: list[dict]) -> list[dict]:
    """Fonte primária manda: fato de status pior que dá OUTRO valor para a mesma medida (mesma
    unidade e mesmo assunto) que um fato de status melhor é descartado. Ex.: "65,23 km de
    extensão" (Wikipédia pt) sai quando há "extensão de 53,85 km" oficial."""
    if not fatos:
        return fatos
    contagem: dict[str, int] = {}
    for f in fatos:
        for w in _palavras(f["texto"]):
            contagem[w] = contagem.get(w, 0) + 1
    tema = {w for w, n in contagem.items() if len(fatos) >= 4 and n * 2 >= len(fatos)}
    fora = set()
    for a in fatos:
        for b in fatos:
            if a is b or STATUS_ORDEM[a["status"]] >= STATUS_ORDEM[b["status"]]:
                continue
            pa, pb = _pares(a["texto"]), _pares(b["texto"])
            mesma_medida = {u for _, u in pa} & {u for _, u in pb}
            if not mesma_medida or not ((_palavras(a["texto"]) & _palavras(b["texto"])) - tema):
                continue
            if not ({n for n, u in pb if u in mesma_medida} & {n for n, u in pa if u in mesma_medida}):
                fora.add(b["id"])
                log.warning("fato %s descartado: contradiz %s, de fonte melhor (%r × %r)", b["id"], a["id"],
                            b["texto"][:60], a["texto"][:60])
    if not fora:
        return fatos
    restantes = [f for f in fatos if f["id"] not in fora]
    return [{**f, "id": f"F{i + 1}"} for i, f in enumerate(restantes)]


def lista_para_escritor(fatos: list[dict]) -> str:
    return "\n".join(f"{f['id']}: {f['texto']}" for f in fatos)


_FRONTEIRAS = (": ", "; ", ", e ", ", mas ", ", enquanto ")


def estima_antes(frase: str, nums_alvo: set[str]) -> str:
    """'Estima-se que' no começo da ORAÇÃO que tem o número (ou da frase, sem número)."""
    if re.search(r"estima", frase, re.I):
        return frase
    posicoes = [m.start() for m in re.finditer(r"\d+(?:[.,]\d+)*", frase) if canon(m.group()) in nums_alvo]
    posicao = min(posicoes) if posicoes else 0
    corte = max((frase.rfind(f, 0, posicao) + len(f) for f in _FRONTEIRAS if frase.rfind(f, 0, posicao) >= 0),
                default=0) if posicao else 0
    if corte == 0:
        m = re.match(r"(E|Mas|Então|Porém|Já|Hoje|Ainda)(,)?\s+", frase)
        if m and m.group(1) in ("E", "Mas", "Então", "Porém"):
            resto = frase[m.end():]
            return frase[:m.end()] + "estima-se que " + resto[0].lower() + resto[1:]
        return "Estima-se que " + frase[0].lower() + frase[1:]
    resto = frase[corte:]
    return frase[:corte] + "estima-se que " + resto[0].lower() + resto[1:]


def _melhor_por_numero(n: str, fatos: list[dict]) -> dict | None:
    candidatos = [f for f in fatos if n in numeros(f["texto"])]
    return min(candidatos, key=lambda f: STATUS_ORDEM[f["status"]]) if candidatos else None


def conferir_frases(script: dict, fatos: list[dict]) -> dict:
    """Depois do escritor: lê as marcas [F#] de cada frase, confere os números contra os fatos
    citados (ou, sem marca certa, contra o fato que tem aquele número), tira a frase cujo número
    não está em fato nenhum, põe "estima-se" quando o fato é secundário e apaga as marcas.
    Frase sem número e sem marca (pergunta, gancho, convite) fica como está."""
    if not fatos:
        return script
    por_id = {f["id"]: f for f in fatos}
    removidas = estimadas = 0
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
            if nums:
                if not nums <= set().union(*(numeros(f["texto"]) for f in citados), set()):
                    # a marca não cobre o número: vale o fato que tem o número (mais oficial primeiro)
                    achados = [_melhor_por_numero(n, fatos) for n in nums]
                    if any(c is None for c in achados):
                        removidas += 1
                        log.warning("frase sem fato para o número — removida: %r", limpa[:90])
                        continue
                    citados = list({c["id"]: c for c in achados if c is not None}.values())
            secundarios = [f for f in citados if f["status"] == "secundario"]
            alvo = set().union(*(numeros(f["texto"]) for f in secundarios), set()) & nums
            if alvo:
                novo = estima_antes(limpa, alvo)
                estimadas += novo != limpa
                limpa = novo
            usados += [f["id"] for f in citados]
            saida.append(limpa)
        cenas.append({**sc, "narration": " ".join(saida), "fatos": list(dict.fromkeys(usados))})
    log.info("conferência por fato: %d frase(s) removida(s), %d com 'estima-se'", removidas, estimadas)
    return {**script, "scenes": [sc for sc in cenas if sc["narration"].strip()]}


_CUSTO_FINAL = [
    (re.compile(r"^O custo total (chegou a|foi de|ficou em|era de|de)\b"), "O orçamento estimado era de"),
    (re.compile(r"\bo custo total (chegou a|foi de|ficou em|era de|de)\b"), "o orçamento estimado era de"),
    (re.compile(r"\b(custou|consumiu)\b"), "teve orçamento estimado de"),
]
_CONVERSAO = re.compile(r",?\s*(o )?(equivalente|cerca) a (US\$\s*)?[\d.,]+\s*(mil|milhões|milhão|bilhões|bilhão)?"
                        r"\s*(de )?(d[óo]lares|euros|reais)", re.I)


def ajustar_texto_final(script: dict) -> dict:
    """Depois da validação, sobre o texto final (o conserto e o corte reescrevem frases sem marca):
    conversão de moeda sem fato sai da frase; valor que só existe como planejamento/orçamento vira
    "orçamento estimado"; e a mesma conferência por número de conferir_frases (número sem fato tira
    a frase, fato secundário põe "estima-se")."""
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
