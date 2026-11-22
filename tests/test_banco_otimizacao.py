"""21/11 — banco de evidências: fontes das lacunas selecionadas pelo assunto, cache seguro das decisões dos juízes
e o contexto (ordem e tempo) de cada citação. Nenhuma chamada real: o juiz é falso e conta as chamadas."""
import json

import pytest

from src import evidencias as ev
from src import fontes, metricas, providers
from src.web_search import _dense_passage, juntar_frases

TEMA = "Como o telescópio James Webb se desdobrou no espaço depois do lançamento?"

# trecho guardado da pesquisa de 20/11 (S9, Wikipédia), sem marca de corte — a frase "First" vem logo depois da
# data de 28/12, mas as notas [35] -> [40] mostram que a pesquisa pulou frases ali
S9 = ("On 27 December 2021, at 60 hours after launch, Webb's rockets fired for nine minutes and 27 seconds to make the "
      "second of three mid-course corrections for the telescope to arrive at its L2 destination.[35] On 28 December "
      "2021, three days after launch, mission controllers began the multi-day deployment of Webb's all-important "
      "sunshield. First, commands deployed the aft \"momentum flap\", a device that provides balance against solar "
      "pressure on the sunshield, saving fuel by reducing the need for thruster firing to maintain Webb's orientation.[40] "
      "Next, mission control released and rolled up covers that protect the sunshield, exposing it to space for the "
      "first time.[41][42]")
E86 = "A aba de momento traseira foi implantada primeiro para equilibrar a pressão solar no escudo solar e economizar combustível de manobra."
TRECHO86 = ("First, commands deployed the aft \"momentum flap\", a device that provides balance against solar pressure on "
            "the sunshield, saving fuel by reducing the need for thruster firing to maintain Webb's orientation.")


class JuizFalso:
    """Responde aos prompts do juiz de sustentação e de mesma medida; guarda cada prompt."""

    def __init__(self, decisao="sustenta", parte="", parcial_se=None):
        self.prompts, self.decisao, self.parte, self.parcial_se = [], decisao, parte, parcial_se

    def __call__(self, messages, **kw):
        p = messages[0]["content"]
        self.prompts.append(p)
        if "MESMA medida" in p:
            n = p.count(") A: ")
            return json.dumps({"pares": [{"n": k, "mesma_medida": True, "justificativa": "as duas dão a mesma medida do mesmo objeto"}
                                         for k in range(1, n + 1)]})
        itens = p.split("\n\n")
        julg = []
        for bloco in itens:
            if ") AFIRMAÇÃO:" not in bloco:
                continue
            n = int(bloco.split(")")[0].strip())
            trecho = bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]
            afirmacao = bloco.split("AFIRMAÇÃO: ", 1)[1].split("\n", 1)[0]
            decisao = ("parcial" if self.parcial_se in afirmacao else "sustenta") if self.parcial_se else self.decisao
            julg.append({"n": n, "decisao": decisao, "sujeito": "ok", "medida": "ok",
                         "trecho_chave": " ".join(trecho.split()[:6]), "justificativa": "o trecho diz exatamente isso no contexto",
                         "parte_sustentada": self.parte if decisao == "parcial" else ""})
        return json.dumps({"julgamentos": julg})


def _ev(afirmacao, trecho, fonte="S1"):
    return {"afirmacao": afirmacao, "tipo": "qualitativo", "numeros": sorted(ev.numeros(afirmacao)),
            "citacoes": [{"fonte": fonte, "url": "https://en.wikipedia.org/wiki/X", "titulo": "X", "dominio": "en.wikipedia.org",
                          "classe": "enciclopedia", "trecho": trecho, "verificado": True, "recuperado": False}]}


@pytest.fixture
def juiz(monkeypatch):
    j = JuizFalso()
    monkeypatch.setattr(providers, "complete", j)
    metricas.zerar()
    return j


# ---------------------------------------------------------------- marca de corte nos recortes
def test_recorte_marca_frases_puladas_e_nao_marca_frases_vizinhas():
    frases = ["A primeira frase do artigo.", "B segunda.", "C terceira.", "D quarta."]
    assert juntar_frases(frases, {0, 1}) == "A primeira frase do artigo. B segunda."
    assert juntar_frases(frases, {0, 2, 3}) == "A primeira frase do artigo. […] C terceira. D quarta."
    texto = ("The James Webb telescope opened its sunshield in space after launch day. " + "Unrelated filler sentence about weather. " * 3
             + "Then the sunshield of the James Webb telescope was tensioned in five layers.")
    passagem = _dense_passage(texto, {"sunshield", "webb"}, 400)
    assert "[…]" in passagem and "filler" not in passagem


def test_bloco_do_extrator_marca_pedaco_pulado():
    meio = " ".join(f"Filler sentence number {k} talks about the weather today." for k in range(40))
    f = [{"titulo": "T", "url": "u", "trecho": f"Webb sunshield deployed in space. {meio} Webb mirror opened in space."}]
    escolhidos, _ = fontes.selecionar(f, "Webb sunshield mirror", 1000)
    assert escolhidos[0].startswith("Webb sunshield") and "Webb mirror" in escolhidos[0] and "[…]" in escolhidos[0]


# ---------------------------------------------------------------- contexto de ordem e tempo
def test_contexto_guarda_frase_anterior_posterior_e_data_quando_a_vizinhanca_e_garantida():
    ctx = ev.contexto_da_citacao(TRECHO86, S9, cortes_marcados=True)
    assert ctx["contiguidade"] == "marcada" and ctx["marcadores"] == ["first"]
    assert ctx["anterior"].startswith("On 28 December 2021") and ctx["posterior"].startswith("Next, mission control")
    assert ctx["referencia_temporal"]["expressao"] == "28 December 2021" and not ctx["referencia_temporal"]["no_trecho"]
    assert TRECHO86[:40] in ctx["paragrafo"]


def test_corte_antes_do_trecho_nao_deixa_a_data_de_outra_frase_virar_referencia():
    texto = "On 28 December 2021 controllers began the sunshield deployment. […] " + TRECHO86 + " Next, covers were released."
    ctx = ev.contexto_da_citacao(TRECHO86, texto, cortes_marcados=True)
    assert ctx["corte_antes"] and ctx["anterior"] == "" and "referencia_temporal" not in ctx
    assert "28 December" not in ev.contexto_do_trecho(TRECHO86, texto)


def test_texto_antigo_sem_marca_fica_com_vizinhanca_nao_garantida():
    ctx = ev.contexto_da_citacao(TRECHO86, S9, cortes_marcados=False)
    assert ctx["contiguidade"] == "nao_garantida"
    aviso = ev._aviso_de_ordem(E86, {"contexto": ctx})
    assert "RECORTE" in aviso and "28 December 2021" in aviso


def test_trecho_que_atravessa_um_corte_deixa_de_valer():
    texto = "Webb unfolded the sunshield in five layers over several days. […] The mirror segments were aligned in March 2022."
    e = _ev("O escudo se abriu e o espelho foi alinhado.", "Webb unfolded the sunshield in five layers over several days. The mirror segments were aligned")
    ev.anotar_contexto([e], [{"trecho": texto, "cortes_marcados": True}])
    assert e["citacoes"][0]["verificado"] is False and "corte" in e["citacoes"][0]["sustentacao"]["justificativa"]


def test_e86_reproduzida_o_juiz_recebe_a_referencia_e_o_aviso_e_a_ordem_nao_e_aprovada_sem_ela(juiz):
    """A E86 com o texto guardado (sem marca de corte): o juiz recebe a regra de ordem, a data de 28/12 e o aviso de
    recorte. Se ele diz "parcial" por causa do "primeiro", a evidência fica só com o que o trecho comprova."""
    juiz.parcial_se = "primeiro"
    juiz.parte = "A aba de momento traseira equilibra a pressão solar no escudo solar e economiza combustível."
    e = _julgar()
    p = juiz.prompts[0]
    assert "ORDEM E TEMPO" in p and "RECORTE" in p and "28 December 2021" in p
    assert len(juiz.prompts) == 2  # a parte comprovada é julgada de novo
    assert e["afirmacao"] == juiz.parte and e["afirmacao_original"] == E86 and e["citacoes"][0]["verificado"]
    assert e["citacoes"][0]["contexto"]["contiguidade"] == "nao_garantida"


def test_bloco_do_escritor_mostra_a_referencia_da_ordem():
    e = _ev(E86, TRECHO86)
    e["citacoes"][0]["contexto"] = ev.contexto_da_citacao(TRECHO86, S9, cortes_marcados=True)
    banco = {"evidencias": [{**e, "id": "E86", "status": "fonte_unica"}]}
    bloco = ev.bloco_para_escritor(banco, 220)
    assert "ordem (first)" in bloco and "On 28 December 2021" in bloco
    e["citacoes"][0]["contexto"] = ev.contexto_da_citacao(TRECHO86, S9, cortes_marcados=False)
    assert "não está comprovada" in ev.bloco_para_escritor({"evidencias": [{**e, "id": "E86", "status": "fonte_unica"}]}, 220)


# ---------------------------------------------------------------- cache das decisões
def _julgar(trecho=TRECHO86, afirmacao=E86, texto=S9):
    e = _ev(afirmacao, trecho)
    wf = [{"trecho": texto, "titulo": "Launch and commissioning of the James Webb Space Telescope"}]
    ev.anotar_contexto([e], wf)
    ev.julgar_sustentacao([e], wf, TEMA)
    return e


def test_material_identico_reaproveita_a_decisao_sem_chamar_o_juiz(juiz):
    a = _julgar()
    assert len(juiz.prompts) == 1
    b = _julgar()
    assert len(juiz.prompts) == 1  # nenhuma chamada nova
    assert b["citacoes"][0]["sustentacao"]["decisao"] == a["citacoes"][0]["sustentacao"]["decisao"]
    assert b["citacoes"][0]["sustentacao"].get("cache") is True
    r = metricas.resumo()
    assert r["cache_sustentacao_acertos"] == 1 and r["cache_sustentacao_falhas"] == 1


@pytest.mark.parametrize("muda", ["trecho", "afirmacao", "contexto", "regras"])
def test_cache_invalida_quando_o_material_ou_as_regras_mudam(juiz, monkeypatch, muda):
    _julgar()
    assert len(juiz.prompts) == 1
    if muda == "trecho":
        _julgar(trecho=TRECHO86.replace("First, commands", "Commands"), texto=S9.replace("First, commands", "Commands"))
    elif muda == "afirmacao":
        _julgar(afirmacao=E86.replace("primeiro ", ""))
    elif muda == "contexto":  # a página guardada mudou longe do trecho: a fonte inteira entra na chave
        _julgar(texto=S9 + " Later, the sunshield layers were tensioned.")
    else:
        monkeypatch.setattr(ev, "_PROMPT_SUSTENTACAO", ev._PROMPT_SUSTENTACAO + "\nRegra nova.")
        _julgar()
    assert len(juiz.prompts) == 2


def test_falha_do_juiz_nunca_vai_para_o_cache(monkeypatch):
    chamadas = []
    monkeypatch.setattr(providers, "complete", lambda m, **kw: chamadas.append(1) or '{"julgamentos": []}')
    e = _julgar()
    assert e["citacoes"][0]["sustentacao"]["decisao"] == "sem_julgamento"
    n = len(chamadas)
    _julgar()
    assert len(chamadas) == 2 * n  # chamou de novo: nada foi guardado


def test_itens_identicos_no_mesmo_pedido_sao_julgados_uma_vez(juiz):
    e1, e2 = _ev(E86, TRECHO86), _ev(E86, TRECHO86)
    wf = [{"trecho": S9, "titulo": "T"}]
    ev.anotar_contexto([e1, e2], wf)
    ev.julgar_sustentacao([e1, e2], wf, TEMA)
    assert len(juiz.prompts) == 1 and juiz.prompts[0].count(") AFIRMAÇÃO:") == 1
    assert e2["citacoes"][0]["sustentacao"]["decisao"] == "sustenta"


def test_mesma_medida_par_identico_nao_e_perguntado_de_novo_e_muda_quando_o_trecho_muda(juiz):
    a = _ev("O escudo tem 21 m de comprimento.", "the sunshield is 21 m long and 14 m wide")
    b = _ev("O escudo tem 22 m de comprimento.", "the sunshield measures 22 m in length")
    assert ev.mesma_medida_ia([(a, b)], TEMA) == {0} and len(juiz.prompts) == 1
    assert ev.mesma_medida_ia([(a, b)], TEMA) == {0} and len(juiz.prompts) == 1
    b["citacoes"][0]["trecho"] = "the sunshield measures 22 m in length when fully deployed"
    ev.mesma_medida_ia([(a, b)], TEMA)
    assert len(juiz.prompts) == 2
    r = metricas.resumo()
    assert r["cache_mesma_medida_acertos"] == 1 and r["cache_mesma_medida_falhas"] == 2


# ---------------------------------------------------------------- seleção das fontes das lacunas
BASE = [{"titulo": "James Webb Space Telescope - Science@NASA", "url": "https://science.nasa.gov/mission/webb/",
         "trecho": "The James Webb Space Telescope (JWST) is the largest telescope in space. Webb launched in 2021."},
        {"titulo": "Webb's Deployment - NASA Science", "url": "https://science.nasa.gov/mission/webb/deployment/",
         "trecho": "JWST unfolded its sunshield and mirrors over two weeks."},
        {"titulo": "Wikipedia (ja): ジェイムズ・ウェッブ宇宙望遠鏡", "url": "https://ja.wikipedia.org/wiki/X", "trecho": "ジェイムズ・ウェッブ宇宙望遠鏡は赤外線望遠鏡。"}]
HUBBLE = ("Hubble was launched in 1990 by the shuttle Discovery. Hubble's mirror had a flaw. Astronauts serviced Hubble "
          "five times. Hubble observes in visible light. Its successor, the James Webb Space Telescope, observes in "
          "infrared. Hubble remains in low Earth orbit. Hubble has made over a million observations.")


def _sel(*fontes_cand, pergunta="Como a equipe podia intervir manualmente no desdobramento?"):
    return ev.selecionar_fontes_lacunas([(pergunta, f) for f in fontes_cand], TEMA, BASE)


def test_nomes_alternativos_vem_das_fontes_do_tema():
    nomes = ev.nomes_alternativos(TEMA, BASE)
    assert "jwst" in nomes
    assert ev._mencoes("ジェイムズ・ウェッブ宇宙望遠鏡の鏡は18枚。", nomes)  # nome em outra escrita


def test_fonte_relevante_entra_mesmo_sem_as_palavras_da_pergunta():
    f = {"titulo": "Webb Observatory - NASA Science", "url": "https://science.nasa.gov/mission/webb/spacecraftoverview/",
         "trecho": "The observatory's sunshield is the size of a tennis court."}
    sel, reg = _sel(f)
    assert sel == [f] and reg[0]["decisao"] == "selecionada" and reg[0]["afinidade_pergunta"] == 0


def test_nome_alternativo_conta_como_o_assunto():
    f = {"titulo": "Deployment timeline", "url": "https://example.org/timeline",
         "trecho": "JWST deployed its momentum flap. JWST then released the sunshield covers. JWST tensioned the layers."}
    sel, reg = _sel(f)
    assert sel == [f] and reg[0]["decisao"] == "selecionada"


def test_pagina_de_outro_assunto_que_nao_cita_o_tema_sai_com_motivo():
    f = {"titulo": "ESA - Hubble overview", "url": "https://www.esa.int/Hubble_overview",
         "trecho": HUBBLE.replace(" Its successor, the James Webb Space Telescope, observes in infrared.", "")}
    sel, reg = _sel(f)
    assert sel == [] and reg[0]["decisao"] == "excluida" and "hubble" in reg[0]["motivo"]


def test_pagina_de_outro_assunto_que_cita_o_tema_fica_so_com_os_trechos_dele():
    f = {"titulo": "Wikipedia (en): Hubble Space Telescope", "url": "https://en.wikipedia.org/wiki/Hubble_Space_Telescope",
         "trecho": HUBBLE + " " + " ".join(f"Hubble fact number {k} about servicing missions." for k in range(8))}
    sel, reg = _sel(f)
    assert reg[0]["decisao"] == "reduzida" and len(sel) == 1
    assert "James Webb Space Telescope" in sel[0]["trecho"] and "Hubble fact number 7" not in sel[0]["trecho"]
    assert "[…]" in sel[0]["trecho"]
    assert reg[0]["chars"][1] < reg[0]["chars"][0]


def test_na_duvida_a_fonte_fica():
    """Não nomeia o assunto, mas também não trata de outro (inclusive a oficial): fica."""
    oficial = {"titulo": "Sunshield deployment sequence", "url": "https://www.nasa.gov/sunshield-sequence",
               "trecho": "The sunshield layers were tensioned one at a time over three days by ground commands."}
    outra_escrita = {"titulo": "展開", "url": "https://example.jp/x", "trecho": "サンシールドは5層で、地上からの指令で展開された。"}
    sel, reg = _sel(oficial, outra_escrita)
    assert len(sel) == 2 and all(r["decisao"] == "selecionada" for r in reg) and "dúvida" in reg[0]["motivo"]


def test_selecao_prioriza_as_mais_relevantes_e_registra_todas():
    cita_pouco = {"titulo": "Wikipedia (en): Hubble Space Telescope", "url": "https://en.wikipedia.org/wiki/Hubble", "trecho": HUBBLE}
    nomeia = {"titulo": "James Webb Space Telescope sunshield", "url": "https://en.wikipedia.org/wiki/JWST_sunshield",
              "trecho": "The sunshield has five layers of Kapton."}
    fora = {"titulo": "Wikipedia (en): STS-31", "url": "https://en.wikipedia.org/wiki/STS-31",
            "trecho": "STS-31 carried Hubble. STS-31 launched in 1990. The STS-31 crew deployed Hubble from the bay."}
    sel, reg = _sel(cita_pouco, nomeia, fora)
    assert sel[0]["url"] == nomeia["url"] and len(sel) == 2 and len(reg) == 3
    assert {r["decisao"] for r in reg} == {"selecionada", "reduzida", "excluida"}


def test_tema_sem_nome_proprio_nao_filtra():
    sel, reg = ev.selecionar_fontes_lacunas([("p", {"titulo": "Hubble", "url": "u", "trecho": HUBBLE})],
                                            "por que o céu é azul?", BASE)
    assert len(sel) == 1 and "sem filtro" in reg[0]["motivo"]


def test_montar_banco_registra_a_selecao_e_so_extrai_o_que_entrou(monkeypatch, juiz):
    """As fontes do Hubble não chegam ao extrator; a seleção fica no banco."""
    extraidas = []

    def chamar(prompt, max_tokens=ev.RESPOSTA_EXTRACAO, validar=None):
        extraidas.append(prompt)
        return {"evidencias": [], "lacunas": ["Como a equipe podia intervir manualmente?"]}
    monkeypatch.setattr(ev, "_chamar", chamar)
    hubble = {"titulo": "ESA - Hubble overview", "url": "https://www.esa.int/Hubble_overview",
              "trecho": HUBBLE.replace(" Its successor, the James Webb Space Telescope, observes in infrared.", "")}
    webb = {"titulo": "Webb Observatory", "url": "https://science.nasa.gov/webb/obs", "trecho": "Webb has a five-layer sunshield in space."}
    banco = ev.montar_banco(TEMA, BASE, buscar=lambda q: [hubble, webb])
    assert [r["decisao"] for r in banco["selecao_lacunas"]] == ["excluida", "selecionada"]
    assert not any("Hubble was launched" in p for p in extraidas)


def test_sem_o_tema_e_com_outro_nome_dominando_o_texto_sai_mesmo_sem_ele_no_titulo():
    """STS-31 (20/11): o título não diz Hubble, o texto fala dele o tempo todo e nunca do Webb."""
    f = {"titulo": "Mission 35 summary", "url": "https://example.org/mission35",
         "trecho": "The crew of Discovery deployed the Hubble telescope. Engineers tested the Hubble arrays. "
                   "Later the Hubble aperture door opened on schedule."}
    sel, reg = _sel(f)
    assert sel == [] and reg[0]["decisao"] == "excluida" and "hubble" in reg[0]["motivo"]
