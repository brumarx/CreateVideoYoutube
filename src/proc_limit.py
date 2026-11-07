"""Execução com tempo limite que mata a ÁRVORE de processos: o pipeline abre
ffmpeg, TTS e outros filhos; matar só o Python deixava tudo rodando. Usa um
grupo de processos próprio e mata o grupo inteiro (SIGTERM, depois SIGKILL)."""
from __future__ import annotations

import logging
import os
import signal
import subprocess

log = logging.getLogger("proc_limit")


def run_limited(cmd: list[str], timeout: int, cwd: str | None = None) -> int:
    """Devolve o código de saída; 124 se estourou o tempo (tudo morto)."""
    proc = subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("passou de %d min, matando o grupo de processos: %s", timeout // 60, " ".join(cmd[-4:]))
        _kill_group(proc.pid)
        proc.wait()
        return 124


def _kill_group(pgid: int) -> None:
    """SIGTERM ao grupo, 3 s pra fechar limpo, depois SIGKILL no que sobrou."""
    import time

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(3)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
