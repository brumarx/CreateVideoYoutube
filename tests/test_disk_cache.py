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
    monkeypatch.setattr(web_search, "_enrich", lambda f, q, *a: f)
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
    monkeypatch.setattr(web_search, "_enrich", lambda f, q, *a: f)
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


def test_busca_do_fractal_traz_outros_idiomas_e_oficiais_primeiro(monkeypatch, tmp_path):
    def resultados(q):
        return {
            "Túnel Seikan": [{"titulo": "Notícia pt", "url": "https://g1.globo.com/a", "trecho": "x"}],
            "Seikan Tunnel": [{"titulo": "News en", "url": "https://www.bbc.co.uk/a", "trecho": "53.85 km"}],
            "青函トンネル": [{"titulo": "記事", "url": "https://www.nikkei.com/a", "trecho": "53.85km"}],
            "Seikan Tunnel official": [{"titulo": "JRTT", "url": "https://www.jrtt.go.jp/a", "trecho": "53.85km"}],
        }.get(q, [])
    chamadas = _sem_internet(monkeypatch, tmp_path, resultados)
    monkeypatch.setattr(web_search, "_article_titles", lambda q: {"pt": ["Túnel Seikan"], "en": ["Seikan Tunnel"], "local": ["青函トンネル"]})
    fontes = web_search.search_topic_facts("Túnel Seikan", oficiais=True)
    urls = [f["url"] for f in fontes]
    assert urls[0] == "https://www.jrtt.go.jp/a"                        # oficial primeiro
    assert {"https://www.bbc.co.uk/a", "https://www.nikkei.com/a", "https://g1.globo.com/a"} <= set(urls)
    assert "青函トンネル" in chamadas and "Seikan Tunnel" in chamadas    # buscou em japonês e em inglês
    assert len(urls) == len(set(urls))                                  # sem repetir fonte


def test_fonte_em_japones_confirma_fato_em_portugues():
    from src import fatos as ft
    fontes = [{"titulo": "Wiki", "url": "https://pt.wikipedia.org/a", "trecho": "O túnel tem 53,85 km."},
              {"titulo": "日経", "url": "https://www.nikkei.com/a", "trecho": "青函トンネルの全長は53.85km。"}]
    fatos = ft.atribuir([{"texto": "O túnel tem 53,85 km.", "fontes": ["S1"]}], fontes)
    assert fatos[0]["status"] == "confirmado"  # dois publicadores, idiomas diferentes, mesmo número


def test_bloco_limita_cada_fonte_e_mantem_as_primeiras():
    from src import fatos as ft
    fontes = [{"titulo": "Oficial", "url": "https://x.go.jp/a", "trecho": "A" * 9000}] + \
             [{"titulo": f"F{i}", "url": f"https://s{i}.com", "trecho": "53,85 km"} for i in range(3)]
    bloco = ft.bloco_numerado(fontes)
    assert bloco.startswith("[S1] Oficial") and "[S4]" in bloco


def test_versao_da_busca_entra_na_chave_do_cache(monkeypatch, tmp_path):
    chamadas = _sem_internet(monkeypatch, tmp_path, lambda q: [{"titulo": "t", "url": "https://a.com", "trecho": "x"}])
    web_search.search_topic_facts("Tema")
    monkeypatch.setattr(web_search, "BUSCA_VERSAO", web_search.BUSCA_VERSAO + 1)
    web_search.search_topic_facts("Tema")
    assert chamadas.count("Tema") == 2  # versão nova da busca não reaproveita o cache da anterior


def test_pdf_e_lido_pelo_pdftotext(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    chamada = {}

    def fake_run(args, **kw):
        chamada["args"] = args
        return SimpleNamespace(stdout="Seikan Tunnel 53.85 km opened 1988".encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert web_search._pdf_text(b"%PDF-1.4 conteudo") == "Seikan Tunnel 53.85 km opened 1988"
    assert chamada["args"][0] == "pdftotext" and "-l" in chamada["args"]


def test_conteudo_que_nao_e_pdf_nao_vai_ao_pdftotext(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("não devia chamar")))
    assert web_search._pdf_text(b"<html>nao sou pdf</html>") == ""


def test_trecho_maior_na_busca_do_fractal(monkeypatch):
    texto = " ".join(f"Frase {i} sobre o túnel Seikan com detalhes da obra e números." for i in range(200))
    monkeypatch.setattr(web_search, "_page_text", lambda url: texto)
    curto = web_search._enrich([{"titulo": "t", "url": "u", "trecho": "x"}], "túnel Seikan")
    longo = web_search._enrich([{"titulo": "t", "url": "u", "trecho": "x"}], "túnel Seikan", web_search.TRECHO_FATOS)
    assert len(curto[0]["trecho"]) <= web_search.ENRICHED_CHARS < len(longo[0]["trecho"]) <= web_search.TRECHO_FATOS


def test_trecho_de_pagina_em_japones_aproveita_frases_com_numero():
    pagina = "青函トンネルは全長53.85kmの鉄道トンネルである。1988年3月13日に開業した。トンネルの説明が続く文章がここにある。"
    trecho = web_search._best_passage(pagina, {"seikan"}, 3000, com_numeros=True)
    assert "53.85km" in trecho and "1988年" in trecho


def test_sem_modo_fatos_a_selecao_continua_por_palavra_chave():
    pagina = "O túnel Seikan liga duas ilhas japonesas pelo fundo do mar. Em 1988 houve outra coisa sem relação nenhuma aqui."
    trecho = web_search._best_passage(pagina, {"seikan"}, 800)
    assert "Seikan" in trecho and "1988" not in trecho


def test_mudanca_no_codigo_invalida_o_cache_sozinha(monkeypatch, tmp_path):
    chamadas = _sem_internet(monkeypatch, tmp_path, lambda q: [{"titulo": "t", "url": "https://a.com", "trecho": "x"}])
    web_search.search_topic_facts("Tema")
    monkeypatch.setattr(disk_cache, "versao_do_codigo", lambda *a: "outro-codigo")
    web_search.search_topic_facts("Tema")
    assert chamadas.count("Tema") == 2


def test_impressao_digital_muda_com_o_arquivo(tmp_path, monkeypatch):
    import src.disk_cache as dc
    a = dc.versao_do_codigo("fatos.py")
    b = dc.versao_do_codigo("fatos.py")
    c = dc.versao_do_codigo("web_search.py")
    assert a == b and a != c
