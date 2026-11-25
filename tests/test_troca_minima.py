"""25/11 (Svalbard 1021): a frase apontada pela leitura do público recebe a troca mínima sugerida pelo leitor,
conferida contra o fato — antes do corte (que tirava a conclusão) e da edição de cena (que voltava inteira)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import providers  # noqa: E402
from src import redacao as rd  # noqa: E402

pytestmark = pytest.mark.troca


def _script():
    return {"title": "Cofre", "scenes": [
        {"frases": [{"texto": "O cofre fica dentro de uma montanha em Svalbard.", "tipo": "factual", "evidencias": ["E1"]}]},
        {"frases": [{"texto": "O permafrost mantém o frio sem energia nenhuma.", "tipo": "factual", "evidencias": ["E2"]},
                    {"texto": "E isso muda tudo.", "tipo": "narrativa", "evidencias": []}]},
    ]}


def _problema():
    return {"tipo": "leitura_enganosa", "termo": "O permafrost mantém o frio sem energia nenhuma.", "cena": 1, "frase": 0,
            "motivo": "o público conclui que o cofre dispensa eletricidade. Corrija: diga que o permafrost ajuda a "
                      "manter o frio", "sequencia": [{"cena": 1, "frase": 0}]}


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setattr(rd, "mapa_do_conhecimento", lambda banco: [
        {"id": "E1", "afirmacao": "O cofre fica dentro de uma montanha.", "trecho": ""},
        {"id": "E2", "afirmacao": "O permafrost ajuda a manter as sementes congeladas se faltar energia.", "trecho": ""}])
    pedidos = []

    def responde(texto):
        def complete(msgs, **k):
            pedidos.append(msgs[0]["content"])
            return json.dumps(texto)
        monkeypatch.setattr(providers, "complete", complete)
    return responde, pedidos


def test_troca_aceita_quando_a_conferencia_passa(ambiente, monkeypatch):
    responde, pedidos = ambiente
    responde({"trocas": [{"n": 2, "texto": "O permafrost ajuda a manter as sementes congeladas."}]})
    conferidas = []
    monkeypatch.setattr(rd, "conferir_contra_trechos",
                        lambda itens, banco: conferidas.extend(itens) or {k: None for k in range(len(itens))})
    novo, reg = rd.trocar_frases_apontadas(_script(), [_problema()], {}, "Svalbard")
    assert novo["scenes"][1]["frases"][0]["texto"] == "O permafrost ajuda a manter as sementes congeladas."
    assert novo["scenes"][1]["frases"][0]["evidencias"] == ["E2"]
    assert conferidas[0]["evidencias"] == ["E2"]
    assert "Corrija: diga que o permafrost" in pedidos[0] and "FATO E2" in pedidos[0]
    assert reg["trocadas"][0]["cena"] == 2


def test_troca_recusada_pela_conferencia_nao_muda_nada(ambiente, monkeypatch):
    responde, _ = ambiente
    responde({"trocas": [{"n": 2, "texto": "O permafrost garante o frio por séculos."}]})
    monkeypatch.setattr(rd, "conferir_contra_trechos", lambda itens, banco: {0: "termo mais forte"})
    novo, reg = rd.trocar_frases_apontadas(_script(), [_problema()], {}, "Svalbard")
    assert novo is None and reg["recusadas"][0]["motivo"] == "termo mais forte"


def test_troca_so_mexe_nas_frases_apontadas(ambiente, monkeypatch):
    responde, _ = ambiente
    responde({"trocas": [{"n": 1, "texto": "O cofre é o maior do mundo."}]})  # frase 1 não foi apontada
    monkeypatch.setattr(rd, "conferir_contra_trechos", lambda itens, banco: {k: None for k in range(len(itens))})
    novo, _ = rd.trocar_frases_apontadas(_script(), [_problema()], {}, "Svalbard")
    assert novo is None


def test_texto_vazio_tira_a_frase(ambiente, monkeypatch):
    responde, _ = ambiente
    responde({"trocas": [{"n": 2, "texto": ""}]})
    monkeypatch.setattr(rd, "conferir_contra_trechos", lambda itens, banco: {})
    novo, _ = rd.trocar_frases_apontadas(_script(), [_problema()], {}, "Svalbard")
    # a cena que fica só com narrativa sai (mesma regra do corte)
    assert [f["texto"] for sc in novo["scenes"] for f in sc["frases"]] == [
        "O cofre fica dentro de uma montanha em Svalbard."]


def test_narrativa_trocada_nao_pode_afirmar(ambiente, monkeypatch):
    responde, _ = ambiente
    p = {**_problema(), "cena": 1, "frase": 1, "sequencia": [{"cena": 1, "frase": 1}]}
    responde({"trocas": [{"n": 3, "texto": "E isso guarda 1,3 milhão de amostras."}]})
    monkeypatch.setattr(rd, "conferir_contra_trechos", lambda itens, banco: {})
    novo, reg = rd.trocar_frases_apontadas(_script(), [p], {}, "Svalbard")
    assert novo is None and "narrativa afirma" in reg["recusadas"][0]["motivo"]
