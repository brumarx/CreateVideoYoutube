"""22/11 — tradução de termos técnicos (E74: "first (port) side" virou "primeiro lado (boreste)") e números em
unidades diferentes (E82: "0,8 milhão de km (500.000 milhas)" recusada por leitura errada de "milhas").
Nenhuma chamada real."""
import json

import pytest

from src import evidencias as ev
from src import fatos, metricas, providers, termos, unidades

TEMA = "Como o telescópio James Webb se desdobrou no espaço depois do lançamento?"
# trecho guardado (S3, ESA "Webb deployment complete")
ESA = ("This was a multi-day process, with the first (port) side of the mirror deployed on 7 January and the second "
       "(starboard) side of the mirror deployed on 8 January.")
E74 = "O primeiro lado (boreste) do espelho primário foi desdobrado em 7 de janeiro."
E75 = "O segundo lado (estibordo) do espelho primário foi desdobrado em 8 de janeiro."
# trecho guardado (S15, Wikipédia do escudo solar)
S15 = ("2022 January 4, completion of tensioning/separating all five layers and the successful deployment of the JWST "
       "sunshield, ten days after launch and more than 0.8 million kilometers (500,000 mi) away from Earth.")
E82 = ("Em 4 de janeiro de 2022, concluiu-se a tensionamento e separação de todas as cinco camadas e a implantação "
       "bem-sucedida do escudo solar do JWST, dez dias após o lançamento e a mais de 0,8 milhão de quilômetros "
       "(500.000 milhas) da Terra.")


# ======================================================================= termos técnicos
def test_e74_port_traduzido_como_boreste_e_conflito_ligado_ao_mesmo_dia():
    [c] = termos.conflitos(E74, ESA)
    assert c["original"] == "(port)" and c["traducao"] == "boreste" and c["conceito_fonte"] == "bombordo"
    assert c["contexto"] == ESA and "7" in c["motivo"]


@pytest.mark.parametrize("afirmacao", [
    E75,
    "O primeiro lado (bombordo) do espelho primário foi desdobrado em 7 de janeiro.",
    "O lado de bombordo do espelho foi desdobrado primeiro, em 7 de janeiro.",
    "O espelho foi desdobrado em dois dias, 7 e 8 de janeiro.",
])
def test_traducoes_certas_de_port_starboard_passam(afirmacao):
    assert termos.conflitos(afirmacao, ESA) == []


@pytest.mark.parametrize("afirmacao,trecho", [
    ("A asa de boreste abriu primeiro.", "The port wing opened first."),
    ("A asa de bombordo abriu primeiro.", "The starboard wing opened first."),
    ("O navio virou para estibordo.", "The ship turned to port."),
    ("La nave giró a babor.", "The ship turned to starboard."),
])
def test_bombordo_boreste_trocados_sao_conflito(afirmacao, trecho):
    assert termos.conflitos(afirmacao, trecho)


def test_port_como_porto_nao_e_termo_de_lado():
    assert termos.conflitos("O navio chegou ao porto de Santos e atracou a boreste.", "The ship reached the port of Santos.") == []
    assert termos.conflitos("A porta USB fica do lado esquerdo.", "The USB port is on the left side.") == []


@pytest.mark.parametrize("afirmacao,trecho,grupo", [
    ("O Webb observa no ultravioleta.", "Webb observes in the infrared.", "faixa do espectro"),
    ("O satélite chegou ao perigeu.", "The satellite reached apogee.", "ponto da órbita terrestre"),
    ("A Terra está no afélio em janeiro.", "Earth is at perihelion in January.", "ponto da órbita solar"),
    ("Os elétrons saem do ânodo.", "Electrons leave the cathode.", "eletrodo"),
    ("A lente côncava junta a luz.", "The convex lens converges light.", "curvatura"),
    ("A lua tem órbita retrógrada.", "The moon has a prograde orbit.", "sentido de órbita ou rotação"),
    ("A cidade fica ao sul do rio.", "The city lies north of the river.", "ponto cardeal"),
    ("O mastro esquerdo abriu.", "The right boom deployed while the left one waited.", "esquerda/direita"),
])
def test_glossario_vale_para_outras_areas(afirmacao, trecho, grupo):
    if grupo == "esquerda/direita":   # os dois lados no trecho: o direito é que abriu
        afirmacao = "O mastro esquerdo abriu primeiro."
        trecho = "First the right boom deployed, then the left one."
        assert termos.conflitos("O mastro direito abriu primeiro.", trecho) == []
    achados = termos.conflitos(afirmacao, trecho)
    assert achados and achados[0]["grupo"] == grupo


def test_termo_sem_conceito_rival_no_trecho_nao_e_conflito():
    assert termos.conflitos("O Webb observa no infravermelho.", "Webb observes in the infrared.") == []
    assert termos.conflitos("O Webb fica ao norte.", "Webb sits in a halo orbit.") == []  # o trecho não fala de direção


def _cit(trecho, sustentacao=None, verificado=True):
    return {"fonte": "S1", "trecho": trecho, "verificado": verificado, **({"sustentacao": sustentacao} if sustentacao else {})}


def test_travar_termos_bloqueia_pelo_glossario_mesmo_com_o_juiz_aprovando():
    aprovou = {"decisao": "sustenta", "sujeito": "ok", "medida": "ok", "por": "ia", "justificativa": "o trecho diz isso"}
    e = {"afirmacao": E74, "citacoes": [_cit(ESA, aprovou)]}
    cont = termos.travar_termos([e])
    c = e["citacoes"][0]
    assert not c["verificado"] and c["sustentacao"]["decisao"] == "traducao_incorreta" and c["sustentacao"]["por"] == "codigo"
    assert c["sustentacao"]["decisao_anterior"]["decisao"] == "sustenta" and cont["bloqueadas_codigo"] == 1
    assert c["termos"][0]["original"] == "(port)" and c["termos"][0]["traducao"] == "boreste" and c["termos"][0]["contexto"] == ESA
    assert e["afirmacao"] == E74                         # a afirmação não é reescrita: o código não escolhe tradução


def test_travar_termos_bloqueia_pelo_juiz_em_termo_fora_do_glossario():
    j = {"decisao": "sustenta", "sujeito": "ok", "medida": "ok", "por": "ia", "justificativa": "x",
         "termos": [{"original": "fairing", "traducao": "carenagem", "mesmo_sentido": True, "justificativa": "ok"},
                    {"original": "momentum flap", "traducao": "aba de impulso", "mesmo_sentido": False,
                     "justificativa": "momentum flap é aba de compensação de pressão solar, não de impulso"}]}
    e = {"afirmacao": "A aba de impulso foi aberta.", "citacoes": [_cit("The momentum flap was deployed.", j)]}
    cont = termos.travar_termos([e])
    c = e["citacoes"][0]
    assert not c["verificado"] and c["sustentacao"]["por"] == "ia" and cont["bloqueadas_ia"] == 1
    assert "momentum flap" in c["sustentacao"]["justificativa"] and len(c["termos"]) == 2


def test_travar_termos_e_idempotente_e_nao_mexe_no_que_esta_certo():
    e = {"afirmacao": E75, "citacoes": [_cit(ESA)]}
    termos.travar_termos([e])
    termos.travar_termos([e])
    assert e["citacoes"][0]["verificado"] and e["citacoes"][0]["termos"] == []


def test_termos_do_juiz_so_valem_copiados_literalmente():
    ts = [{"original": "port", "traducao": "boreste", "mesmo_sentido": False, "justificativa": "lado oposto"},
          {"original": "bow", "traducao": "proa", "mesmo_sentido": False},      # não está no trecho: não vale
          {"original": "port", "traducao": "bombordo", "mesmo_sentido": "não"}]  # não booleano: não vale
    validos = ev._termos_validos(ts, E74, ESA)
    assert [(t["original"], t["traducao"]) for t in validos] == [("port", "boreste")] and validos[0]["contexto"] == ESA


class Juiz:
    """Juiz falso que APROVA tudo e não lista termos — a trava em código tem de pegar a E74 sozinha."""

    def __call__(self, messages, **kw):
        p = messages[0]["content"]
        julg = []
        for bloco in p.split("\n\n"):
            if ") AFIRMAÇÃO:" in bloco:
                n = int(bloco.split(")")[0].strip())
                trecho = bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]
                julg.append({"n": n, "decisao": "sustenta", "sujeito": "ok", "medida": "ok",
                             "trecho_chave": " ".join(trecho.split()[:5]), "justificativa": "o trecho diz exatamente isso"})
        return json.dumps({"julgamentos": julg})


def test_e74_no_banco_fica_fora_e_e75_continua(monkeypatch):
    from src import disk_cache
    monkeypatch.setattr(disk_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(disk_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(providers, "complete", Juiz())
    metricas.zerar()
    fontes_web = [{"titulo": "Webb deployment complete - ESA", "url": "https://www.esa.int/webb", "trecho": ESA}]
    evs = ev._validar([{"afirmacao": E74, "citacoes": [{"fonte": "S1", "trecho": ESA}]},
                       {"afirmacao": E75, "citacoes": [{"fonte": "S1", "trecho": ESA}]}], fontes_web, ev._fontes_do_banco(fontes_web))
    ev.anotar_contexto(evs, fontes_web)
    ev.julgar_sustentacao(evs, fontes_web, TEMA)
    e74, e75 = (ev._status(e) for e in evs)
    assert e74["status"] == "nao_verificado" and "boreste" in e74["motivo"]
    assert e75["status"] in ev.STATUS_USAVEIS
    assert metricas.resumo().get("termos_bloqueadas_codigo") == 1


def test_prompt_do_juiz_pede_a_conferencia_dos_termos():
    assert "TERMOS TÉCNICOS" in ev._PROMPT_SUSTENTACAO and '"mesmo_sentido"' in ev._PROMPT_SUSTENTACAO
    assert "mesmo que a frase pareça plausível" in ev._PROMPT_SUSTENTACAO


# ======================================================================= números e unidades
def test_milhas_e_milimetros_nao_sao_multiplicadas_por_mil():
    assert fatos.numeros("500.000 milhas") == {"500000"}
    assert fatos.numeros("5 milímetros") == {"5"}
    assert fatos.numeros("2 military bases") == {"2"}
    assert fatos.numeros("2 mil metros") == {"2", "2000"} and fatos.numeros("5億円") == {"5", "500000000"}


def test_e82_e_verificada_com_os_valores_originais():
    ok, conversoes = ev.numeros_conferidos(E82, fatos.numeros(E82), S15)
    assert ok and conversoes == []                       # os números da afirmação estão no trecho (sem conversão)


@pytest.mark.parametrize("afirmacao,trecho", [
    ("A mais de 800 mil quilômetros da Terra.", "more than 500,000 miles away from Earth"),
    ("A mais de 500 mil milhas da Terra.", "more than 0.8 million kilometers away from Earth"),
    ("O espelho tem 21 pés de diâmetro.", "The mirror is 6.5 meters across."),
    ("O lado quente chega a 230 °F.", "The hot side reaches 110 °C."),
    ("Pesa cerca de 2 toneladas.", "It weighs about 4,400 pounds."),
    ("Voa a cerca de 800 km/h.", "It flies at about 500 mph."),
    ("A área é de 2,6 km².", "The area is 1 square mile."),
])
def test_conversoes_compativeis_sao_aceitas_e_registradas(afirmacao, trecho):
    nums = fatos.numeros(afirmacao)
    ok, conversoes = ev.numeros_conferidos(afirmacao, nums, trecho)
    assert ok and conversoes and conversoes[0]["unidade_original"] != conversoes[0]["unidade_afirmacao"]
    assert conversoes[0]["fonte"] in trecho                 # o valor e a unidade da fonte ficam registrados como estão


@pytest.mark.parametrize("afirmacao,trecho,porque", [
    ("A mais de 700 mil quilômetros da Terra.", "more than 500,000 miles away", "valor diferente"),
    ("A menos de 800 mil quilômetros da Terra.", "more than 500,000 miles away", "sentido trocado (mais × menos)"),
    ("A mais de 804.672 quilômetros da Terra.", "more than 500,000 miles away", "precisão que a fonte não tem"),
    ("A 500 mil quilômetros da Terra.", "500,000 miles away", "unidade trocada sem converter"),
    ("Pesa 800 mil quilos.", "500,000 miles away", "grandezas diferentes"),
    ("Sobreviveram a −300 °C.", "survived −272 °C", "mesma unidade: o número tem de estar na fonte"),
    ("Sobreviveram a −500 °F.", "survived −272 °C", "arredondamento grosso demais (9%)"),
    ("Sobreviveram a −460 °F.", "survived −272 °C", "abaixo do zero absoluto"),
    ("A ponte tem 25 pés.", "The bridge is 6.5 meters long.", "conversão errada"),
    ("Voa a 805 km/h.", "It flies at 500 mph.", "805 tem três algarismos; 500 mph, um"),
    ("O espelho tem 21,3 metros.", "The mirror is 6.5 meters (21.3 feet) across.", "21,3 é pés, não metros"),
    ("A estrada tem 1,500 km.", "The road is 932 miles long.", "1,500 em português é 1,5"),
])
def test_conversoes_incompativeis_sao_recusadas(afirmacao, trecho, porque):
    ok, conversoes = ev.numeros_conferidos(afirmacao, fatos.numeros(afirmacao), trecho)
    assert not ok and conversoes == [], porque


def test_validar_guarda_a_conversao_na_citacao():
    fontes_web = [{"titulo": "x", "url": "https://en.wikipedia.org/wiki/X", "trecho": "The probe is more than 500,000 miles away from Earth."}]
    [e] = ev._validar([{"afirmacao": "A sonda está a mais de 800 mil quilômetros da Terra.",
                        "citacoes": [{"fonte": "S1", "trecho": "The probe is more than 500,000 miles away from Earth."}]}],
                      fontes_web, ev._fontes_do_banco(fontes_web))
    c = e["citacoes"][0]
    assert c["verificado"] and c["conversoes"][0]["fonte"] == "more than 500,000 miles"
    assert c["trecho"] == "The probe is more than 500,000 miles away from Earth."     # trecho original intacto


def test_unidade_nova_e_uma_linha_na_tabela():
    assert {"comprimento", "massa", "temperatura", "velocidade", "area", "volume"} <= {g for g, *_ in unidades.UNIDADES.values()}
