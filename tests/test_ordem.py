"""21/11 — ordem cronológica conferida em código (E27, E28, E86) e trecho guardado sem corte cego (E43/E47).
Nenhuma chamada real: o juiz é substituído pela DECISÃO REAL que o Nemotron deu na rodada 8 (aprovou "Primeiro, ..."
num recorte sem marca de corte, mesmo avisado) — o código não pode deixar essa decisão virar cronologia comprovada."""
import json

import pytest

from src import evidencias as ev
from src import metricas, providers
from src import ordem as o
from src import redacao as rd

TEMA = "Como o telescópio James Webb se desdobrou no espaço depois do lançamento?"
# texto guardado da pesquisa de 20/11 (S9, Wikipédia "Launch and commissioning of the JWST"), sem marca de corte;
# as notas [35] -> [40] e a frase "EST on 25 December" começando no meio mostram que a pesquisa pulou frases
S9 = ("The separation and solar panel extension were both visible in a live feed from a camera on the rocket.[32] EST on "
      "25 December 2021, about 12 hours after launch, the telescope's pair of primary rockets began firing for 65 minutes "
      "to make the first of three planned mid-course corrections.[34] On day two, the high gain communication antenna "
      "deployed automatically.[30] On 27 December 2021, at 60 hours after launch, Webb's rockets fired for nine minutes "
      "and 27 seconds to make the second of three mid-course corrections for the telescope to arrive at its L2 "
      "destination.[35] On 28 December 2021, three days after launch, mission controllers began the multi-day deployment "
      "of Webb's all-important sunshield. First, commands deployed the aft \"momentum flap\", a device that provides "
      "balance against solar pressure on the sunshield, saving fuel by reducing the need for thruster firing to maintain "
      "Webb's orientation.[40] Next, mission control released and rolled up covers that protect the sunshield, exposing "
      "it to space for the first time.[41][42]")
TRECHO_FLAP = ("First, commands deployed the aft \"momentum flap\", a device that provides balance against solar pressure "
               "on the sunshield, saving fuel by reducing the need for thruster firing to maintain Webb's orientation.")
TRECHO_COVERS = "Next, mission control released and rolled up covers that protect the sunshield, exposing it to space for the first time."
E27 = ("Primeiro, comandos desdobraram a aba de momento traseira, um dispositivo que fornece equilíbrio contra a pressão "
       "solar no protetor solar, economizando combustível ao reduzir a necessidade de acionamento de propulsores para "
       "manter a orientação.")
E28 = ("Em seguida, o controle da missão liberou e enrolou as coberturas que protegem o protetor solar, expondo-o ao "
       "espaço pela primeira vez.")
E86 = "A aba de momento traseira foi implantada primeiro para equilibrar a pressão solar no escudo solar e economizar combustível de manobra."
E23_ANTIGA = ("O primeiro comando de desdobramento acionou a 'aba de momento' traseira, um dispositivo que fornece equilíbrio "
              "contra a pressão solar no protetor solar.")
# decisão REAL do juiz na rodada 8 (jwst3/banco.json, E27), contra o aviso de recorte que ele recebeu
REAL_E27 = {"decisao": "sustenta", "sujeito": "ok", "medida": "ok", "trecho_chave": TRECHO_FLAP,
            "justificativa": "O trecho descreve exatamente a ação, o dispositivo e sua função, e a palavra 'First' no "
                             "contexto contínuo refere-se ao primeiro passo do desdobramento do protetor solar.",
            "parte_sustentada": ""}
REAL_E28 = {"decisao": "sustenta", "sujeito": "ok", "medida": "ok", "trecho_chave": TRECHO_COVERS,
            "justificativa": "O trecho afirma que, em seguida, o controle da missão liberou e enrolou as coberturas do "
                             "protetor solar, expondo-o ao espaço pela primeira vez.", "parte_sustentada": ""}


def _ev(afirmacao, trecho, fonte="S1"):
    return {"afirmacao": afirmacao, "tipo": "qualitativo", "numeros": sorted(ev.numeros(afirmacao)),
            "citacoes": [{"fonte": fonte, "url": "https://en.wikipedia.org/wiki/X", "titulo": "X", "dominio": "en.wikipedia.org",
                          "classe": "enciclopedia", "trecho": trecho, "verificado": True, "recuperado": False}]}


class JuizReal:
    """Devolve, para cada item, a decisão REAL gravada (ou 'sustenta' copiando o trecho); conta as chamadas."""

    def __init__(self, gravadas=None):
        self.gravadas, self.prompts = gravadas or {}, []

    def __call__(self, messages, **kw):
        p = messages[0]["content"]
        self.prompts.append(p)
        julg = []
        for bloco in p.split("\n\n"):
            if ") AFIRMAÇÃO:" not in bloco:
                continue
            n = int(bloco.split(")")[0].strip())
            trecho = bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]
            d = next((v for k, v in self.gravadas.items() if k in trecho), None)
            julg.append({"n": n, **(d or {"decisao": "sustenta", "sujeito": "ok", "medida": "ok",
                                         "trecho_chave": " ".join(trecho.split()[:6]),
                                         "justificativa": "o trecho diz exatamente isso", "parte_sustentada": ""})})
        return json.dumps({"julgamentos": julg})


@pytest.fixture
def juiz(monkeypatch):
    j = JuizReal({"momentum flap": REAL_E27, "rolled up covers": REAL_E28})
    monkeypatch.setattr(providers, "complete", j)
    metricas.zerar()
    return j


def _julgar(afirmacao, trecho, texto=S9, marcado=False):
    e = _ev(afirmacao, trecho)
    wf = [{"trecho": texto, "titulo": "Launch and commissioning of the James Webb Space Telescope", "cortes_marcados": marcado}]
    ev.anotar_contexto([e], wf)
    ev.julgar_sustentacao([e], wf, TEMA)
    return ev._status(e)


# ---------------------------------------------------------------- o que é marca de ordem (e o que não é)
@pytest.mark.parametrize("texto,classes", [
    ("Primeiro, comandos desdobraram a aba.", ["inicio"]),
    ("Primeiro a torre subiu dois metros.", ["inicio"]),
    ("A aba foi implantada primeiro para equilibrar a pressão.", ["inicio"]),
    ("Depois liberaram a aba traseira.", ["seguinte"]),
    ("Em seguida, enrolaram as coberturas.", ["seguinte"]),
    ("Posteriormente, a NASA confirmou o resultado.", ["seguinte"]),
    ("Pouco depois, a antena abriu.", ["seguinte"]),
    ("Meses depois, a primeira imagem saiu.", ["seguinte"]),
    ("Antes disso, a torre subiu.", ["anterior"]),
    ("Por fim, as camadas foram tensionadas.", ["fim"]),
    ("O primeiro passo foi liberar a aba.", ["inicio"]),
    ("O rei assinou o tratado e depois a guerra recomeçou.", ["seguinte"]),
])
def test_marcas_de_ordem_relativa(texto, classes):
    assert sorted({m["classe"] for m in o.marcas_de_ordem(texto)}) == classes


@pytest.mark.parametrize("texto", [
    "A primeira imagem de campo profundo foi apresentada em 11 de julho de 2022.",
    "Doze horas depois, os foguetes queimaram por 65 minutos.",       # intervalo com número: conferido pelos números
    "Vinte e sete minutos depois, foi liberado do estágio superior.",
    "Depois do lançamento, o painel abriu.",                           # referência nomeada na própria frase
    "O painel abriu antes do previsto.",
    "As observações começaram em 2022, marcando um antes e um depois na exploração.",
    "Foi o primeiro telescópio dobrável já lançado.",
    "Perto do final do primeiro mês, foi executada a última manobra.",
])
def test_nao_e_ordem_relativa(texto):
    assert o.marcas_de_ordem(texto) == []


def test_reformular_tira_so_a_ordem_e_mantem_o_acontecimento():
    assert o.sem_ordem(E27).startswith("Comandos desdobraram a aba de momento traseira")
    assert o.sem_ordem(E28).startswith("O controle da missão liberou e enrolou as coberturas")
    assert o.sem_ordem(E86) == ("A aba de momento traseira foi implantada para equilibrar a pressão solar no escudo "
                                "solar e economizar combustível de manobra.")
    assert o.sem_ordem("O rei assinou o tratado e depois a guerra recomeçou.") == "O rei assinou o tratado e a guerra recomeçou."
    assert o.sem_ordem("As asas foram desdobradas por último: a de bombordo em 7 de janeiro.") == \
        "As asas foram desdobradas: a de bombordo em 7 de janeiro."
    # a ordem É o fato: não há como tirar sem mudar a afirmação
    assert o.sem_ordem(E23_ANTIGA) is None
    assert o.sem_ordem("O painel solar abriu antes, e a antena depois.") is None


# ---------------------------------------------------------------- o que a fonte comprova
def test_recorte_sem_marca_de_corte_nao_comprova_a_referencia_de_first():
    ctx = ev.contexto_da_citacao(TRECHO_FLAP, S9, cortes_marcados=False)
    a = o.avaliar_citacao(E27, TRECHO_FLAP, ctx)
    assert a["exige"] and not a["comprovada"] and "recorte sem marca de corte" in a["motivo"]


def test_vizinhanca_garantida_comprova_e_registra_a_referencia():
    ctx = ev.contexto_da_citacao(TRECHO_FLAP, S9, cortes_marcados=True)
    a = o.avaliar_citacao(E27, TRECHO_FLAP, ctx)
    assert a["comprovada"] and a["referente"].startswith("On 28 December 2021")


def test_corte_logo_antes_nao_comprova():
    texto = "On 28 December 2021 controllers began the sunshield deployment. […] " + TRECHO_FLAP
    a = o.avaliar_citacao(E27, TRECHO_FLAP, ev.contexto_da_citacao(TRECHO_FLAP, texto, cortes_marcados=True))
    assert not a["comprovada"] and "pulado" in a["motivo"]


def test_ordem_que_a_fonte_nao_diz_e_ordem_acrescentada():
    trecho = "Commands deployed the aft momentum flap, a device that provides balance against solar pressure."
    texto = "On 28 December 2021 controllers began the sunshield deployment. " + trecho
    a = o.avaliar_citacao(E27, trecho, ev.contexto_da_citacao(trecho, texto, cortes_marcados=True))
    assert not a["comprovada"] and "acrescentada" in a["motivo"]


def test_referencia_dentro_do_proprio_trecho_comprova_mesmo_em_recorte():
    trecho = "The king signed the treaty in Paris, and then the war resumed in the north."
    a = o.avaliar_citacao("O rei assinou o tratado e depois a guerra recomeçou.", trecho, {"contiguidade": "nao_garantida"})
    assert a["comprovada"] and a["referente"] == "no próprio trecho"


def test_sem_contexto_a_vizinhanca_e_desconhecida():
    assert not o.avaliar_citacao(E27, TRECHO_FLAP, None)["comprovada"]


# ---------------------------------------------------------------- o juiz real discorda do código: o código vale
def test_e27_juiz_real_aprova_primeiro_e_o_codigo_reformula(juiz):
    e = _julgar(E27, TRECHO_FLAP)
    c = e["citacoes"][0]
    assert c["sustentacao"]["decisao"] == "sustenta" and "contínuo" in c["sustentacao"]["justificativa"]  # a IA disse isso
    assert c["ordem"]["ia_discordou"] and c["ordem"]["decisao_ia"] == "sustenta"
    assert e["afirmacao"].startswith("Comandos desdobraram") and e["afirmacao_com_ordem"] == E27
    assert e["ordem"]["indeterminada"] and "recorte" in e["ordem"]["motivo"]
    assert e["status"] == "fonte_unica"  # o acontecimento continua usável, sem a ordem
    assert metricas.resumo()["ordem_ia_discordou"] == 1


def test_e28_juiz_real_aprova_em_seguida_e_o_codigo_reformula(juiz):
    e = _julgar(E28, TRECHO_COVERS)
    assert e["afirmacao"].startswith("O controle da missão liberou") and e["ordem"]["indeterminada"]
    assert e["citacoes"][0]["ordem"]["ia_discordou"]


def test_e86_texto_guardado_ordem_indeterminada(juiz):
    e = _julgar(E86, TRECHO_FLAP)
    assert "primeiro" not in e["afirmacao"] and e["afirmacao_com_ordem"] == E86 and e["status"] != "nao_verificado"


def test_ordem_que_e_o_proprio_fato_fica_bloqueada_mesmo_com_o_juiz_aprovando(juiz):
    e = _julgar(E23_ANTIGA, TRECHO_FLAP)
    c = e["citacoes"][0]
    assert c["verificado"] is False and c["sustentacao"]["decisao"] == "ordem_nao_comprovada"
    assert c["sustentacao"]["por"] == "codigo" and c["sustentacao"]["decisao_anterior"]["decisao"] == "sustenta"
    assert e["status"] == "nao_verificado" and e["ordem"]["bloqueada"]


def test_decisao_do_cache_tambem_passa_pela_trava(juiz):
    _julgar(E27, TRECHO_FLAP)
    n = len(juiz.prompts)
    e = _julgar(E27, TRECHO_FLAP)
    assert len(juiz.prompts) == n and e["citacoes"][0]["sustentacao"].get("cache")  # veio do cache...
    assert e["afirmacao"].startswith("Comandos desdobraram")                         # ...e a ordem saiu igual


def test_com_vizinhanca_marcada_a_ordem_fica_e_e_comprovada(juiz):
    e = _julgar(E27, TRECHO_FLAP, marcado=True)
    assert e["afirmacao"] == E27 and e["ordem"]["comprovada"] and e["ordem"]["referente"].startswith("On 28 December")


# ---------------------------------------------------------------- redação e bloco do roteirista
def _banco_ordem(ordem):
    e = {**_ev("Comandos desdobraram a aba de momento traseira.", TRECHO_FLAP), "id": "E27", "status": "fonte_unica",
         "conflitos": [], "ordem": ordem}
    return {"evidencias": [e], "lacunas": []}


def _frases(*textos, ev_ids=("E27",), tipo="factual"):
    return {"scenes": [{"frases": [{"texto": t, "tipo": tipo, "evidencias": list(ev_ids)} for t in textos]}]}


def test_redacao_nao_pode_introduzir_ordem_sem_prova():
    banco = _banco_ordem({"comprovada": False, "indeterminada": True, "motivo": "recorte"})
    _, prob = rd.auditar_codigo(_frases("Primeiro, liberaram uma aba traseira que equilibra a pressão da luz solar."), banco)
    assert [p["tipo"] for p in prob] == ["ordem_nao_comprovada"] and "\"Primeiro\"" in prob[0]["motivo"]
    _, prob = rd.auditar_codigo(_frases("Liberaram uma aba traseira que equilibra a pressão da luz solar."), banco)
    assert prob == []


def test_redacao_com_ordem_comprovada_da_mesma_classe_passa():
    banco = _banco_ordem({"comprovada": True, "classes": ["inicio"], "referente": "On 28 December...", "marcas": ["Primeiro"]})
    _, prob = rd.auditar_codigo(_frases("Primeiro, liberaram uma aba traseira."), banco)
    assert prob == []
    _, prob = rd.auditar_codigo(_frases("Por fim, liberaram uma aba traseira."), banco)  # "por fim" a fonte não dá
    assert [p["tipo"] for p in prob] == ["ordem_nao_comprovada"]


def test_frase_narrativa_tambem_nao_afirma_sequencia():
    _, prob = rd.auditar_codigo(_frases("Depois, tudo mudou.", ev_ids=(), tipo="narrativa"), _banco_ordem({}))
    assert [p["tipo"] for p in prob] == ["ordem_nao_comprovada"]


def test_data_da_evidencia_ancora_a_ordem():
    banco = _banco_ordem({})
    banco["evidencias"][0].update(afirmacao="Em 4 de janeiro de 2022, os controladores tensionaram as camadas.",
                                  numeros=["4", "2022"])
    _, prob = rd.auditar_codigo(_frases("Depois, em 4 de janeiro de 2022, os controladores tensionaram as camadas."), banco)
    assert not [p for p in prob if p["tipo"] == "ordem_nao_comprovada"]


def test_bloco_do_escritor_diz_ordem_indeterminada_ou_comprovada():
    e = {**_ev("Comandos desdobraram a aba.", TRECHO_FLAP), "id": "E27", "status": "fonte_unica"}
    e["citacoes"][0]["contexto"] = ev.contexto_da_citacao(TRECHO_FLAP, S9, cortes_marcados=False)
    e["ordem"] = {"comprovada": False, "indeterminada": True, "motivo": "recorte sem marca de corte"}
    assert "ordem INDETERMINADA" in ev.bloco_para_escritor({"evidencias": [e]}, 220)
    e["ordem"] = {"comprovada": True, "marcas": ["Primeiro"], "referente": "On 28 December 2021, ..."}
    assert "ordem COMPROVADA" in ev.bloco_para_escritor({"evidencias": [e]}, 220)


# ---------------------------------------------------------------- E43/E47: o trecho guardado é o trecho conferido
S16 = ("The sunshield of the James Webb Space Telescope (JWST) is a kite-shaped, five-layer passive thermal control system "
       "composed of thin Kaptonpolyimide membranes coated with aluminum and, on the outermost layers, doped silicon, "
       "designed to shield the observatory's infrared instruments from solar, Earth, and lunar heat while maintaining a "
       "stark temperature gradient—up to 230°F (110°C) on the sun-facing side and as low as −388°F (−233°C) on the "
       "instrument side.[1][2]This innovative structure, spanning approximately 21.2 meters by 14.2 meters (the length of "
       "a tennis court), is the largest component of the JWST and enables the telescope to operate effectively at its "
       "Lagrange point 2 (L2) orbit, 1.5 million kilometers from Earth, by blocking nearly all infrared radiation and "
       "providing SPF 1 million-level protection against overheating.[3]")
E43 = ("A estrutura bloqueia quase toda a radiação infravermelha e oferece proteção equivalente a FPS 1 milhão contra "
       "superaquecimento, permitindo a operação no ponto de Lagrange L2 a 1,5 milhão de km da Terra.")


def test_trecho_longo_guarda_a_frase_com_os_numeros_em_vez_de_cortar_em_600():
    cortado = ev._cortar_trecho(S16, E43)
    assert "SPF 1 million" in cortado and len(cortado) <= ev.LIMITE_TRECHO
    assert ev.numeros(E43) <= ev.numeros_fonte(cortado) and ev.trecho_existe(cortado, S16)


def test_citacao_verificada_contem_o_que_foi_conferido():
    """Antes: conferida com o parágrafo inteiro (tinha "1 million") e guardada com os 600 primeiros caracteres (sem)."""
    fontes = [{"id": "S1", "url": "https://grokipedia.com/page/x", "titulo": "JWST sunshield", "dominio": "grokipedia.com",
               "classe": "outra"}]
    saida = ev._validar([{"afirmacao": E43, "citacoes": [{"fonte": "S1", "trecho": S16}]}], [{"trecho": S16}], fontes)
    c = saida[0]["citacoes"][0]
    assert c["verificado"] and "SPF 1 million" in c["trecho"] and ev.numeros(E43) <= ev.numeros_fonte(c["trecho"])


def test_trecho_chave_em_pedacos_vale_se_cada_pedaco_existe_no_trecho():
    trecho = ("This innovative structure enables the telescope to operate at its Lagrange point 2 (L2) orbit, 1.5 million "
              "kilometers from Earth, by blocking nearly all infrared radiation and providing SPF 1 million-level protection.")
    j = {"decisao": "sustenta", "justificativa": "o trecho diz isso com todas as letras",
         "trecho_chave": "providing SPF 1 million-level protection... operate at its Lagrange point 2 (L2) orbit"}
    assert ev._julgamento_valido(j, trecho)
    assert not ev._julgamento_valido({**j, "trecho_chave": "providing SPF 1 billion-level protection... operate at"}, trecho)
    assert not ev._julgamento_valido({**j, "trecho_chave": "SPF"}, trecho)


def test_sem_decisao_valida_nunca_promove_a_evidencia(monkeypatch):
    """E43: o juiz devolve algo que não dá para conferir -> sem_julgamento -> nao_verificado (ausência não aprova)."""
    monkeypatch.setattr(providers, "complete", lambda m, **kw: json.dumps({"julgamentos": [
        {"n": 1, "decisao": "sustenta", "sujeito": "ok", "medida": "ok", "trecho_chave": "texto que não está no trecho",
         "justificativa": "aprovo porque parece certo"}]}))
    e = _julgar(E43, ev._cortar_trecho(S16, E43), texto=S16)
    assert e["citacoes"][0]["sustentacao"]["decisao"] == "sem_julgamento" and e["status"] == "nao_verificado"
