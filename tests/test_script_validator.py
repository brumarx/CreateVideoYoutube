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
