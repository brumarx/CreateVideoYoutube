"""Roda um comando do pipeline só se a trava única estiver livre (painel e
reenvios usam isto). Ocupada: sai com código 3 sem iniciar nada.
Uso: python scripts/run_locked.py <timeout_s> <comando...>"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.runlock import try_acquire  # noqa: E402
from src.proc_limit import run_limited  # noqa: E402


def main() -> int:
    handle = try_acquire()
    if handle is None:
        print("trava ocupada: outra geração de vídeo está rodando")
        return 3
    timeout = int(sys.argv[1])
    rc = run_limited(sys.argv[2:], timeout)
    handle.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
