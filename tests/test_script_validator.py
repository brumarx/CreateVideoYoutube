"""Validador do roteiro: recusa com motivo no log (truncado, sem campo, válido)."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.script_gen import _is_parseable_script  # noqa: E402

REQ = {"title", "description", "tags", "scenes"}


def test_json_truncado_e_recusado_com_motivo(caplog):
    with caplog.at_level(logging.WARNING, logger="script_gen"):
        assert _is_parseable_script('{"title":"a","description":"b","tags":[],"scenes":[', REQ) is False
    assert "não é JSON" in caplog.text


def test_campo_faltando_e_recusado_com_motivo(caplog):
    with caplog.at_level(logging.WARNING, logger="script_gen"):
        assert _is_parseable_script('{"title":"a","description":"b","tags":[]}', REQ) is False
    assert "faltam campos ['scenes']" in caplog.text


def test_roteiro_completo_passa():
    assert _is_parseable_script('{"title":"a","description":"b","tags":[],"scenes":[]}', REQ) is True


def test_orcamento_do_roteiro_cobre_12_cenas_do_fractal():
    # 10/11: o roteiro de 12 cenas veio com 9.171 caracteres (~2.450 tokens) e foi cortado.
    # Com 350 palavras (1,5-2,5 min) e 12 cenas, o orçamento tem de passar disso com folga.
    from src.script_gen import _script_max_tokens
    assert _script_max_tokens(350, 12) >= 4000
    assert _script_max_tokens(10_000, 200) <= 16000


def test_numeros_com_separador_brasileiro_e_ingles_batem():
    from src.script_gen import fatos_com_numeros_da_fonte
    fontes = "com 65,23 quilômetros; 23,3 km sob o leito; custo de ¥ 538,4 bilhões; 1155 pessoas"
    fatos = ["O túnel tem 65.23 km", "Trecho de 23,3 km", "Custo de 538,4 bilhões", "Naufrágio matou 1.155 pessoas"]
    assert fatos_com_numeros_da_fonte(fatos, fontes) == fatos


def test_fato_com_numero_fora_das_fontes_e_descartado():
    from src.script_gen import fatos_com_numeros_da_fonte
    fontes = "com 65,23 quilômetros; 1155 pessoas"
    fatos = ["Tem 53,85 km", "Tem 65,23 km", "Matou 1.430 pessoas", "Foi uma obra ferroviária", "Inaugurada em 1988"]
    assert fatos_com_numeros_da_fonte(fatos, fontes) == ["Tem 65,23 km", "Foi uma obra ferroviária"]


def test_moeda_simbolo_vira_extenso_no_titulo_e_na_narracao():
    from src.script_gen import _normalize_currency
    script = {"title": "Túnel de ¥ 538 bi", "thumbnail_text": "US$ 3,6 bilhões",
              "scenes": [{"narration": "Custou ¥ 538,4 bilhões, antes US$ 3,6 bilhões."}]}
    out = _normalize_currency(script)
    assert out["title"] == "Túnel de 538 bi de ienes"
    assert out["thumbnail_text"] == "3,6 bilhões de dólares"
    assert out["scenes"][0]["narration"] == "Custou 538,4 bilhões de ienes, antes 3,6 bilhões de dólares."


def test_moeda_sem_simbolo_nao_muda():
    from src.script_gen import _normalize_currency
    s = {"title": "Custou 538 bilhões de ienes", "scenes": [{"narration": "R$ 2 milhões"}]}
    assert _normalize_currency(s) == s
