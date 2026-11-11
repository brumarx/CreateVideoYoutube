"""Limite de correção factual: uma chamada de conserto, uma nova validação, sem laço.
Zero API: conserto e revisor são contados por mock."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pipeline():
    return importlib.import_module("scripts.run_pipeline")


def test_laco_de_correcao_faz_uma_chamada_e_uma_validacao(monkeypatch):
    rp = _pipeline()
    assert rp.MAX_REPAIR_ROUNDS == 1
    contagem = {"conserto": 0, "validacao": 0}

    def conserto(script, problems, facts, web_facts, topic):
        contagem["conserto"] += 1
        return {**script, "scenes": [{"narration": "a corrigida"}, {"narration": "b"}]}

    def revisor(script, topic, facts, web_facts):
        contagem["validacao"] += 1
        return [{"tipo": "fato_nao_confirmado", "termo": "x", "motivo": "ainda errado"}]

    monkeypatch.setattr(rp, "repair_script", conserto)
    monkeypatch.setattr(rp, "review_script", revisor)
    roteiro = {"title": "t", "thumbnail_text": "", "scenes": [{"narration": "a"}, {"narration": "b"}]}
    resultado = rp._repair_loop(roteiro, [{"tipo": "fato_nao_confirmado", "termo": "a", "motivo": "m"}],
                                "t", None, None, "job-x")
    assert resultado is None  # bloqueio persistente: o job reprova
    assert contagem == {"conserto": 1, "validacao": 1}


def test_canal_de_fatos_reprova_depois_de_uma_correcao():
    # a rejeição vem antes do bloco must_publish: nos canais de fatos nada passa daí
    texto = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    trecho_rejeicao = texto.index("bloqueio factual depois de uma correção")
    trecho_forcado = texto.index("                if must_publish:\n                    # última tentativa do dia")
    assert trecho_rejeicao < trecho_forcado


def test_revisor_fora_do_ar_nao_publica_em_canal_de_fatos():
    texto = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "if must_publish and channel_name not in FATOS_CANAIS:" in texto
