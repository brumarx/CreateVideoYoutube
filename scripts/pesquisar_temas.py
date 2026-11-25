"""Pesquisa semanal de temas pelo que o público já provou (src/pesquisa_temas.py): outliers do YouTube via
yt-dlp (sem cota), demanda pelo autocompletar e pedidos nos comentários do nosso canal (2-3 unidades de cota).
Grava data/temas_pesquisados/<canal>.json, que o src/topics.pick_topic usa por 14 dias.

Uso: python scripts/pesquisar_temas.py [--canal curiosidades ...] [--rapido] [--max-buscas N] [--resultados N]
     [--max-canais N] [--top-com-data N] [--sem-comentarios]
Sem --canal: todo canal com `pesquisa_temas` no yaml.

Cron sugerido (NÃO instalado — 25/11), domingo 06:00, antes da rodada diária:
  0 6 * * 0 cd /home/brmx/Youtube && .venv/bin/python scripts/pesquisar_temas.py >> logs/pesquisa_temas.log 2>&1
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # antes de qualquer import de src (cron não põe o projeto no path)

from src import pesquisa_temas  # noqa: E402

log = logging.getLogger("pesquisar_temas")


def canais_configurados() -> list[str]:
    return sorted(p.stem for p in (ROOT / "channels").glob("*.yaml") if pesquisa_temas.config_do_canal(p.stem))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--canal", action="append", default=[], help="canal (repetível); padrão: todos configurados")
    ap.add_argument("--rapido", action="store_true", help="autocompletar sem as 26 letras (só termo e perguntas)")
    ap.add_argument("--max-buscas", type=int, default=None, help="usa só as N primeiras buscas do yaml")
    ap.add_argument("--resultados", type=int, default=30, help="resultados por busca (padrão 30)")
    ap.add_argument("--max-canais", type=int, default=pesquisa_temas.MAX_CANAIS, help="canais lidos por rodada")
    ap.add_argument("--top-com-data", type=int, default=pesquisa_temas.TOP_COM_DATA,
                    help="outliers que ganham data de envio e views_por_dia")
    ap.add_argument("--wiki-top", type=int, default=10, help="candidatos conferidos na Wikipédia")
    ap.add_argument("--sem-comentarios", action="store_true", help="não lê comentários (não gasta cota da API)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    canais = args.canal or canais_configurados()
    falhas = 0
    for canal in canais:
        cfg = pesquisa_temas.config_do_canal(canal)
        if not cfg:
            log.warning("[%s] sem `pesquisa_temas` no yaml — pulado", canal)
            continue
        comentarios = None if args.sem_comentarios else pesquisa_temas.ideias_dos_comentarios(canal)
        try:
            r = pesquisa_temas.pesquisar_canal(
                canal, cfg, resultados=args.resultados, max_buscas=args.max_buscas, max_canais=args.max_canais,
                rapido=args.rapido, top_com_data=args.top_com_data, wiki_top=args.wiki_top, comentarios=comentarios)
        except Exception:  # noqa: BLE001 — um canal com problema não para os outros
            log.exception("[%s] pesquisa falhou", canal)
            falhas += 1
            continue
        cands = r["candidatos"]
        log.info("[%s] %d candidatos (%d muito fortes) → %s", canal, len(cands),
                 sum(c["muito_forte"] for c in cands), pesquisa_temas.DIR / f"{canal}.json")
        for c in cands[:10]:
            log.info("  %6.1fx  rank %6.1f  %9d views  dem %2d  %s  [%s]", c["score"], c["rank"], c["views"],
                     c["demanda"], c["assunto"], c["canal"])
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
