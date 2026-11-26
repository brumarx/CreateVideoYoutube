"""26/11: repete a redação (escrita + auditoria) de uma rodada gravada, sem chamar nenhum modelo.

  PYTHONPATH=. .venv/bin/python scripts/repetir_redacao.py JOB [--mista]

Usa data/gravacoes/partida_JOB.json (canal, tema, durações, banco) e as chamadas gravadas em
data/gravacoes/*.jsonl. Pedido que o código novo faz diferente (prompt mudou) não está na gravação: sem --mista
a repetição para ali e diz qual etapa mudou; com --mista ele vai ao modelo de verdade (e fica gravado).
A busca de evidência na web durante a edição fica desligada (o resultado da web muda de um dia para outro).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--mista", action="store_true", help="pedido fora da gravação vai ao modelo de verdade")
    args = ap.parse_args()
    from src import gravacao
    partida = json.loads((gravacao.DIR / f"partida_{args.job}.json").read_text(encoding="utf-8"))
    os.environ["GRAVACAO_REPETIR"] = ":".join(str(p) for p in sorted(gravacao.DIR.glob("*.jsonl")))
    if args.mista:
        os.environ["GRAVACAO_MISTA"] = "1"
    else:
        os.environ["GRAVACAO_DESLIGADA"] = "1"  # repetição pura não grava de novo
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from src.config import ChannelConfig
    from src.redacao import RoteiroReprovado, auditar_e_corrigir, escrever
    from scripts.run_pipeline import _min_palavras_redacao
    canal = ChannelConfig.load(partida["canal"])
    banco = partida["banco"]
    try:
        script = escrever(canal, partida["tema"], banco, min_minutes=partida["min_minutes"],
                          max_minutes=partida["max_minutes"], cenas_ref=partida["scenes"])
        script, rel = auditar_e_corrigir(script, banco, partida["tema"], web_facts=None, buscar_evidencia=None,
                                         min_palavras=_min_palavras_redacao(partida["min_minutes"]))
    except gravacao.SemGravacao as exc:
        print(f"REPETIÇÃO PAROU: {exc}")
        return 2
    except RoteiroReprovado as exc:
        print(f"REPROVADO: {exc}")
        return 1
    print(f"APROVADO: versão {rel['versao_final']}, editorial {rel['qualidade']['nota']:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
