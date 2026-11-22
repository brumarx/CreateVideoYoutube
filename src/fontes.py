"""Bloco de fontes ÚNICO: lido igual pela extração de fatos, pelo revisor, pelo juiz e pelo
conserto (mesma ordem, mesmo separador, mesmo limite). Antes cada um montava o seu e um mesmo
número podia ser aceito numa fatia e reprovado noutra (rodada 828).

19/11 (jobs 987-989, Yakutsk): o bloco era cortado às cegas no limite. Sete páginas genéricas de
engenharia de fundações vieram primeiro e ocuparam tudo; as fontes que diziam "em Yakutsk os
prédios ficam sobre estacas de 15 metros" ficaram depois do corte e nem o extrator nem o juiz as
leram. Agora, quando o material não cabe, os trechos entram por RELEVÂNCIA ao tema (palavra rara
do tema pesa mais que palavra comum), cada fonte relevante com pelo menos o seu melhor trecho, e o
que ficou de fora é registrado no log. Quando cabe, nada muda (mesma ordem, mesmo texto)."""
from __future__ import annotations

import json
import logging
import math
import re
import unicodedata

log = logging.getLogger("fontes")

LIMITE_FONTES = 9000
TAMANHO_TRECHO = 450  # caracteres por trecho na seleção por relevância

_PARADAS = {
    "como", "para", "pelo", "pela", "pelos", "pelas", "sobre", "entre", "porque", "quando", "onde", "qual",
    "quais", "mais", "menos", "muito", "cada", "esse", "essa", "este", "esta", "isso", "isto", "seus", "suas",
    "tem", "têm", "foram", "ser", "são", "sao", "está", "esta", "estão", "mundo", "cidade", "funciona", "fazer",
    "with", "from", "that", "this", "what", "which", "about", "their", "there", "into", "have", "were", "does",
    "uma", "umas", "uns", "dos", "das", "nos", "nas", "the", "and", "por", "que", "acima", "abaixo", "depois",
}


# 21/11 (E86): marca de texto omitido num recorte (frases puladas entre duas escolhidas). Nada dos dois lados
# de uma marca é vizinho na fonte original.
MARCA_CORTE = "[…]"


def dobrar(texto: str) -> str:
    """Minúsculas sem acento (comparação de palavras entre fontes e tema)."""
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def palavras_chave(topic: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", dobrar(topic)) if len(w) >= 4 and w not in _PARADAS}


def _trechos(texto: str, tamanho: int = TAMANHO_TRECHO) -> list[str]:
    frases = [f for f in re.split(r"(?<=[.!?])\s+|\n+", str(texto or "")) if f.strip()]
    saida, atual = [], ""
    for f in frases:
        if atual and len(atual) + len(f) + 1 > tamanho:
            saida.append(atual)
            atual = f
        else:
            atual = f"{atual} {f}".strip()
    if atual:
        saida.append(atual)
    return saida


def selecionar(web_facts: list[dict] | None, topic: str, limite: int, por_fonte: int | None = None,
               custo_fixo: int = 0) -> tuple[dict[int, str], list[dict]]:
    """Escolhe, de cada fonte, os trechos que cabem em `limite` caracteres (somando `custo_fixo`
    por fonte para o cabeçalho), por relevância ao tema. Devolve ({índice da fonte: texto
    escolhido, na ordem original dos trechos}, [descartados com relevância > 0]).
    Sem palavra-chave no tema, devolve o corte antigo (na ordem)."""
    fontes = list(web_facts or [])
    chaves = palavras_chave(topic)
    pedacos: list[tuple[int, int, str, set[str]]] = []  # (fonte, posição, texto, chaves presentes no trecho)
    titulo_casa: dict[int, bool] = {}
    for i, f in enumerate(fontes):
        trecho = str(f.get("trecho", ""))
        if por_fonte:
            trecho = trecho[:por_fonte]
        titulo_casa[i] = any(c in dobrar(f.get("titulo", "")) for c in chaves)
        for j, t in enumerate(_trechos(trecho)):
            pedacos.append((i, j, t, {c for c in chaves if c in dobrar(t)}))
    if not chaves or not pedacos:
        escolhidos: dict[int, str] = {}
        usado = 0
        for i, f in enumerate(fontes):
            t = str(f.get("trecho", ""))[:por_fonte] if por_fonte else str(f.get("trecho", ""))
            if usado + custo_fixo >= limite:
                break
            t = t[:max(0, limite - usado - custo_fixo)]
            escolhidos[i] = t
            usado += len(t) + custo_fixo
        return escolhidos, []
    # peso de cada palavra-chave: rara entre os trechos pesa mais ("yakutsk" > "estacas")
    df = {c: sum(1 for p in pedacos if c in p[3]) for c in chaves}
    peso = {c: math.log(1 + len(pedacos) / df[c]) if df[c] else 0.0 for c in chaves}

    def assunto(p: tuple[int, int, str, set[str]]) -> float:
        """Quanto o trecho fala do tema: só as palavras do próprio trecho (o título da página não
        conta aqui, senão todo trecho da página ganharia a palavra e ela deixaria de ser rara)."""
        return sum(peso[c] for c in p[3])

    def nota(p: tuple[int, int, str, set[str]]) -> float:
        return assunto(p) + (0.5 if titulo_casa[p[0]] else 0.0) + (0.3 if re.search(r"\d", p[2]) else 0.0)

    ordem = sorted(range(len(pedacos)), key=lambda k: (-nota(pedacos[k]), pedacos[k][0], pedacos[k][1]))
    melhor_de: dict[int, int] = {}
    for k in ordem:
        melhor_de.setdefault(pedacos[k][0], k)
    # 1ª passada: o melhor trecho de cada fonte relevante; 2ª: o resto por nota
    fila = [k for k in ordem if k == melhor_de[pedacos[k][0]] and assunto(pedacos[k]) > 0]
    fila += [k for k in ordem if k not in set(fila)]
    usado, pegos, fontes_com = 0, set(), set()
    for k in fila:
        i, _, t, _ = pedacos[k]
        custo = len(t) + 1 + (custo_fixo if i not in fontes_com else 0)
        if usado + custo > limite:
            continue
        pegos.add(k)
        fontes_com.add(i)
        usado += custo
    escolhidos = {}
    ultimo: dict[int, int] = {}
    for k in sorted(pegos, key=lambda k: (pedacos[k][0], pedacos[k][1])):
        i, j, t, _ = pedacos[k]
        # 21/11 (E86): pedaço pulado entre dois escolhidos fica marcado — o extrator não liga frases que não são
        # vizinhas na fonte ("First, ..." depois de uma data que não é a dele)
        escolhidos[i] = (f"{escolhidos[i]} {'' if j == ultimo[i] + 1 else MARCA_CORTE + ' '}{t}") if i in escolhidos else t
        ultimo[i] = j
    descartados = [{"url": fontes[pedacos[k][0]].get("url", ""), "nota": round(nota(pedacos[k]), 2),
                    "trecho": pedacos[k][2][:120]}
                   for k in ordem if k not in pegos and assunto(pedacos[k]) > 0]
    if descartados:
        log.warning("bloco de fontes: %d trecho(s) relevante(s) não couberam em %d caracteres (o mais relevante "
                    "de fora: nota %.2f, %s)", len(descartados), limite, descartados[0]["nota"], descartados[0]["url"][:80])
    return escolhidos, descartados


def cabe(web_facts: list[dict] | None, limite: int, por_fonte: int | None, custo_fixo: int, extra: int = 0) -> bool:
    total = extra + sum(min(len(str(f.get("trecho", ""))), por_fonte or 10**9) + custo_fixo for f in web_facts or [])
    return total <= limite


def bloco_fontes(facts: dict | None, web_facts: list[dict] | None, topic: str = "") -> str:
    partes: list[str] = []
    if facts:
        partes.append(json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    fixo = sum(len(p) + 1 for p in partes)
    cabecalho = 6  # "- " + ": " + "\n"
    if not topic or cabe(web_facts, LIMITE_FONTES, None, cabecalho, fixo + sum(len(str(f.get("titulo", ""))) for f in web_facts or [])):
        partes += [f"- {f.get('titulo', '')}: {f.get('trecho', '')}" for f in web_facts or []]
        return "\n".join(partes)[:LIMITE_FONTES]
    escolhidos, _ = selecionar(web_facts, topic, max(0, LIMITE_FONTES - fixo), custo_fixo=cabecalho + 40)
    partes += [f"- {f.get('titulo', '')}: {escolhidos[i]}" for i, f in enumerate(web_facts or []) if i in escolhidos]
    return "\n".join(partes)[:LIMITE_FONTES]
