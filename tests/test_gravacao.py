"""26/11: gravação/repetição das chamadas ao modelo e o 2º pedido com aviso de formato."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import gravacao  # noqa: E402
from src import providers as pv  # noqa: E402
from src.config import LLMKeys  # noqa: E402


@pytest.fixture
def gravando(tmp_path, monkeypatch):
    monkeypatch.setattr(gravacao, "DIR", tmp_path)
    monkeypatch.setattr(gravacao, "ligada", lambda: True)
    monkeypatch.setattr(gravacao, "_repeticao", None)
    monkeypatch.setattr(gravacao, "_usadas", {})
    monkeypatch.setattr(pv, "HEALTH_FILE", tmp_path / "health.json")
    monkeypatch.setattr(pv, "_rotators", {})
    monkeypatch.setattr(pv, "_fetch_models", lambda prov, url, key: list(pv._STRONG_MODELS.get(prov, ["m"])))
    monkeypatch.setattr(pv, "_health_update", lambda *a, **k: None)
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)
    return tmp_path


def _chaves():
    base = dict(nvidia=["n1"], xai=[], groq=[], cerebras=[], openrouter=[], mistral=[], gemini=[], cloudflare=[],
                deepseek=[])
    return LLMKeys(**base)


def test_resposta_fora_do_formato_tem_2o_pedido_avisado_e_tudo_fica_gravado(gravando, monkeypatch):
    pedidos = []

    def falso(ep, key, model, messages, *a, **k):
        pedidos.append(messages)
        return ('{"decisao": "factual: sustentada"}' if len(pedidos) == 1 else '{"decisao": "sustentada"}'), 200, 0
    monkeypatch.setattr(pv, "_call_openai_compat", falso)
    r = pv.complete([{"role": "user", "content": "julgue"}], keys=_chaves(),
                    validate=lambda x: json.loads(x)["decisao"] == "sustentada")
    assert r == '{"decisao": "sustentada"}'
    assert len(pedidos) == 2 and pedidos[1][-1] is pv.AVISO_DE_FORMATO
    linhas = [json.loads(x) for f in gravando.glob("*.jsonl") for x in open(f)]
    assert [x["aceita"] for x in linhas] == [False, True]
    assert linhas[0]["resposta"] == '{"decisao": "factual: sustentada"}'
    assert gravacao.recusadas(list(gravando.glob("*.jsonl")))[0]["pedido"] == "julgue"


def test_repeticao_devolve_a_resposta_gravada_sem_chamar(gravando, monkeypatch):
    monkeypatch.setattr(pv, "_call_openai_compat", lambda *a, **k: ('{"ok": 1}', 200, 0))
    msgs = [{"role": "user", "content": "x"}]
    pv.complete(msgs, keys=_chaves())
    arquivo = next(gravando.glob("*.jsonl"))
    monkeypatch.setenv("GRAVACAO_REPETIR", str(arquivo))
    monkeypatch.setattr(pv, "_call_openai_compat", lambda *a, **k: pytest.fail("chamou o modelo na repetição"))
    assert pv.complete(msgs, keys=_chaves()) == '{"ok": 1}'
    with pytest.raises(gravacao.SemGravacao):
        pv.complete([{"role": "user", "content": "outro pedido"}], keys=_chaves())


def test_gravacao_desligada_nos_testes():
    assert gravacao.ligada() is False
