"""Cascata de provedores: falha de rede/5xx gera UMA nova tentativa depois de 90 s (zero API: tudo mockado)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import providers  # noqa: E402


def _chaves():
    return SimpleNamespace(nvidia=["chave-nvidia-teste"], groq=None, openrouter=None, mistral=None, deepseek=None,
                           cloudflare=None, gemini=None)


def _sem_rede(monkeypatch):
    monkeypatch.setattr(providers, "_fetch_models", lambda provider, url, key: ["modelo-teste"])
    monkeypatch.setattr(providers, "_too_big_for", lambda name, messages, max_tokens=0: False)


def test_queda_do_nvidia_espera_e_tenta_de_novo(monkeypatch):
    _sem_rede(monkeypatch)
    dormiu = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: dormiu.append(s))
    chamadas = {"n": 0}

    def fake(endpoint, key, model, messages, max_tokens, temperature, **kw):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            return None, None, 0          # desconexão
        return "resposta ok", 200, 0

    monkeypatch.setattr(providers, "_call_openai_compat", fake)
    assert providers.complete([{"role": "user", "content": "oi"}], keys=_chaves()) == "resposta ok"
    assert dormiu == [90]


def test_erro_de_cota_nao_repete_com_espera(monkeypatch):
    _sem_rede(monkeypatch)
    dormiu = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: dormiu.append(s))
    # 400 = pedido recusado, não é queda: não há nova tentativa
    monkeypatch.setattr(providers, "_call_openai_compat", lambda *a, **k: (None, 400, 0))
    try:
        providers.complete([{"role": "user", "content": "oi"}], keys=_chaves())
        assert False, "devia levantar RuntimeError"
    except RuntimeError:
        pass
    assert dormiu == []


def test_queda_se_repete_levanta_erro_sem_loop(monkeypatch):
    _sem_rede(monkeypatch)
    dormiu = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: dormiu.append(s))
    monkeypatch.setattr(providers, "_call_openai_compat", lambda *a, **k: (None, 503, 0))
    try:
        providers.complete([{"role": "user", "content": "oi"}], keys=_chaves())
        assert False, "devia levantar RuntimeError"
    except RuntimeError:
        pass
    assert dormiu == [90]  # uma espera só, não em loop


def test_visao_queda_espera_90s_e_tenta_de_novo(monkeypatch):
    from src import visual_check
    dormiu = []
    monkeypatch.setattr(visual_check.time, "sleep", lambda s: dormiu.append(s))
    monkeypatch.setattr(visual_check, "_gemini", lambda *a, **k: None)
    monkeypatch.setattr(visual_check, "_cloudflare_vision", lambda *a, **k: None)
    chamadas = {"n": 0}

    def fake(prompt, jpeg, falhas=None):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            falhas.append("nvidia")  # caiu
            return None
        return "cena ok"

    monkeypatch.setattr(visual_check, "_openai_compat", fake)
    assert visual_check.ask_vision("p", b"jpeg") == "cena ok"
    assert dormiu == [90]


def test_deepseek_so_entra_depois_dos_gratis(monkeypatch):
    # ordem da cascata: DeepSeek (pago) é o último provedor
    nomes = [n for n, *_ in providers.OPENAI_COMPAT_PROVIDERS]
    assert nomes[-1] == "deepseek"


def test_deepseek_sem_saldo_registra_e_nao_repete(monkeypatch):
    from src import paid_usage
    registrados = []
    monkeypatch.setattr(paid_usage, "record_exhausted", lambda d: registrados.append(d))
    monkeypatch.setattr(providers, "_health_update", lambda *a, **k: None)  # não grava descanso no arquivo real
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    _sem_rede(monkeypatch)
    keys = SimpleNamespace(nvidia=None, groq=None, openrouter=None, mistral=None, cloudflare=None, gemini=None,
                           deepseek=["sk-teste-isolado-402"])
    monkeypatch.setattr(providers, "_call_openai_compat", lambda ep, key, model, *a, **k: (None, 402, 86400)
                        if "deepseek" in ep else (None, 400, 0))
    try:
        providers.complete([{"role": "user", "content": "oi"}], keys=keys)
        assert False, "devia levantar RuntimeError"
    except RuntimeError:
        pass
    assert len(registrados) == 1
