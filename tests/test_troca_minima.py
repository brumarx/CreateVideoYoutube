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


def test_limpeza_tira_fato_ja_contado_e_narrativa_que_afirma():
    s = {"scenes": [
        {"frases": [{"texto": "E aí, mente curiosa!", "tipo": "narrativa", "evidencias": []},
                    {"texto": "Svalbard fica entre os lugares mais frios.", "tipo": "factual", "evidencias": ["E6", "E26"]}]},
        {"frases": [{"texto": "Ninguém mora no cofre.", "tipo": "factual", "evidencias": ["E100"]},
                    {"texto": "Espera-se que Svalbard siga fria.", "tipo": "factual", "evidencias": ["E26"]}]},
        {"frases": [{"texto": "E o permafrost garante o frio.", "tipo": "narrativa", "evidencias": []},
                    {"texto": "O cofre faz parte do Tratado.", "tipo": "factual", "evidencias": ["E55"]},
                    {"texto": "Comenta SEMENTE se você guardaria a sua?", "tipo": "narrativa", "evidencias": []}]},
    ]}
    limpo = rd.limpar_em_codigo(s)
    assert [f["texto"] for sc in limpo["scenes"] for f in sc["frases"]] == [
        "E aí, mente curiosa!", "Svalbard fica entre os lugares mais frios.", "Ninguém mora no cofre.",
        "O cofre faz parte do Tratado.", "Comenta SEMENTE se você guardaria a sua?"]


def test_limpeza_nunca_esvazia_cena():
    s = {"scenes": [
        {"frases": [{"texto": "A.", "tipo": "factual", "evidencias": ["E1"]}]},
        {"frases": [{"texto": "B.", "tipo": "factual", "evidencias": ["E1"]}]},
    ]}
    assert rd.limpar_em_codigo(s) is s


def test_troca_tira_etiqueta_copiada(ambiente, monkeypatch):
    responde, _ = ambiente
    responde({"trocas": [{"n": 2, "texto": "[factual] O permafrost ajuda a manter as sementes congeladas."}]})
    monkeypatch.setattr(rd, "conferir_contra_trechos", lambda itens, banco: {k: None for k in range(len(itens))})
    novo, _ = rd.trocar_frases_apontadas(_script(), [_problema()], {}, "Svalbard")
    assert novo["scenes"][1]["frases"][0]["texto"] == "O permafrost ajuda a manter as sementes congeladas."


def _com_fim():
    s = _script()
    s["scenes"][1]["narration"] = " ".join(f["texto"] for f in s["scenes"][1]["frases"])
    return s


@pytest.mark.fechamento
def test_fechamento_substitui_a_narrativa_final(ambiente):
    responde, _ = ambiente
    responde({"frases": ["No fim, o cofre é uma cópia de segurança dentro da montanha.",
                         "Para mim, o mais bonito é ninguém morar lá dentro.",
                         "Comenta SEMENTE se você guardaria uma da sua cidade."]})
    novo = rd.escrever_fechamento(_com_fim(), {"angulo": {"pergunta": "Como?", "ponto_de_vista": "x"}}, "Svalbard", 500)
    fim = novo["scenes"][1]["frases"]
    assert [f["texto"] for f in fim][0] == "O permafrost mantém o frio sem energia nenhuma."
    assert "E isso muda tudo." not in [f["texto"] for f in fim]
    assert fim[-1]["fechamento"] and fim[-1]["tipo"] == "narrativa"


@pytest.mark.fechamento
@pytest.mark.parametrize("frases", [
    ["O cofre guarda 1,3 milhão de amostras.", "Você guardaria uma?"],          # número
    ["O cofre garante o futuro.", "Você guardaria uma?"],                       # afirma
    ["Para mim, é lindo.", "Para mim, é muito lindo."],                         # não chama o espectador
    ["Para mim, é lindo.", "Deixa o like e me diz: você guardaria?"],           # pede like
])
def test_fechamento_recusado_fica_o_roteiro(ambiente, frases):
    responde, _ = ambiente
    responde({"frases": frases})
    s = _com_fim()
    assert rd.escrever_fechamento(s, None, "Svalbard", 500) is s
