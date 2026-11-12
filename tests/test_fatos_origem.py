"""Fatos com origem (src/fatos.py): classificação da fonte, atribuição, conferência por frase e
ajuste do texto final. Zero API."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fatos as ft  # noqa: E402

OFICIAL = {"titulo": "MLIT", "url": "https://www.mlit.go.jp/x", "trecho": "Orçamento de planejamento do túnel: 538,4 bilhões de ienes. Extensão de 53,85 km."}
JR = {"titulo": "JR Hokkaido", "url": "https://www.jrhokkaido.co.jp/y", "trecho": "Extensão 53,85 km, dos quais 23,30 km submarinos.", "oficial": True}
WIKI_PT = {"titulo": "Wikipédia", "url": "https://pt.wikipedia.org/wiki/T", "trecho": "Morreram 34 operários. Cerca de 50 trens de carga por dia."}
WIKI_EN = {"titulo": "Wikipedia", "url": "https://en.wikipedia.org/wiki/T", "trecho": "34 workers died."}
BLOG = {"titulo": "Blog", "url": "https://blog.exemplo.com/z", "trecho": "O museu fechou em 2006."}
BRIT = {"titulo": "Britannica", "url": "https://www.britannica.com/x", "trecho": "34 workers died."}
FONTES = [OFICIAL, JR, WIKI_PT, BRIT, BLOG]


# ---------- classificação da fonte
@pytest.mark.parametrize("url,esperado", [
    ("https://www.mlit.go.jp/a", "oficial"), ("https://www.gov.br/a", "oficial"), ("https://data.gov/a", "oficial"),
    ("https://www.gob.mx/a", "oficial"), ("https://europa.eu/a", "oficial"), ("https://mit.edu/a", "oficial"),
    ("https://pt.wikipedia.org/a", "secundaria"), ("https://www.jrhokkaido.co.jp/a", "secundaria"),
    ("https://g1.globo.com/a", "secundaria"), ("", "secundaria"),
])
def test_classificar_fonte_pelo_dominio(url, esperado):
    assert ft.classificar_fonte({"url": url}) == esperado


def test_marca_do_arquivo_vale_sobre_o_dominio():
    assert ft.classificar_fonte({"url": "https://www.jrhokkaido.co.jp/a", "oficial": True}) == "oficial"
    assert ft.classificar_fonte({"url": "https://www.mlit.go.jp/a", "oficial": False}) == "secundaria"


# ---------- números
@pytest.mark.parametrize("a,b", [("23,30", "23,3"), ("23,3", "23.3"), ("6.900", "6900"), ("538,4", "538.4"), ("1988", "1988.0")])
def test_canon_iguala_formatos(a, b):
    assert ft.canon(a) == ft.canon(b)


def test_canon_nao_confunde_valores_diferentes():
    assert ft.canon("53,85") != ft.canon("65,23")
    assert ft.canon("23,3") != ft.canon("233")


# ---------- atribuição
def test_atribuir_descarta_fato_com_numero_fora_das_fontes():
    fatos = ft.atribuir([{"texto": "O túnel tem 65,23 km.", "fontes": ["S1"]}], FONTES)
    assert fatos == []


def test_atribuir_status_oficial_confirmado_secundario():
    fatos = ft.atribuir([
        {"texto": "O orçamento de planejamento foi de 538,4 bilhões de ienes.", "fontes": ["S1"]},
        {"texto": "Morreram 34 operários.", "fontes": ["S3"]},
        {"texto": "O museu fechou em 2006.", "fontes": ["S5"]},
    ], FONTES)
    assert [f["status"] for f in fatos] == ["oficial", "confirmado", "secundario"]
    assert [f["id"] for f in fatos] == ["F1", "F2", "F3"]


def test_atribuir_usa_os_numeros_e_nao_so_a_citacao_do_modelo():
    # o modelo citou a fonte errada (S5), mas 23,3 km está na JR (oficial): vale a fonte que tem o número
    fatos = ft.atribuir([{"texto": "O trecho submarino tem 23,3 km.", "fontes": ["S5"]}], FONTES)
    assert fatos[0]["status"] == "oficial" and fatos[0]["fontes"] == [JR["url"]]


def test_atribuir_fato_sem_numero_usa_as_fontes_citadas():
    fatos = ft.atribuir([{"texto": "O túnel liga Honshu a Hokkaido.", "fontes": ["S2"]}], FONTES)
    assert fatos[0]["status"] == "oficial"
    sem_citacao = ft.atribuir([{"texto": "O túnel liga Honshu a Hokkaido.", "fontes": []}], FONTES)
    assert sem_citacao[0]["status"] == "secundario"


def test_atribuir_ignora_citacao_invalida():
    fatos = ft.atribuir([{"texto": "Extensão de 53,85 km.", "fontes": ["S99", "lixo", None]}], FONTES)
    assert fatos[0]["status"] == "oficial"


# ---------- conferência por frase (depois do escritor)
FATOS = ft.atribuir([
    {"texto": "Extensão de 53,85 km.", "fontes": ["S2"]},
    {"texto": "O trecho submarino tem 23,30 km.", "fontes": ["S2"]},
    {"texto": "Morreram 34 operários.", "fontes": ["S3"]},
    {"texto": "O museu fechou em 2006.", "fontes": ["S5"]},
    {"texto": "Orçamento de planejamento do túnel: 538,4 bilhões de ienes.", "fontes": ["S1"]},
], FONTES)


def _rot(*narr):
    return {"title": "", "scenes": [{"narration": n} for n in narr]}


def test_marcas_saem_da_narracao_e_ficam_na_cena():
    out = ft.conferir_frases(_rot("O túnel tem 53,85 km [F1]. Sob o mar são 23,3 km [F2]."), FATOS)
    assert out["scenes"][0]["narration"] == "O túnel tem 53,85 km. Sob o mar são 23,3 km."
    assert out["scenes"][0]["fatos"] == ["F1", "F2"]


def test_numero_que_nao_esta_em_fato_nenhum_tira_a_frase():
    out = ft.conferir_frases(_rot("O túnel tem 65,23 km [F1]. Sob o mar são 23,3 km [F2]."), FATOS)
    assert out["scenes"][0]["narration"] == "Sob o mar são 23,3 km."


def test_marca_errada_e_corrigida_pelo_numero():
    out = ft.conferir_frases(_rot("Sob o mar são 23,3 km [F1]."), FATOS)
    assert out["scenes"][0]["fatos"] == ["F2"]


def test_fato_secundario_ganha_estima_se_na_oracao_do_numero():
    out = ft.conferir_frases(_rot("Elas serviam de abrigo, e o museu fechou em 2006 [F4]."), FATOS)
    assert out["scenes"][0]["narration"] == "Elas serviam de abrigo, e estima-se que o museu fechou em 2006."


def test_fato_confirmado_nao_ganha_estima_se():
    out = ft.conferir_frases(_rot("Durante a obra, 34 operários morreram [F3]."), FATOS)
    assert out["scenes"][0]["narration"] == "Durante a obra, 34 operários morreram."


def test_estima_se_nao_duplica():
    out = ft.conferir_frases(_rot("Estima-se que o museu fechou em 2006 [F4]."), FATOS)
    assert out["scenes"][0]["narration"] == "Estima-se que o museu fechou em 2006."


def test_pergunta_e_gancho_sem_marca_ficam():
    out = ft.conferir_frases(_rot("E aí, mente curiosa! Você teria coragem de atravessar?"), FATOS)
    assert out["scenes"][0]["narration"] == "E aí, mente curiosa! Você teria coragem de atravessar?"


def test_frase_com_numero_sem_marca_e_atribuida_pelo_numero():
    out = ft.conferir_frases(_rot("O túnel tem 53,85 km de extensão."), FATOS)
    assert out["scenes"][0]["fatos"] == ["F1"]


def test_cena_que_fica_vazia_sai():
    out = ft.conferir_frases(_rot("O túnel tem 65,23 km.", "Sob o mar são 23,3 km."), FATOS)
    assert len(out["scenes"]) == 1


def test_sem_fatos_nao_mexe():
    roteiro = _rot("Qualquer texto 999 [F1].")
    assert ft.conferir_frases(roteiro, []) == roteiro


# ---------- texto final (depois da validação)
def test_conversao_sem_fato_sai_e_orcamento_vira_estimado():
    out = ft.ajustar_texto_final({**_rot("O custo total chegou a 538,4 bilhões de ienes, o equivalente a 3,6 bilhões de dólares."), "_fatos": FATOS})
    assert out["scenes"][0]["narration"] == "O orçamento estimado era de 538,4 bilhões de ienes."


def test_texto_final_reconfere_frase_reescrita_pelo_conserto():
    out = ft.ajustar_texto_final({**_rot("Desde 2014 locomotivas novas operam ali. O museu fechou em 2006."), "_fatos": FATOS})
    assert out["scenes"][0]["narration"] == "Estima-se que o museu fechou em 2006."


def test_texto_final_sem_fatos_nao_mexe():
    roteiro = _rot("Desde 2014 locomotivas novas operam ali.")
    assert ft.ajustar_texto_final(roteiro) == roteiro


# ---------- integração no gerador (sem LLM)
def test_finaliza_confere_frases_e_nao_chama_a_correcao_por_ia(monkeypatch):
    from src import script_gen as sg

    def nao_pode(*a, **k):
        raise AssertionError("não devia chamar a IA: conferir_frases já resolveu os números")

    monkeypatch.setattr(sg, "complete", nao_pode)
    roteiro = {"title": "t", "scenes": [{"narration": "O túnel tem 53,85 km [F1]. Tinha 65,23 km [F1]."}]}
    out = sg._finaliza(roteiro, [f["texto"] for f in FATOS], FATOS)
    assert out["scenes"][0]["narration"] == "O túnel tem 53,85 km."
    assert out["_fatos"] == FATOS and out["_fatos_verificados"] == [f["texto"] for f in FATOS]


def test_extracao_com_origem_e_cache(monkeypatch, tmp_path):
    from src import disk_cache, script_gen as sg
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    chamadas = []
    brutos = [{"texto": "Extensão de 53,85 km.", "fontes": ["S2"]}] * 9

    def fake(messages, **kw):
        chamadas.append(messages[0]["content"])
        return json.dumps({"fatos": brutos})

    monkeypatch.setattr(sg, "complete", fake)
    a = sg.extract_facts("Seikan", FONTES)
    b = sg.extract_facts("Seikan", FONTES)
    assert a == b and len(a) == 9 and a[0]["status"] == "oficial"
    assert len(chamadas) == 1
    assert "[S1]" in chamadas[0] and "[S2]" in chamadas[0]  # fontes numeradas para a citação


@pytest.mark.parametrize("url", ["https://gov.br/a", "https://www.planalto.gov.br/a", "https://ibge.gov.br/a",
                                 "https://www.mext.go.jp/a", "https://www.ox.ac.uk/a", "https://www.who.int/a"])
def test_mais_dominios_oficiais(url):
    assert ft.classificar_fonte({"url": url}) == "oficial"


@pytest.mark.parametrize("url", ["https://www.governo-falso.com/a", "https://gov.example.com/a", "https://www.golang.org/a",
                                 "https://blog.gov.example.net/a"])
def test_dominio_parecido_nao_e_oficial(url):
    assert ft.classificar_fonte({"url": url}) == "secundaria"


WIKI_65 = {"titulo": "Wikipédia pt", "url": "https://pt.wikipedia.org/wiki/T", "trecho": "O túnel tem 65,23 km de extensão."}


def test_secundario_que_contradiz_oficial_na_mesma_medida_sai():
    fatos = ft.atribuir([
        {"texto": "Extensão de 53,85 km.", "fontes": ["S1"]},
        {"texto": "O túnel tem 65,23 km de extensão.", "fontes": ["S2"]},
    ], [JR, WIKI_65])
    assert [f["texto"] for f in fatos] == ["Extensão de 53,85 km."]


def test_medidas_diferentes_do_mesmo_tunel_ficam():
    fatos = ft.atribuir([
        {"texto": "Extensão de 53,85 km.", "fontes": ["S1"]},
        {"texto": "O trecho submarino tem 23,30 km.", "fontes": ["S1"]},
        {"texto": "Morreram 34 operários.", "fontes": ["S3"]},
    ], [JR, WIKI_65, WIKI_PT])
    assert len(fatos) == 3


def test_ids_sao_renumerados_depois_do_descarte():
    fatos = ft.atribuir([
        {"texto": "O túnel tem 65,23 km de extensão.", "fontes": ["S2"]},
        {"texto": "Extensão de 53,85 km.", "fontes": ["S1"]},
    ], [JR, WIKI_65])
    assert [f["id"] for f in fatos] == ["F1"]


def test_mesmo_status_nao_descarta_ninguem():
    a = {"titulo": "A", "url": "https://a.com/x", "trecho": "Extensão de 53,85 km."}
    b = {"titulo": "B", "url": "https://b.com/x", "trecho": "O túnel tem 65,23 km de extensão."}
    fatos = ft.atribuir([{"texto": "Extensão de 53,85 km.", "fontes": ["S1"]},
                         {"texto": "O túnel tem 65,23 km de extensão.", "fontes": ["S2"]}], [a, b])
    assert len(fatos) == 2  # sem fonte melhor, os dois ficam e a revisão decide


# ---------- defeitos achados no teste com os dados reais do Seikan
MLIT_PLAN = {"titulo": "MLIT", "url": "https://www.mlit.go.jp/x", "trecho": "Custo do túnel em base de planejamento: 538,4 bilhões de ienes."}
WIKI_CUSTO = {"titulo": "Wiki", "url": "https://pt.wikipedia.org/x", "trecho": "O custo total foi de 538,4 bilhões de ienes (US$ 3,6 bilhões)."}


def test_planejamento_vem_de_qualquer_fonte_mesmo_se_o_fato_diz_custo_total():
    fatos = ft.atribuir([{"texto": "O túnel teve custo total de ¥538,4 bilhões (US$ 3,6 bilhões).", "fontes": ["S2"]}],
                        [MLIT_PLAN, WIKI_CUSTO])
    assert fatos[0]["status"] == "secundario" and ft.canon("538,4") in fatos[0]["planejamento"]
    out = ft.ajustar_texto_final({"scenes": [{"narration": "E o custo total chegou a 538,4 bilhões de ienes, o equivalente a 3,6 bilhões de dólares."}],
                                  "_fatos": fatos})
    texto = out["scenes"][0]["narration"]
    assert "custo total" not in texto and "orçamento estimado era de 538,4 bilhões" in texto
    assert not texto.lower().startswith("estima-se que e ")


def test_conjuncao_continua_no_comeco():
    assert ft.estima_antes("E o museu fechou em 2006.", {ft.canon("2006")}) == "E estima-se que o museu fechou em 2006."
    assert ft.estima_antes("Mas o museu fechou em 2006.", {ft.canon("2006")}) == "Mas estima-se que o museu fechou em 2006."


def test_fato_secundario_sem_numero_nao_ganha_estima_se():
    fatos = ft.atribuir([{"texto": "O túnel liga Aomori a Hokkaido.", "fontes": []}], [WIKI_CUSTO])
    out = ft.conferir_frases({"scenes": [{"narration": "A obra liga Aomori a Hokkaido [F1]."}]}, fatos)
    assert out["scenes"][0]["narration"] == "A obra liga Aomori a Hokkaido."


@pytest.mark.parametrize("url,raiz", [("https://pt.wikipedia.org/a", "wikipedia.org"), ("https://en.wikipedia.org/a", "wikipedia.org"),
                                      ("https://www.mlit.go.jp/a", "mlit.go.jp"), ("https://g1.globo.com/a", "globo.com"),
                                      ("https://www.bbc.co.uk/a", "bbc.co.uk"), ("https://www.gov.br/a", "gov.br")])
def test_dominio_raiz(url, raiz):
    assert ft.dominio_raiz(url) == raiz


def test_wikipedia_em_duas_linguas_nao_confirma_sozinha():
    fatos = ft.atribuir([{"texto": "Morreram 34 operários.", "fontes": ["S1", "S2"]}], [WIKI_PT, WIKI_EN])
    assert fatos[0]["status"] == "secundario"


def test_duas_fontes_independentes_confirmam():
    g1 = {"titulo": "G1", "url": "https://g1.globo.com/x", "trecho": "34 operários morreram."}
    fatos = ft.atribuir([{"texto": "Morreram 34 operários.", "fontes": []}], [WIKI_PT, g1])
    assert fatos[0]["status"] == "confirmado"


def test_gerador_inteiro_com_ia_simulada(monkeypatch, tmp_path):
    """Fluxo real do generate_script: extração com origem → escritor com marcas → conferência.
    A IA é simulada; nada vai à internet."""
    from src import disk_cache, script_gen as sg
    from src.config import ChannelConfig
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    web = [JR, WIKI_PT, BLOG, OFICIAL]
    narracoes = [
        "E aí, mente curiosa! O túnel tem 53,85 km [F1].",
        "Sob o mar ficam 23,3 km [F2].",
        "Durante a obra, 34 operários morreram [F3].",
        "E o museu fechou em 2006 [F4].",
        "O túnel tinha 65,23 km de extensão [F1].",
    ] + [f"Cena {letra} sem número, só ligação entre as partes da história." for letra in "abcdefg"]
    roteiro = {"title": "Túnel Seikan", "description": "d", "tags": ["t"], "thumbnail_text": "x",
               "thumbnail_image_prompt": "tunnel", "scenes": [{"narration": n, "image_prompt": "tunnel", "stock_query": "tunnel"}
                                                              for n in narracoes]}

    def fake(messages, **kw):
        texto = messages[-1]["content"]
        if "Extraia de 10 a 12 FATOS" in texto:
            return json.dumps({"fatos": [{"texto": "Extensão de 53,85 km.", "fontes": ["S1"]},
                                         {"texto": "O trecho submarino tem 23,30 km.", "fontes": ["S1"]},
                                         {"texto": "Morreram 34 operários.", "fontes": ["S2"]},
                                         {"texto": "O museu fechou em 2006.", "fontes": ["S3"]}]
                               + [{"texto": f"Fato extra {letra} sem número.", "fontes": ["S1"]} for letra in "abcde"]})
        if '"gancho"' in texto or "gancho" in texto and "premissa" in texto:
            raise RuntimeError("sem plano neste teste")
        return json.dumps(roteiro)

    monkeypatch.setattr(sg, "complete", fake)
    monkeypatch.setattr(sg, "plan_story", lambda *a, **k: None)
    out = sg.generate_script(ChannelConfig.load("curiosidades"), "Túnel Seikan", None, scenes=12, web_facts=web)
    texto = " ".join(sc["narration"] for sc in out["scenes"])
    assert "[F" not in texto                      # as marcas saíram
    assert "65,23" not in texto                   # número sem fato saiu
    assert "53,85 km" in texto and "23,3 km" in texto
    assert "cerca de 34 operários" in texto and "calcula-se que o museu fechou em 2006" in texto  # secundários, palavras alternadas
    assert out["_fatos"] and out["_fatos_verificados"]


def test_fato_inserido_com_um_custo_total_e_iene_vira_orcamento_por_extenso():
    from src.script_gen import _normalize_currency
    fatos = ft.atribuir([{"texto": "O túnel teve um custo total de ¥ 538,4 bilhões.", "fontes": ["S2"]}], [MLIT_PLAN, WIKI_CUSTO])
    out = _normalize_currency(ft.ajustar_texto_final(
        {"scenes": [{"narration": "O túnel teve um custo total de ¥ 538,4 bilhões."}], "_fatos": fatos}))
    texto = out["scenes"][0]["narration"]
    assert "custo total" not in texto and "orçamento estimado de" in texto
    assert "¥" not in texto and "ienes" in texto


# ---------- rodada 21: frase sem marca, troca por fato pelas marcas, busca oficial pelo nome em inglês
def test_afirmacao_sem_marca_fica_pendente_quando_o_escritor_usa_marcas():
    out = ft.conferir_frases(_rot("O túnel tem 53,85 km [F1]. Hoje é atração para visitantes. Você teria coragem?"), FATOS)
    assert out["_sem_fato"] == ["Hoje é atração para visitantes."]  # a verificação por citação decide


def test_sem_nenhuma_marca_no_roteiro_so_os_numeros_sao_conferidos():
    out = ft.conferir_frases(_rot("O túnel tem 53,85 km. Hoje é atração para visitantes."), FATOS)
    assert out["scenes"][0]["narration"] == "O túnel tem 53,85 km. Hoje é atração para visitantes."


@pytest.mark.parametrize("frase", ["E aí, mente curiosa!", "Você teria coragem de atravessar?",
                                   "Deixe sua resposta nos comentários e compartilhe com quem ama engenharia.",
                                   "Se inscreva no canal para mais histórias."])
def test_frases_livres_ficam_mesmo_sem_marca(frase):
    out = ft.conferir_frases(_rot(f"O túnel tem 53,85 km [F1]. {frase}"), FATOS)
    assert frase in out["scenes"][0]["narration"]


def test_troca_por_fato_usa_as_marcas_da_conferencia():
    from src import fact_check as fc
    fatos = [{"id": f"F{i}", "texto": f"Fato verificado número {i} sobre a obra.", "status": "oficial", "fontes": []}
             for i in range(1, 10)]
    cenas = [{"narration": f"Estima-se que algo da cena {i}.", "fatos": [f"F{i}"]} for i in range(1, 9)]
    cenas[0]["narration"] += " Hoje é atração para visitantes."
    roteiro = {"title": "t", "_fatos": fatos, "scenes": cenas}
    out, n = fc.cortar_e_trocar_por_fato(roteiro, [{"tipo": "fora_da_fonte", "termo": "Hoje é atração para visitantes", "motivo": "x"}])
    assert n == 1
    assert "Fato verificado número 9 sobre a obra." in out["scenes"][0]["narration"]
    assert "atração" not in out["scenes"][0]["narration"]


def test_troca_por_fato_reprova_abaixo_do_minimo():
    from src import fact_check as fc
    fatos = [{"id": f"F{i}", "texto": f"Fato verificado número {i} sobre a obra.", "status": "oficial", "fontes": []}
             for i in range(1, 4)]
    roteiro = {"title": "t", "_fatos": fatos, "scenes": [{"narration": "Algo. Hoje é atração.", "fatos": ["F1"]}]}
    assert fc.cortar_e_trocar_por_fato(roteiro, [{"tipo": "fora_da_fonte", "termo": "Hoje é atração", "motivo": "x"}]) is None


def test_busca_oficial_usa_o_nome_em_ingles(monkeypatch, tmp_path):
    from src import disk_cache, web_search
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    consultas = []
    monkeypatch.setattr(web_search, "_article_titles", lambda q: {"pt": ["Túnel Seikan"], "en": ["Seikan Tunnel"]})
    monkeypatch.setattr(web_search, "_tavily", lambda q, n: consultas.append(q) or [])
    monkeypatch.setattr(web_search, "_duckduckgo", lambda q, n: None)
    web_search.official_facts("Túnel Seikan: como o Japão construiu o túnel")
    assert consultas == ["Seikan Tunnel official", "Seikan Tunnel government report", "Túnel Seikan site oficial governo"]


def test_titulos_da_wikipedia_ficam_em_cache(monkeypatch, tmp_path):
    from src import disk_cache, providers, web_search
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    chamadas = []
    monkeypatch.setattr(providers, "complete", lambda *a, **k: chamadas.append(1) or '{"pt": ["Túnel Seikan"], "en": ["Seikan Tunnel"]}')
    a = web_search._article_titles("Túnel Seikan")
    b = web_search._article_titles("Túnel Seikan")
    assert a == b == {"pt": ["Túnel Seikan"], "en": ["Seikan Tunnel"], "local": []} and len(chamadas) == 1


# ---------- confirmação de afirmação sem número (busca + citação) e prioridade no escritor
def _roteiro_com_afirmacao():
    return {"scenes": [{"narration": "O túnel tem 53,85 km. Hoje as estações são atração para visitantes."}]}


def test_afirmacao_provada_por_citacao_deixa_de_ser_problema(monkeypatch):
    from src import fact_check as fc
    evid = [{"titulo": "JR", "url": "https://jr.example/a", "trecho": "As estações são atração para visitantes."}]
    monkeypatch.setattr(fc, "evidence_for_problems", lambda probs, topic: evid)
    monkeypatch.setattr(fc, "ungrounded_sentences", lambda s, f, w: [])  # tudo sustentado
    problema = [{"tipo": "fora_da_fonte", "termo": "Hoje as estações são atração para visitantes", "motivo": "x"}]
    restantes, fontes = fc.confirmar_afirmacoes(_roteiro_com_afirmacao(), problema, "Seikan", [])
    assert restantes == [] and fontes == evid


def test_afirmacao_sem_prova_continua_problema(monkeypatch):
    from src import fact_check as fc
    monkeypatch.setattr(fc, "evidence_for_problems", lambda probs, topic: [])
    monkeypatch.setattr(fc, "ungrounded_sentences",
                        lambda s, f, w: [{"tipo": "sem_fonte", "termo": "Hoje as estações são atração para visitantes."}])
    problema = [{"tipo": "fora_da_fonte", "termo": "Hoje as estações são atração para visitantes", "motivo": "x"}]
    restantes, _ = fc.confirmar_afirmacoes(_roteiro_com_afirmacao(), problema, "Seikan", [])
    assert restantes == problema


def test_afirmacao_com_numero_nao_passa_pela_confirmacao(monkeypatch):
    from src import fact_check as fc
    chamou = []
    monkeypatch.setattr(fc, "evidence_for_problems", lambda probs, topic: chamou.append(1) or [])
    problema = [{"tipo": "fora_da_fonte", "termo": "O túnel tem 53,85 km", "motivo": "x"}]
    restantes, _ = fc.confirmar_afirmacoes(_roteiro_com_afirmacao(), problema, "Seikan", [])
    assert restantes == problema and chamou == []  # número é com a conferência por fato


def test_checagem_fora_do_ar_nao_muda_nada(monkeypatch):
    from src import fact_check as fc
    monkeypatch.setattr(fc, "evidence_for_problems", lambda probs, topic: [])
    monkeypatch.setattr(fc, "ungrounded_sentences", lambda s, f, w: None)
    problema = [{"tipo": "fora_da_fonte", "termo": "Hoje as estações são atração para visitantes", "motivo": "x"}]
    assert fc.confirmar_afirmacoes(_roteiro_com_afirmacao(), problema, "Seikan", [])[0] == problema


def test_pipeline_confirma_antes_de_trocar_ou_cortar():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert src.index("confirmar_afirmacoes(script, problems, topic, web_facts)") < src.index("substituir_pela_lista(script, problems)")
    assert src.index("confirmar_afirmacoes(script, problems, topic, web_facts)") < src.index("cortar_e_trocar_por_fato(script, problems)")


def test_escritor_recebe_secundarios_so_para_completar_oito(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig
    fatos = ([{"id": f"F{i}", "texto": f"Fato oficial {i}.", "status": "oficial", "fontes": []} for i in range(1, 7)]
             + [{"id": f"F{i}", "texto": f"Fato secundário {i}.", "status": "secundario", "fontes": []} for i in range(7, 13)])
    recebido = {}
    monkeypatch.setattr(sg, "extract_facts_e_outliers", lambda t, w: (fatos, []))

    def fake_plan(topic, facts, fontes):
        recebido["lista"] = fontes[0]["trecho"]
        raise RuntimeError("parar aqui")

    monkeypatch.setattr(sg, "plan_story", fake_plan)
    try:
        sg.generate_script(ChannelConfig.load("curiosidades"), "Tema", None, web_facts=[{"titulo": "x", "url": "u", "trecho": "y"}])
    except RuntimeError:
        pass
    lista = recebido["lista"]
    assert all(f"Fato oficial {i}." in lista for i in range(1, 7))
    assert "Fato secundário 7." in lista and "Fato secundário 8." in lista   # 6 + 2 = 8
    assert "Fato secundário 9." not in lista


# ---------- variação do "estima-se" (cerca de, aproximadamente, calcula-se...)
@pytest.mark.parametrize("frase,nums,vez,esperado", [
    ("Hoje, 50 trens de carga cruzam o túnel.", {"50"}, 0, "Hoje, cerca de 50 trens de carga cruzam o túnel."),
    ("O preço humano foi alto: 34 operários morreram.", {"34"}, 1, "O preço humano foi alto: aproximadamente 34 operários morreram."),
    ("34 operários morreram.", {"34"}, 2, "Em torno de 34 operários morreram."),
    ("Cerca de 50 trens de carga cruzam o túnel.", {"50"}, 0, "Cerca de 50 trens de carga cruzam o túnel."),
    ("Elas serviam de abrigo, e o museu fechou em 2006.", {"2006"}, 1, "Elas serviam de abrigo, e calcula-se que o museu fechou em 2006."),
    ("Em setembro de 1971, as escavadeiras começaram.", {"1971"}, 0, "Estima-se que em setembro de 1971, as escavadeiras começaram."),
    ("E o museu fechou em 16 de março de 2006.", {"16", "2006"}, 2, "E aponta-se que o museu fechou em 16 de março de 2006."),
    ("Calcula-se que o museu fechou em 2006.", {"2006"}, 0, "Calcula-se que o museu fechou em 2006."),
])
def test_aproximacao_varia_pelo_contexto(frase, nums, vez, esperado):
    assert ft.estima_antes(frase, {ft.canon(n) for n in nums}, vez) == esperado


def test_roteiro_inteiro_nao_repete_a_mesma_palavra():
    fatos = ft.atribuir([{"texto": f"Cerca de {n} trens por dia.", "fontes": ["S1"]} for n in (50, 30, 20)],
                        [{"titulo": "Blog", "url": "https://b.com", "trecho": "50 trens, 30 trens, 20 trens"}])
    out = ft.conferir_frases(_rot("Passam 50 trens [F1]. Passam 30 trens [F2]. Passam 20 trens [F3]."), fatos)
    texto = out["scenes"][0]["narration"]
    assert texto == "Passam cerca de 50 trens. Passam aproximadamente 30 trens. Passam em torno de 20 trens."


# ---------- título: número com o contexto da narração (job 847 e rodada 22: "53,85 km sob o mar")
NARR = "O Túnel Seikan tem 53,85 km de extensão. Sob o mar ficam 23,3 km sob o leito marinho. Foi aberto em 13 de março de 1988."


@pytest.mark.parametrize("titulo", ["Túnel Seikan: 53,85 km sob o mar", "53,85 km submersos", "23,3 km de extensão"])
def test_numero_do_titulo_com_contexto_errado(titulo):
    assert ft.numero_fora_de_contexto(titulo, NARR)


@pytest.mark.parametrize("titulo", ["Túnel Seikan: 23,3 km sob o mar", "Túnel Seikan: 53,85 km de extensão",
                                    "Aberto em 1988", "53,85 km", "Túnel Seikan: 23,3 km sob o leito marinho"])
def test_numero_do_titulo_com_contexto_certo(titulo):
    assert ft.numero_fora_de_contexto(titulo, NARR) == []


def test_titulo_com_contexto_errado_e_refeito(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig
    respostas = iter([
        json.dumps({"titulo": "Túnel Seikan: 53,85 km sob o mar", "thumbnail_text": "53,85 KM SOB O MAR", "thumbnail_image_prompt": "p"}),
        json.dumps({"titulo": "Túnel Seikan: 23,3 km sob o leito marinho", "thumbnail_text": "53,85 KM DE EXTENSÃO", "thumbnail_image_prompt": "p"}),
    ])
    monkeypatch.setattr(sg, "complete", lambda *a, **k: next(respostas))
    import src.fact_check as fcm
    monkeypatch.setattr(fcm, "_title_problems", lambda s: [])
    roteiro = {"title": "", "_fatos_verificados": ["f"], "scenes": [{"narration": NARR}]}
    out = sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"), "Túnel Seikan")
    assert out["title"] == "Túnel Seikan: 23,3 km sob o leito marinho"



# ---------- fonte fora da curva (contradiz as outras na mesma medida) sai
def _fonte(dom, trecho):
    return {"titulo": dom, "url": f"https://{dom}/x", "trecho": trecho}


def test_valor_isolado_perde_para_o_valor_de_mais_publicadores():
    fontes = [_fonte("a.com", "Extensão de 53,85 km."), _fonte("b.com", "Extensão de 53,85 km."),
              _fonte("c.com", "Extensão de 53,85 km."), _fonte("pt.wikipedia.org", "O túnel tem 65,23 km de extensão."),
              _fonte("d.com", "O túnel tem 65,23 km de extensão.")]
    outliers = []
    fatos = ft.atribuir([{"texto": "Extensão de 53,85 km.", "fontes": []},
                         {"texto": "O túnel tem 65,23 km de extensão.", "fontes": []}], fontes, outliers)
    assert [f["texto"] for f in fatos] == ["Extensão de 53,85 km."]          # 3 publicadores × 2
    assert set(outliers) == {"https://pt.wikipedia.org/x", "https://d.com/x"}  # só sustentavam o 65,23


def test_empate_mantem_os_dois_e_nao_tira_fonte():
    fontes = [_fonte("a.com", "Extensão de 53,85 km."), _fonte("b.com", "O túnel tem 65,23 km de extensão.")]
    outliers = []
    fatos = ft.atribuir([{"texto": "Extensão de 53,85 km.", "fontes": []},
                         {"texto": "O túnel tem 65,23 km de extensão.", "fontes": []}], fontes, outliers)
    assert len(fatos) == 2 and outliers == []


def test_fonte_que_tambem_sustenta_o_valor_vencedor_nao_e_outlier():
    fontes = [_fonte("a.go.jp", "Extensão de 53,85 km."),
              _fonte("misto.com", "Extensão de 53,85 km. Outro texto diz 65,23 km de extensão.")]
    outliers = []
    ft.atribuir([{"texto": "Extensão de 53,85 km.", "fontes": []},
                 {"texto": "O túnel tem 65,23 km de extensão.", "fontes": []}], fontes, outliers)
    assert outliers == []


def test_extracao_devolve_outliers_e_guarda_em_cache(monkeypatch, tmp_path):
    from src import disk_cache, script_gen as sg
    monkeypatch.setattr(disk_cache, "BASE", tmp_path)
    fontes = [_fonte("a.go.jp", "Extensão de 53,85 km. Trecho submarino de 23,3 km. Inaugurado em 1988."),
              _fonte("pt.wikipedia.org", "O túnel tem 65,23 km de extensão.")]
    brutos = ([{"texto": "Extensão de 53,85 km.", "fontes": ["S1"]}, {"texto": "O túnel tem 65,23 km de extensão.", "fontes": ["S2"]}]
              + [{"texto": f"Fato {letra} sem número.", "fontes": ["S1"]} for letra in "abcdefg"])
    chamadas = []
    monkeypatch.setattr(sg, "complete", lambda *a, **k: chamadas.append(1) or json.dumps({"fatos": brutos}))
    a = sg.extract_facts_e_outliers("Seikan", fontes)
    b = sg.extract_facts_e_outliers("Seikan", fontes)
    assert a == b and a[1] == ["https://pt.wikipedia.org/x"] and len(chamadas) == 1


def test_pipeline_tira_fontes_fora_da_curva_antes_da_revisao():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    tira = src.index('web_facts = [f for f in web_facts if f.get("url") not in set(script["_fontes_outlier"])]')
    # a revisão do laço principal vem depois do filtro (e só existe uma depois dele, no mesmo bloco)
    revisao = src.find("problems = review_script(script, topic, facts, web_facts)", tira)
    assert revisao != -1 and src.find("def ", tira, revisao) == -1


# ---------- rodada 23: afirmação sem marca verificada, frase quebrada trocada, trecho parafraseado
def test_afirmacao_sem_marca_vai_para_verificacao_e_nao_some():
    out = ft.conferir_frases(_rot("O túnel tem 53,85 km [F1]. A escavação exigiu três galerias: piloto, serviço e principal."), FATOS)
    assert "três galerias" in out["scenes"][0]["narration"]
    assert out["_sem_fato"] == ["A escavação exigiu três galerias: piloto, serviço e principal."]


def test_verificacao_mantem_a_sustentada_e_tira_a_sem_prova(monkeypatch):
    from src import fact_check as fc
    monkeypatch.setattr(fc, "ungrounded_sentences",
                        lambda s, f, w: [{"tipo": "sem_fonte", "termo": "Mesmo com os acidentes, a obra continuou."}])
    roteiro = {"scenes": [{"narration": "A escavação exigiu três galerias. Mesmo com os acidentes, a obra continuou. Você teria coragem?"}],
               "_sem_fato": ["A escavação exigiu três galerias.", "Mesmo com os acidentes, a obra continuou."]}
    out = fc.verificar_sem_fato(roteiro, [{"titulo": "x", "url": "u", "trecho": "três galerias"}])
    assert out["scenes"][0]["narration"] == "A escavação exigiu três galerias. Você teria coragem?"
    assert "_sem_fato" not in out


def test_verificacao_fora_do_ar_tira_as_pendentes(monkeypatch):
    from src import fact_check as fc
    monkeypatch.setattr(fc, "ungrounded_sentences", lambda s, f, w: None)
    roteiro = {"scenes": [{"narration": "O túnel tem 53,85 km. A obra continuou."}], "_sem_fato": ["A obra continuou."]}
    assert fc.verificar_sem_fato(roteiro, [])["scenes"][0]["narration"] == "O túnel tem 53,85 km."


def test_sem_pendentes_nao_chama_a_verificacao(monkeypatch):
    from src import fact_check as fc
    monkeypatch.setattr(fc, "ungrounded_sentences", lambda *a: (_ for _ in ()).throw(AssertionError("não devia chamar")))
    roteiro = {"scenes": [{"narration": "Texto."}]}
    assert fc.verificar_sem_fato(roteiro, []) == roteiro


def test_trecho_parafraseado_pelo_revisor_acha_a_frase():
    from src import fact_check as fc
    frases = ["O túnel tem 53,85 km.", "Ele detém o recorde de túnel ferroviário mais profundo.", "Você teria coragem?"]
    assert fc.achar_frase("recorde de profundidade para túnel ferroviário", frases) == 1
    assert fc.achar_frase("algo que não está em lugar nenhum", frases) is None


def test_frase_quebrada_tambem_e_trocada_por_fato():
    from src import fact_check as fc
    fatos = [{"id": f"F{i}", "texto": f"Fato verificado número {i} sobre a obra.", "status": "oficial", "fontes": []}
             for i in range(1, 10)]
    cenas = [{"narration": f"Algo da cena {i}.", "fatos": [f"F{i}"]} for i in range(1, 9)]
    cenas[0]["narration"] += " A piloto foi concluída em 1983."
    out, n = fc.cortar_e_trocar_por_fato({"_fatos": fatos, "scenes": cenas},
                                         [{"tipo": "frase_quebrada", "termo": "A piloto foi concluída em 1983", "motivo": "x"}])
    assert n == 1 and "piloto" not in out["scenes"][0]["narration"]


def test_pipeline_verifica_sem_fato_depois_de_tirar_outliers():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert src.index('web_facts = [f for f in web_facts if f.get("url") not in set(script["_fontes_outlier"])]') \
        < src.index("script = verificar_sem_fato(script, web_facts)")


# ---------- rodada 24: falsos positivos de "mesma medida" (uma palavra em comum não basta)
@pytest.mark.parametrize("a,b", [
    ("O Túnel Seikan tem 53,85 km de extensão total.", "A seção submarina do túnel tem 23,3 km de extensão."),
    ("34 trabalhadores morreram durante a construção.", "O acidente do navio Toya Maru em 1954, com 1.175 mortos, impulsionou a construção."),
    ("A seção submarina do túnel tem 23,3 km de extensão.", "A Taisei Construction foi responsável pela seção Yoshioka, com 13 km."),
])
def test_medidas_diferentes_com_palavra_em_comum_nao_conflitam(a, b):
    fontes = [_fonte("x.go.jp", a), _fonte("y.com", b)]
    outliers = []
    fatos = ft.atribuir([{"texto": a, "fontes": ["S1"]}, {"texto": b, "fontes": ["S2"]}], fontes, outliers)
    assert len(fatos) == 2 and outliers == []


@pytest.mark.parametrize("a,b", [
    ("Extensão de 53,85 km.", "O túnel tem 65,23 km de extensão."),
    ("O túnel tem 53,85 km de extensão total.", "A extensão total do túnel é de 65,23 km."),
])
def test_mesma_medida_com_valor_diferente_continua_conflitando(a, b):
    fontes = [_fonte("x.go.jp", a), _fonte("pt.wikipedia.org", b)]
    outliers = []
    fatos = ft.atribuir([{"texto": a, "fontes": ["S1"]}, {"texto": b, "fontes": ["S2"]}], fontes, outliers)
    assert [f["texto"] for f in fatos] == [a] and outliers == ["https://pt.wikipedia.org/x"]


# ---------- job 863: pergunta com premissa, roteiro esvaziado, unidade repetida
def test_pergunta_com_premissa_nova_vai_para_checagem():
    from src.fatos import conferir_frases
    fatos = [{"id": "F1", "texto": "As eclusas elevam navios 26 metros.", "status": "oficial", "fontes": []}]
    roteiro = {"scenes": [
        {"narration": "As eclusas elevam navios 26 metros [F1]."},
        {"narration": "Você confiaria sua carga num elevador movido só por chuva e gravidade? Deixe nos comentários."},
    ]}
    out = conferir_frases(roteiro, fatos)
    assert out.get("_sem_fato") == ["Você confiaria sua carga num elevador movido só por chuva e gravidade?"]
    roteiro["scenes"][1]["narration"] = "Você teria coragem de subir nessas eclusas?"
    assert not conferir_frases(roteiro, fatos).get("_sem_fato")


def test_roteiro_magro_reprova():
    from src.fatos import roteiro_magro
    magro = {"scenes": [{"narration": "E aí, mente curiosa! Como isso é possível?"},
                        {"narration": "As Esclusas de Gatún erguem embarcações até o Lago Gatún."}]}
    assert "magro" in roteiro_magro(magro)
    frase = "A ponte tem 1991 metros de vão, 3911 metros no total, 60 metros de fundação e foi aberta em 1998. "
    assert roteiro_magro({"scenes": [{"narration": frase * 6}]}) is None


def test_unidade_de_medida_nao_e_palavra_repetida():
    from src.script_gen import _overused_word
    texto = " ".join(f"A câmara {i} tem {i}0 metros e sobe outros metros." for i in range(1, 10))
    assert not (_overused_word({"scenes": [{"narration": texto}]}) or "").startswith("metro")


def test_premissa_corrigida_nao_troca_o_assunto():
    from src.fact_check import mantem_assunto
    original = "Como o Canal do Panamá sobe navios 26 metros nas três câmaras das Esclusas de Gatún?"
    assert not mantem_assunto(original, "Como o novo sistema de duas esclusas do Canal do Panamá economiza água?")
    assert mantem_assunto(original, "Como o Canal do Panamá eleva navios nas Esclusas de Gatún?")
    assert mantem_assunto("Por que a SpaceX decidiu encerrar o Falcon 9?", "Como a SpaceX reutiliza o Falcon 9?")


def test_premissa_le_a_wikipedia_no_canal_de_fatos():
    pipeline = (Path(__file__).parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "premise = check_premise(topic, facts, premise_fontes)" in pipeline
    assert "_mantem_assunto(topic, premise[1])" in pipeline


# ---------- job 865: troca de número entre assuntos, "exatamente cerca de", marca sem número, número repetido
def test_troca_nao_poe_numero_de_outro_assunto():
    from src.fact_check import substituir_pela_lista
    lista = ["As Esclusas de Gatún erguem navios em cerca de 26 metros (85 pés).",
             "O lift das Esclusas de Miraflores, de dois degraus, é de 54 ft (16 m)."]
    frase = "Miraflores eleva 16 metros em dois degraus, quase a metade do total de Gatún."
    roteiro = {"scenes": [{"narration": frase}], "_fatos_verificados": lista}
    problema = {"tipo": "fato_nao_confirmado", "termo": frase}
    novo, restantes = substituir_pela_lista(roteiro, [problema])
    assert "16 metros" in novo["scenes"][0]["narration"] and restantes == [problema]


def test_aproximado_nao_fica_exato():
    from src.fatos import estima_antes
    out = estima_antes("O desnível total nos três degraus é de exatamente 85 pés.", {"85"}, 0)
    assert "exatamente" not in out and "85" in out


def test_frase_marcada_sem_numero_que_diz_mais_que_o_fato_vai_para_checagem():
    from src.fatos import conferir_frases
    fatos = [{"id": "F1", "texto": "As Esclusas de Gatún têm três câmaras que erguem navios 26 metros.", "status": "oficial", "fontes": []}]
    roteiro = {"scenes": [{"narration": "As câmaras erguem navios 26 metros [F1]. A gravidade move a água dos lagos superiores para encher cada câmara sem bombas [F1]."}]}
    assert conferir_frases(roteiro, fatos)["_sem_fato"] == ["A gravidade move a água dos lagos superiores para encher cada câmara sem bombas."]


def test_numero_repetido_em_muitas_frases():
    from src.fatos import numero_repetido
    texto = "Sobe 26 metros. São 26 metros no total. Os 26 metros vêm do lago. Em 1914 abriu. Em 1914 mesmo."
    assert "26" in numero_repetido({"scenes": [{"narration": texto}]})
    assert numero_repetido({"scenes": [{"narration": "Sobe 26 metros. São 26 metros no total. Abriu em 1914."}]}) is None
