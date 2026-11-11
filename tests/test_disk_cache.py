"""Cache em disco da busca e da extração (zero API: busca e LLM mockados)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import disk_cache, web_search  # noqa: E402


def test_cache_guarda_e_devolve(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    disk_cache.put("t", ["a", 1], [{"x": 1}])
    assert disk_cache.get("t", ["a", 1]) == [{"x": 1}]
    assert disk_cache.get("t", ["b", 1]) is None


def test_cache_vencido_nao_volta(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    disk_cache.put("t", "k", [1])
    assert disk_cache.get("t", "k", ttl_days=0) is None


def test_vazio_nao_entra(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    disk_cache.put("t", "k", [])
    assert disk_cache.get("t", "k") is None


def test_busca_repetida_nao_vai_a_internet(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    chamadas = []
    monkeypatch.setattr(web_search, "_tavily", lambda q, n: chamadas.append(q) or [{"titulo": "t", "url": "u", "trecho": "x"}])
    monkeypatch.setattr(web_search, "_enrich", lambda f, q: f)
    monkeypatch.setattr(web_search, "wikipedia_facts", lambda q: [])
    monkeypatch.setattr(web_search, "wikidata_facts", lambda q: [])
    monkeypatch.setattr(web_search, "openalex_facts", lambda q: [])
    primeira = web_search.search_topic_facts("Túnel Seikan")
    segunda = web_search.search_topic_facts("Túnel Seikan")
    assert primeira == segunda
    assert chamadas == ["Túnel Seikan"]  # a segunda veio do disco


def test_extracao_repetida_nao_chama_o_modelo(tmp_path, monkeypatch):
    from src import script_gen
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    chamadas = []
    fatos = [f"Fato {i} com 1{i} km" for i in range(10)]
    import json
    monkeypatch.setattr(script_gen, "complete", lambda *a, **k: chamadas.append(1) or json.dumps({"fatos": fatos}))
    monkeypatch.setattr(script_gen, "fatos_com_numeros_da_fonte", lambda f, fontes: f)
    fontes = [{"titulo": "F", "url": "u", "trecho": " ".join(fatos)}]
    a = script_gen.extract_verified_facts("Tema", fontes)
    b = script_gen.extract_verified_facts("Tema", fontes)
    assert a == b == fatos
    assert len(chamadas) == 1


def _sem_internet(monkeypatch, tmp_path, resultados):
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    chamadas = []
    monkeypatch.setattr(web_search, "_tavily", lambda q, n: chamadas.append(q) or resultados(q))
    monkeypatch.setattr(web_search, "_duckduckgo", lambda q, n: None)
    monkeypatch.setattr(web_search, "_enrich", lambda f, q: f)
    monkeypatch.setattr(web_search, "wikipedia_facts", lambda q: [])
    monkeypatch.setattr(web_search, "wikidata_facts", lambda q: [])
    monkeypatch.setattr(web_search, "openalex_facts", lambda q: [])
    monkeypatch.setattr(web_search, "_article_titles", lambda q: {"pt": ["Túnel Seikan"], "en": ["Seikan Tunnel"]})
    return chamadas


def test_fontes_oficiais_so_entram_com_dominio_oficial(monkeypatch, tmp_path):
    def resultados(q):
        if "official" in q or "oficial" in q:
            return [{"titulo": "Ministério", "url": "https://www.mlit.go.jp/a", "trecho": "53,85 km"},
                    {"titulo": "Blog", "url": "https://blog.com/a", "trecho": "65 km"}]
        return [{"titulo": "Notícia", "url": "https://g1.globo.com/a", "trecho": "x"}]
    _sem_internet(monkeypatch, tmp_path, resultados)
    fontes = web_search.search_topic_facts("Túnel Seikan", oficiais=True)
    urls = {f["url"]: f.get("oficial") for f in fontes}
    assert urls["https://www.mlit.go.jp/a"] is True
    assert "https://blog.com/a" not in urls  # resultado não oficial da busca oficial é descartado
    assert "https://g1.globo.com/a" in urls and not urls["https://g1.globo.com/a"]


def test_busca_sem_oficiais_nao_faz_consulta_extra(monkeypatch, tmp_path):
    chamadas = _sem_internet(monkeypatch, tmp_path, lambda q: [{"titulo": "t", "url": "https://a.com", "trecho": "x"}])
    web_search.search_topic_facts("Túnel Seikan")
    assert chamadas == ["Túnel Seikan"]


def test_cache_separa_busca_com_e_sem_oficiais(monkeypatch, tmp_path):
    chamadas = _sem_internet(monkeypatch, tmp_path, lambda q: [{"titulo": "t", "url": "https://x.gov.br/a", "trecho": "x"}])
    web_search.search_topic_facts("Tema")
    web_search.search_topic_facts("Tema", oficiais=True)
    web_search.search_topic_facts("Tema", oficiais=True)
    assert chamadas.count("Tema") == 2  # sem e com oficiais são buscas diferentes; a repetida veio do cache


def test_pipeline_pede_fontes_oficiais_no_fractal():
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "search_topic_facts(topic, oficiais=channel_name in FATOS_CANAIS)" in src
