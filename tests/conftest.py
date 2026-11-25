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
def isolamento(request, tmp_path, monkeypatch):
    """Teste não depende do cache real do projeto (data/cache) nem espera a pausa de 90 s que a
    cascata faz quando um provedor cai: sem isso, a suíte levava 9 s ou 157 s conforme o cache real
    tivesse ou não a busca guardada (11/11)."""
    from src import disk_cache, providers
    monkeypatch.setattr(disk_cache, "BASE", tmp_path / "cache_teste")
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    from src import web_search
    monkeypatch.setattr(web_search, "referencias_da_wikipedia", lambda *a, **k: [])  # 25/11: abre dezenas de páginas
    monkeypatch.setattr(web_search, "textos_via_tavily", lambda urls: {})  # 25/11: reserva paga cota do Tavily
    from src import redacao
    monkeypatch.setattr(redacao, "_GANCHOS_USADOS", tmp_path / "ganchos_usados.json")  # 25/11: rodízio do gancho
    if not request.node.get_closest_marker("ritmo"):
        monkeypatch.setattr(redacao, "ritmo_de_referencia", lambda canal: "")  # 25/11: lê o queue.db real


def pytest_configure(config):
    config.addinivalue_line("markers", "plano: o teste usa o plano da história de verdade (redacao.planejar_historia)")
    config.addinivalue_line("markers", "conferencia: o teste usa a conferência na escrita de verdade (redacao.conferir_na_escrita)")
    config.addinivalue_line("markers", "ritmo: o teste usa redacao.ritmo_de_referencia de verdade")
    config.addinivalue_line("markers", "revisao: o teste usa a revisão das afirmações de verdade (evidencias.revisar_afirmacoes)")


@pytest.fixture(autouse=True)
def sem_plano_da_historia(request, monkeypatch):
    """23/11: o plano da história é uma chamada de IA antes do escritor; os testes do escritor simulam só as
    respostas dele. Quem testa o plano marca @pytest.mark.plano."""
    if request.node.get_closest_marker("plano"):
        return
    from src import redacao
    monkeypatch.setattr(redacao, "planejar_historia", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def conferencia_na_escrita_simulada(request, monkeypatch):
    """25/11: cada versão do escritor passa pela conferência contra os trechos (redacao.conferir_na_escrita: até
    3 chamadas de IA). Os testes do escritor simulam só as respostas dele: aqui a conferência aprova tudo.
    Quem testa a conferência de verdade marca @pytest.mark.conferencia."""
    if request.node.get_closest_marker("conferencia"):
        return
    from src import redacao
    monkeypatch.setattr(redacao, "conferir_na_escrita", lambda script, banco, topic: (script, []))
    monkeypatch.setattr(redacao, "conferir_edicao", lambda *a, **k: None)  # 25/11: a mesma conferência na edição


@pytest.fixture(autouse=True)
def revisao_das_afirmacoes_simulada(request, monkeypatch):
    """25/11: o banco escolhido para o roteiro passa pela revisão das afirmações (evidencias.revisar_afirmacoes:
    chamadas ao juiz fixo). Os testes do pipeline simulam só as outras respostas: aqui a revisão devolve o banco
    como veio. Quem testa a revisão de verdade marca @pytest.mark.revisao."""
    if request.node.get_closest_marker("revisao"):
        return
    from src import evidencias
    monkeypatch.setattr(evidencias, "revisar_afirmacoes", lambda banco, topic, ids=None: banco)
