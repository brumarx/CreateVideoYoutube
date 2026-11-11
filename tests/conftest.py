"""Configuração comum dos testes: nada de chamada de IA ou internet escondida."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def assunto_sem_rede(monkeypatch):
    """O nome do assunto (título da Wikipédia) viria de uma chamada de IA; nos testes é a parte do
    tema antes dos dois-pontos."""
    from src import script_gen
    monkeypatch.setattr(script_gen, "_assunto_do_tema", lambda topic: (topic or "").split(":")[0].strip())
