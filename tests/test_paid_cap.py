"""DeepSeek (pago): sem raciocínio, teto de gasto por job/dia e fatos verificados como fonte.
Zero API: httpx mockado e arquivo de estado num diretório temporário."""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fact_check, paid_usage, providers  # noqa: E402


@pytest.fixture(autouse=True)
def estado_isolado(tmp_path, monkeypatch):
    monkeypatch.setattr(paid_usage, "STATE_FILE", tmp_path / "paid_usage.json")
    monkeypatch.setattr(paid_usage, "_job_atual", None)
    monkeypatch.setattr(providers, "_health_update", lambda *a, **k: None)
    yield


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def _resposta(prompt_tokens=1000, completion_tokens=500, conteudo='{"ok": true}', finish="stop"):
    return {"choices": [{"message": {"content": conteudo}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def test_deepseek_manda_thinking_desligado(monkeypatch):
    enviados = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        enviados.update(json)
        return _Resp(_resposta())

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    providers._call_openai_compat("https://api.deepseek.com/chat/completions", "k", "deepseek-flash",
                                  [{"role": "user", "content": "oi"}], 100)
    assert enviados["thinking"] == {"type": "disabled"}


def test_outro_provedor_nao_recebe_thinking(monkeypatch):
    enviados = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        enviados.update(json)
        return _Resp(_resposta())

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    providers._call_openai_compat("https://api.groq.com/openai/v1/chat/completions", "k", "llama",
                                  [{"role": "user", "content": "oi"}], 100)
    assert "thinking" not in enviados


def test_gasto_e_contado_mesmo_com_resposta_cortada(monkeypatch):
    monkeypatch.setattr(providers.httpx, "post",
                        lambda *a, **k: _Resp(_resposta(prompt_tokens=8000, completion_tokens=12000, finish="length")))
    paid_usage.set_job("900")
    providers._call_openai_compat("https://api.deepseek.com/chat/completions", "k", "deepseek-flash",
                                  [{"role": "user", "content": "x"}], 12000)
    estado = json.loads(paid_usage.STATE_FILE.read_text(encoding="utf-8"))
    custo = estado["jobs"]["900"]["custo_usd"]
    # 8000 entrada a US$0,30/M + 12000 saída a US$1,20/M = US$ 0,0168
    assert abs(custo - 0.0168) < 1e-6


def test_teto_do_job_bloqueia_a_deepseek(monkeypatch):
    paid_usage.STATE_FILE.write_text(json.dumps({"jobs": {"901": {"custo_usd": 0.16, "tokens": 1}}}), encoding="utf-8")
    paid_usage.set_job("901")
    assert paid_usage.dentro_do_teto() is False
    chamadas = []
    monkeypatch.setattr(providers, "_call_openai_compat", lambda *a, **k: chamadas.append(a) or (None, None, 0))
    monkeypatch.setattr(providers, "_fetch_models", lambda *a, **k: ["deepseek-flash"])
    keys = SimpleNamespace(nvidia=None, groq=None, openrouter=None, mistral=None, cloudflare=None, gemini=None,
                           deepseek=["sk-teste-teto-job"])
    with pytest.raises(RuntimeError):
        providers.complete([{"role": "user", "content": "oi"}], keys=keys)
    assert chamadas == []  # nenhuma chamada paga saiu


def test_teto_do_dia_bloqueia_mesmo_sem_job():
    from datetime import datetime
    dia = datetime.now().strftime("%Y-%m-%d")
    paid_usage.STATE_FILE.write_text(json.dumps({"dias": {dia: {"custo_usd": 0.51}}}), encoding="utf-8")
    assert paid_usage.dentro_do_teto() is False


def test_abaixo_do_teto_libera():
    paid_usage.STATE_FILE.write_text(json.dumps({"jobs": {"902": {"custo_usd": 0.01}}}), encoding="utf-8")
    paid_usage.set_job("902")
    assert paid_usage.dentro_do_teto() is True


def test_revisor_recebe_fatos_verificados_como_fonte():
    roteiro = {"title": "Túnel Seikan", "thumbnail_text": "", "scenes": [{"narration": "O custo foi alto."}],
               "_fatos_verificados": ["Custo total de 538,4 bilhões de ienes", "23,3 km sob o leito marinho"]}
    prompt = fact_check._review_prompt(roteiro, "Túnel Seikan", None, None)
    assert "FATOS JÁ CONFIRMADOS NAS FONTES" in prompt
    assert "538,4 bilhões de ienes" in prompt and "23,3 km sob o leito marinho" in prompt


def test_sem_fatos_verificados_o_prompt_nao_muda():
    roteiro = {"title": "t", "thumbnail_text": "", "scenes": [{"narration": "n"}]}
    assert fact_check._com_fatos_verificados("FONTES: x", roteiro) == "FONTES: x"


def test_limite_de_ciclos_do_conserto_e_3():
    texto = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert re.search(r"^MAX_REPAIR_ROUNDS = 1\b", texto, re.M)
