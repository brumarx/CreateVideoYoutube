"""Conserto só nas cenas apontadas e bloco único de fontes (zero API: complete é substituído)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import script_gen  # noqa: E402
from src.fontes import LIMITE_FONTES, bloco_fontes  # noqa: E402
from src.fact_check import _sources_text  # noqa: E402


def _roteiro():
    return {
        "title": "Túnel Seikan",
        "scenes": [
            {"narration": "Cena um fala do túnel e de 1971."},
            {"narration": "Cena dois usa explosivos e picaretas para abrir caminho sob o mar."},
            {"narration": "Cena três fala da inauguração em 1988."},
        ],
    }


def test_cenas_apontadas_sao_so_as_que_contem_o_termo():
    problemas = [{"termo": "explosivos e picaretas", "motivo": "fora_da_fonte", "tipo": "fora_da_fonte"}]
    assert script_gen._cenas_apontadas(_roteiro(), problemas) == [1]


def test_conserto_troca_so_a_cena_apontada(monkeypatch):
    chamadas = []

    def fake(messages, **kw):
        chamadas.append(messages[0]["content"])
        return json.dumps({"titulo": "Túnel Seikan", "cenas": ["Abriram caminho sob o mar com o trabalho de cada dia."]})

    monkeypatch.setattr(script_gen, "complete", fake)
    problemas = [{"termo": "explosivos e picaretas", "motivo": "fora_da_fonte", "tipo": "fora_da_fonte"}]
    out = script_gen.repair_script(_roteiro(), problemas, None, [{"titulo": "Seikan", "trecho": "1971 e 1988"}], "Túnel Seikan")
    assert out is not None
    assert out["scenes"][0]["narration"] == "Cena um fala do túnel e de 1971."
    assert out["scenes"][2]["narration"] == "Cena três fala da inauguração em 1988."
    assert out["scenes"][1]["narration"] == "Abriram caminho sob o mar com o trabalho de cada dia."
    # o pedido só leva a cena apontada, não as outras
    assert "Cena três" not in chamadas[0] and "Cena um" not in chamadas[0]


def test_termo_sem_mapeamento_manda_todas_as_cenas(monkeypatch):
    enviadas = []

    def fake(messages, **kw):
        enviadas.append(messages[0]["content"])
        return json.dumps({"titulo": "Túnel Seikan", "cenas": ["a", "b", "c"]})

    monkeypatch.setattr(script_gen, "complete", fake)
    problemas = [{"termo": "frase que não existe no roteiro nenhum", "motivo": "x", "tipo": "fato_nao_confirmado"}]
    script_gen.repair_script(_roteiro(), problemas, None, None, "Túnel")
    assert "Cena um" in enviadas[0] and "Cena três" in enviadas[0]


def test_bloco_fontes_unico_e_com_limite():
    web = [{"titulo": "Wiki", "trecho": "x" * 6000}, {"titulo": "Outra", "trecho": "y" * 6000}]
    bloco = bloco_fontes(None, web)
    assert len(bloco) == LIMITE_FONTES
    assert bloco.startswith("- Wiki: ")


def test_revisor_e_extracao_leem_o_mesmo_bloco():
    web = [{"titulo": "Wiki", "trecho": "65,23 quilômetros"}]
    assert _sources_text(None, web) == bloco_fontes(None, web)


def _roteiro_com_miniatura():
    roteiro = _roteiro()
    roteiro["title"] = "Túnel Seikan: 53,85 km sob o mar"
    roteiro["thumbnail_text"] = "54 km ESCAVADOS À MÃO"
    return roteiro


def test_conserto_troca_miniatura_com_numero_fora_da_narracao(monkeypatch):
    def fake(messages, **kw):
        return json.dumps({"titulo": "Túnel Seikan: 53,85 km sob o mar", "thumbnail_text": "1971 A 1988",
                           "cenas": ["Cena um corrigida.", "Cena dois corrigida.", "Cena três corrigida."]})

    monkeypatch.setattr(script_gen, "complete", fake)
    problemas = [{"termo": "Texto da miniatura: '54 km ESCAVADOS À MÃO'", "motivo": "fora_da_fonte",
                  "tipo": "fora_da_fonte"}]
    out = script_gen.repair_script(_roteiro_com_miniatura(), problemas, None,
                                   [{"titulo": "Seikan", "trecho": "1971 e 1988"}], "Túnel Seikan: 53,85 km")
    assert out is not None
    assert out["thumbnail_text"] == "1971 A 1988"


def test_miniatura_com_numero_que_nao_esta_na_lista_e_tirada(monkeypatch):
    def fake(messages, **kw):
        # 54 não está na narração, nas fontes nem no tema: a miniatura sai
        return json.dumps({"titulo": "Túnel Seikan: 53,85 km sob o mar", "thumbnail_text": "54 km ESCAVADOS",
                           "cenas": ["Cena um.", "Cena dois.", "Cena três."]})

    monkeypatch.setattr(script_gen, "complete", fake)
    problemas = [{"termo": "Texto da miniatura: '54 km ESCAVADOS À MÃO'", "motivo": "fora_da_fonte",
                  "tipo": "fora_da_fonte"}]
    out = script_gen.repair_script(_roteiro_com_miniatura(), problemas, None,
                                   [{"titulo": "Seikan", "trecho": "1971 e 1988"}], "Túnel Seikan: 53,85 km")
    assert out is not None
    assert out["thumbnail_text"] is None


def test_titulo_com_numero_fora_da_lista_volta_ao_tema(monkeypatch):
    def fake(messages, **kw):
        return json.dumps({"titulo": "Túnel Seikan: 54 km escavados", "thumbnail_text": "1971 A 1988",
                           "cenas": ["Cena um.", "Cena dois.", "Cena três."]})

    monkeypatch.setattr(script_gen, "complete", fake)
    problemas = [{"termo": "Túnel Seikan: 53,85 km sob o mar", "motivo": "titulo_nao_confere",
                  "tipo": "titulo_nao_confere"}]
    out = script_gen.repair_script(_roteiro_com_miniatura(), problemas, None, None, "Túnel Seikan: 53,85 km")
    assert out is not None
    assert "54" not in out["title"]


def test_ultima_cena_com_promessa_e_reprovada_e_regra_esta_no_escritor():
    roteiro = {"scenes": [{"narration": "O túnel tem 53,85 km."},
                          {"narration": "Mas será que aguenta? A resposta está no próximo capítulo."}]}
    assert script_gen._ending_problem(roteiro) is not None
    assert "ÚLTIMA cena" in script_gen.SYSTEM_PROMPT
    assert "pergunta direta ao espectador" in script_gen.SYSTEM_PROMPT


def test_pergunta_de_engajamento_sem_fato_novo_no_fim_passa():
    roteiro = {"scenes": [{"narration": "O túnel tem 53,85 km e foi aberto em 1988."},
                          {"narration": "Passa o trem a 160 km/h por baixo do mar. Você teria coragem de atravessar?"}]}
    assert script_gen._ending_problem(roteiro) is None


def test_pergunta_com_numero_inventado_no_fim_continua_reprovada():
    roteiro = {"scenes": [{"narration": "O túnel tem 53,85 km."},
                          {"narration": "Você atravessaria 300 quilômetros debaixo d'água?"}]}
    assert script_gen._ending_problem(roteiro) is not None


def test_pergunta_do_fim_nao_e_cortada_pelo_trim():
    roteiro = {"scenes": [{"narration": "O túnel tem 53,85 km."},
                          {"narration": "Passa o trem sob o mar. O que você acha dessa obra?"}]}
    out = script_gen.trim_teaser_ending(roteiro)
    assert out["scenes"][-1]["narration"].endswith("O que você acha dessa obra?")
