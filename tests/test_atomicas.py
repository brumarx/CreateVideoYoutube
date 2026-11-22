"""22/11 — evidências atômicas: cada evidência do banco é UM fato independente, com a própria citação e o próprio
julgamento. Os casos são os da prova C da rodada 11 (lote grande, pedido novo: 3 evidências sem verificação; lote
pequeno: uma afirmação "parcial" com duas citações), com as frases copiadas das fontes guardadas. Nenhuma chamada
real: o modelo falso responde à segunda chance da citação e ao juiz por regras fixas e guarda cada pedido."""
import json

import pytest

from src import disk_cache, metricas, providers
from src import evidencias as ev

TEMA = "Como o telescópio James Webb se desdobrou no espaço depois do lançamento?"

# frases literais das fontes guardadas (jwst3/todas_fontes.json): S6, S3 (ESA), S9 (Wikipédia), S12 (pt), S10 (NASA)
MAIOR = ("JWST is NASA's flagship observatory and is the largest telescope ever launched, with an aperture of 6.5 meters "
         "distributed over 18 mirror segments.")
DOBRADO = ("The two wings of Webb's primary mirror were folded to fit inside the fairing of an ESA-provided Ariane 5 "
           "rocket prior to launch.")
AJUSTE = ("The telescope was released from the upper stage 27 minutes 7 seconds after launch, beginning a 30-day adjustment "
          "to place the telescope in a Lissajous orbit around the L2 Lagrange point.")
ARIANE = ("A precisão do lançamento foi excepcional, garantindo ao telescópio uma sobrevida maior, devido a menor "
          "necessidade de queima de combustível para posicioná-lo em orbita.")
DISTANCIA = ("From its orbit nearly a million miles from Earth, Webb will study some of the most distant objects in the "
             "universe—but first it has to get there.")
GRADUAL = ("Webb–folded up inside an Ariane 5 rocket as it launched into space–will gradually unfurl as it travels to its "
           "destination beyond the Moon.")

FONTES_WEB = [
    {"titulo": "Space telescope - Wikipedia", "url": "https://en.wikipedia.org/wiki/Space_telescope", "trecho": MAIOR},
    {"titulo": "Webb deployment complete - ESA", "url": "https://www.esa.int/webb", "trecho": DOBRADO},
    {"titulo": "Launch and commissioning of JWST - Wikipedia",
     "url": "https://en.wikipedia.org/wiki/Launch_and_commissioning_of_the_James_Webb_Space_Telescope", "trecho": AJUSTE},
    {"titulo": "Ariane 5 - Wikipédia", "url": "https://pt.wikipedia.org/wiki/Ariane_5", "trecho": ARIANE},
    {"titulo": "Webb Telescope Deployment Sequence - NASA", "url": "https://svs.gsfc.nasa.gov/webb", "trecho": f"{DISTANCIA} {GRADUAL}"},
]
FONTES = ev._fontes_do_banco(FONTES_WEB)

# as 3 evidências sem verificação do lote grande (pedido novo) e a do lote pequeno, como o extrator as escreveu
C1 = ("O telescópio é o maior observatório já lançado, com abertura de 6,5 metros distribuída em 18 segmentos de espelho, "
      "e precisou ser dobrado dentro do foguete Ariane 5.")
C2 = ("Após um ajuste de 30 dias para entrar na órbita ao redor de L2 e meses de alinhamento e comissionamento, a NASA "
      "apresentou a primeira imagem de campo profundo em 11 de julho de 2022.")
C3 = ("A precisão excepcional do lançamento do Ariane 5 garantiu ao telescópio uma sobrevida maior por reduzir a "
      "necessidade de queima de combustível para posicionamento.")
C4 = ("O observatório se desdobra gradualmente durante a viagem até seu destino além da Lua, a quase um milhão de "
      "milhas da Terra.")

P_MAIOR = "O telescópio é o maior telescópio já lançado."
P_ABERTURA = "A abertura de 6,5 metros do telescópio é distribuída em 18 segmentos de espelho."
P_DOBRADO = "As duas asas do espelho primário foram dobradas para caber no foguete Ariane 5."
P_AJUSTE = "Após a separação, começou um ajuste de 30 dias para levar o telescópio a uma órbita ao redor de L2."
P_IMAGEM = "A NASA apresentou a primeira imagem de campo profundo em 11 de julho de 2022."
P_GRADUAL = "O observatório se desdobra gradualmente durante a viagem até seu destino além da Lua."
P_DISTANCIA = "A órbita do observatório fica a quase um milhão de milhas da Terra."

# o que a segunda chance devolve para cada afirmação (o modelo separa os fatos e copia a frase de cada um)
PARTES = {
    C1: [{"afirmacao": P_MAIOR, "fonte": "S1", "trecho": "is the largest telescope ever launched, with an aperture of 6.5 meters distributed over 18 mirror segments"},
         {"afirmacao": P_ABERTURA, "fonte": "S1", "trecho": "with an aperture of 6.5 meters distributed over 18 mirror segments"},
         {"afirmacao": P_DOBRADO, "fonte": "S2", "trecho": DOBRADO}],
    C2: [{"afirmacao": P_AJUSTE, "fonte": "S3", "trecho": AJUSTE},
         {"afirmacao": P_IMAGEM, "fonte": "S3", "trecho": ""}],
    C3: [{"afirmacao": C3, "fonte": "S3", "trecho": ""}],
}


class Modelo:
    """Segunda chance da citação + juiz. `regras(afirmacao, trecho)` -> julgamento; padrão: aprova."""

    def __init__(self, regras=None, partes=None):
        self.regras, self.partes, self.pedidos = regras or (lambda a, t: {"decisao": "sustenta"}), partes or PARTES, []

    def __call__(self, messages, **kw):
        p = messages[0]["content"]
        self.pedidos.append(p)
        if "não foi achada literalmente" in p:
            resp = []
            for bloco in p.split("\n\n"):
                if ") AFIRMAÇÃO:" in bloco:
                    n = int(bloco.split(")")[0].strip())
                    a = bloco.split("AFIRMAÇÃO: ", 1)[1].split("\n", 1)[0]
                    resp.append({"n": n, "partes": self.partes.get(a, [{"afirmacao": a, "fonte": "S1", "trecho": ""}])})
            return json.dumps({"evidencias": resp})
        if "MESMA medida" in p:
            return json.dumps({"pares": []})
        julg = []
        for bloco in p.split("\n\n"):
            if ") AFIRMAÇÃO:" not in bloco:
                continue
            n = int(bloco.split(")")[0].strip())
            a = bloco.split("AFIRMAÇÃO: ", 1)[1].split("\n", 1)[0]
            t = bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]
            j = {"sujeito": "ok", "medida": "ok", "trecho_chave": " ".join(t.split()[:5]),
                 "justificativa": "decisão tomada pelo trecho no contexto", "parte_sustentada": "", "partes": [], **self.regras(a, t)}
            julg.append({"n": n, **j})
        return json.dumps({"julgamentos": julg})

    def julgados(self):
        """(afirmação, trecho) de cada item que foi ao juiz."""
        out = []
        for p in self.pedidos:
            if "Você confere se o TRECHO" not in p:
                continue
            for bloco in p.split("\n\n"):
                if ") AFIRMAÇÃO:" in bloco:
                    out.append((bloco.split("AFIRMAÇÃO: ", 1)[1].split("\n", 1)[0], bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]))
        return out


@pytest.fixture
def modelo(monkeypatch):
    monkeypatch.setattr(disk_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(disk_cache, "put", lambda *a, **k: None)
    metricas.zerar()

    def usar(m):
        monkeypatch.setattr(providers, "complete", m)
        return m
    return usar


def _brutas(*pares):
    return [{"afirmacao": a, "citacoes": [{"fonte": f, "trecho": t}]} for a, f, t in pares]


def _c1(trecho_s2="the mirror was folded inside an Ariane 5"):
    """C1 como o extrator a escreveu: duas citações (S1 e S2), nenhuma com trecho literal que tenha tudo."""
    return [{"afirmacao": C1, "citacoes": [{"fonte": "S1", "trecho": JUNTADO_C1}, {"fonte": "S2", "trecho": trecho_s2}]}]


def _extrair_e_separar(brutos, existentes=None):
    evs = ev._validar(brutos, FONTES_WEB, FONTES)
    pend = [e for e in evs if e["citacoes"] and not any(c["verificado"] for c in e["citacoes"])]
    repetidas = []
    evs = ev._aplicar_trocas(evs, ev._recuperar_por_llm(pend, FONTES_WEB, FONTES), existentes, repetidas)
    ev.anotar_contexto(evs, FONTES_WEB)
    return evs, repetidas


# trechos como o extrator os montou: pedaços de frases juntados (não existem literalmente na fonte)
JUNTADO_C1 = "the largest telescope ever launched, with an aperture of 6.5 meters ... folded to fit inside the fairing of an Ariane 5"
JUNTADO_C2 = "beginning a 30-day adjustment to place the telescope in a Lissajous orbit, first deep field image on 11 July 2022"


# ======================================================================= 1) separação dos fatos
def test_composta_sem_trecho_literal_vira_fatos_atomicos_cada_um_com_a_sua_citacao(modelo):
    modelo(Modelo())
    existente = ev._validar(_brutas(("O Webb tem uma abertura de 6,5 metros distribuída sobre 18 segmentos de espelho.",
                                     "S1", "with an aperture of 6.5 meters distributed over 18 mirror segments")), FONTES_WEB, FONTES)
    assert existente[0]["citacoes"][0]["verificado"]
    evs, repetidas = _extrair_e_separar(_c1(), existentes=existente)
    assert [e["afirmacao"] for e in evs] == [P_MAIOR, P_DOBRADO]
    assert all(len(e["citacoes"]) == 1 and e["citacoes"][0]["verificado"] and e["desmembrada_de"] == C1 for e in evs)
    assert evs[0]["citacoes"][0]["fonte"] == "S1" and evs[1]["citacoes"][0]["fonte"] == "S2"
    # o fato da abertura já estava no banco: não entra de novo
    assert [r["afirmacao"] for r in repetidas] == [P_ABERTURA]


def test_fato_sem_frase_na_fonte_fica_bloqueado_e_o_vizinho_segue(modelo):
    modelo(Modelo())
    evs, _ = _extrair_e_separar(_brutas((C2, "S3", JUNTADO_C2)))
    ajuste, imagem = evs
    assert ajuste["afirmacao"] == P_AJUSTE and ajuste["citacoes"][0]["verificado"] and "30" in ajuste["numeros"]
    assert imagem["afirmacao"] == P_IMAGEM and not any(c["verificado"] for c in imagem["citacoes"])
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    ajuste, imagem = (ev._status(e) for e in evs)
    assert ajuste["status"] in ev.STATUS_USAVEIS and imagem["status"] == "nao_verificado"


def test_fato_unico_sem_frase_literal_continua_como_estava(modelo):
    """C3 não é composta. A segunda chance aponta uma frase de uma fonte (S4) que a evidência não cita: não vale —
    a citação não troca de fonte por conta própria, e a evidência continua como estava, bloqueada."""
    modelo(Modelo(partes={C3: [{"afirmacao": C3, "fonte": "S4", "trecho": ARIANE}]}))
    evs, _ = _extrair_e_separar(_brutas((C3, "S3", "the precision of the launch was exceptional")))
    assert [e["afirmacao"] for e in evs] == [C3] and not evs[0]["citacoes"][0]["verificado"]
    assert "desmembrada_de" not in evs[0]


def test_composta_sem_nenhum_fato_verificado_nao_e_trocada(modelo):
    modelo(Modelo(partes={C2: [{"afirmacao": P_AJUSTE.replace("30", "45"), "fonte": "S3", "trecho": "a 45-day trip to the Moon"},
                               {"afirmacao": P_IMAGEM, "fonte": "S3", "trecho": ""}]}))
    evs, _ = _extrair_e_separar(_brutas((C2, "S3", JUNTADO_C2)))
    assert [e["afirmacao"] for e in evs] == [C2] and not evs[0]["citacoes"][0]["verificado"]


def test_parte_com_fonte_que_a_evidencia_nao_cita_nao_vale(modelo):
    modelo(Modelo(partes={C1: [{"afirmacao": P_MAIOR, "fonte": "S1", "trecho": MAIOR},
                               {"afirmacao": P_DOBRADO, "fonte": "S2", "trecho": DOBRADO}]}))
    evs, _ = _extrair_e_separar(_brutas((C1, "S1", JUNTADO_C1)))   # só S1 citada
    maior, dobrado = evs
    assert maior["citacoes"][0]["verificado"]
    assert not any(c["verificado"] for c in dobrado["citacoes"])  # S2 não estava nas citações da evidência


# ======================================================================= 2) cada fato com a sua validação
def test_cada_fato_vai_ao_juiz_sozinho_com_o_seu_trecho(modelo):
    m = modelo(Modelo())
    evs, _ = _extrair_e_separar(_c1())
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    julgados = m.julgados()
    assert sorted(a for a, _ in julgados) == sorted([P_MAIOR, P_ABERTURA, P_DOBRADO])
    assert dict(julgados)[P_DOBRADO] == DOBRADO and C1 not in dict(julgados)


def test_um_fato_aprovado_nao_aprova_o_outro(modelo):
    m = modelo(Modelo(regras=lambda a, t: {"decisao": "contradiz"} if a == P_DOBRADO else {"decisao": "sustenta"}))
    evs, _ = _extrair_e_separar(_c1())
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    st = {e["afirmacao"]: ev._status(e)["status"] for e in evs}
    assert st[P_MAIOR] in ev.STATUS_USAVEIS and st[P_DOBRADO] == "nao_verificado"
    assert len(m.julgados()) == 3


# ======================================================================= 3) a composta que só o juiz pega (lote pequeno)
def _c4_com_duas_citacoes():
    evs = ev._validar([{"afirmacao": C4, "citacoes": [{"fonte": "S5", "trecho": DISTANCIA}, {"fonte": "S5", "trecho": GRADUAL}]}],
                      FONTES_WEB, FONTES)
    assert all(c["verificado"] for c in evs[0]["citacoes"])  # os dois trechos existem: só o juiz vê a junção
    ev.anotar_contexto(evs, FONTES_WEB)
    return evs


def _juiz_c4(aprovar_reavaliacao=lambda a, t: True):
    def regras(a, t):
        if a == C4:  # a composta: cada trecho comprova só um dos dois fatos
            dist = t == DISTANCIA
            return {"decisao": "parcial", "partes": [{"texto": P_GRADUAL, "comprovada": not dist},
                                                     {"texto": P_DISTANCIA, "comprovada": dist}]}
        certo = (a == P_DISTANCIA and t == DISTANCIA) or (a == P_GRADUAL and t == GRADUAL)
        return {"decisao": "sustenta" if certo and aprovar_reavaliacao(a, t) else "irrelevante"}
    return regras


def test_parcial_com_duas_citacoes_vira_dois_fatos_cada_um_com_a_citacao_que_o_comprova(modelo):
    m = modelo(Modelo(regras=_juiz_c4()))
    evs = _c4_com_duas_citacoes()
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    assert len(evs) == 2
    finais = {e["afirmacao"]: ev._status(e) for e in evs}
    assert set(finais) == {P_GRADUAL, P_DISTANCIA}
    assert [c["trecho"] for c in finais[P_DISTANCIA]["citacoes"]] == [DISTANCIA]
    assert [c["trecho"] for c in finais[P_GRADUAL]["citacoes"]] == [GRADUAL]
    assert all(e["status"] in ev.STATUS_USAVEIS for e in finais.values())
    assert evs[0]["afirmacao_original"] == C4 and evs[1]["desmembrada_de"] == C4
    # cada fato foi julgado contra cada trecho (2 fatos x 2 trechos), além dos 2 julgamentos da composta
    assert len(m.julgados()) == 6
    assert metricas.resumo().get("atomicas_desmembradas") == 1


def test_fato_que_o_juiz_diz_comprovado_mas_nao_se_confirma_fica_bloqueado(modelo):
    modelo(Modelo(regras=_juiz_c4(aprovar_reavaliacao=lambda a, t: a != P_GRADUAL)))
    evs = _c4_com_duas_citacoes()
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    finais = {e["afirmacao"]: ev._status(e) for e in evs}
    assert finais[P_DISTANCIA]["status"] in ev.STATUS_USAVEIS
    assert finais[P_GRADUAL]["status"] == "nao_verificado"
    assert all(not c["verificado"] for c in finais[P_GRADUAL]["citacoes"])


def test_composta_sem_fato_confirmado_continua_uma_so_e_bloqueada(modelo):
    modelo(Modelo(regras=_juiz_c4(aprovar_reavaliacao=lambda a, t: False)))
    evs = _c4_com_duas_citacoes()
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    assert len(evs) == 1 and evs[0]["afirmacao"] == C4 and ev._status(evs[0])["status"] == "nao_verificado"


def test_fato_separado_que_ja_esta_no_banco_nao_duplica(modelo):
    modelo(Modelo(regras=_juiz_c4()))
    ja = ev._validar(_brutas(("A órbita do observatório fica a quase um milhão de milhas da Terra.", "S5", DISTANCIA)),
                     FONTES_WEB, FONTES)
    evs = ja + _c4_com_duas_citacoes()
    ev.anotar_contexto(evs, FONTES_WEB)
    ev.julgar_sustentacao(evs, FONTES_WEB, TEMA)
    textos = [e["afirmacao"] for e in evs]
    assert textos.count(P_DISTANCIA) == 1 and P_GRADUAL in textos and len(evs) == 2


def test_partes_do_juiz_sao_validadas():
    assert ev._partes_validas([{"texto": "um fato com palavras", "comprovada": True}]) == []        # um fato só: não é composta
    assert ev._partes_validas([{"texto": "fato um aqui", "comprovada": "sim"},
                               {"texto": "fato dois aqui", "comprovada": True}]) == []               # não booleano não vale
    assert len(ev._partes_validas([{"texto": "fato um aqui", "comprovada": False},
                                   {"texto": "fato dois aqui", "comprovada": True}])) == 2


# ======================================================================= 4) pedidos
def test_regra_atomica_no_pedido_de_extracao_e_na_busca_dirigida():
    for prompt in (ev._PROMPT, ev._PROMPT_DIRIGIDO):
        assert "UMA evidência = UM fato independente" in prompt and "O roteiro\njunta fatos depois" in prompt
    assert '"partes"' in ev._PROMPT_SUSTENTACAO and "um fato comprovado não comprova o outro" in ev._PROMPT_SUSTENTACAO
