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
    texto = texto or ""
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
            if not ({n for n, u in pb if u in mesma_medida} & {n for n, u in pa if u in mesma_medida}):
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


def _frase_livre(frase: str) -> bool:
    """Frase que não afirma fato: pergunta, exclamação curta (saudação) ou convite ao público."""
    f = frase.strip()
    return f.endswith("?") or (f.endswith("!") and len(f.split()) <= 6) or bool(_LIVRE.search(f))


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
            if com_marcas and not citados and not nums and not _frase_livre(limpa):
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


def titulo_copiado(titulo: str, narracao: str) -> bool:
    """Título "Assunto: trecho" — cada parte separada por dois-pontos tem de ser cópia da narração."""
    partes = [p.strip() for p in re.split(r"[:—–|]", titulo or "") if p.strip()]
    return bool(partes) and all(copiado_do_texto(p, narracao) for p in partes)


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
