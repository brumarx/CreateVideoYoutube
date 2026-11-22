"""Diversidade temática (20/11): a camada UNIVERSAL de evidências e auditoria em 8 temas de naturezas
diferentes — biologia, história, geografia e clima, ciência, engenharia, sociedade, mistérios e um tema
sem regra própria (culinária). Nenhuma regra por categoria: o mesmo código decide tudo. Zero rede e zero
IA: a extração, o juiz e o auditor são simulados (a parte semântica real é avaliada à parte, com IA grátis).

Cada tema (tests/fixtures/diversidade.json) tem a fonte SOBRE o assunto, uma fonte RELACIONADA que só cita
o assunto em outra parte e uma com valor DIVERGENTE, e um roteiro com erros plantados."""
import json
import re
from pathlib import Path

import pytest

from src import evidencias as ev
from src import fatos as ft
from src import providers
from src import redacao as rd
from tests.auditor_falso import AUD

TEMAS = json.loads((Path(__file__).parent / "fixtures" / "diversidade.json").read_text())["temas"]
IDS = [t["categoria"] for t in TEMAS]


class IA:
    """Extração devolve as afirmações da fixture; o juiz recusa só as da fonte relacionada e aprova o resto
    (o código tem de chegar lá sozinho no que é dele); o auditor responde AUD com as exceções dadas."""

    def __init__(self, t, auditor=None, conserto=None):
        self.t, self.auditor, self.conserto = t, auditor, conserto
        self.juiz_recebeu: list[str] = []
        self.pedidos_juiz: list[str] = []

    def __call__(self, messages, **kw):
        p = messages[-1]["content"]
        if "Você confere se o TRECHO" in p:
            self.pedidos_juiz.append(p)
            out = []
            for n, (af, tr) in enumerate(re.findall(r"AFIRMAÇÃO: (.+?)\n.*?TRECHO: \"(.+?)\"\n", p, re.S), 1):
                self.juiz_recebeu.append(af)
                relacionada = any(e["afirmacao"] == af and e["_esperado"] == "relacionada" for e in self.t["extracao"])
                out.append({"n": n, "decisao": "irrelevante" if relacionada else "sustenta",
                            "sujeito": "outro" if relacionada else "ok", "medida": "nao_se_aplica",
                            "trecho_chave": " ".join(tr.split()[:3]),
                            "justificativa": "o trecho trata do assunto em geral" if relacionada else "o trecho diz isso"})
            return json.dumps({"julgamentos": out})
        if "Você é o auditor" in p or "ESPECTADOR atento" in p or "Você é o EDITOR" in p:
            return json.dumps((self.auditor or AUD(8))(p))
        if "EDIÇÃO LOCALIZADA" in p:
            return json.dumps(self.conserto)
        return json.dumps({"evidencias": [{k: v for k, v in e.items() if k != "_esperado"} for e in self.t["extracao"]],
                           "lacunas": []})


def _banco(t, monkeypatch, **kw) -> tuple[dict, "IA"]:
    ia = IA(t, **kw)
    monkeypatch.setattr(providers, "complete", ia)
    banco = ev.montar_banco(t["tema"], t["fontes"])
    assert banco is not None
    return banco, ia


def _roteiro(t):
    fr = [{k: v for k, v in f.items() if k != "_esperado"} for f in t["roteiro"]]
    return {"title": t["tema"][:40], "scenes": [{"frases": [f], "narration": f["texto"]} for f in fr]}


# ======================================================================= banco de evidências
@pytest.mark.parametrize("t", TEMAS, ids=IDS)
def test_cada_afirmacao_recebe_o_status_certo(t, monkeypatch):
    banco, _ = _banco(t, monkeypatch)
    for e, x in zip(banco["evidencias"], t["extracao"]):
        esp = x["_esperado"]
        if esp == "fato":
            assert e["status"] in ev.STATUS_USAVEIS and not e["hipotese"], e
        elif esp == "estimado":
            assert e["status"] in ev.STATUS_USAVEIS and e["estimado"] and not e["hipotese"], e
        elif esp == "hipotese":
            assert e["status"] in ev.STATUS_USAVEIS and e["hipotese"], e
        elif esp.startswith("conflito"):
            assert e["status"] == "contraditorio" and e["conflitos"], e
        elif esp in ("relacionada", "numero_errado"):
            assert e["status"] == "nao_verificado", e


@pytest.mark.parametrize("t", TEMAS, ids=IDS)
def test_fonte_relacionada_nao_comprova_e_quem_pega_e_o_esperado(t, monkeypatch):
    """O código só recusa quando SABE que a página fala do assunto em outra parte (mesmo nome); assunto sem
    nome próprio ou com outro nome na página vai ao juiz — e nos dois casos a evidência fica fora."""
    banco, ia = _banco(t, monkeypatch)
    rel = next(x["afirmacao"] for x in t["extracao"] if x["_esperado"] == "relacionada")
    e = next(e for e in banco["evidencias"] if e["afirmacao"] == rel)
    assert e["status"] == "nao_verificado"
    if t["relacionada_pega_por"] == "codigo":
        assert rel not in ia.juiz_recebeu and "só em outra parte" in e["motivo"]
    else:
        assert rel in ia.juiz_recebeu and "irrelevante" in e["motivo"]


@pytest.mark.parametrize("t", [t for t in TEMAS if t["categoria"] in ("sociedade e países", "mistérios e curiosidades")],
                         ids=["Butão/Bhutan", "Bermudas/Bermuda"])
def test_assunto_com_outro_nome_na_fonte_nao_e_descartado(t, monkeypatch):
    """Não descartar fato verdadeiro por falha do mecanismo: a página diz "Bhutan"/"Bermuda Triangle"; o juiz
    recebe o aviso de que o nome não aparece e decide — os fatos verdadeiros continuam usáveis."""
    banco, ia = _banco(t, monkeypatch)
    fatos = [e for e, x in zip(banco["evidencias"], t["extracao"]) if x["_esperado"] in ("fato", "estimado", "hipotese")]
    assert fatos and all(e["status"] in ev.STATUS_USAVEIS for e in fatos)
    assert any("ATENÇÃO: o nome" in p for p in ia.pedidos_juiz)


def test_tema_composto_basta_uma_palavra_do_nome():
    """"Grande Muralha da China": a página em inglês diz "Great Wall of China" — "China" basta."""
    t = next(t for t in TEMAS if t["categoria"] == "história")
    fonte = t["fontes"][0]
    assert ev.sustentacao_por_codigo(t["extracao"][0]["afirmacao"], t["extracao"][0]["citacoes"][0]["trecho"], fonte,
                                     ev.sujeitos_do_tema(t["tema"])) is None


# ======================================================================= roteiro: código + auditor + conserto
@pytest.mark.parametrize("t", TEMAS, ids=IDS)
def test_roteiro_cada_erro_plantado_e_pego_e_o_correto_passa(t, monkeypatch):
    banco, _ = _banco(t, monkeypatch)
    s = rd.sincronizar(_roteiro(t))
    out, probs = rd.auditar_codigo(s, banco, t["tema"])
    por_cena = {p["cena"]: p["tipo"] for p in probs}
    for k, f in enumerate(t["roteiro"]):
        esp, tipo, texto = f["_esperado"], por_cena.get(k), out["scenes"][k]["frases"][0]["texto"]
        if esp == "ok":
            assert tipo is None, (f["texto"], probs)
        elif esp == "contraditorio":
            assert tipo == "numero_contraditorio"
        elif esp == "hipotese_como_fato":
            assert tipo == "hipotese_como_fato"
        elif esp == "numero":
            assert tipo in ("numero_sem_evidencia", "numero_arredondado")
        elif esp == "estimativa_marcada":
            assert tipo is None and re.search(r"cerca de|aproximadamente|estima|calcula", texto, re.I), texto


@pytest.mark.parametrize("t", TEMAS, ids=IDS)
def test_causa_inventada_e_pega_pelo_auditor_com_justificativa(t, monkeypatch):
    k = next(k for k, f in enumerate(t["roteiro"]) if f["_esperado"] == "causal")
    banco, _ = _banco(t, monkeypatch, auditor=AUD(8, especiais={k + 1: ("excede", "a evidência não sustenta essa causa")}))
    r = rd.auditar_ia(rd.sincronizar(_roteiro(t)), banco, t["tema"])
    assert r is not None
    probs, q = r
    assert [d["n"] for d in q["decisoes"]] == list(range(1, len(t["roteiro"]) + 1))
    assert any(p["cena"] == k and p["tipo"] == "excede" and "causa" in p["motivo"] for p in probs)


@pytest.mark.parametrize("t", TEMAS, ids=IDS)
def test_edicao_corrige_a_hipotese_sem_destruir_a_narrativa(t, monkeypatch):
    k = next(k for k, f in enumerate(t["roteiro"]) if f["_esperado"] == "hipotese_como_fato")
    hip = next(f"E{n}" for n, x in enumerate(t["extracao"], 1) if x["_esperado"] == "hipotese")
    banco, _ = _banco(t, monkeypatch, conserto={"cenas": [{"cena": k + 1, "funcao": "f", "preserva": "p", "corrige": "c",
                                                            "frases": [{"texto": t["conserto"], "tipo": "factual",
                                                                        "evidencias": [hip]}]}], "titulo": ""})
    s = rd.sincronizar(_roteiro(t))
    s, probs = rd.auditar_codigo(s, banco, t["tema"])
    alvo = [p for p in probs if p["tipo"] == "hipotese_como_fato"]
    novo, reg = rd.editar_cenas(s, rd.alvos_da_edicao(s, {"nota": 9}, alvo), banco, t["tema"], problemas=alvo)
    assert reg["cenas_editadas"] == [k + 1]
    antes = [f["texto"] for _, _, f in rd.frases_de(s)]
    depois = [f["texto"] for _, _, f in rd.frases_de(novo)]
    assert depois[k] == t["conserto"]                                     # a hipótese virou hipótese
    assert depois[:k] + depois[k + 1:] == antes[:k] + antes[k + 1:]       # o resto da história ficou intacto
    assert not [p for p in rd.auditar_codigo(novo, banco, t["tema"])[1] if p["cena"] == k]


def test_nenhuma_regra_por_categoria_no_codigo():
    """A camada é universal: nenhum nome de categoria ou de tema das fixtures aparece nas regras."""
    codigo = (Path(rd.__file__).read_text() + Path(ev.__file__).read_text()).lower()
    for palavra in ("tardígrado", "tardigrade", "muralha", "atacama", "sagittarius", "burj", "butão", "bhutan",
                    "bermuda", "culinária", "honey", " mel ", "eurot", "pancras", "mancha"):
        assert palavra not in codigo, palavra
    assert "biologia" not in codigo and "categoria" not in codigo


# ======================================================================= paráfrase: o código acha, a IA confirma
PARAFRASES = [  # escritas pela extração REAL na avaliação de 20/11
    ("O Deserto do Atacama se estende por mais de 1.600 km ao longo da costa do Pacífico na América do Sul.",
     "O Deserto do Atacama se estende por cerca de 1.000 km ao longo da costa do Chile."),
    ("Durante a construção, o concreto foi bombeado até a altura recorde de 606 metros.",
     "Segundo outra fonte, o concreto foi bombeado a uma altura de 580 metros."),
]


def _ev_simples(i, afirmacao):
    return {"id": f"E{i}", "afirmacao": afirmacao, "status": "fonte_unica", "estimado": False,
            "numeros": sorted(ft.numeros(afirmacao)), "citacoes": [{"verificado": True, "trecho": afirmacao, "url": "x"}]}


@pytest.mark.parametrize("a,b", PARAFRASES)
def test_parafrase_com_valor_diferente_vira_conflito_pelo_codigo_ou_pela_ia(a, b):
    perguntas = []

    def confirmar(pares):
        perguntas.extend(pares)
        return {0}
    evs = [_ev_simples(1, a), _ev_simples(2, b)]
    ev.marcar_conflitos(evs, {"atacama", "deserto"}, confirmar=confirmar)
    assert len(perguntas) <= 1 and [e["status"] for e in evs] == ["contraditorio", "contraditorio"]


def test_parafrase_que_a_ia_diz_ser_outra_medida_nao_vira_conflito():
    evs = [_ev_simples(1, PARAFRASES[1][0]), _ev_simples(2, PARAFRASES[1][1])]
    ev.marcar_conflitos(evs, set(), confirmar=lambda pares: set())
    assert [e["status"] for e in evs] == ["fonte_unica", "fonte_unica"]


def test_ia_sem_resposta_valida_conta_como_divergente(monkeypatch):
    monkeypatch.setattr(providers, "complete", lambda m, **kw: '{"pares": [{"n": 1, "mesma_medida": false, "justificativa": "x"}]}')
    pares = [(_ev_simples(1, PARAFRASES[0][0]), _ev_simples(2, PARAFRASES[0][1]))]
    assert ev.mesma_medida_ia(pares, "tema") == {0}       # justificativa vazia demais: não vale como decisão
    monkeypatch.setattr(providers, "complete", lambda m, **kw: json.dumps(
        {"pares": [{"n": 1, "mesma_medida": False, "justificativa": "uma fala da costa do Pacífico, a outra só do Chile"}]}))
    assert ev.mesma_medida_ia(pares, "tema") == set()


def test_medidas_diferentes_do_mesmo_objeto_nem_chegam_a_ia():
    """"A torre tem 828 m de altura" × "o concreto foi bombeado a 606 m": uma palavra em comum só, não é candidato."""
    chamou = []
    evs = [_ev_simples(1, "A torre tem 828 m de altura total."), _ev_simples(2, "O concreto foi bombeado a 606 m na obra.")]
    ev.marcar_conflitos(evs, set(), confirmar=lambda pares: chamou.append(pares) or {0})
    assert not chamou and [e["status"] for e in evs] == ["fonte_unica", "fonte_unica"]


def test_juiz_sabe_que_classe_geral_nao_comprova_caso_especifico():
    assert "CLASSE em\n  geral" in ev._PROMPT_SUSTENTACAO and '"sujeito": "outro"' in ev._PROMPT_SUSTENTACAO
