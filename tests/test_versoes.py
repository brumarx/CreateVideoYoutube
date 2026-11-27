"""23/11 (Svalbard, job 1003): a edição corrigia fatos e a história caía (7,2 → 5,6). Cada versão fica
guardada inteira, a versão nova é comparada com a anterior critério a critério e cena a cena, a cena que
piorou a história volta para reformulação localizada (sem o erro factual de volta) e a escolha final segue
fato > leitura enganosa > coerência > nota, podendo montar uma versão com as cenas seguras de cada versão."""
import copy
import json

import pytest

from src import providers
from src import redacao as rd

BOM = dict(abertura=8, progressao=8, novidade=8, sem_repeticao=8, narrativa=8, titulo=8, conclusao=8, linguagem=8)


def Q(alvos=(), **mudancas):
    """Qualidade editorial como avaliar_editorial devolve (nota = média; trava em critério essencial ≤ 4)."""
    crit = {**BOM, **mudancas}
    travas = [f"{k} com nota {crit[k]:.0f}" for k in rd.CRITERIOS_TRAVA if crit[k] <= 4]
    nota = round(sum(crit.values()) / len(crit), 1)
    if travas:
        nota = min(nota, rd.NOTA_COM_TRAVA)
    return {"nota": nota, "criterios": crit, "travas": travas, "alvos": list(alvos), "problemas": travas,
            "aprovado": not travas and nota >= rd.NOTA_MINIMA, "factual": {"nota": 9, "enganos": []}}


def F(texto, tipo="factual", evid=("E1",)):
    return {"texto": texto, "tipo": tipo, "evidencias": list(evid) if tipo != "narrativa" else []}


def cena(*frases):
    return {"narration": " ".join(f["texto"] for f in frases), "frases": list(frases)}


ABERTURA = "No meio do gelo, uma porta de aço esconde sementes do mundo todo."
EXAGERO = "Iraque e Síria perderam coleções inteiras na guerra."
LISTA = "Iraque teve 2 bancos danificados, Síria 1, Filipinas 1, em 3 anos."    # corrige, mas vira planilha
RESTRITA = "Bancos de genes do Iraque e da Síria foram danificados por guerras."  # corrige sem perder a cena
MEIO = "Por isso o cofre guarda cópias de segurança."
MEIO_RUIM = "O cofre guarda cópias. O cofre guarda cópias de segurança."
FIM = "Quando um banco cai, a cópia está no gelo."
FIM_ERRADO = "Todos os bancos do mundo já usaram o cofre."


def roteiro(*cenas):
    return {"title": "O cofre", "description": "d", "tags": [], "_redacao": True, "scenes": list(cenas)}


V1 = roteiro(cena(F(ABERTURA, "narrativa")), cena(F(EXAGERO)), cena(F(MEIO, "explicativa")), cena(F(FIM, "explicativa")))
BANCO = {"versao": 1, "tema": "t", "evidencias": [
    {"id": "E1", "afirmacao": "Bancos de genes no Iraque e na Síria foram danificados por guerras.", "status": "oficial",
     "tipo": "qualitativo", "estimado": False, "numeros": [], "conflitos": [], "citacoes": []}]}


def com(base, i, nova):
    s = copy.deepcopy(base)
    s["scenes"][i] = nova
    return s


class Oraculo:
    """_auditar_versao simulado: problema factual pelas frases erradas; história pela função dada."""
    def __init__(self, erros: dict[str, str], historia):
        self.erros, self.historia, self.auditadas = erros, historia, []

    def __call__(self, script, banco, topic, previas):
        self.auditadas.append([sc["narration"] for sc in script["scenes"]])
        probs = [rd._p(self.erros[f["texto"]], f["texto"], i, j, "a evidência não diz isso")
                 for i, j, f in rd.frases_de(script) if f["texto"] in self.erros]
        return script, probs, self.historia(script)


def textos(s):
    return [sc["narration"] for sc in s["scenes"]]


class EditorFalso:
    def __init__(self, *saidas):
        self.saidas, self.pedidos = list(saidas), []

    def __call__(self, script, alvos, banco, topic, **k):
        self.pedidos.append({"script": copy.deepcopy(script), "alvos": copy.deepcopy(alvos)})
        novo, cenas = self.saidas.pop(0)
        return novo, {"cenas_editadas": cenas, "titulo": None, "revertidas": []}


def _inst(n, script, problemas=(), mudou=(), **crit):
    q = Q(**crit)
    return rd.instantaneo(n, "edicao" if n > 1 else "escrita", list(mudou), script, list(problemas), q, BANCO)


# ======================================================================= 1. correção que prejudica a narrativa
def test_correcao_factual_que_prejudica_a_narrativa_volta_para_reformulacao_sem_o_erro(monkeypatch):
    def historia(s):
        t = textos(s)
        if LISTA in t:  # a correção virou lista: narrativa e conclusão caem, o editor aponta a cena 2
            return Q([{"criterio": "narrativa", "cenas": [2], "descricao": "a cena 2 virou lista de números"}],
                     narrativa=4, conclusao=6)
        return Q()
    oraculo = Oraculo({EXAGERO: "excede"}, historia)
    monkeypatch.setattr(rd, "_auditar_versao", oraculo)
    v2 = com(V1, 1, cena(F(LISTA)))
    v3 = com(v2, 1, cena(F(RESTRITA)))
    editor = EditorFalso((v2, [2]), (v3, [2]))
    monkeypatch.setattr(rd, "editar_cenas", editor)
    final, rel = rd.auditar_e_corrigir(copy.deepcopy(V1), BANCO, "t")

    comp = rel["comparacoes"][0]
    assert comp["factual"]["resolvidos"] == [EXAGERO] and comp["factual"]["novos"] == []
    assert comp["pioraram"] == ["narrativa", "conclusao"] and comp["corrigiu_fato_em"] == [2]
    assert list(comp["responsaveis"]) == ["2"] and "narrativa 8→4" in comp["responsaveis"]["2"][0]
    # a 2ª rodada parte da versão corrigida (não da que tinha o erro) e pede reformulação localizada
    segunda = editor.pedidos[1]
    assert textos(segunda["script"])[1] == LISTA
    pedido = " ".join(segunda["alvos"][1])
    assert "PRESERVAR A HISTÓRIA" in pedido and "NÃO o traga de volta" in pedido and EXAGERO in pedido
    assert 0 not in segunda["alvos"] and 2 not in segunda["alvos"]  # as cenas que continuam boas não são tocadas
    assert textos(final) == [ABERTURA, RESTRITA, MEIO, FIM] and rel["versao_final"] == 3
    assert EXAGERO not in " ".join(textos(final))


# ======================================================================= 2. melhora a narrativa, mas erra o fato
def test_edicao_que_melhora_a_narrativa_mas_introduz_erro_factual_nao_vence(monkeypatch):
    def historia(s):
        return Q(abertura=9, narrativa=10, conclusao=10, titulo=9) if FIM_ERRADO in textos(s) else Q([{"criterio": "conclusao", "cenas": [4], "descricao": "fim fraco"}], abertura=6, novidade=7)
    v1 = com(V1, 1, cena(F(RESTRITA)))
    oraculo = Oraculo({FIM_ERRADO: "sem_suporte"}, historia)
    monkeypatch.setattr(rd, "_auditar_versao", oraculo)
    v2 = com(v1, 3, cena(F(FIM_ERRADO)))
    editor = EditorFalso((v2, [4]), (copy.deepcopy(v2), [4]))
    monkeypatch.setattr(rd, "editar_cenas", editor)
    final, rel = rd.auditar_e_corrigir(copy.deepcopy(v1), BANCO, "t")
    comp = rel["comparacoes"][0]
    assert comp["melhoraram"] and comp["factual"]["novos"] == [f"sem_suporte: {FIM_ERRADO}"]
    notas = {v["versao"]: v["nota_editorial"] for v in rel["versoes"]}
    assert notas[2] > notas[1] >= rd.NOTA_MINIMA
    # nota maior com erro de fato perde para a nota menor sem erro
    assert rel["versao_final"] == 1 and FIM_ERRADO not in " ".join(textos(final))
    assert rel["selecao"][0]["versao"] == 1 and rel["selecao"][0]["problemas_factuais"] == 0


def test_chave_de_selecao_segue_fato_leitura_coerencia_e_so_depois_nota():
    s = copy.deepcopy(V1)
    erro = [rd._p("excede", EXAGERO, 1, 0, "m")]
    engano = [rd._p("leitura_enganosa", FIM, 3, 0, "m")]
    com_erro = _inst(1, s, erro, abertura=10, narrativa=10, conclusao=10, titulo=10)
    com_engano = _inst(2, s, engano, abertura=10)
    incoerente = _inst(3, s, (), progressao=6, narrativa=6, novidade=10, linguagem=10, abertura=10)
    coerente = _inst(4, s, ())
    ordem = sorted([com_erro, com_engano, incoerente, coerente], key=rd.chave_selecao, reverse=True)
    assert [v["versao"] for v in ordem] == [4, 3, 2, 1]
    assert incoerente["editorial"]["nota"] >= coerente["editorial"]["nota"] - 0.5  # nota global não decide sozinha


# ======================================================================= 3. repete informação de outra cena
BANCO_SUP = {"versao": 1, "tema": "t", "fontes": [], "lacunas": [], "nao_lidos": [], "evidencias": [
    {"id": "E1", "afirmacao": "O cofre de Svalbard guarda a maior coleção de sementes do mundo.", "tipo": "qualitativo",
     "status": "oficial", "estimado": False, "numeros": [], "conflitos": [],
     "citacoes": [{"fonte": "S1", "url": "https://seedvault.no/a", "titulo": "x", "dominio": "seedvault.no",
                   "classe": "oficial", "trecho": "the world's largest collection of crop seeds", "verificado": True}]}]}


def test_edicao_que_repete_a_afirmacao_de_outra_cena_volta_a_original(monkeypatch):
    s = roteiro(cena(F("No meio do gelo, uma porta de aço.", "narrativa")),
                cena(F("Lá dentro fica a maior coleção de sementes do mundo.", evid=["E1"])))
    resposta = json.dumps({"cenas": [{"cena": 1, "funcao": "abertura", "preserva": "", "corrige": "", "justificativa": "",
                                      "frases": [F("Em Svalbard fica o maior cofre de sementes do planeta.", evid=["E1"])]}],
                           "titulo": ""}, ensure_ascii=False)
    monkeypatch.setattr(providers, "complete", lambda *a, **k: resposta)
    novo, reg = rd.editar_cenas(s, {0: ["HISTÓRIA — abertura: genérica"]}, BANCO_SUP, "t")
    assert textos(novo) == textos(s) and reg["cenas_editadas"] == []
    assert "maior" in reg["revertidas"][0]["motivo"] and "sementes" in reg["revertidas"][0]["motivo"]


def test_comparacao_aponta_a_cena_editada_que_passou_a_repetir_outra():
    v1 = roteiro(cena(F(ABERTURA, "narrativa")), cena(F("Em 2015, a Síria pediu as sementes de volta.")),
                 cena(F(FIM, "explicativa")))
    v2 = com(v1, 2, cena(F("O cofre salvou a Síria, que pediu as sementes de volta.", "explicativa")))
    a = _inst(1, v1)
    b = rd.instantaneo(2, "edicao", [3], v2, [],
                       Q([{"criterio": "sem_repeticao", "cenas": [2, 3], "descricao": "a Síria de novo"}],
                         sem_repeticao=5, conclusao=6), BANCO)
    comp = rd.comparar_versoes(a, b)
    assert comp["repeticao"]["nomes"] == {"3": ["siria"]}
    assert list(comp["responsaveis"]) == ["3"]  # a cena 2 não foi editada: não leva a culpa
    assert any("Síria de novo" in m or "siria" in m for m in comp["responsaveis"]["3"])


# ======================================================================= 4. edição que reduz a nota editorial
def test_comparacao_mostra_cada_criterio_e_nao_so_a_nota_global():
    v2 = com(com(V1, 0, cena(F("Uma porta.", "narrativa"))), 3, cena(F("Fim.", "narrativa")))
    a = _inst(1, V1, progressao=7)
    b = rd.instantaneo(2, "edicao", [1, 4], v2, [], Q([], abertura=6, conclusao=5, linguagem=8.5, progressao=8), BANCO)
    comp = rd.comparar_versoes(a, b)
    assert comp["pioraram"] == ["abertura", "conclusao"] and comp["melhoraram"] == ["progressao"]
    assert comp["criterios"]["linguagem"]["diferenca"] == 0.5  # variação do avaliador: nem melhorou nem piorou
    assert comp["abertura"] == {"antes": 8, "depois": 6, "diferenca": -2}
    assert set(comp["responsaveis"]) == {"1", "4"} and comp["titulo"]["mudou"] is False
    assert comp["interpretacao"]["correta"] and comp["factual"] == {"resolvidos": [], "novos": [], "antes": 0, "depois": 0}


# ======================================================================= 5. recuperação da melhor versão
def test_versao_montada_recupera_a_cena_boa_da_versao_anterior_sem_o_erro(monkeypatch):
    def historia(s):
        t = textos(s)
        if MEIO_RUIM in t:
            return Q([{"criterio": "progressao", "cenas": [3], "descricao": "a cena 3 repete e trava"}],
                     progressao=4, sem_repeticao=4)
        return Q()
    oraculo = Oraculo({EXAGERO: "excede", FIM_ERRADO: "sem_suporte"}, historia)
    monkeypatch.setattr(rd, "_auditar_versao", oraculo)
    v2 = com(com(V1, 1, cena(F(RESTRITA))), 2, cena(F(MEIO_RUIM, "explicativa")))  # corrige a 2, estraga a 3
    v3 = com(v2, 3, cena(F(FIM_ERRADO)))                                              # e a 2ª rodada erra o fim
    monkeypatch.setattr(rd, "editar_cenas", EditorFalso((v2, [2, 3]), (v3, [3, 4])))
    final, rel = rd.auditar_e_corrigir(copy.deepcopy(V1), BANCO, "t")
    assert textos(final) == [ABERTURA, RESTRITA, MEIO, FIM]
    montada = rel["versoes"][-1]
    assert montada["origem"] == "composicao" and montada["origem_das_cenas"] == [2, 2, 1, 2]
    assert montada["aprovado"] and rel["versao_final"] == montada["versao"] == 4
    assert rel["edicoes"] == 2  # a montagem não é rodada de edição
    assert all(EXAGERO not in " ".join(t) and FIM_ERRADO not in " ".join(t) for t in oraculo.auditadas[-1:])


def test_montagem_nunca_usa_cena_com_erro_factual():
    erro = [rd._p("excede", EXAGERO, 1, 0, "m")]
    v2 = com(V1, 2, cena(F(MEIO_RUIM, "explicativa")))
    a = _inst(1, V1, erro)
    b = _inst(2, v2, erro, mudou=[3])
    assert rd.compor_versao([a, b], BANCO, "t") is None  # a cena 2 só existe com o erro: montar não resolve


# ======================================================================= 6. versão antiga que já não passa
def test_versao_antiga_aprovada_que_nao_passa_mais_na_auditoria_e_rejeitada(monkeypatch):
    banco = copy.deepcopy(BANCO)
    NOVA = "Os bancos da Síria foram danificados."

    def auditar(script, b, topic, previas):
        t = textos(script)
        probs = []
        if len(b["evidencias"]) > 1 and RESTRITA in t:   # a evidência nova contradiz a frase da v1
            probs.append(rd._p("contradiz", RESTRITA, 1, 0, "a evidência nova diz outra coisa"))
        if FIM_ERRADO in t:
            probs.append(rd._p("sem_suporte", FIM_ERRADO, 3, 0, "nada sustenta"))
        if FIM_ERRADO in t:  # história melhor, fato errado: a 2ª rodada parte dela (e busca evidência)
            return script, probs, Q([{"criterio": "abertura", "cenas": [1], "descricao": "x"}], narrativa=10)
        return script, probs, (Q(abertura=7) if NOVA in t else Q([{"criterio": "conclusao", "cenas": [4], "descricao": "x"}]))
    monkeypatch.setattr(rd, "_auditar_versao", auditar)
    v1 = com(V1, 1, cena(F(RESTRITA)))
    v2 = com(v1, 3, cena(F(FIM_ERRADO)))
    v3 = com(com(v2, 3, cena(F(FIM, "explicativa"))), 1, cena(F(NOVA)))
    monkeypatch.setattr(rd, "editar_cenas", EditorFalso((v2, [4]), (v3, [2])))
    monkeypatch.setattr(rd, "adicionar_fontes", lambda b, t, novas, web: b["evidencias"].append(
        {**b["evidencias"][0], "id": "E2", "afirmacao": "Outra coisa."}))
    monkeypatch.setattr(rd, "NOTA_ALVO", 9)  # nota 8 não basta: as duas rodadas acontecem
    final, rel = rd.auditar_e_corrigir(copy.deepcopy(v1), banco, "t", web_facts=[], buscar_evidencia=lambda p, t: [{}])
    versoes = {v["versao"]: v for v in rel["versoes"]}
    assert "reauditada como versão 4" in versoes[1]["substituida"]
    assert versoes[4]["reauditada_de"] == 1 and versoes[4]["problemas_factuais"] == 1 and not versoes[4]["aprovado"]
    assert rel["versao_final"] == 3 and NOVA in " ".join(textos(final))


def test_texto_de_cena_reprovado_numa_auditoria_posterior_nao_e_seguro():
    """O mesmo texto passou na v1 e foi reprovado na v2 (outra leitura do auditor): não serve para montar."""
    v2 = com(V1, 2, cena(F(MEIO_RUIM, "explicativa")))
    a = _inst(1, com(V1, 1, cena(F(RESTRITA))))
    b = rd.instantaneo(2, "edicao", [3], com(v2, 1, cena(F(RESTRITA))),
                       [rd._p("excede", RESTRITA, 1, 0, "agora o auditor reprovou")],
                       Q([{"criterio": "progressao", "cenas": [3], "descricao": "x"}], progressao=5), BANCO)
    assert rd.compor_versao([a, b], BANCO, "t") is None


def test_superlativo_le_o_objeto_o_complemento_e_o_alcance():
    a = rd._superlativos("o maior backup de sementes do planeta")[0]
    assert (a["raiz"], a["objeto"], a["complemento"], a["alcance"]) == ("maior", "backup", "sementes", True)
    assert rd._superlativos("O primeiro teste real veio da guerra.")[0]["objeto"] == "teste"  # "real" não é o objeto


def test_primeiro_teste_e_primeiro_deposito_sao_acontecimentos_diferentes():
    """24/11 (job 1007): a conferência recusou a correção de "destruiu em Alepo" por tomar os dois pela mesma afirmação."""
    s = roteiro(cena(F("O primeiro teste real veio da guerra, em 2015.")),
                cena(F("Em 2025, o Sudão fez o primeiro depósito em meio à guerra civil.")))
    assert rd._superlativos_repetidos(s) == set()


def test_primeiro_deposito_duas_vezes_e_a_mesma_afirmacao_quando_o_contexto_confirma():
    mesmo = roteiro(cena(F("Em 2025, o Sudão fez o primeiro depósito do país.")),
                    cena(F("Foi o primeiro depósito do Sudão, em meio à guerra.")))
    assert rd._superlativos_repetidos(mesmo) == {"prime… deposito"}
    outro = roteiro(cena(F("O primeiro depósito da Irlanda trouxe batatas.")),
                    cena(F("O primeiro depósito do Sudão veio em 2025.")))
    assert rd._superlativos_repetidos(outro) == set()  # mesmo objeto, contexto diferente: outro acontecimento


def test_maior_colecao_e_maior_backup_so_repetem_se_forem_o_mesmo_objeto():
    # os dois dizem que o cofre é o maior de SEMENTES no mundo: a mesma afirmação com outra palavra
    mesmo = roteiro(cena(F("Ali está o maior backup de sementes do planeta.")),
                    cena(F("É a maior coleção de sementes do mundo.")))
    assert rd._superlativos_repetidos(mesmo)
    # objetos diferentes, sem o mesmo complemento e alcance: afirmações diferentes
    outro = roteiro(cena(F("Ali está a maior coleção de sementes do mundo.")),
                    cena(F("O maior risco é a água no túnel.")))
    assert rd._superlativos_repetidos(outro) == set()


def test_edicao_que_encolhe_abaixo_do_minimo_volta_ao_editor_para_reescrever(monkeypatch):
    """27/11 (Chernobyl 1040: 357 -> 284 palavras, mínimo 286): a edição curta demais era descartada e o roteiro
    reprovado com uma edição ainda sobrando; agora volta ao editor com o tamanho da cena."""
    monkeypatch.setattr(rd, "_auditar_versao", Oraculo({EXAGERO: "excede"}, lambda s: Q()))
    encolhida = com(V1, 1, cena(F("Guerra.")))
    v3 = com(V1, 1, cena(F(RESTRITA)))
    editor = EditorFalso((encolhida, [2]), (v3, [2]))
    monkeypatch.setattr(rd, "editar_cenas", editor)
    minimo = rd._palavras(V1) - 3
    final, rel = rd.auditar_e_corrigir(copy.deepcopy(V1), BANCO, "t", min_palavras=minimo)
    assert len(editor.pedidos) == 2
    assert "TIROU PALAVRAS DEMAIS" in " ".join(editor.pedidos[1]["alvos"][1])
    assert textos(editor.pedidos[1]["script"])[1] == EXAGERO  # refeita a partir da versão auditada, não da encolhida
    assert RESTRITA in textos(final)
