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
    assert fatos[0]["status"] == "secundario" and fatos[0]["planejamento"] == [ft.canon("538,4")]
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
    assert "estima-se que o museu fechou em 2006" in texto  # secundário (blog) ganhou estima-se
    assert out["_fatos"] and out["_fatos_verificados"]


def test_fato_inserido_com_um_custo_total_e_iene_vira_orcamento_por_extenso():
    from src.script_gen import _normalize_currency
    fatos = ft.atribuir([{"texto": "O túnel teve um custo total de ¥ 538,4 bilhões.", "fontes": ["S2"]}], [MLIT_PLAN, WIKI_CUSTO])
    out = _normalize_currency(ft.ajustar_texto_final(
        {"scenes": [{"narration": "O túnel teve um custo total de ¥ 538,4 bilhões."}], "_fatos": fatos}))
    texto = out["scenes"][0]["narration"]
    assert "custo total" not in texto and "orçamento estimado de" in texto
    assert "¥" not in texto and "ienes" in texto
