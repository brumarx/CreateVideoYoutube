"""26/11: passa tudo o que já foi produzido (roteiros publicados + temas da fila, todos os canais) pelas tabelas
centrais e mostra o que elas NÃO cobrem — para alimentá-las com casos reais, não com palpite:

- números com unidade que o leitor único (src/unidades.py) não entende;
- siglas (para a tabela de siglas: soletrar ou ler como palavra);
- nomes próprios (para a pronúncia central, src/pronuncia.py) e quais deles já são lugares do vocabulário;
- palavras do léxico de afirmação (absolutos, causais, garantia) — frequência real.

Uso: scripts/minerar_corpus.py [--classificar-nomes]  (a opção pergunta ao modelo a pronúncia dos nomes que a
tabela não conhece e os guarda; sem ela, nenhuma chamada)."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src import vocabulario  # noqa: E402
from src.fatos import _pares, extenso_para_algarismos  # noqa: E402
from src.pronuncia import _carregar as pronuncias, nomes_da_narracao  # noqa: E402
from src.unidades import quantidades  # noqa: E402

_NUM_COM_PALAVRA = re.compile(r"(?<![\w.,])[−-]?\d+(?:[.,]\d+)*\s?(?:°\s?[CF]?|%|[a-zA-ZÀ-ÿµ²³/]+)")
_SIGLA = re.compile(r"\b[A-ZÀ-Ý]{2,}[0-9]?s?\b")
# palavra comum depois de número que não é unidade (não é falha do leitor)
_NAO_UNIDADE = {"anos", "ano", "dias", "dia", "horas", "hora", "minutos", "segundos", "mil", "milhões", "milhão", "bilhões",
                "bilhão", "de", "e", "a", "o", "em", "vezes", "pessoas", "x", "mortos", "gols", "jogos", "pontos", "meses",
                "mês", "semanas", "séculos", "metros", "vitórias", "partidas", "rodadas", "reais", "dólares", "euros",
                "por", "do", "da", "no", "na", "para", "as", "os", "deputados", "votos", "países", "km", "milhões", "bilhões",
                "não", "histórias", "fatos", "com", "contra", "parlamentares", "jogadores", "passes", "casos", "coisas",
                "linhas", "passos", "senadores", "gastos", "cirurgias", "finalizações", "vitórias", "mensais", "pelo"}
_LEXICO = re.compile(r"\b(sempre|nunca|jamais|único|única|primeir[oa]|todos|todas|garante|garantiu|assegura|protege|"
                     r"salvou|resolve|prova|provou|porque|por isso|graças|já|total|totalmente|maior do mundo|"
                     r"mais \w+ do mundo|acredita-se|talvez|provavelmente)\b", re.I)


def corpus() -> list[tuple[str, str, str]]:
    """(canal, tipo, texto)."""
    out = []
    for f in sorted((RAIZ / "data" / "published_scripts").glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out.append((f.parent.name, "titulo", str(d.get("title") or "")))
        narr = d.get("narration")
        out.append((f.parent.name, "narracao", " ".join(narr) if isinstance(narr, list) else str(narr or "")))
    con = sqlite3.connect(RAIZ / "data" / "queue.db")
    for canal, tema in con.execute("select channel, topic from jobs"):
        out.append((canal, "tema", tema or ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classificar-nomes", action="store_true")
    a = ap.parse_args()
    textos = corpus()
    print(f"corpus: {len(textos)} textos ({sum(t == 'narracao' for _, t, _ in textos)} narrações)")

    unidades_falhas, siglas, nomes, lexico = Counter(), Counter(), Counter(), Counter()
    comuns = {w for _, _, t in textos for w in re.findall(r"\b[a-zà-ÿ][\w-]*", t)}
    for _canal, tipo, texto in textos:
        t = extenso_para_algarismos(texto)
        lidas = {q["numero"] for q in quantidades(t)} | {v for v, _ in _pares(t)}
        for m in _NUM_COM_PALAVRA.finditer(t):
            bruto = m.group(0)
            palavra = re.sub(r"^[−-]?[\d.,\s]+", "", bruto).strip().lower()
            num = re.match(r"[−-]?[\d.,]+", bruto).group(0)  # type: ignore[union-attr]
            if palavra in _NAO_UNIDADE or not palavra:
                continue
            if not any(num.lstrip("−-") in str(x) or str(x) in num for x in lidas):
                unidades_falhas[palavra] += 1
        siglas.update(_SIGLA.findall(texto))
        if tipo != "tema":
            nomes.update(nomes_da_narracao(texto, comuns))
        lexico.update(x.lower() for x in _LEXICO.findall(texto))

    print("\n== números com 'unidade' que o leitor único não leu (palavra: vezes)")
    print(", ".join(f"{p}: {n}" for p, n in unidades_falhas.most_common(40)) or "-")
    print(f"\n== siglas ({len(siglas)})")
    print(", ".join(f"{s}: {n}" for s, n in siglas.most_common(80)))
    tabela = pronuncias()
    lugares = {n for n in nomes if vocabulario.lugar(n)}
    sem = [n for n, _ in nomes.most_common() if n not in tabela and n not in lugares]
    print(f"\n== nomes próprios: {len(nomes)} distintos; {len(lugares)} já são lugares do vocabulário; "
          f"{len([n for n in nomes if n in tabela])} já na pronúncia central; {len(sem)} sem classificação")
    print("mais frequentes sem classificação:", ", ".join(sem[:60]))
    print("\n== léxico de afirmação (frequência real)")
    print(", ".join(f"{p}: {n}" for p, n in lexico.most_common()))

    if a.classificar_nomes and sem:
        from src.pronuncia import classificar
        for k in range(0, len(sem), 60):
            classificar(sem[k:k + 60])
        print(f"\nclassificados: {len([n for n in sem if n in pronuncias()])} de {len(sem)}")


if __name__ == "__main__":
    main()
