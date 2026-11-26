"""26/11: vocabulário central — meses e lugares (países, regiões, estados, oceanos) em pt/en/es.

Antes havia três tabelas de meses diferentes (fatos, evidencias, gravacao), uma delas por prefixo ("mar\\w*"
casava "maritime", "set\\w*" casava "setting"), e nenhuma tabela de lugares: "Noruega" na frase e "Norway" no
trecho da fonte eram nomes diferentes. Agora tudo sai de src/dados/vocabulario.json, gerado do CLDR por
scripts/gerar_vocabulario.py (países e regiões em pt/en/es; estados do Brasil e dos EUA, oceanos e regiões
polares escritos no gerador). Para acrescentar: uma linha no gerador e rodar de novo.

Unidades de medida (km, kg, °C…) ficam em src/unidades.py — a mesma ideia: uma tabela, um leitor.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent / "dados" / "vocabulario.json"

# apelidos que o CLDR não traz (siglas e nomes de uso comum)
_APELIDOS = {"US": ["EUA", "USA", "U.S.", "U.S.A.", "América do Norte (EUA)"], "GB": ["UK", "Grã-Bretanha", "Great Britain",
             "Britain", "Inglaterra", "England"], "RU": ["URSS", "União Soviética", "Soviet Union", "USSR"],
             "NL": ["Holanda", "Holland"], "CZ": ["República Tcheca", "Czech Republic"], "KR": ["Coreia do Sul"],
             "KP": ["Coreia do Norte"], "AE": ["Emirados Árabes", "UAE"], "SJ": ["Svalbard"]}


def _dobrar(texto: str) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower().strip()


@lru_cache(maxsize=1)
def _dados() -> dict:
    return json.loads(ARQUIVO.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ meses
@lru_cache(maxsize=1)
def meses() -> dict[str, int]:
    """forma (minúscula, com acento como escrita) -> número do mês."""
    return dict(_dados()["meses"])


@lru_cache(maxsize=1)
def re_mes() -> str:
    """Alternância regex de todas as formas de mês, inteiras (palavra completa; "mar" não casa "maritime").
    Abreviação aceita ponto opcional ("Jan.", "fev.")."""
    formas = sorted(meses(), key=len, reverse=True)
    return r"(?:" + "|".join(re.escape(f) + (r"\.?" if len(f) <= 4 else "") for f in formas) + r")(?![^\W\d_])"


def mes(palavra: str) -> int | None:
    return meses().get(str(palavra or "").lower().rstrip("."))


_DATA_DMA = None


def datas(texto: str) -> set[tuple[int | None, int, int | None]]:
    """(dia, mês, ano) de cada data escrita em pt/en/es: "7 de janeiro de 2015", "7 January 2015",
    "January 7, 2015", "jan. 2015", "07/01/2015" (dia/mês), "2015-01-07". Dia ou ano ausentes: None."""
    global _DATA_DMA
    if _DATA_DMA is None:
        m = re_mes()
        _DATA_DMA = re.compile(
            rf"\b(?:(?P<d1>\d{{1,2}})(?:º|°|st|nd|rd|th)?\s+(?:de\s+)?(?P<m1>{m})(?:,?\s+(?:de\s+)?(?P<a1>\d{{4}}))?"
            rf"|(?P<m2>{m})\s+(?:(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s*)?(?:(?:de\s+)?(?P<a2>\d{{4}}))?"
            rf"|(?P<d3>\d{{1,2}})/(?P<m3>\d{{1,2}})/(?P<a3>\d{{4}})"
            rf"|(?P<a4>\d{{4}})-(?P<m4>\d{{2}})-(?P<d4>\d{{2}}))\b", re.I)
    out = set()
    for x in _DATA_DMA.finditer(texto or ""):
        g = x.groupdict()
        if g["m1"]:
            dia, mm, ano = g["d1"], mes(g["m1"]), g["a1"]
        elif g["m2"]:
            if not (g["d2"] or g["a2"]):
                continue  # mês sozinho ("may", "mar", "set") é palavra comum demais para ser data
            dia, mm, ano = g["d2"], mes(g["m2"]), g["a2"]
        elif g["m3"]:
            dia, mm, ano = g["d3"], int(g["m3"]), g["a3"]
        else:
            dia, mm, ano = g["d4"], int(g["m4"]), g["a4"]
        if not mm or not 1 <= int(mm) <= 12 or (dia and not 1 <= int(dia) <= 31):
            continue
        out.add((int(dia) if dia else None, int(mm), int(ano) if ano else None))
    return out


# ------------------------------------------------------------------ lugares
@lru_cache(maxsize=1)
def _indice() -> tuple[dict[str, str], dict[str, set[str]]]:
    """(forma dobrada -> id, id -> formas)."""
    por_forma, formas = {}, {}
    for lugar in _dados()["lugares"]:
        todas = set(lugar["formas"]) | set(_APELIDOS.get(lugar["id"], []))
        formas[lugar["id"]] = todas
        for f in todas:
            por_forma.setdefault(_dobrar(f), lugar["id"])
    return por_forma, formas


def lugar(nome: str) -> str | None:
    """Id do lugar (código de país, "BR-…", "US-…", "G-…") ou None."""
    return _indice()[0].get(_dobrar(nome))


def formas_do_lugar(nome: str) -> set[str]:
    """Todas as formas (pt/en/es, apelidos) do lugar; o próprio nome se não é lugar conhecido."""
    i = lugar(nome)
    return set(_indice()[1][i]) if i else {nome}


def lugares_no_texto(texto: str) -> set[str]:
    """Ids dos lugares citados no texto (a forma mais longa vence: "Rio Grande do Sul" antes de "Rio")."""
    alvo = " " + re.sub(r"[^\w]+", " ", _dobrar(texto)) + " "
    achados = set()
    for forma in sorted(_indice()[0], key=len, reverse=True):
        chave = " " + re.sub(r"[^\w]+", " ", forma).strip() + " "
        if len(chave.strip()) >= 3 and chave in alvo:
            achados.add(_indice()[0][forma])
            alvo = alvo.replace(chave, " ")
    return achados


def mesmo_lugar_no_texto(nome: str, texto: str) -> bool:
    """O lugar `nome` aparece em `texto` em qualquer idioma ("Síria" × "...in Syria...")."""
    i = lugar(nome)
    return bool(i) and i in lugares_no_texto(texto)
