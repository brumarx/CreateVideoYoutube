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
