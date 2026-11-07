"""Trava única de geração de vídeo: rodada diária, painel (rodar, aprovar,
reenviar) e reenvios disputam a mesma trava. Quem chega com ela ocupada não
inicia outro processo: o painel avisa, e o roteiro aprovado espera a rodada."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parent.parent / "data" / "pipeline.lock"


def try_acquire():
    """Devolve o handle da trava (segure até o fim) ou None se já está ocupada."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def is_busy() -> bool:
    handle = try_acquire()
    if handle is None:
        return True
    handle.close()
    return False
