"""26/11: leitura das decisões dos juízes e aprendizado das palavras desconhecidas."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import decisoes  # noqa: E402
from src import providers  # noqa: E402

PERM = ("sustentada", "parcial", "excede", "sem_suporte", "contradiz", "narrativa_ok", "afirmacao_oculta")


@pytest.mark.parametrize("bruto,esperado", [
    ("factual: sustentada", "sustentada"), ("Sustentada", "sustentada"), ('"sustentada"', "sustentada"),
    ("narrativa: narrativa ok", "narrativa_ok"), ("sem suporte", "sem_suporte"), ("Sem-suporte", "sem_suporte"),
    ("**excede**", "excede"), ("contradiz.", "contradiz"),
])
def test_forma_e_lida(bruto, esperado):
    assert decisoes.ler(bruto, PERM, "auditor") == esperado


def test_desconhecida_fica_registrada():
    assert decisoes.ler("confirmada", PERM, "auditor") is None
    assert json.loads(decisoes.ARQUIVO.read_text())["desconhecidas"]["auditor"]["confirmada"] == 1


@pytest.mark.aprende
def test_aprende_uma_vez_e_depois_le_sem_chamar(monkeypatch):
    chamadas = []
    monkeypatch.setattr(providers, "complete", lambda msgs, **k: chamadas.append(1) or
                        json.dumps({"mapa": {"confirmada": "sustentada", "parcialmente_sustentada": "sustentada",
                                             "extrapola": "excede"}}))
    lidas = decisoes.ler_lote(["confirmada", "parcialmente sustentada", "extrapola", "sustentada"], PERM,
                              ("sustentada", "narrativa_ok"), "auditor")
    # "parcialmente…" tem sentido de reprovação: nunca vira aprovação, diga o modelo o que disser
    assert lidas == ["sustentada", None, "excede", "sustentada"] and len(chamadas) == 1
    assert decisoes.ler("confirmada", PERM, "auditor") == "sustentada" and len(chamadas) == 1


@pytest.mark.aprende
def test_sem_modelo_nao_aprende(monkeypatch):
    def falha(*a, **k):
        raise RuntimeError("fora do ar")
    monkeypatch.setattr(providers, "complete", falha)
    assert decisoes.ler_lote(["confirmada"], PERM, ("sustentada",), "auditor") == [None]
