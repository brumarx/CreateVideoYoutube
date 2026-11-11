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


@pytest.fixture(autouse=True)
def isolamento(tmp_path, monkeypatch):
    """Teste não depende do cache real do projeto (data/cache) nem espera a pausa de 90 s que a
    cascata faz quando um provedor cai: sem isso, a suíte levava 9 s ou 157 s conforme o cache real
    tivesse ou não a busca guardada (11/11)."""
    from src import disk_cache, providers
    monkeypatch.setattr(disk_cache, "BASE", tmp_path / "cache_teste")
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
