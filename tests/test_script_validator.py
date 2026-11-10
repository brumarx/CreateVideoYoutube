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
