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
