"""25/11: revisão das afirmações do banco pelo juiz fixo, contra o trecho que o escritor vê."""
import json

import pytest

from src import disk_cache, evidencias, providers

pytestmark = pytest.mark.revisao

TEMA = "Cofre de Sementes de Svalbard"


def _ev(id_, afirmacao, trecho, status="oficial"):
    return {"id": id_, "afirmacao": afirmacao, "tipo": "qualitativo", "status": status, "numeros": [],
            "citacoes": [{"fonte": "S1", "url": "https://www.seedvault.no/x", "titulo": "t", "dominio": "seedvault.no",
                          "classe": "oficial", "trecho": trecho, "verificado": True, "recuperado": False}]}


def _banco():
    return {"tema": TEMA, "fontes": [], "lacunas": [], "evidencias": [
        _ev("E1", "As amostras são duplicatas, garantindo recuperação se as originais forem perdidas.",
            "Each sample is a duplicate, helping ensure that crop diversity can be recovered if seeds are lost."),
        _ev("E2", "Existe um portal de sementes com aba de depositantes.",
            "see the Depositors tab in the menu above"),
        _ev("E3", "As câmaras ficam a -18 °C.", "the chambers are kept at minus 18C."),
    ]}


def _fake(respostas, chamadas):
    def complete(messages, **kw):
        chamadas.append(kw)
        r = respostas.pop(0) if respostas else None
        if isinstance(r, Exception):
            raise r
        return json.dumps(r)
    return complete


def test_isolamento_do_cache(tmp_path):
    assert "cache_teste" in str(disk_cache.BASE) and str(tmp_path) in str(disk_cache.BASE)


def test_corrigir_descartar_e_fiel(monkeypatch):
    chamadas = []
    monkeypatch.setattr(providers, "complete", _fake([{"revisoes": [
        {"n": 1, "decisao": "corrigir", "motivo": "garantindo > helping ensure",
         "afirmacao_corrigida": "Cada amostra é uma duplicata, o que ajuda a garantir que a diversidade possa ser recuperada."},
        {"n": 2, "decisao": "descartar", "motivo": "texto de interface"},
        {"n": 3, "decisao": "fiel"}]}], chamadas))
    banco = _banco()
    novo = evidencias.revisar_afirmacoes(banco, TEMA)
    e = evidencias.por_id(novo)
    assert chamadas and chamadas[0].get("juiz") is True and chamadas[0].get("temperature") == 0.1
    assert e["E1"]["revisada"] == "corrigida"
    assert e["E1"]["afirmacao"].startswith("Cada amostra é uma duplicata, o que ajuda")
    assert e["E1"]["afirmacao_original"] == banco["evidencias"][0]["afirmacao"]
    assert e["E2"]["revisada"] == "descartada" and e["E2"]["status"] == "nao_verificado"
    assert [x["id"] for x in evidencias.usaveis(novo)] == ["E1", "E3"]
    assert e["E3"]["revisada"] == "fiel" and e["E3"]["afirmacao"] == "As câmaras ficam a -18 °C."
    assert banco["evidencias"][0].get("revisada") is None  # o banco recebido não muda
    with pytest.raises(evidencias.PesquisaInsuficiente):
        evidencias.exigir_suficiente(novo, 3)  # descartada não conta


def test_numero_fora_do_trecho_recusa_correcao(monkeypatch):
    monkeypatch.setattr(providers, "complete", _fake([{"revisoes": [
        {"n": 3, "decisao": "corrigir", "afirmacao_corrigida": "As câmaras ficam a -20 °C desde 2008."}]}], []))
    novo = evidencias.revisar_afirmacoes(_banco(), TEMA)
    e = evidencias.por_id(novo)["E3"]
    assert e["afirmacao"] == "As câmaras ficam a -18 °C." and "revisada" not in e


def test_juiz_fora_cai_na_cascata_e_sem_resposta_nada_muda(monkeypatch):
    chamadas = []
    monkeypatch.setattr(providers, "complete", _fake([RuntimeError("juiz fora"), RuntimeError("cascata fora")], chamadas))
    banco = _banco()
    novo = evidencias.revisar_afirmacoes(banco, TEMA)
    assert novo["evidencias"] == banco["evidencias"]
    assert chamadas[0].get("juiz") is True and chamadas[1].get("model_filter") is providers.is_strong_model


def test_provedores_indisponiveis_sobe(monkeypatch):
    monkeypatch.setattr(providers, "complete",
                        _fake([providers.ProvedoresIndisponiveis("descanso longo")], []))
    with pytest.raises(providers.ProvedoresIndisponiveis):
        evidencias.revisar_afirmacoes(_banco(), TEMA)


def test_resposta_incompleta_mantem_os_sem_decisao(monkeypatch):
    monkeypatch.setattr(providers, "complete", _fake([{"revisoes": [{"n": 2, "decisao": "descartar"}]}], []))
    e = evidencias.por_id(evidencias.revisar_afirmacoes(_banco(), TEMA))
    assert e["E2"]["status"] == "nao_verificado"
    assert e["E1"]["status"] == "oficial" and "revisada" not in e["E1"]


def test_cache_por_evidencia(monkeypatch):
    chamadas = []
    resp = {"revisoes": [{"n": 1, "decisao": "fiel"}, {"n": 2, "decisao": "descartar", "motivo": "menu"},
                         {"n": 3, "decisao": "fiel"}]}
    monkeypatch.setattr(providers, "complete", _fake([resp], chamadas))
    evidencias.revisar_afirmacoes(_banco(), TEMA)
    assert len(chamadas) == 1
    novo = evidencias.revisar_afirmacoes(_banco(), TEMA)  # a fila de respostas está vazia: chamar quebraria
    assert len(chamadas) == 1
    assert evidencias.por_id(novo)["E2"]["revisada"] == "descartada"


def test_so_utilizaveis_vao_ao_juiz(monkeypatch):
    vistos = []

    def complete(messages, **kw):
        vistos.append(messages[0]["content"])
        return json.dumps({"revisoes": []})
    monkeypatch.setattr(providers, "complete", complete)
    banco = _banco()
    banco["evidencias"][1]["status"] = "nao_verificado"
    evidencias.revisar_afirmacoes(banco, TEMA)
    assert "Depositors" not in vistos[0] and "minus 18C" in vistos[0]
