"""23/11 (Svalbard, jobs 1001-1006): mapa do conhecimento (natureza e autoridade de cada evidência), plano da
história antes da escrita, vocabulário de precisão (dano parcial ≠ destruição), gravidade na leitura de
espectador (só erro factual ou engano razoável bloqueiam) e registro completo de toda reprovação."""
import json
import logging

import pytest

from src import fatos as ft
from src import providers as pv
from src import redacao as rd


def _ev(id_, afirmacao, dominio, classe, status="oficial", trecho=None, **extra):
    return {"id": id_, "afirmacao": afirmacao, "tipo": "qualitativo", "status": status, "estimado": False,
            "numeros": sorted(ft.numeros(afirmacao)), "conflitos": [], **extra,
            "citacoes": [{"fonte": id_, "url": f"https://{dominio}/x", "titulo": "x", "dominio": dominio, "classe": classe,
                          "trecho": trecho or afirmacao, "verificado": True}]}


BANCO = {"versao": 1, "tema": "t", "fontes": [], "lacunas": [], "nao_lidos": [], "evidencias": [
    _ev("E1", "O cofre guarda cópias de segurança de bancos de genes.", "seedvault.no", "oficial"),
    _ev("E2", "O governo pagou a construção.", "regjeringen.no", "outra", "fonte_unica"),
    _ev("E3", "O permafrost mantém as câmaras frias.", "pages.hmc.edu", "oficial", "fonte_unica"),
    _ev("E4", "O cofre fica numa montanha.", "en.wikipedia.org", "enciclopedia", "fonte_unica"),
    _ev("E5", "A capacidade é de cerca de 4,5 milhões de amostras.", "croptrust.org", "oficial", estimado=True),
    _ev("E6", "O banco de Alepo foi parcialmente danificado pela guerra.", "croptrust.org", "oficial",
        trecho="the Aleppo genebank was partially damaged by the war"),
    _ev("E7", "Uma página que ninguém conferiu.", "blog.example", "outra", "nao_verificado"),
]}


# ======================================================================= mapa do conhecimento
def test_mapa_classifica_natureza_e_autoridade_sem_contar_a_nao_verificada():
    m = {x["id"]: x for x in rd.mapa_do_conhecimento(BANCO)}
    assert (m["E1"]["autoridade"], m["E1"]["natureza"]) == ("primária", "fonte primária/oficial")
    assert m["E2"]["autoridade"] == "institucional" and m["E2"]["natureza"] == "fonte única"
    assert m["E3"]["autoridade"] == "científica"
    assert m["E4"]["autoridade"] == "enciclopédia" and m["E4"]["so_enciclopedia"]
    assert m["E5"]["natureza"] == "estimativa"
    assert "E7" not in m
    bloco = rd.bloco_do_mapa(list(m.values()))
    assert "só enciclopédia" in bloco and "autoridade primária: seedvault.no" in bloco


# ======================================================================= plano antes da escrita
PLANO = {"angulo": {"pergunta": "o que acontece quando um banco de genes some?", "resposta": "a cópia está no cofre"},
         "blocos": [{"funcao": "abertura", "conta": "um banco some", "evidencias": ["E6"]},
                    {"funcao": "o problema", "conta": "bancos de genes podem perder coleções", "evidencias": ["E6"]},
                    {"funcao": "como funciona", "conta": "cópias de segurança", "evidencias": ["E1"]},
                    {"funcao": "por que ali", "conta": "frio da montanha", "evidencias": ["E3", "E4"]},
                    {"funcao": "conclusão", "conta": "a cópia volta", "evidencias": ["E1", "E99"]}],
         "evidencias": {"E1": {"comprova": "guarda cópias", "nao_comprova": "que todas as coleções do mundo estão lá"},
                        "E3": {"comprova": "frio natural", "nao_comprova": "temperatura exata"},
                        "E4": {"comprova": "fica numa montanha", "nao_comprova": "profundidade"},
                        "E6": {"comprova": "dano parcial em Alepo", "nao_comprova": "destruição total"}},
         "nao_exagerar": ["dano parcial não é destruição"]}


class Canal:
    name = "curiosidades"
    channel_title = "Fractal"
    niche = ""


@pytest.mark.plano
def test_escritor_recebe_o_plano_e_so_as_evidencias_dele(monkeypatch):
    pedidos = []
    roteiro = json.dumps({"titulo": "O cofre", "descricao": "d", "tags": [], "cenas": [
        {"frases": [{"texto": "Um banco de genes pode sumir.", "tipo": "narrativa", "evidencias": []}]}]})

    def complete(messages, **kw):
        p = messages[-1]["content"]
        pedidos.append(p)
        return json.dumps(PLANO) if "Você planeja a HISTÓRIA" in p else roteiro
    monkeypatch.setattr(pv, "complete", complete)
    monkeypatch.setattr("src.script_gen.recent_mistakes", lambda *a: "")
    rd.escrever(Canal(), "Svalbard", BANCO, min_minutes=2.5, max_minutes=5, cenas_ref=12, tentativas=1)
    plano_p, escrita = pedidos[0], pedidos[1]
    assert "MAPA DO CONHECIMENTO" in plano_p and "E7" not in plano_p                # não verificada fica fora
    assert "PLANO DA HISTÓRIA" in escrita and "o que acontece quando um banco de genes some?" in escrita
    assert "NÃO comprova: destruição total" in escrita
    # 25/11: fora do plano o escritor vê só a afirmação, em OUTROS FATOS APROVADOS (citava id errado sem eles)
    plano_parte, outros = escrita.split("OUTROS FATOS APROVADOS")
    assert "E2 [" not in plano_parte and "E5 [" not in plano_parte and "E2 [" in outros
    assert "trecho (" not in outros.split("Responda SÓ JSON")[0]
    assert "E99" not in escrita                                                      # id inventado pelo plano sai


@pytest.mark.plano
def test_plano_invalido_volta_ao_banco_inteiro(monkeypatch):
    pedidos = []

    def complete(messages, **kw):
        p = messages[-1]["content"]
        pedidos.append(p)
        if "Você planeja a HISTÓRIA" in p:
            assert kw["validate"](json.dumps({"blocos": []})) is False
            raise RuntimeError("Nenhum provedor devolveu resposta válida")
        return json.dumps({"titulo": "t", "descricao": "d", "tags": [], "cenas": [
            {"frases": [{"texto": "Um banco de genes pode sumir.", "tipo": "narrativa", "evidencias": []}]}]})
    monkeypatch.setattr(pv, "complete", complete)
    monkeypatch.setattr("src.script_gen.recent_mistakes", lambda *a: "")
    rd.escrever(Canal(), "Svalbard", BANCO, min_minutes=2.5, max_minutes=5, cenas_ref=12, tentativas=1)
    assert "EVIDÊNCIAS (fontes oficiais" in pedidos[-1] and "PLANO DA HISTÓRIA" not in pedidos[-1]


# ======================================================================= precisão: dano parcial não é destruição
def test_dano_parcial_que_vira_destruicao_e_exagero_e_destruicao_dita_pela_fonte_passa():
    ev = {e["id"]: e for e in BANCO["evidencias"]}
    assert rd.marcadores_sem_suporte("A guerra destruiu o banco de Alepo.", [ev["E6"]]) == {"extensao": "destruiu"}
    destruido = _ev("E8", "O banco das Filipinas foi destruído por um incêndio.", "croptrust.org", "oficial",
                    trecho="the Philippines genebank was destroyed by a fire")
    assert rd.marcadores_sem_suporte("O banco das Filipinas foi destruído por um incêndio.", [destruido]) == {}
    assert rd.marcadores_sem_suporte("O túnel tinha uma vulnerabilidade crônica.", [ev["E1"]]) == {"frequencia": "crônica"}


# ======================================================================= gravidade na leitura de espectador
LISTA = [{"texto": f"Frase número {k}.", "tipo": "factual", "evidencias": ["E1"]} for k in range(1, 7)]


def _espectador(*leituras):
    fila = [json.dumps({"conclusoes_do_publico": ["a coisa um", "a coisa dois"], "enganos": e}) for e in leituras]
    return lambda messages, **kw: fila.pop(0)


def G(frases, gravidade, razao="porque é a leitura natural da frase", tipo="combinacao"):
    return {"frases": frases, "tipo": tipo, "gravidade": gravidade, "o_publico_conclui": "o público conclui algo errado",
            "por_que_engana": "a fonte não diz isso", "por_que_razoavel": razao, "como_corrigir": "diga só o que a fonte diz"}


def test_so_erro_factual_ou_engano_razoavel_bloqueia_ambiguidade_e_alerta_improvavel_sai(monkeypatch):
    monkeypatch.setattr(pv, "complete", _espectador(
        [G([1, 2], "erro_factual"), G([3], "ambiguidade"), G([4], "improvavel"), G([5], "estilo")],
        [G([1, 2], "enganosa"), G([3], "ambiguidade"), G([4], "improvavel")],
        [G([4], "improvavel")]))
    r = rd.leitura_espectador({"scenes": [{"narration": "x", "frases": LISTA}]}, {"evidencias": []}, "t")
    assert [e["frases"] for e in r["enganos"]] == [[1, 2]]                 # erro factual + enganosa: 2 votos
    assert [a["frases"] for a in r["alertas"]] == [[3]]                    # ambiguidade: alerta, não bloqueia
    assert all(4 not in a["frases"] and 5 not in a["frases"] for a in r["enganos"] + r["alertas"])


def test_engano_sem_dizer_por_que_e_razoavel_nao_bloqueia(monkeypatch):
    monkeypatch.setattr(pv, "complete", _espectador([G([2], "enganosa", razao="")], [G([2], "enganosa", razao="")], []))
    r = rd.leitura_espectador({"scenes": [{"narration": "x", "frases": LISTA}]}, {"evidencias": []}, "t")
    assert r["enganos"] == [] and [a["frases"] for a in r["alertas"]] == [[2]]


def test_prompt_do_espectador_pede_gravidade_e_por_que_e_razoavel():
    for g in rd.GRAVIDADES:
        assert f'"{g}"' in rd._PROMPT_ESPECTADOR
    assert "por_que_razoavel" in rd._PROMPT_ESPECTADOR


# ======================================================================= registro completo da reprovação
def test_toda_reprovacao_fica_no_log_com_texto_cena_evidencia_trecho_e_motivo(caplog):
    s = {"scenes": [{"narration": "x", "frases": []},
                    {"narration": "A guerra destruiu o banco de Alepo.",
                     "frases": [{"texto": "A guerra destruiu o banco de Alepo.", "tipo": "factual", "evidencias": ["E6"]}]}]}
    p = rd._p("excede", "A guerra destruiu o banco de Alepo.", 1, 0, "a fonte diz parcialmente danificado")
    with caplog.at_level(logging.INFO, logger="redacao"):
        rd._registra_problemas(3, s, [p], BANCO)
    txt = caplog.text
    for trecho in ("reprovação v3", "cena 2", "excede", "A guerra destruiu o banco de Alepo.", "E6 (oficial, croptrust.org)",
                   "partially damaged by the war", "a fonte diz parcialmente danificado"):
        assert trecho in txt, trecho


def test_prompt_de_edicao_pede_a_parte_exata_o_trecho_e_a_forma_mais_restrita():
    for trecho in ("parte EXATA", "MAIS RESTRITA", "dano parcial vira destruição", "\"reprovado\"", "\"trecho\""):
        assert trecho in rd._PROMPT_EDICAO, trecho


# ======================================================================= correção que completa a frase com nome novo
def test_correcao_factual_que_poe_pais_que_nem_a_cena_nem_a_evidencia_trazem_e_recusada():
    orig = [{"texto": "O ICARDA retirou as sementes porque a guerra destruiu o banco de Alepo.", "tipo": "factual",
             "evidencias": ["E6"]}]
    motivos = ["FACTUAL — diz mais do que a evidência na frase ...: parcialmente danificado"]
    inventada = [{"texto": "O ICARDA retirou as sementes e as regenerou no Marrocos e no Líbano.", "tipo": "factual",
                  "evidencias": ["E6"]}]
    m = rd.nome_novo_na_correcao(inventada, orig, motivos, BANCO)
    assert m and "marrocos" in m and "libano" in m
    restrita = [{"texto": "O ICARDA retirou as sementes porque a guerra danificou parte do banco de Alepo.", "tipo": "factual",
                 "evidencias": ["E6"]}]
    assert rd.nome_novo_na_correcao(restrita, orig, motivos, BANCO) is None
    # nome que vem da evidência citada pode entrar (restringir um caso pede dizer qual é)
    sem_alepo = [{"texto": "O ICARDA retirou as sementes por causa da guerra.", "tipo": "factual", "evidencias": ["E6"]}]
    com_fonte = [{"texto": "O ICARDA retirou as sementes porque o banco de Alepo foi parcialmente danificado.",
                  "tipo": "factual", "evidencias": ["E6"]}]
    assert rd.nome_novo_na_correcao(com_fonte, sem_alepo, motivos, BANCO) is None  # Alepo está na E6
    # edição de HISTÓRIA pode trazer informação nova
    assert rd.nome_novo_na_correcao(inventada, orig, ["HISTÓRIA — novidade: x"], BANCO) is None
