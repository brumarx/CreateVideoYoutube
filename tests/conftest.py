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


def pytest_configure(config):
    config.addinivalue_line("markers", "plano: o teste usa o plano da história de verdade (redacao.planejar_historia)")
    config.addinivalue_line("markers", "frases_da_fonte: o teste usa as frases da fonte de verdade (redacao.frases_da_fonte)")


@pytest.fixture(autouse=True)
def sem_plano_da_historia(request, monkeypatch):
    """23/11: o plano da história é uma chamada de IA antes do escritor; os testes do escritor simulam só as
    respostas dele. Quem testa o plano marca @pytest.mark.plano."""
    if request.node.get_closest_marker("plano"):
        return
    from src import redacao
    monkeypatch.setattr(redacao, "planejar_historia", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def frases_da_fonte_simuladas(request, monkeypatch):
    """24/11: o escritor monta o roteiro com FRASES DA FONTE já conferidas (redacao.frases_da_fonte: duas
    chamadas de IA antes da montagem). Os testes antigos do escritor simulam só a resposta dele: aqui cada
    evidência utilizável vira a própria frase, e a resposta no formato antigo (frases com texto) é aceita como
    montagem. Quem testa as frases da fonte de verdade marca @pytest.mark.frases_da_fonte."""
    if request.node.get_closest_marker("frases_da_fonte"):
        return
    from src import redacao
    monkeypatch.setattr(redacao, "FRASES_FONTE_MIN", 1)
    monkeypatch.setattr(redacao, "frases_da_fonte", lambda topic, banco, ids: [
        {"ref": f"F{k}", "id": e["id"], "texto": e["afirmacao"]} for k, e in enumerate(redacao.usaveis(banco), 1)])
    original = redacao.montar_de_frases

    def montar(data, frases, banco, nomes):
        formato_antigo = any(isinstance(x, dict) and "texto" in x
                             for c in data.get("cenas") or [] if isinstance(c, dict) for x in c.get("frases") or [])
        return redacao.de_json(data, banco) if formato_antigo else original(data, frases, banco, nomes)
    monkeypatch.setattr(redacao, "montar_de_frases", montar)
