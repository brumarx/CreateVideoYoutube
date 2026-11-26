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


def test_decisoes_numeradas_de_um_sao_lidas_pela_posicao():
    d = {"frases": [{"n": k, "decisao": "sustentada", "justificativa": "a fonte diz isso"} for k in (1, 2, 3)]}
    assert sorted(rd._decisoes_validas(d, {13, 14, 15})) == [13, 14, 15]
    # com algum número do bloco, a numeração vale como veio
    d2 = {"frases": [{"n": 13, "decisao": "sustentada", "justificativa": "a fonte diz isso"},
                     {"n": 2, "decisao": "sustentada", "justificativa": "a fonte diz isso"}]}
    assert sorted(rd._decisoes_validas(d2, {13, 14, 15})) == [13]


@pytest.mark.parametrize("bruto", ["factual: sustentada", "Sustentada", '"sustentada"', "narrativa: narrativa_ok"])
def test_decisao_com_rotulo_e_lida(bruto):
    assert rd._decisao_normalizada(bruto) in ("sustentada", "narrativa_ok")
    d = {"frases": [{"n": 1, "decisao": bruto, "justificativa": "a fonte diz isso"}]}
    assert rd._decisoes_validas(d, {1})


@pytest.mark.segunda_opiniao
def test_segunda_opiniao_retira_so_com_justificativa(ambiente):
    responde, pedidos = ambiente
    responde({"revisao": [{"n": 1, "decisao": "retira", "justificativa": "a frase diz ajuda a manter, o fato diz ajuda"},
                          {"n": 2, "decisao": "confirma", "justificativa": "garante é mais forte que ajuda"},
                          {"n": 3, "decisao": "retira", "justificativa": "curta"}]})
    itens = [{"texto": f"frase {k}", "evidencias": ["E2"], "motivo": "excede"} for k in range(3)]
    assert rd.segunda_opiniao(itens, {}) == {0: "a frase diz ajuda a manter, o fato diz ajuda"}
    assert "MOTIVO DA REPROVAÇÃO: excede" in pedidos[0] and "FATO E2" in pedidos[0]


@pytest.mark.segunda_opiniao
def test_segunda_opiniao_sem_resposta_nao_retira(monkeypatch):
    def falha(*a, **k):
        raise RuntimeError("fora do ar")
    monkeypatch.setattr(providers, "complete", falha)
    monkeypatch.setattr(rd, "mapa_do_conhecimento", lambda banco: [])
    assert rd.segunda_opiniao([{"texto": "x", "evidencias": [], "motivo": "excede"}], {}) == {}


@pytest.mark.segunda_opiniao
def test_conferencia_retirada_na_segunda_opiniao_ainda_passa_pela_trava_de_causa(monkeypatch):
    monkeypatch.setattr(rd, "mapa_do_conhecimento", lambda banco: [
        {"id": "E2", "afirmacao": "O permafrost ajuda a manter o frio.", "trecho": ""}])
    respostas = iter([json.dumps({"decisoes": [{"n": 1, "decisao": "excede", "o_que_excede": "x"},
                                               {"n": 2, "decisao": "excede", "o_que_excede": "y"}]}),
                      json.dumps({"revisao": [{"n": 1, "decisao": "retira", "justificativa": "diz o mesmo que o fato E2"},
                                              {"n": 2, "decisao": "retira", "justificativa": "diz o mesmo que o fato E2"}]})])
    monkeypatch.setattr(providers, "complete", lambda msgs, **k: next(respostas))
    v = rd.conferir_contra_trechos([{"texto": "O permafrost ajuda a manter o frio.", "evidencias": ["E2"]},
                                    {"texto": "Por isso o cofre nunca esquenta.", "evidencias": ["E2"]}], {})
    assert v[0] is None and v[1]  # a 2ª tem conector de causa sem causa no fato: a trava em código segura


def test_fechamento_pedido_lista_todas_as_palavras_que_a_conferencia_recusa():
    """Svalbard 26/11: "protege" era recusado pela regex e não estava no pedido — 3 recusas iguais."""
    pedido = rd._PROMPT_FECHAMENTO.format(tema="t", pergunta="Como ela protege?", ponto="p", roteiro="r",
                                          proibidas=rd._PALAVRAS_ALEGA)
    palavras = rd._ALEGA.pattern.split("(", 1)[1].split(")", 1)[0].split("|")
    assert palavras and all(f'"{p}"' in pedido for p in palavras)


@pytest.mark.parametrize("texto,esperado", [
    ("with a stable temperature of between minus 3 and 4°C.", {("-3", "°C"), ("-4", "°C")}),
    ("cerca de -3 a -4 °C", {("-3", "°C"), ("-4", "°C")}),
    ("a constant minus 18C.", {("-18", "°C")}),
    ("between -3 and -4 degrees Celsius", {("-3", "°C"), ("-4", "°C")}),
    ("entre 3 e 4 graus negativos", {("-3", "°C"), ("-4", "°C")}),
    ("de 10 a 15 graus", {("10", "°C"), ("15", "°C")}),
    ("Em 2004 e 3 °C", {("3", "°C")}),
])
def test_temperaturas_em_faixa_e_em_ingles(texto, esperado):
    """Svalbard 26/11: "minus 3 and 4°C" era lido como +4 e a frase certa "-3 a -4 °C" reprovou em toda versão."""
    from src.fatos import temperaturas
    assert temperaturas(texto) == esperado


def test_sinal_certo_se_uma_evidencia_concorda():
    ev = [{"id": "E1", "afirmacao": "O permafrost fica a 4 °C.", "citacoes": []},
          {"id": "E2", "afirmacao": "O permafrost fica entre -3 e -4 °C.", "citacoes": []}]
    assert rd._sinal_invertido("O permafrost mantém -3 a -4 °C.", ev) is None
    assert rd._sinal_invertido("O permafrost mantém 4 °C.", ev[1:]) is not None


def test_pergunta_do_plano_na_abertura_nao_vira_fato():
    """Svalbard 1029: "…sem nunca plantar uma única semente?" reprovou como fato absoluto sem evidência."""
    p = "Como uma estrutura fria protege a comida sem nunca plantar uma única semente?"
    s = {"scenes": [{"frases": [{"texto": p, "tipo": "factual", "evidencias": []}], "narration": p}],
         "_plano": {"angulo": {"pergunta": p}}}
    s2, probs = rd.auditar_codigo(s, {"evidencias": []}, "Svalbard")
    assert s2["scenes"][0]["frases"][0]["tipo"] == "narrativa"
    assert not [x for x in probs if x["termo"] == p]
    s.pop("_plano")
    assert [x for x in rd.auditar_codigo(s, {"evidencias": []}, "Svalbard")[1] if x["termo"] == p]


def test_medida_tem_dono_no_plano_e_os_outros_blocos_sao_avisados():
    """Svalbard 26/11: −18 °C nas câmaras (bloco 2) e de novo nas sementes recalcitrantes (bloco 5)."""
    banco = {"evidencias": [
        {"id": "E1", "afirmacao": "As câmaras ficam a -18 °C.", "status": "oficial", "citacoes": []},
        {"id": "E2", "afirmacao": "Sementes recalcitrantes morrem a -18 °C.", "status": "oficial", "citacoes": []},
        {"id": "E3", "afirmacao": "O cofre abriu em 2008.", "status": "oficial", "citacoes": []}]}
    plano = {"blocos": [{"evidencias": ["E3"]}, {"evidencias": ["E1"]}, {"evidencias": ["E2"]}]}
    avisos = rd.donos_de_medida(plano, banco)
    assert list(avisos) == [3] and avisos[3][0][1] == 2


def test_escritor_recebe_cada_valor_com_seu_bloco():
    banco = {"evidencias": [
        {"id": "E1", "afirmacao": "As câmaras ficam a -18 °C.", "status": "oficial", "citacoes": []},
        {"id": "E3", "afirmacao": "O cofre abriu em 2008.", "status": "oficial", "citacoes": []}]}
    plano = {"blocos": [{"evidencias": ["E3"]}, {"evidencias": ["E1"]}]}
    texto = rd.bloco_de_medidas(plano, banco)
    assert "2008 → bloco 1" in texto and "-18 °C → bloco 2" in texto and "NÃO escreva o valor de novo" in texto


@pytest.mark.fechamento
@pytest.mark.parametrize("frases,aceito", [
    (["No fim, o cofre é uma cópia de segurança.", "Para mim, impressiona.", "Qual outra estrutura faria isso?"], False),
    (["No fim, o cofre é uma cópia de segurança.", "Para mim, impressiona.", "Você confiaria suas sementes a ele?"], True),
    (["No fim, o cofre é uma cópia de segurança.", "Para mim, impressiona.", "Comenta SEMENTE se você guardaria uma."], True),
])
def test_fechamento_aceito_sobrevive_ao_corte_final(ambiente, frases, aceito):
    """Varredura 26/11: o fechamento aceitava qualquer pergunta e o corte final (trim_teaser_ending, depois da
    auditoria) apagava a que não fala com o espectador — o vídeo terminava sem chamar ninguém."""
    from src.script_gen import trim_teaser_ending
    responde, _ = ambiente
    responde({"frases": frases})
    s = _com_fim()
    novo = rd.escrever_fechamento(s, None, "Svalbard", 500, tentativas=1)
    assert (novo is not s) is aceito
    if aceito:
        assert trim_teaser_ending(novo) is novo


def test_saudacao_do_canal_sai_do_texto_do_escritor():
    def s(*textos):
        fr = [{"texto": t, "tipo": "narrativa", "evidencias": []} for t in textos]
        return {"scenes": [{"frases": fr, "narration": " ".join(textos)}, {"frases": [], "narration": "x"}]}
    g = ["E aí, mente curiosa!"]
    assert [f["texto"] for f in rd.sem_saudacao(s("E aí, mente curiosa!", "O cofre não é mina."), g)["scenes"][0]["frases"]] \
        == ["O cofre não é mina."]
    assert rd.sem_saudacao(s("E aí, mente curiosa! O cofre não é mina."), g)["scenes"][0]["frases"][0]["texto"] \
        == "O cofre não é mina."
    original = s("O cofre não é mina.")
    assert rd.sem_saudacao(original, g) is original


@pytest.mark.plano
def test_plano_com_absoluto_na_pergunta_pede_correcao_com_o_motivo(monkeypatch):
    """1031/1032: recusar em silêncio travava o plano; agora o pedido diz a palavra, e sem correção a pergunta
    perde a isenção (passa pela conferência)."""
    def plano(pergunta):
        return json.dumps({"angulo": {"pergunta": pergunta, "resposta": "Uma cópia de segurança na montanha."},
                           "blocos": [{"funcao": f"b{k}", "conta": "c", "evidencias": [f"E{k}"]} for k in range(1, 5)]
                           + [{"funcao": "fim", "conta": "c", "evidencias": []}],
                           "evidencias": {f"E{k}": {"comprova": "x", "nao_comprova": "y"} for k in range(1, 5)}})
    respostas = [plano("Como ele virou o único seguro?"), plano("Como uma montanha guarda as sementes?")]
    pedidos = []
    monkeypatch.setattr(providers, "complete", lambda msgs, **k: pedidos.append(msgs) or respostas.pop(0))
    monkeypatch.setattr(rd, "mapa_do_conhecimento", lambda b: [{"id": f"E{k}", "afirmacao": "a", "trecho": ""}
                                                                for k in range(1, 6)])
    monkeypatch.setattr(rd, "bloco_do_mapa", lambda *a, **k: "")
    from src import disk_cache
    monkeypatch.setattr(disk_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(disk_cache, "put", lambda *a, **k: None)
    p = rd.planejar_historia("Svalbard", {"evidencias": []}, min_minutes=2.5, max_minutes=5)
    assert p["angulo"]["pergunta"] == "Como uma montanha guarda as sementes?"
    assert "único" in pedidos[1][-1]["content"] and not p.get("pergunta_conferida")
