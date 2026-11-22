"""Modo redação do Fractal (19/11): banco de evidências (src/evidencias.py), roteirista livre e
auditoria por categoria (src/redacao.py). Zero API: toda resposta de IA é simulada; as fontes de
Yakutsk são as 17 páginas reais que a pesquisa trouxe no job 987 (tests/fixtures)."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import evidencias as ev  # noqa: E402
from src import fatos as ft  # noqa: E402
from src import fontes as fo  # noqa: E402
from src import redacao as rd  # noqa: E402
from tests.auditor_falso import AUD  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
YAKUTSK_987 = json.loads((Path(__file__).parent / "fixtures" / "yakutsk_fontes_job987.json").read_text())
TEMA_987 = "Como Yakutsk, a cidade mais fria do mundo, funciona com edifícios sobre estacas e tubulações acima do solo a -50 °C?"
TEMA_988 = "Por que edifícios em Yakutsk são construídos sobre estacas cravadas a 15 metros no permafrost?"

CLICK = {"titulo": "Click Petróleo", "url": "https://clickpetroleoegas.com.br/yakutsk",
         "trecho": "Em Yakutsk, na Rússia, prédios são construídos sobre estacas profundas para não aquecer o solo; "
                   "o permafrost nunca descongela e toda a engenharia urbana depende de fundações elevadas."}
WIKI_EN = {"titulo": "Permafrost", "url": "https://en.wikipedia.org/wiki/Permafrost",
           "trecho": "Only two large cities are built on continuous permafrost, Norilsk and Yakutsk. The Melnikov "
                     "Permafrost Institute in Yakutsk found that pile foundations should extend down to 15 metres "
                     "(49 ft) to avoid the risk of buildings sinking."}
NOAA = {"titulo": "NOAA", "url": "https://arctic.noaa.gov/x",
        "trecho": "In Yakutsk winter temperatures regularly fall below -40 degrees. Piles of 20 metres are used in some districts."}


_PEDIDOS_DO_BANCO = (("defina a HISTÓRIA", "historia"), ("Diagnóstico da cobertura", "perguntas"),
                     ("Busca dirigida nas fontes", "respostas"))


class FalsoLLM:
    """Substitui providers.complete: devolve as respostas na ordem e guarda os pedidos."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos: list[str] = []
        self.pedidos_cobertura: list[str] = []  # 22/11: a segunda passagem fica à parte (os índices dos testes antigos)

    ultimo_aud = None

    def __call__(self, messages, **kw):
        p = messages[-1]["content"]
        (self.pedidos_cobertura if any(m in p for m, _ in _PEDIDOS_DO_BANCO) else self.pedidos).append(p)
        if "ESPECTADOR atento" in p or "Você é o EDITOR" in p:
            # 20/11: leitura de espectador e editor respondem pelo mesmo auditor simulado da rodada
            aud = self.ultimo_aud or next((r for r in self.respostas if callable(r)), None) or AUD(8)
            return json.dumps(aud(p), ensure_ascii=False)
        extra = next((k for m, k in _PEDIDOS_DO_BANCO if m in p), None)
        if extra and not (self.respostas and isinstance(self.respostas[0], dict) and extra in self.respostas[0]):
            # 22/11: história do vídeo e segunda passagem respondem "sem resposta" a menos que o teste enfileire uma
            # resposta delas — os testes antigos enfileiram só extração e juiz (sem história, sem segunda passagem)
            return "{}"
        if not self.respostas:
            raise RuntimeError("Nenhum provedor respondeu")
        r = self.respostas.pop(0)
        if callable(r):
            self.ultimo_aud = r
            r = r(p)
        r = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        validate = kw.get("validate")
        if validate and not validate(r):
            raise RuntimeError("Nenhum provedor devolveu resposta válida")
        return r


@pytest.fixture(autouse=True)
def juiz_aprova(monkeypatch, request):
    """O juiz de sustentação (20/11) aprova tudo nos testes que não tratam dele; os que tratam pedem
    a fixture `juiz_real` e respondem pelo FalsoLLM."""
    if "juiz_real" in request.fixturenames:
        return
    monkeypatch.setattr(ev, "_julgar_lote", lambda itens, topic: {
        n: {"decisao": "sustenta", "sujeito": "ok", "medida": "nao_se_aplica",
            "trecho_chave": " ".join(it["trecho"].split()[:3]), "justificativa": "aprovado no teste sem juiz"}
        for n, it in enumerate(itens, 1)})


@pytest.fixture
def juiz_real():
    return True


@pytest.fixture
def llm(monkeypatch):
    def instalar(*respostas):
        falso = FalsoLLM(*respostas)
        from src import providers
        monkeypatch.setattr(providers, "complete", falso)
        return falso
    return instalar


def _ev(id_, afirmacao, status="oficial", numeros=None, trecho="", conflitos=None, estimado=False):
    return {"id": id_, "afirmacao": afirmacao, "tipo": "quantitativo" if numeros else "qualitativo",
            "status": status, "estimado": estimado, "numeros": sorted(numeros or ft.numeros(afirmacao)),
            "conflitos": conflitos or [],
            "citacoes": [{"fonte": "S1", "url": "https://x.gov/a", "titulo": "x", "dominio": "x.gov",
                          "classe": "oficial", "trecho": trecho or afirmacao, "verificado": True}]}


BANCO = {"versao": 1, "tema": TEMA_988, "fontes": [], "lacunas": [], "nao_lidos": [], "evidencias": [
    _ev("E1", "Yakutsk é uma das duas grandes cidades construídas sobre permafrost contínuo."),
    _ev("E2", "O Instituto Melnikov concluiu que as estacas devem descer até 15 metros para os prédios não afundarem.",
        trecho="pile foundations should extend down to 15 metres (49 ft) to avoid the risk of buildings sinking"),
    _ev("E3", "Os prédios ficam sobre estacas para não aquecer o solo congelado.", status="fonte_unica"),
    _ev("E4", "O permafrost é solo que permanece a 0 °C ou menos por pelo menos 2 anos seguidos."),
]}


def _roteiro(frases_por_cena):
    return {"title": "t", "description": "d", "tags": [], "_redacao": True, "_banco": BANCO,
            "scenes": [{"narration": " ".join(f["texto"] for f in fr), "frases": fr} for fr in frases_por_cena]}


def F(texto, tipo="factual", evid=()):
    return {"texto": texto, "tipo": tipo, "evidencias": list(evid)}


def ED(*cenas, titulo=""):
    """Resposta da edição por cena: ED((2, [F(...)]), (3, [F(...)], "justificativa"))."""
    return {"cenas": [{"cena": c[0], "funcao": "f", "preserva": "p", "corrige": "c",
                       "justificativa": c[2] if len(c) > 2 else "", "frases": c[1]} for c in cenas], "titulo": titulo}


# ======================================================================= 1. fato qualitativo
def test_1_fato_qualitativo_entra_no_banco_sem_numero(llm):
    llm({"evidencias": [{"afirmacao": "Em Yakutsk, os prédios são construídos sobre estacas profundas para não aquecer o solo.",
                         "tipo": "qualitativo",
                         "citacoes": [{"fonte": "S1", "trecho": "Em Yakutsk, na Rússia, prédios são construídos sobre estacas "
                                                                "profundas para não aquecer o solo"}]}], "lacunas": []})
    banco = ev.montar_banco("Yakutsk sobre estacas", [CLICK])
    e = banco["evidencias"][0]
    assert e["tipo"] == "qualitativo" and e["numeros"] == []
    assert e["status"] == "fonte_unica" and e in ev.usaveis(banco)
    assert e["citacoes"][0]["verificado"] is True


def test_1b_citacao_parafraseada_e_recuperada_da_fonte_pelo_codigo(llm):
    # o modelo "melhorou" a citação; o código acha a frase real da fonte que a sustenta
    llm({"evidencias": [{"afirmacao": "Em Yakutsk, os prédios ficam sobre estacas profundas para não aquecer o solo.",
                         "citacoes": [{"fonte": "S1", "trecho": "In Yakutsk, Russia, buildings are constructed on deep "
                                       "estacas profundas para não aquecer o solo permafrost prédios construídos"}]}]})
    banco = ev.montar_banco("Yakutsk sobre estacas", [CLICK])
    c = banco["evidencias"][0]["citacoes"][0]
    assert c["verificado"] and c["recuperado"] and "estacas profundas" in c["trecho"]


def test_1c_citacao_inventada_fica_nao_verificada_com_motivo_e_nao_sustenta_nada(llm):
    llm({"evidencias": [{"afirmacao": "Os prédios de Yakutsk têm calefação a gás natural.",
                         "citacoes": [{"fonte": "S1", "trecho": "All buildings in Yakutsk are heated by natural gas boilers"}]}]},
        {"evidencias": [{"n": 1, "trecho": ""}]})
    banco = ev.montar_banco("Yakutsk sobre estacas", [CLICK])
    e = banco["evidencias"][0]
    assert e["status"] == "nao_verificado" and "não existe" in e["motivo"]
    assert ev.usaveis(banco) == []


@pytest.mark.parametrize("trecho,ok", [
    ("pile foundations should extend down to 15 metres", True),
    ("Pile   foundations SHOULD extend down to 15 metres.", True),           # caixa/espaço/pontuação
    ("Melnikov Permafrost Institute ... pile foundations should extend", True),  # reticências
    ("pile foundations should extend down to 25 metres", False),             # número trocado
    ("foundations", False),                                                  # curto demais para provar
])
def test_trecho_existe_confere_no_texto_baixado(trecho, ok):
    assert ev.trecho_existe(trecho, WIKI_EN["trecho"]) is ok


# ======================================================================= 2. narrativa
def test_2_prompt_do_escritor_pede_historia_e_nao_ids_no_texto(llm):
    falso = llm({"titulo": "Yakutsk", "descricao": "d", "tags": [], "cenas": [{"frases": [F("Mas por quê?", "narrativa")]}]})
    from src.config import ChannelConfig
    rd.escrever(ChannelConfig.load("curiosidades"), TEMA_988, BANCO, min_minutes=0.01, max_minutes=5, cenas_ref=6,
                tentativas=1)
    pedido = falso.pedidos[0]
    assert "CONTAR A MELHOR HISTÓRIA" in pedido and "Não é para percorrer E1, E2, E3" in pedido
    assert "nunca no texto" in pedido and "[F" not in pedido


def test_2b_lista_de_fatos_e_detectada_e_historia_passa():
    lista = _roteiro([[F("Yakutsk fica sobre permafrost.", evid=["E1"])], [F("As estacas têm 15 metros.", evid=["E2"])],
                      [F("O solo fica a 0 °C.", evid=["E4"])], [F("Os prédios ficam sobre estacas.", evid=["E3"])],
                      [F("O instituto estudou o solo.", evid=["E2"])], [F("A cidade fica na Rússia.", evid=["E1"])]])
    assert rd.parece_lista(lista)
    historia = _roteiro([[F("Imagine morar num lugar assim.", "narrativa"),
                          F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])],
                         [F("Mas aí vem o problema.", "narrativa"),
                          F("Um prédio aquecido derreteria o solo embaixo dele.", "explicativa", ["E3"])],
                         [F("Por isso os prédios ficam no alto, sobre estacas.", "explicativa", ["E3"]),
                          F("E elas descem até 15 metros, segundo o Instituto Melnikov.", evid=["E2"])]])
    assert rd.parece_lista(historia) is None


def test_2c_escritor_recebe_defeitos_e_tenta_de_novo(llm):
    lista = {"titulo": "t", "cenas": [{"frases": [F(f"Fato número {k} sobre a cidade.", evid=["E1"])]} for k in range(1, 8)]}
    boa = {"titulo": "t", "descricao": "d", "tags": [], "cenas": [
        {"frases": [F("Imagine morar num lugar assim.", "narrativa"),
                    F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])]},
        {"frases": [F("Mas aí vem o problema.", "narrativa"),
                    F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"])]},
        {"frases": [F("E as estacas descem até 15 metros.", evid=["E2"])]},
        {"frases": [F("Agora pense no inverno.", "narrativa")]},
        {"frases": [F("É assim que a cidade continua de pé.", "narrativa")]}]}
    falso = llm(lista, boa)
    from src.config import ChannelConfig
    s = rd.escrever(ChannelConfig.load("curiosidades"), TEMA_988, BANCO, min_minutes=0.1, max_minutes=5, cenas_ref=6)
    assert len(falso.pedidos) == 2 and "corrija TODOS" in falso.pedidos[1]
    assert s["scenes"][0]["frases"][0]["tipo"] == "narrativa"


# ======================================================================= 3. rastreabilidade
def test_3_toda_afirmacao_factual_aponta_para_evidencia_real():
    s = _roteiro([[F("Mas o que segura esses prédios?", "narrativa")],
                  [F("As estacas descem até 15 metros.")],               # sem id: ligado pelo número
                  [F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"])]])
    s, problemas = rd.auditar_codigo(s, BANCO)
    assert problemas == []
    ids = ev.por_id(BANCO)
    for _, _, f in rd.frases_de(s):
        if f["tipo"] != "narrativa":
            assert f["evidencias"] and all(ids[x]["status"] in ev.STATUS_USAVEIS for x in f["evidencias"])
    assert s["scenes"][1]["frases"][0]["evidencias"] == ["E2"]
    assert "[E" not in " ".join(sc["narration"] for sc in s["scenes"])


def test_3b_metadados_sobrevivem_a_edicao_do_texto():
    s = _roteiro([[F("Os predios ficam sobre estacas para nao aquecer o solo congelado.", "explicativa", ["E3"])]])
    s["scenes"][0]["narration"] = "Os prédios ficam sobre estacas para não aquecer o solo congelado."  # ortografia
    s = rd.sincronizar(s)
    assert s["scenes"][0]["frases"][0]["evidencias"] == ["E3"]
    s["scenes"][0]["narration"] += " Eles foram erguidos em 1632."  # frase nova sem metadado
    s = rd.sincronizar(s)
    nova = s["scenes"][0]["frases"][1]
    assert nova["tipo"] == "factual" and nova["evidencias"] == []
    _, problemas = rd.auditar_codigo(s, BANCO)
    assert any("1632" in p["termo"] for p in problemas)


# ======================================================================= 4. narrativa sem citação
@pytest.mark.parametrize("frase", ["Mas viver nessas condições não é nada simples.", "E aí, mente curiosa!",
                                   "Agora vem a parte mais estranha.", "Parece impossível, né?"])
def test_4_construcao_narrativa_nao_precisa_de_fonte(frase):
    s = _roteiro([[F(frase, "narrativa")]])
    _, problemas = rd.auditar_codigo(s, BANCO, TEMA_988)
    assert problemas == []


@pytest.mark.parametrize("frase", ["É a cidade mais fria do planeta.", "Ali vivem 300 mil pessoas.",
                                   "Tudo começou com engenheiros da Finlândia."])
def test_4b_narrativa_que_esconde_afirmacao_e_cobrada(frase):
    s = _roteiro([[F(frase, "narrativa")]])
    _, problemas = rd.auditar_codigo(s, BANCO, TEMA_988)
    assert problemas and problemas[0]["tipo"] in ("sem_evidencia", "numero_sem_evidencia")


def test_4c_auditor_ia_nao_reprova_narrativa_por_falta_de_fonte(llm):
    llm(AUD(8, [], {1: ("sem_suporte", "não há fonte para isso"),
                    2: ("excede", "a fonte não diz que é o único motivo")}))
    s = _roteiro([[F("Mas viver ali não é simples.", "narrativa"),
                   F("O único motivo das estacas é o frio.", "explicativa", ["E3"])]])
    problemas, q = rd.auditar_ia(s, BANCO, TEMA_988)
    assert [p["tipo"] for p in problemas] == ["excede"] and q["nota"] == 8


# ======================================================================= 5. número inventado
def test_5_numero_que_nao_esta_nas_evidencias_e_bloqueado():
    s = _roteiro([[F("As estacas descem até 40 metros.", evid=["E2"])]])
    _, problemas = rd.auditar_codigo(s, BANCO)
    assert problemas[0]["tipo"] == "numero_sem_evidencia" and "40" in problemas[0]["motivo"]


def test_5b_numero_por_extenso_tambem_e_conferido():
    s = _roteiro([[F("As estacas descem até quarenta metros.", evid=["E2"])]])
    _, problemas = rd.auditar_codigo(s, BANCO)
    assert problemas and problemas[0]["tipo"] == "numero_sem_evidencia"


# ======================================================================= 6. fontes contraditórias
def test_6_valores_divergentes_de_mesma_forca_viram_contraditorios_os_dois():
    a = _ev("E1", "As estacas dos prédios de Yakutsk têm 15 metros.", status="fonte_unica")
    b = _ev("E2", "As estacas dos prédios de Yakutsk têm 20 metros.", status="fonte_unica")
    ev.marcar_conflitos([a, b])
    assert a["status"] == b["status"] == "contraditorio"
    assert a["conflitos"] == ["E2"] and b["conflitos"] == ["E1"]


def test_6b_fonte_mais_forte_prevalece_e_a_fraca_fica_marcada():
    a = _ev("E1", "As estacas dos prédios de Yakutsk têm 15 metros.", status="oficial")
    b = _ev("E2", "As estacas dos prédios de Yakutsk têm 20 metros.", status="fonte_unica")
    ev.marcar_conflitos([a, b])
    assert a["status"] == "oficial" and b["status"] == "contraditorio" and a["conflitos"] == ["E2"]


def test_6c_roteiro_nao_escolhe_um_valor_contraditorio_mas_pode_apresentar_a_divergencia():
    banco = {**BANCO, "evidencias": [_ev("E1", "As estacas dos prédios de Yakutsk têm 15 metros.", status="contraditorio",
                                         conflitos=["E2"]),
                                     _ev("E2", "As estacas dos prédios de Yakutsk têm 20 metros.", status="contraditorio",
                                         conflitos=["E1"])]}
    escolhe = _roteiro([[F("As estacas têm 15 metros.", evid=["E1"])]])
    _, p = rd.auditar_codigo(escolhe, banco)
    assert p and p[0]["tipo"] == "numero_contraditorio"
    apresenta = _roteiro([[F("As fontes divergem: as estacas teriam entre 15 e 20 metros.", evid=["E1", "E2"])]])
    _, p = rd.auditar_codigo(apresenta, banco)
    assert p == []


# ======================================================================= 7. evidência perdida
def test_7_evidencia_de_yakutsk_chega_ao_extrator_mesmo_com_17_fontes(llm):
    falso = llm({"evidencias": [], "lacunas": []}, {"evidencias": [], "lacunas": []})
    ev.montar_banco(TEMA_987, YAKUTSK_987)
    lido = "\n".join(falso.pedidos)
    assert "Melnikov Permafrost Institute in Yakutsk" in lido
    assert "estacas profundas para não aquecer o solo" in lido


def test_7b_o_que_nao_cabe_em_nenhum_lote_fica_registrado_no_banco(llm, monkeypatch):
    monkeypatch.setattr(ev, "LIMITE_LOTE", 1500)
    llm({"evidencias": [], "lacunas": []}, {"evidencias": [], "lacunas": []})
    banco = ev.montar_banco(TEMA_987, YAKUTSK_987)
    assert banco["nao_lidos"] and all(x["url"] and x["nota"] > 0 for x in banco["nao_lidos"])


def test_7c_bloco_do_revisor_e_do_juiz_tambem_tem_a_evidencia():
    bloco = fo.bloco_fontes(None, YAKUTSK_987, TEMA_987)
    assert len(bloco) <= fo.LIMITE_FONTES
    assert "Melnikov" in bloco and "estacas profundas" in bloco


def test_7d_material_que_cabe_nao_muda_de_ordem_nem_de_texto():
    fontes = [CLICK, WIKI_EN]
    assert fo.bloco_fontes(None, fontes, TEMA_987) == fo.bloco_fontes(None, fontes)
    assert ft.bloco_numerado(fontes, TEMA_987) == ft.bloco_numerado(fontes)


# ======================================================================= 8. correção narrativa
def test_8_frase_sem_prova_e_reformulada_sem_destruir_o_resto(llm):
    s = _roteiro([[F("Imagine morar num lugar assim.", "narrativa"),
                   F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])],
                  [F("Os russos inventaram a estaca em 1890.", evid=["E3"])],
                  [F("Por isso os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"])]])
    s, problemas = rd.auditar_codigo(s, BANCO)
    assert len(problemas) == 1
    falso = llm(ED((2, [F("E a solução foi tirar os prédios do chão.", "narrativa")])))
    alvos = rd.alvos_da_edicao(s, {"nota": 9, "aprovado": True}, problemas)
    assert list(alvos) == [1] and "FACTUAL" in alvos[1][0] and "1890" in alvos[1][0]
    novo, reg = rd.editar_cenas(s, alvos, BANCO, TEMA_988, problemas=problemas)
    assert reg["cenas_editadas"] == [2] and not reg["factuais_sem_correcao"]
    assert "Nunca ponha outro número no lugar" in falso.pedidos[0] and "FACTUAL" in falso.pedidos[0]
    textos = [f["texto"] for _, _, f in rd.frases_de(novo)]
    assert "E a solução foi tirar os prédios do chão." in textos
    assert textos[0] == "Imagine morar num lugar assim." and len(textos) == 4
    assert "1890" not in " ".join(sc["narration"] for sc in novo["scenes"])


def test_8b_edicao_que_inventa_outro_numero_volta_a_original_e_fica_registrada(llm):
    s = _roteiro([[F("Imagine uma cidade gelada.", "narrativa")], [F("As estacas têm 40 metros.", evid=["E2"])],
                  [F("E descem até 15 metros.", evid=["E2"])]])
    s, problemas = rd.auditar_codigo(s, BANCO)
    llm(ED((2, [F("As estacas têm 45 metros.", evid=["E2"])])))
    novo, reg = rd.editar_cenas(s, rd.alvos_da_edicao(s, {"nota": 9}, problemas), BANCO, TEMA_988, problemas=problemas)
    # a trava em código continua: o número substituto não entra; a frase reprovada fica para a auditoria seguinte
    assert [f["texto"] for _, _, f in rd.frases_de(novo)] == [f["texto"] for _, _, f in rd.frases_de(s)]
    assert [r["cena"] for r in reg["revertidas"]] == [2] and reg["factuais_sem_correcao"][0]["cena"] == 2


def test_8c_auditar_e_corrigir_busca_evidencia_antes_de_reformular(llm):
    s = _roteiro([[F("Imagine morar num lugar assim.", "narrativa")],
                  [F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])],
                  [F("As estacas evitam que o calor dos prédios derreta o permafrost.", "explicativa", [])]])
    banco = json.loads(json.dumps(BANCO))
    banco["evidencias"] = banco["evidencias"][:2]
    buscas = []

    def buscar(problemas, tema):
        buscas.append([p["termo"] for p in problemas])
        return [CLICK]
    llm(AUD(8, []),                                   # auditoria 1
        {"evidencias": [{"afirmacao": "Os prédios de Yakutsk ficam sobre estacas para não aquecer o solo.",
                         "citacoes": [{"fonte": "S1", "trecho": "prédios são construídos sobre estacas profundas "
                                                                "para não aquecer o solo"}]}]},          # extração da fonte nova
        ED((3, [F("As estacas existem para os prédios não aquecerem o solo.", "explicativa", ["E3"])])),  # edição
        AUD(8, []))                                       # auditoria 2
    final, rel = rd.auditar_e_corrigir(s, banco, TEMA_988, web_facts=[], buscar_evidencia=buscar)
    assert buscas and "derreta o permafrost" in buscas[0][0]
    assert rel["registro_edicoes"][0]["evidencias_novas"] == ["E3"]
    ultima = final["scenes"][-1]["frases"][-1]
    assert ultima["evidencias"] == ["E3"] and ev.por_id(banco)["E3"]["status"] in ev.STATUS_USAVEIS


# ======================================================================= 9. sem quatro números
def test_9_roteiro_comprovado_com_um_numero_so_e_aprovado(llm):
    s = _roteiro([[F("Imagine morar num lugar assim.", "narrativa"),
                   F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])],
                  [F("Mas aí vem o problema.", "narrativa"),
                   F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"])],
                  [F("E elas descem até 15 metros.", evid=["E2"])]])
    llm(AUD(8, []))
    final, rel = rd.auditar_e_corrigir(s, BANCO, TEMA_988)
    assert len(ft.numeros(" ".join(sc["narration"] for sc in final["scenes"]))) < ft.MIN_NUMEROS_FINAL
    assert rel["qualidade"]["nota"] == 8
    assert rd.roteiro_insuficiente(final, 30) is None


def test_9b_historia_fraca_nao_passa_so_por_ter_fatos(llm):
    s = _roteiro([[F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])]])
    llm(AUD(3, ["abertura sem gancho"]))
    with pytest.raises(rd.RoteiroReprovado, match="qualidade narrativa"):
        rd.auditar_e_corrigir(s, BANCO, TEMA_988)


def test_9c_sem_auditor_forte_o_roteiro_nao_sai(llm):
    llm()  # nenhum provedor
    with pytest.raises(rd.RoteiroReprovado, match="auditor forte indisponível"):
        rd.auditar_e_corrigir(_roteiro([[F("Mas por quê?", "narrativa")]]), BANCO, TEMA_988)


# ======================================================================= 10. integração
def test_10_roteiro_da_redacao_passa_pelo_acabamento_e_chega_inteiro_a_producao(monkeypatch):
    from src import spellcheck
    from src.config import ChannelConfig
    from src.script_gen import clean_stat_overlays, trim_teaser_ending
    monkeypatch.setattr(spellcheck, "fix_text", lambda t, lang="pt-BR": t.replace("predios", "prédios"))
    monkeypatch.setattr(spellcheck, "_proofread_short", lambda t: t)
    data = {"titulo": "Yakutsk", "descricao": "d", "tags": ["a"], "cenas": [
        {"frases": [F("Imagine morar num lugar assim.", "narrativa"),
                    F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", "factual", ["E1"])]},
        {"frases": [F("Mas os predios aquecidos derreteriam o solo.", "explicativa", ["E3"])]},
        {"frases": [F("Por isso ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"]),
                    F("E elas descem até 15 metros.", "factual", ["E2"])]}]}
    s = rd.de_json(data, BANCO)
    s = ChannelConfig.load("curiosidades").with_greeting(s)
    s = trim_teaser_ending(rd.sincronizar(s))
    s = spellcheck.fix_script(s, "pt-BR")
    s = clean_stat_overlays(ft.encurtar_roteiro(s, 368))
    s = rd.conferencia_final(s, BANCO, TEMA_988)
    assert s["scenes"][0]["narration"].startswith("E aí, mente curiosa!")
    assert "prédios" in s["scenes"][1]["narration"]
    for sc in s["scenes"]:
        assert isinstance(sc["narration"], str) and sc["narration"]
        assert sc["narration"] == " ".join(f["texto"] for f in sc["frases"])
    assert s["scenes"][1]["frases"][0]["evidencias"] == ["E3"]       # metadado sobreviveu à ortografia
    json.dumps(s, ensure_ascii=False)                                 # script.json grava banco e metadados
    assert s["_banco"] is BANCO and "_fatos_verificados" not in s  # título livre, conferido (job 996)


def test_10b_pipeline_liga_o_modo_redacao_e_mantem_a_producao():
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    assert 'redacao_mode = channel.modo_roteiro == "redacao"' in src
    for trecho in ("escrever(channel, topic, banco", "auditar_e_corrigir(", "conferencia_final(script, banco, topic)",
                   "range(0 if (approved or redacao_ativa)", "roteiro_insuficiente("):
        assert trecho in src
    # a produção continua depois do texto validado: título/miniatura e imagens do roteiro final
    pos = src.index("conferencia_final(script, banco, topic)")
    assert src.index("image_prompts_from_final(script, topic)") > pos
    assert src.index("art_direction(script, channel)") > pos


def test_10c_so_o_fractal_esta_no_modo_redacao():
    from src.config import ChannelConfig
    assert ChannelConfig.load("curiosidades").modo_roteiro == "redacao"
    assert ChannelConfig.load("motivacional").modo_roteiro == ""
    assert ChannelConfig.load("aprendervia").modo_roteiro == ""


# ======================================================================= regressão Yakutsk
def test_regressao_987_evidencia_cortada_pelo_limite_do_bloco():
    """Antes: o bloco da extração parava na fonte 9 de 17; Melnikov (15) e Click (17) nunca eram lidos."""
    antes = ft.bloco_numerado(YAKUTSK_987)  # sem tema = corte antigo
    assert "Melnikov" not in antes
    agora = ft.bloco_numerado(YAKUTSK_987, TEMA_987)
    assert "Melnikov" in agora and "estacas profundas" in agora
    assert "[S15]" in agora and "[S17]" in agora  # numeração continua sendo a posição da fonte


def test_regressao_987_virgula_de_milhar_do_ingles():
    assert "1524" in ft.numeros_fonte("down to 5,000 feet (1,524 m) in Siberia")
    fatos = ft.atribuir([{"texto": "O permafrost chega a 1.524 metros de espessura na Sibéria.", "fontes": ["S1"]}],
                        [{"titulo": "Permafrost", "url": "https://en.wikipedia.org/wiki/Permafrost",
                          "trecho": "permafrost reaches 1,524 m (5,000 ft) in Siberia"}])
    assert fatos, "o fato com 1.524 metros não pode ser descartado"


def test_regressao_988_por_cento_nao_e_numero_por_extenso():
    assert ft.numero_por_extenso("O permafrost ocorre em cerca de 26 por cento da área terrestre.") == []
    assert ft.extenso_para_algarismos("cerca de 26 por cento") == "cerca de 26 por cento"
    assert ft.extenso_para_algarismos("vinte e seis por cento") == "26 por cento"


def test_regressao_988_texto_bom_com_um_numero_nao_reprova_por_magro():
    s = {"scenes": [{"narration": "palavra " * 162 + "15 metros."}]}
    assert ft.roteiro_magro(s)                       # a regra antiga reprovava
    assert rd.roteiro_insuficiente(s, 126) is None   # a da redação não


def test_regressao_989_afirmacao_qualitativa_sustentada_nao_e_cortada():
    """Job 989: 13 afirmações sem marca, 0 sustentadas, 13 removidas — inclusive a das estacas."""
    s = _roteiro([[F("Para o calor dos prédios não derreter o permafrost, as construções ficam sobre estacas.",
                     "explicativa", ["E3"])]])
    final, problemas = rd.auditar_codigo(s, BANCO, TEMA_988)
    assert problemas == [] and "estacas" in final["scenes"][0]["narration"]


def test_regressao_989_promessa_qualitativa_tem_evidencia_no_banco_real(llm):
    """O fato central do tema (estacas em Yakutsk) entra no banco a partir das fontes reais do job."""
    llm({"evidencias": [{"afirmacao": "O Instituto Melnikov, em Yakutsk, concluiu que as estacas devem descer até 15 metros.",
                         "citacoes": [{"fonte": "S15", "trecho": "The Melnikov Permafrost Institute in Yakutsk found that "
                                       "pile foundations should extend down to 15 metres (49 ft) to avoid the risk of "
                                       "buildings sinking"}]},
                        {"afirmacao": "Em Yakutsk, os prédios são construídos sobre estacas profundas para não aquecer o solo.",
                         "citacoes": [{"fonte": "S17", "trecho": "Em Yakutsk, na Rússia, prédios são construídos sobre "
                                                                 "estacas profundas para não aquecer o solo"}]}],
         "lacunas": []}, {"evidencias": [], "lacunas": []})
    banco = ev.montar_banco(TEMA_987, YAKUTSK_987)
    status = {e["afirmacao"][:20]: e["status"] for e in banco["evidencias"]}
    assert all(s in ev.STATUS_USAVEIS for s in status.values()), status
    assert any(e["tipo"] == "qualitativo" for e in ev.usaveis(banco))


# ======================================================================= cabe no Groq grátis (job 994/995)
def test_pedidos_de_julgamento_cabem_no_groq_gratis(llm):
    """Extração e auditoria reservavam 12 mil tokens de resposta: o Groq (8 chaves, gpt-oss-120b) era
    pulado sempre e tudo dependia da única chave NVIDIA. Agora cada pedido real cabe nos 8 mil."""
    from src import providers
    tamanhos = []

    def medir(messages, **kw):
        tamanhos.append(providers._too_big_for("groq", messages, kw.get("max_tokens", 0)))
        if "ROTEIRO, frase a frase" in messages[-1]["content"]:
            return json.dumps(AUD(8, []))
        return json.dumps({"evidencias": [], "lacunas": []})
    import src.providers as pv
    original = pv.complete
    pv.complete = medir
    try:
        ev.montar_banco(TEMA_987, YAKUTSK_987)
        grande = {**BANCO, "evidencias": [_ev(f"E{k}", f"Afirmação número {k} sobre as estacas de Yakutsk e o "
                                               f"permafrost contínuo da cidade.", trecho="x " * 150) for k in range(1, 40)]}
        frases = [[F("Imagine morar num lugar assim.", "narrativa"), F(f"Fato {k} das estacas.", evid=[f"E{k}"])]
                  for k in range(1, 13)]
        rd.auditar_ia(_roteiro(frases), grande, TEMA_987)
    finally:
        pv.complete = original
    assert tamanhos and not any(tamanhos), tamanhos


# ======================================================================= 20/11: os "64 graus negativos" (job 998)
BANCO_998 = json.loads((Path(__file__).parent / "fixtures" / "yakutsk_banco_job998.json").read_text())
FRASE_998 = ("O termômetro explica o porquê: a média anual ronda oito graus negativos, mas o inverno já marcou "
             "sessenta e quatro graus abaixo de zero.")


def _uma(texto, evid=(), tipo="factual"):
    return {"scenes": [{"frases": [F(texto, tipo, evid)]}]}


def test_regressao_998_origem_do_64_e_o_recorde_de_1891_que_o_escritor_recebe():
    """O 64 não veio de outro job nem do treino do modelo: o banco do próprio tema tem o recorde de
    −64,4 °C (Wikipédia, trecho verificado), e é isso que o escritor lê. Nenhuma evidência traz 64 inteiro."""
    e35 = rd.por_id(BANCO_998)["E35"]
    assert e35["status"] in ev.STATUS_USAVEIS and "64.4" in ev.numeros_da_evidencia(e35)
    assert any(c["verificado"] and "−64.4 °C" in c["trecho"] for c in e35["citacoes"])
    assert "−64,4 °C" in ev.bloco_para_escritor(BANCO_998, 0)
    assert not any("64" in ev.numeros_da_evidencia(e) for e in ev.usaveis(BANCO_998))


def test_regressao_998_64_seco_e_apontado_com_o_valor_e_a_evidencia_certos():
    """Antes: "número 64 não está em nenhuma evidência" — falso aos olhos do modelo (ele via −64,4),
    e ele insistia. Agora o motivo diz que é arredondamento de 64,4 da E35 e o que fazer."""
    _, probs = rd.auditar_codigo(_uma(FRASE_998, ["E34"]), BANCO_998, BANCO_998["tema"])
    assert [p["tipo"] for p in probs] == ["numero_arredondado"]
    m = probs[0]["motivo"]
    assert "64,4" in m and "E35" in m and '"aproximadamente 64"' in m and "não troque por outro número" in m


@pytest.mark.parametrize("texto,passa", [
    ("A menor temperatura já registrada foi −64,4 °C, em 1891.", True),            # valor exato
    ("O inverno já marcou mais de 64 graus abaixo de zero.", True),                 # aproximação para o lado certo
    ("O inverno já marcou mais de sessenta e quatro graus abaixo de zero.", True),  # por extenso também
    ("O inverno já marcou quase 64 graus negativos.", False),                       # lado errado: −64,4 passa de 64
    ("O inverno já marcou quase 65 graus negativos.", True),
    ("O inverno já marcou 64 graus negativos.", False),                             # arredondado sem avisar
    ("O inverno já marcou 64 graus abaixo de zero.", False),                        # "abaixo de" não é aproximação
])
def test_arredondamento_de_decimal_so_com_aproximacao_no_sentido_certo(texto, passa):
    _, probs = rd.auditar_codigo(_uma(texto, ["E35"]), BANCO_998, BANCO_998["tema"])
    assert (not probs) is passa, probs


@pytest.mark.parametrize("texto", ["O inverno já marcou 70 graus negativos.",
                                   "O inverno já marcou cerca de 70 graus negativos.",
                                   "O inverno já marcou mais de 66 graus negativos."])
def test_numero_que_nao_esta_em_evidencia_nenhuma_continua_bloqueado(texto):
    _, probs = rd.auditar_codigo(_uma(texto, ["E35"]), BANCO_998, BANCO_998["tema"])
    assert [p["tipo"] for p in probs] == ["numero_sem_evidencia"]
    assert "não ponha outro número no lugar" in probs[0]["motivo"]


def _cenas_64(frase_temp):
    return {"titulo": "Yakutsk", "descricao": "d", "tags": [], "cenas": [
        {"frases": [F("Imagine morar num lugar assim.", "narrativa"),
                    F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])]},
        {"frases": [F(frase_temp, evid=["E4"])]},
        {"frases": [F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"])]},
        {"frases": [F("E as estacas descem até 15 metros.", evid=["E2"])]},
        {"frases": [F("É assim que a cidade continua de pé.", "narrativa")]}]}


BANCO_FRIO = {**BANCO, "evidencias": BANCO["evidencias"] + [
    _ev("E5", "A temperatura mais baixa registrada em Yakutsk foi −64,4 °C em 5 de fevereiro de 1891.", "fonte_unica",
        trecho="The lowest temperature recorded in Yakutsk was −64.4 °C (−83.9 °F) on February 5, 1891"),
    _ev("E6", "Os invernos de Yakutsk são de frio extremo.", "fonte_unica")]}


def test_escritor_que_insiste_no_64_recebe_o_valor_certo_e_o_numero_nao_chega_ao_roteiro(llm):
    """O modelo repete "64 graus" nas 3 tentativas e na 1ª edição: o pedido de correção traz o valor da
    evidência (64,4), a cena que insiste volta à original e a frase segue reprovada; na 2ª edição vem o
    valor exato — o roteiro aprovado nunca tem o 64 seco."""
    teimoso = _cenas_64("O inverno já marcou 64 graus negativos.")
    falso = llm(teimoso, teimoso, teimoso,
                AUD(8, []),                                                                    # auditoria 1
                ED((2, [F("O inverno já marcou 64 graus negativos.", evid=["E5"])])),          # edição 1 insiste
                ED((2, [F("A menor temperatura já registrada, em 1891, foi de −64,4 °C.", evid=["E5"])])),  # edição 2, sem
                AUD(8, []))                          # auditar de novo o mesmo texto; auditoria da v2
    from src.config import ChannelConfig
    s = rd.escrever(ChannelConfig.load("curiosidades"), TEMA_988, BANCO_FRIO, min_minutes=0.1, max_minutes=5, cenas_ref=6)
    assert len(falso.pedidos) == 3
    assert all("64,4" in p and "E5" in p for p in falso.pedidos[1:3])  # a correção diz o valor real
    final, rel = rd.auditar_e_corrigir(s, BANCO_FRIO, TEMA_988)
    texto = " ".join(sc["narration"] for sc in final["scenes"])
    assert "64" not in ft.numeros(texto) and "64 graus" not in texto and "−64,4 °C" in texto
    assert "Imagine morar num lugar assim." in texto and "15 metros" in texto  # o resto da história fica
    assert rel["registro_edicoes"][0]["factuais_sem_correcao"][0]["cena"] == 2 and rel["edicoes"] == 2
    assert "DEVOLVEU IGUAL" in [p for p in falso.pedidos if "EDIÇÃO LOCALIZADA" in p][-1]
    assert [v["problemas_factuais"] > 0 for v in rel["versoes"]] == [True, False]


def test_editor_que_insiste_duas_vezes_reprova_o_roteiro(llm):
    s = rd.de_json(_cenas_64("O inverno já marcou 64 graus negativos."), BANCO_FRIO)
    llm(AUD(8, []), ED((2, [F("O inverno já marcou 64 graus negativos.", evid=["E5"])])),
        ED((2, [F("O inverno já marcou 63 graus negativos.", evid=["E5"])])), AUD(8, []))
    with pytest.raises(rd.RoteiroReprovado, match="sem evidência depois de 2 edição"):
        rd.auditar_e_corrigir(s, BANCO_FRIO, TEMA_988)


def test_conserto_com_valor_exato_e_aceito(llm):
    s = rd.de_json(_cenas_64("O inverno já marcou 64 graus negativos."), BANCO_FRIO)
    llm(AUD(8, []),
        ED((2, [F("A menor temperatura já registrada, em 1891, foi de −64,4 °C.", evid=["E5"])])),
        AUD(8, []))
    final, _ = rd.auditar_e_corrigir(s, BANCO_FRIO, TEMA_988)
    assert "−64,4 °C" in " ".join(sc["narration"] for sc in final["scenes"])


def test_sem_evidencia_de_temperatura_vale_descricao_qualitativa_sustentada_e_nao_numero_substituto(llm):
    """Banco sem nenhuma temperatura: "70 graus" sai; o conserto que troca por outro número (60) também
    sai; a descrição sem número sustentada pela evidência (E6, frio extremo) fica."""
    banco = {**BANCO, "evidencias": BANCO["evidencias"] + [BANCO_FRIO["evidencias"][-1]]}
    s = rd.de_json(_cenas_64("O inverno já marcou 70 graus negativos."), banco)
    _, probs = rd.auditar_codigo(s, banco, TEMA_988)
    assert [p["tipo"] for p in probs] == ["numero_sem_evidencia"]
    alvos = rd.alvos_da_edicao(s, {"nota": 9}, probs)
    llm(ED((2, [F("O inverno já marcou 60 graus negativos.", evid=["E6"])])))
    trocado, _ = rd.editar_cenas(s, alvos, banco, TEMA_988, problemas=probs)
    assert "60" not in ft.numeros(" ".join(sc["narration"] for sc in trocado["scenes"]))
    falso = llm(ED((2, [F("E os invernos de lá são de frio extremo.", evid=["E6"])])))
    bom, _ = rd.editar_cenas(s, alvos, banco, TEMA_988, problemas=probs)
    assert "frio extremo" in " ".join(sc["narration"] for sc in bom["scenes"])
    assert "Nunca ponha outro número no lugar" in falso.pedidos[0]


def test_prompts_proibem_arredondar_decimal_sem_avisar_e_numero_substituto(llm):
    falso = llm(_cenas_64("O recorde de frio foi de −64,4 °C."))
    from src.config import ChannelConfig
    rd.escrever(ChannelConfig.load("curiosidades"), TEMA_988, BANCO_FRIO, min_minutes=0.1, max_minutes=5,
                cenas_ref=6, tentativas=1)
    p = falso.pedidos[0]
    assert "Decimal (−64,4 °C)" in p and "mais de 64 graus\n  negativos" in p
    assert "nunca outro número no lugar" in p and "Mantenha o sinal" in p


def test_sinal_negativo_fica_colado_ao_numero_na_estimativa():
    """Job 998: "A temperatura média anual é de −cerca de 8 °C"."""
    assert ft.estima_antes("A temperatura média anual é de −8 °C.", {"8"}) == "A temperatura média anual é de cerca de −8 °C."
    assert ft.estima_antes("−8 °C é a média.", {"8"}) == "Cerca de −8 °C é a média."
    assert ft.estima_antes("O vírus COVID-19 teve 30 casos.", {"30"}) == "O vírus COVID-19 teve cerca de 30 casos."


@pytest.mark.parametrize("texto,esperado", [
    ("entre vinte e cinquenta anos", {"20", "50"}),          # validação real 20/11: virava 70
    ("dez e onze andares", {"10", "11"}),
    ("cento e vinte e cinco metros", {"125"}),
    ("dois mil e quinhentos", {"2500"}),
    ("mil novecentos e noventa e um", {"1991"}),
    ("vinte e um mil", {"21", "21000"}),
    ("sessenta e quatro graus", {"64"}),
])
def test_extenso_so_compoe_o_que_o_portugues_compoe(texto, esperado):
    assert ft.numeros(ft.extenso_para_algarismos(texto)) == esperado


def test_faixa_por_extenso_nao_vira_numero_inventado():
    """'entre vinte e cinquenta anos' com a evidência de 20 a 50 anos: passa (antes: "número 70")."""
    banco = {**BANCO, "evidencias": [_ev("E1", "A vida útil dos prédios caiu para 20 a 50 anos.")]}
    _, probs = rd.auditar_codigo(_uma("A vida útil dos prédios caiu para entre vinte e cinquenta anos.", ["E1"]),
                                 banco, TEMA_988)
    assert probs == []


def test_sinal_negativo_falado_fica_colado_ao_numero():
    """Validação real 20/11: "O recorde histórico chegou a menos cerca de 64,4 graus"."""
    assert ft.estima_antes("O recorde chegou a menos 64,4 graus em 1891.", {"64.4"}) == \
        "O recorde chegou a cerca de menos 64,4 graus em 1891."
    assert ft.estima_antes("O recorde chegou a menos de 65 graus.", {"65"}) == "O recorde chegou a menos de 65 graus."


# ======================================================================= 20/11: E18 × E34 (média anual de Yakutsk)
FONTES_E18 = json.loads((Path(__file__).parent / "fixtures" / "yakutsk_fontes_e18_e34.json").read_text())
TEMA_998 = BANCO_998["tema"]
E18_BRUTA = {"afirmacao": "A temperatura média anual em Yakutsk fica em torno de 0 °C.",
             "citacoes": [{"fonte": "S1", "trecho": "average annual temperature is around 0°C, isolated permafrost bodies"}]}
E34_BRUTA = {"afirmacao": "A temperatura média anual em Yakutsk é de −8,0 °C (17,6 °F).",
             "citacoes": [{"fonte": "S2", "trecho": "Yakutsk has an average annual temperature of −8.0 °C (17.6 °F)"}]}


def test_regressao_e18_a_fonte_nao_fala_de_yakutsk():
    """Origem do erro: o relatório da arctic.gov descreve a zona de permafrost isolado ("average annual
    temperature is around 0°C, isolated permafrost bodies") e não cita Yakutsk em lugar nenhum."""
    arctic = FONTES_E18["arctic_gov"]
    assert ev.trecho_existe(E18_BRUTA["citacoes"][0]["trecho"], arctic["trecho"])  # a citação literal existe...
    assert not ev.fonte_fala_de("yakutsk", arctic)                                   # ...mas não é sobre Yakutsk


def test_e18_vai_ao_juiz_avisada_e_e34_continua(llm, juiz_real):
    """20/11: o código não sabe se a página sem "Yakutsk" fala de outro assunto (E18) ou do mesmo com outro nome
    (Bhutan/Butão). Não descarta: manda ao juiz COM O AVISO; o juiz recusa a E18 e mantém a E34."""
    falso = llm({"evidencias": [E18_BRUTA, E34_BRUTA], "lacunas": []},
                {"julgamentos": [{"n": 1, "decisao": "irrelevante", "sujeito": "ausente", "medida": "nao_se_aplica",
                                  "trecho_chave": "isolated permafrost bodies",
                                  "justificativa": "o trecho descreve a zona de permafrost isolado, não Yakutsk"},
                                 {"n": 2, "decisao": "sustenta", "sujeito": "ok", "medida": "ok",
                                  "trecho_chave": "average annual temperature of −8.0 °C",
                                  "justificativa": "o trecho dá exatamente a média anual de Yakutsk"}]})
    banco = ev.montar_banco(TEMA_998, [FONTES_E18["arctic_gov"], FONTES_E18["wiki_en"]])
    e18, e34 = banco["evidencias"]
    juiz = falso.pedidos[1]
    assert "ATENÇÃO: o nome yakutsk não aparece nesta página" in juiz.split("2) AFIRMAÇÃO")[0]
    assert "ATENÇÃO" not in juiz.split("2) AFIRMAÇÃO")[1]                    # a E34 é de página sobre Yakutsk
    assert e18["status"] == "nao_verificado" and "zona de permafrost isolado" in e18["motivo"]
    assert e18["citacoes"][0]["sujeito_nao_achado"] == ["yakutsk"]         # guardada com o registro
    assert e34["status"] == "fonte_unica" and e34 in ev.usaveis(banco)


def test_sujeito_com_outro_nome_ou_alfabeto_nao_e_descartado(llm):
    """Não descartar fato verdadeiro por falha do mecanismo: a Wikipédia em português chama a cidade de
    Iacutusque (o título diz Yakutsk) e a russa escreve Якутск."""
    assert ev.fonte_fala_de("yakutsk", FONTES_E18["wiki_pt"]) and ev.fonte_fala_de("yakutsk", FONTES_E18["wiki_ru"])
    llm({"evidencias": [{"afirmacao": "A amplitude térmica em Yakutsk pode passar de 100 °C.",
                         "citacoes": [{"fonte": "S1", "trecho": "são as únicas cidades no mundo onde a amplitude térmica "
                                                                "pode variar mais que 100 °C"}]}], "lacunas": []})
    banco = ev.montar_banco(TEMA_998, [FONTES_E18["wiki_pt"]])
    assert banco is None or ev.usaveis(banco) or banco["evidencias"][0]["status"] != "nao_verificado" or \
        "não fala de" not in banco["evidencias"][0].get("motivo", "")


def test_temperatura_com_sinal_entra_na_deteccao_de_conflito():
    assert ft.temperaturas("média anual de −8,0 °C (17,6 °F)") == {("-8", "°C"), ("17.6", "°F")}
    assert ft.temperaturas("em torno de 0 °C") == {("0", "°C")}
    assert ("-8", "°C") in ft._pares("A temperatura média anual em Yakutsk é de −8,0 °C.")


def test_divergencia_sem_prova_local_bloqueia_as_duas():
    """Mesmo se a E18 passasse na verificação, "oficial" (domínio .gov) não vence: o trecho dela não
    fala de Yakutsk. As duas viram "contraditorio" — nenhuma é fato confirmado da média anual."""
    import copy
    E = rd.por_id(BANCO_998)
    a, b = copy.deepcopy(E["E18"]), copy.deepcopy(E["E34"])
    ev.marcar_conflitos([a, b], {"yakutsk"})
    assert (a["status"], b["status"]) == ("contraditorio", "contraditorio")
    assert a["conflitos"] == ["E34"] and b["conflitos"] == ["E18"]


def test_fonte_mais_forte_com_prova_local_continua_prevalecendo():
    """A regra da fonte primária (11/11) continua quando o trecho oficial fala do sujeito."""
    a = _ev("E1", "A temperatura média anual em Yakutsk é de −8,8 °C.", trecho="Yakutsk mean annual temperature −8.8 °C")
    b = _ev("E2", "A temperatura média anual em Yakutsk é de −7,0 °C.", "fonte_unica",
            trecho="Yakutsk average annual temperature −7.0 °C")
    ev.marcar_conflitos([a, b], {"yakutsk"})
    assert (a["status"], b["status"]) == ("oficial", "contraditorio")


def test_roteiro_nao_usa_media_anual_divergente_como_fato():
    import copy
    banco = copy.deepcopy(BANCO_998)
    E = rd.por_id(banco)
    ev.marcar_conflitos([E["E18"], E["E34"]], {"yakutsk"})
    _, probs = rd.auditar_codigo(_uma("A média anual de Yakutsk é de 8 graus negativos.", ["E34"]), banco, TEMA_998)
    assert probs and probs[0]["tipo"] in ("numero_contraditorio", "sem_evidencia")
    _, probs = rd.auditar_codigo(_uma("A média anual de Yakutsk fica em torno de 0 °C.", ["E18"]), banco, TEMA_998)
    assert probs


# ======================================================================= 20/11: precisão × confiabilidade
def test_valor_exato_de_fonte_unica_fica_exato():
    """O recorde medido de −64,4 °C (fonte única) não vira "cerca de −64,4 °C"."""
    t = "A menor temperatura já registrada em Yakutsk foi −64,4 °C, em 1891."
    out, probs = rd.auditar_codigo(_uma(t, ["E35"]), BANCO_998, TEMA_998)
    assert probs == [] and out["scenes"][0]["frases"][0]["texto"] == t
    assert out["scenes"][0]["frases"][0]["confianca"] == "fonte_unica"


def test_estimativa_continua_estimativa():
    banco = {**BANCO, "evidencias": [_ev("E1", "Yakutsk tem cerca de 355 mil habitantes.", "fonte_unica", estimado=True,
                                         trecho="Yakutsk has an estimated population of 355000")]}
    out, probs = rd.auditar_codigo(_uma("Yakutsk tem 355 mil habitantes.", ["E1"]), banco, TEMA_988)
    assert probs == [] and "cerca de 355 mil" in out["scenes"][0]["frases"][0]["texto"]


def test_recorde_de_fonte_unica_dito_como_consenso_pede_atribuicao():
    _, probs = rd.auditar_codigo(_uma("O recorde de frio chegou a −64,4 °C, em 1891.", ["E35"]), BANCO_998, TEMA_998)
    assert [p["tipo"] for p in probs] == ["fonte_unica_sem_atribuicao"] and "sem mudar o valor" in probs[0]["motivo"]
    _, probs = rd.auditar_codigo(_uma("Segundo os registros, o recorde de frio chegou a −64,4 °C, em 1891.", ["E35"]),
                                 BANCO_998, TEMA_998)
    assert probs == []


def test_recorde_corroborado_nao_precisa_de_atribuicao():
    import copy
    banco = copy.deepcopy(BANCO_998)
    rd.por_id(banco)["E35"]["status"] = "corroborado"
    _, probs = rd.auditar_codigo(_uma("O recorde de frio chegou a −64,4 °C, em 1891.", ["E35"]), banco, TEMA_998)
    assert probs == []


def test_prompt_separa_estimativa_de_fonte_unica(llm):
    falso = llm(_cenas_64("O recorde de frio foi de −64,4 °C."))
    from src.config import ChannelConfig
    rd.escrever(ChannelConfig.load("curiosidades"), TEMA_988, BANCO_FRIO, min_minutes=0.1, max_minutes=5,
                cenas_ref=6, tentativas=1)
    p = falso.pedidos[0]
    assert "Valor medido/exato fica EXATO, mesmo de fonte única" in p and '"estimado" ou "fonte_unica"' not in p
    assert '"fonte_unica" com recorde ou superlativo: diga de onde vem' in p


# ======================================================================= 20/11: sentido dos números negativos
@pytest.mark.parametrize("texto,tipo", [
    ("Segundo os registros, o recorde foi de −64,4 °C.", None),                                   # exato
    ("Segundo os registros, o recorde foi de aproximadamente 64 graus abaixo de zero.", None),   # aproximação
    ("Segundo os registros, o recorde passou de mais de 64 graus abaixo de zero.", None),        # sentido certo
    ("Segundo os registros, o recorde foi de menos sessenta e quatro vírgula quatro graus.", None),  # por extenso
    ("Segundo os registros, o recorde foi de sessenta e quatro graus negativos e quatro décimos.", "numero_arredondado"),
    ("Segundo os registros, o recorde foi de cerca de menos 64,4 graus.", None),                 # sinal depois do "cerca de"
    ("Segundo os registros, o recorde foi de menos cerca de 64 graus.", None),                   # sinal antes do "cerca de"
    ("Segundo os registros, o recorde passou de mais de 64 °C.", "sinal_invertido"),             # sem indicar negativo
    ("Segundo os registros, o recorde foi de 64,4 °C.", "sinal_invertido"),                      # sinal invertido
    ("Segundo os registros, o recorde foi de quase 64 graus abaixo de zero.", "numero_arredondado"),  # lado errado
    ("Segundo os registros, o recorde foi de −70 °C.", "numero_sem_evidencia"),                  # outro número
    ("Segundo os registros, o recorde foi de −60 °C.", "numero_sem_evidencia"),
])
def test_sentido_do_menos_64_4(texto, tipo):
    _, probs = rd.auditar_codigo(_uma(texto, ["E35"]), BANCO_998, TEMA_998)
    assert [p["tipo"] for p in probs] == ([tipo] if tipo else []), probs


def test_temperatura_positiva_nao_vira_negativa():
    banco = {**BANCO, "evidencias": [_ev("E1", "Em julho, Yakutsk chega a 38 °C.", "oficial",
                                         trecho="In July Yakutsk can reach 38 °C")]}
    assert rd.auditar_codigo(_uma("Em julho, Yakutsk chega a 38 graus.", ["E1"]), banco, TEMA_988)[1] == []
    _, probs = rd.auditar_codigo(_uma("Em julho, Yakutsk chega a 38 graus negativos.", ["E1"]), banco, TEMA_988)
    assert [p["tipo"] for p in probs] == ["sinal_invertido"]


@pytest.mark.parametrize("texto,esperado", [
    ("menos 64,4 graus", {("-64.4", "°C")}), ("64 graus negativos", {("-64", "°C")}),
    ("64 graus abaixo de zero", {("-64", "°C")}), ("−cerca de 64 graus", {("-64", "°C")}),
    ("menos cerca de 64 graus", {("-64", "°C")}), ("menos de 65 graus", {("65", "°C")}),  # "menos de" não é sinal
    ("−83,9 °F", {("-83.9", "°F")}), ("0 °C", {("0", "°C")}),
])
def test_leitura_do_sinal(texto, esperado):
    assert ft.temperaturas(ft.extenso_para_algarismos(texto)) == esperado


def test_media_nao_diverge_de_amplitude_nem_de_frase_sem_o_sujeito():
    """Não descartar fato verdadeiro: a média de −8 °C (E34) não conflita com a amplitude de 100 °C (E5)
    nem com a frase genérica sobre permafrost que não fala de Yakutsk (E31)."""
    import copy
    E = rd.por_id(BANCO_998)
    evs = [copy.deepcopy(E[k]) for k in ("E5", "E31", "E34", "E35")]
    ev.marcar_conflitos(evs, {"yakutsk"})
    assert [e["status"] for e in evs] == ["fonte_unica", "oficial", "fonte_unica", "fonte_unica"]
    assert not any(e.get("conflitos") for e in evs)


@pytest.mark.parametrize("afirmacao,ind", [
    ("A temperatura média anual em Yakutsk é de −8,0 °C.", "media"),
    ("A temperatura mais baixa registrada em Yakutsk foi −64,4 °C.", "minima"),
    ("A temperatura anual média de Yakutsk pode variar mais de 100 °C.", "amplitude"),
    ("Em julho a máxima chega a 38 °C.", "maxima"),
])
def test_indicador_da_temperatura(afirmacao, ind):
    assert ev._indicador(afirmacao) == ind



# ======================================================================= 20/11: sinal × comparação × aproximação
@pytest.mark.parametrize("trecho_frase,tipo", [
    ("menos 64,4 graus", None),                                   # sinal negativo
    ("menos sessenta e quatro vírgula quatro graus", None),       # sinal por extenso
    ("64,4 graus abaixo de zero", None),
    ("Abaixo de zero, o termômetro chegou a 64,4 graus", None),   # "abaixo de zero" antes, na mesma frase
    ("o termômetro chegou a 64,4 graus, bem abaixo de zero", None),
    ("menos de 65 graus", "sinal_invertido"),                     # comparação com valor POSITIVO, não sinal
    ("menos de sessenta e cinco graus", "sinal_invertido"),
    ("menos de 65 graus abaixo de zero", "numero_arredondado"),   # verdade na matemática, ambíguo para quem ouve
    ("menos de sessenta e cinco graus negativos", "numero_arredondado"),
    ("64,4 graus abaixo de zero", None),                          # o que o canal prefere
    ("aproximadamente 64 graus negativos", None),
    ("abaixo de 65 graus negativos", "numero_arredondado"),
    ("menos de 64 graus abaixo de zero", "numero_arredondado"),   # e falsa (64,4 não é menos que 64)
    ("menos de 70 graus abaixo de zero", "numero_sem_evidencia"), # verdadeira, mas folgada: outro número
    ("mais de 64 graus abaixo de zero", None),
    ("mais de 63 graus abaixo de zero", "numero_sem_evidencia"),
    ("mais de menos 64 graus", "numero_arredondado"),             # comparação com sinal no meio: ambígua
    ("menos 64 graus", "numero_arredondado"),                     # sinal certo, arredondado sem dizer
    ("cerca de 64 graus abaixo de zero", None),                   # aproximação
    ("cerca de menos 64,4 graus", None),
])
def test_sinal_comparacao_e_aproximacao_nao_se_confundem(trecho_frase, tipo):
    _, probs = rd.auditar_codigo(_uma(f"Segundo os registros, {trecho_frase}.", ["E35"]), BANCO_998, TEMA_998)
    assert [p["tipo"] for p in probs] == ([tipo] if tipo else []), probs


def test_menos_de_nao_e_lido_como_sinal_nem_como_aproximacao():
    assert ft.temperaturas("menos de 65 graus") == {("65", "°C")}
    assert rd.relacao_do_numero("menos de 65 graus", 9) == ("<", "menos de")
    assert rd.relacao_do_numero("menos 65 graus", 6) == ("=", "")          # sinal, não comparação
    assert rd.relacao_do_numero("cerca de 65 graus", 9) == ("≈", "cerca de")
    assert not rd._aproximacao_coerente("menos de 65 graus", "65", 66.0)   # 66 não é menos que 65
    _, probs = rd.auditar_codigo(_uma("Segundo os registros, menos de 65 graus.", ["E35"]), BANCO_998, TEMA_998)
    assert "é comparação, não sinal negativo" in probs[0]["motivo"]


# ======================================================================= 20/11: o trecho comprova a afirmação? (E19)
NSF = FONTES_E18["par_nsf"]
WIKI = FONTES_E18["wiki_en"]


def _banco_de(afirmacao, trecho, fonte, llm, *julgamentos):
    llm({"evidencias": [{"afirmacao": afirmacao, "citacoes": [{"fonte": "S1", "trecho": trecho}]}], "lacunas": []},
        *julgamentos)
    return ev.montar_banco(TEMA_998, [fonte])


def _julg(decisao, chave, justificativa="o juiz explica a decisão aqui", sujeito="ok", medida="nao_se_aplica"):
    return {"julgamentos": [{"n": 1, "decisao": decisao, "sujeito": sujeito, "medida": medida,
                             "trecho_chave": chave, "justificativa": justificativa}]}


def test_e19_fonte_correta_trecho_que_comprova(llm, juiz_real):
    b = _banco_de("Em Yakutsk, cerca de 10% das estruturas sobre permafrost tiveram deformações.",
                  "many buildings on permafrost had deformations, ranging from ~10% of structures in Yakutsk", NSF, llm,
                  _julg("sustenta", "10% of structures in Yakutsk", "o trecho diz que ~10% das estruturas de Yakutsk deformaram"))
    e = b["evidencias"][0]
    assert e["status"] == "oficial" and e["citacoes"][0]["sustentacao"]["decisao"] == "sustenta"


def test_e19_fonte_sobre_o_sujeito_mas_trecho_irrelevante(llm, juiz_real):
    b = _banco_de("Yakutsk tem um sistema de metrô.", "Yakutsk has an average annual temperature of −8.0 °C (17.6 °F)",
                  WIKI, llm, _julg("irrelevante", "average annual temperature", "o trecho fala de temperatura, não de metrô"))
    e = b["evidencias"][0]
    assert e["status"] == "nao_verificado" and "irrelevante" in e["motivo"] and "não de metrô" in e["motivo"]


def test_e19_real_sujeito_so_em_outra_parte_da_pagina(llm, juiz_real):
    """O caso da E19: artigo geral sobre infraestrutura (par.nsf.gov) que cita Yakutsk 1.200 caracteres
    adiante. O código recusa sem chamar a IA."""
    falso = llm({"evidencias": [{"afirmacao": "A maioria das edificações em Yakutsk é fundada em estacas no permafrost.",
                                 "citacoes": [{"fonte": "S1", "trecho": "Engineering solutions are able to mitigate the "
                                               "effects of degrading permafrost, but their economic cost is often high"}]}],
                 "lacunas": []})
    b = ev.montar_banco(TEMA_998, [NSF])
    e = b["evidencias"][0]
    assert e["status"] == "nao_verificado" and "cita yakutsk só em outra parte" in e["motivo"]
    assert len(falso.pedidos) == 1  # só a extração: o juiz não foi chamado
    assert ev.fonte_fala_de("yakutsk", NSF)  # a página fala de Yakutsk — e isso não basta


def test_e19_trecho_que_sustenta_so_parte(llm, juiz_real):
    b = _banco_de("Em Yakutsk, 10% das estruturas deformaram por causa do aquecimento global.",
                  "many buildings on permafrost had deformations, ranging from ~10% of structures in Yakutsk", NSF, llm,
                  _julg("parcial", "10% of structures in Yakutsk", "o trecho não diz que a causa é o aquecimento global"))
    e = b["evidencias"][0]
    assert e["status"] == "nao_verificado" and "parcial" in e["motivo"]


def test_e19_trecho_que_contradiz(llm, juiz_real):
    b = _banco_de("Em Yakutsk, nenhuma estrutura sobre permafrost teve deformação.",
                  "many buildings on permafrost had deformations, ranging from ~10% of structures in Yakutsk", NSF, llm,
                  _julg("contradiz", "had deformations", "o trecho diz que houve deformações em ~10% das estruturas"))
    assert b["evidencias"][0]["status"] == "nao_verificado" and "contradiz" in b["evidencias"][0]["motivo"]


def test_juiz_sem_trecho_chave_identificavel_e_perguntado_de_novo_e_sem_decisao_fica_fora(llm, juiz_real):
    b = _banco_de("Em Yakutsk, cerca de 10% das estruturas sobre permafrost tiveram deformações.",
                  "many buildings on permafrost had deformations, ranging from ~10% of structures in Yakutsk", NSF, llm,
                  _julg("sustenta", "texto que não está no trecho"),        # trecho-chave inventado
                  _julg("sustenta", "10% of structures", "ok"))             # justificativa vazia demais
    c = b["evidencias"][0]["citacoes"][0]
    assert b["evidencias"][0]["status"] == "nao_verificado" and c["sustentacao"]["decisao"] == "sem_julgamento"


def test_sinal_e_unidade_sao_conferidos_pelo_codigo():
    fonte = {"titulo": "Yakutsk", "url": "https://x.org/Yakutsk", "trecho": "In Yakutsk the record is 64.4 °C and piles go 15 km"}
    assert "outro sinal" in ev.sustentacao_por_codigo("O recorde de Yakutsk é −64,4 °C.", "the record is 64.4 °C", fonte, {"yakutsk"})
    assert ev.sustentacao_por_codigo("Em Yakutsk as estacas vão a 15 metros.", "piles go 15 km", fonte, {"yakutsk"})
    assert ev.sustentacao_por_codigo("O recorde de Yakutsk é 64,4 °C.", "the record is 64.4 °C", fonte, {"yakutsk"}) is None


def test_parcial_fica_so_com_a_parte_comprovada_depois_de_novo_julgamento(llm, juiz_real):
    """Não descartar o que é verdade: "capital de Sakha e porto no Lena" com trecho que só comprova o porto."""
    fonte = {"titulo": "Yakutsk", "url": "https://en.wikipedia.org/wiki/Yakutsk",
             "trecho": "Yakutsk is a major port on the Lena River, with a population of about 355,000."}
    parcial = _julg("parcial", "major port on the Lena River", "o trecho não diz que é a capital de Sakha")
    parcial["julgamentos"][0]["parte_sustentada"] = "Yakutsk é um grande porto no rio Lena."
    b = _banco_de("Yakutsk é a capital da República de Sakha e um grande porto no rio Lena.",
                  "Yakutsk is a major port on the Lena River", fonte, llm, parcial,
                  _julg("sustenta", "major port on the Lena River", "o trecho diz que Yakutsk é um grande porto no Lena"))
    e = b["evidencias"][0]
    assert e["status"] == "fonte_unica" and e["afirmacao"] == "Yakutsk é um grande porto no rio Lena."
    assert e["afirmacao_original"].startswith("Yakutsk é a capital")
    assert e["citacoes"][0]["sustentacao"]["reduzida_de"] == e["afirmacao_original"]


def test_parte_sustentada_que_nao_se_confirma_continua_fora(llm, juiz_real):
    fonte = {"titulo": "Yakutsk", "url": "https://en.wikipedia.org/wiki/Yakutsk",
             "trecho": "Yakutsk is a major port on the Lena River, with a population of about 355,000."}
    parcial = _julg("parcial", "major port on the Lena River", "o trecho não diz que é a capital de Sakha")
    parcial["julgamentos"][0]["parte_sustentada"] = "Yakutsk é o maior porto da Rússia."
    b = _banco_de("Yakutsk é a capital da República de Sakha e um grande porto no rio Lena.",
                  "Yakutsk is a major port on the Lena River", fonte, llm, parcial,
                  _julg("parcial", "major port on the Lena River", "o trecho não diz que é o maior da Rússia"))
    e = b["evidencias"][0]
    assert e["status"] == "nao_verificado" and "afirmacao_original" not in e


# ======================================================================= 20/11: auditor decide TODA frase
def _roteiro_grande(n_frases):
    frases = []
    for k in range(n_frases):
        frases.append(F("E aí vem a virada.", "narrativa") if k % 4 == 0 else
                      F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"]) if k % 4 == 1 else
                      F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", "explicativa", ["E3"]) if k % 4 == 2 else
                      F("E as estacas descem até 15 metros.", evid=["E2"]))
    return _roteiro([frases[k:k + 3] for k in range(0, len(frases), 3)])


def test_toda_frase_de_um_roteiro_grande_recebe_decisao_em_blocos_com_contexto_e_juiz_final(llm):
    s = _roteiro_grande(50)
    falso = llm(*[AUD(8)] * 10)
    problemas, q = rd.auditar_ia(s, BANCO, TEMA_988)
    # factual limpa; editorial travada: o roteiro de teste repete "15 metros" em várias cenas
    assert problemas == [] and q["nota"] == rd.NOTA_COM_TRAVA and not q["aprovado"]
    assert any("mesmo fato aparece em mais de uma cena" in t for t in q["travas"])
    assert [d["n"] for d in q["decisoes"]] == list(range(1, 51))           # nenhuma frase sem decisão
    assert all(d["justificativa"] for d in q["decisoes"])
    assert q["blocos"] == 5 and len(falso.pedidos) == 7          # 5 blocos + espectador + editor
    blocos = [p for p in falso.pedidos if "FRASES A JULGAR" in p]
    assert all(len(re.findall(r"^\d+\) \[", p.split("FRASES A JULGAR")[1].split("Dê uma decisão")[0], re.M)) <= 12
               for p in blocos)
    assert "CONTEXTO ANTES (não julgue)" in blocos[1] and "CONTEXTO DEPOIS (não julgue)" in blocos[0]
    for p in blocos:                                                          # as evidências citadas chegam
        julgar = p.split("FRASES A JULGAR")[1].split("Dê uma decisão")[0]
        for ident in set(re.findall(r"\[\w+; (E\d+)\]", julgar)):
            assert f"\n{ident} [" in p.split("FRASES A JULGAR")[0]
    assert "ESPECTADOR atento" in falso.pedidos[-2] and "Você é o EDITOR" in falso.pedidos[-1]


def test_frase_esquecida_pelo_auditor_e_perguntada_de_novo(llm):
    s = _roteiro_grande(6)
    falso = llm(AUD(8, omitir={4}), AUD(8))
    problemas, q = rd.auditar_ia(s, BANCO, TEMA_988)
    assert [d["n"] for d in q["decisoes"]] == [1, 2, 3, 4, 5, 6] and len(falso.pedidos) == 4  # + espectador + editor
    assert re.findall(r"^(\d+)\) \[", falso.pedidos[1].split("FRASES A JULGAR")[1], re.M) == ["4"]


def test_frase_que_continua_sem_decisao_reprova_a_auditoria(llm):
    s = _roteiro_grande(6)
    llm(AUD(8, omitir={4}), AUD(8, omitir={4}))
    assert rd.auditar_ia(s, BANCO, TEMA_988) is None
    llm(AUD(8, omitir={4}), AUD(8, omitir={4}), AUD(8, omitir={4}), AUD(8, omitir={4}))
    with pytest.raises(rd.RoteiroReprovado, match="auditor forte indisponível"):
        rd.auditar_e_corrigir(s, BANCO, TEMA_988)


def test_decisao_sem_justificativa_ou_desconhecida_nao_conta(llm):
    s = _roteiro_grande(4)
    llm(AUD(8, especiais={2: ("sustentada", "ok"), 3: ("talvez", "não sei se a evidência sustenta")}), AUD(8))
    _, q = rd.auditar_ia(s, BANCO, TEMA_988)
    assert [d["n"] for d in q["decisoes"]] == [1, 2, 3, 4]  # 2 e 3 foram perguntadas de novo


def test_factual_aprovada_como_narrativa_volta_como_sem_suporte(llm):
    s = _roteiro([[F("As estacas descem até 15 metros.", evid=["E2"])]])
    llm(AUD(8, especiais={1: ("narrativa_ok", "parece só uma transição da história")}))
    problemas, _ = rd.auditar_ia(s, BANCO, TEMA_988)
    assert [p["tipo"] for p in problemas] == ["sem_suporte"]


def test_parcial_vira_problema_com_a_justificativa_do_auditor(llm):
    s = _roteiro([[F("As estacas de 15 metros existem desde 1950.", evid=["E2"])]])
    llm(AUD(8, especiais={1: ("parcial", "a evidência traz os 15 metros, não o ano de 1950")}))
    problemas, _ = rd.auditar_ia(s, BANCO, TEMA_988)
    assert problemas[0]["tipo"] == "excede" and "1950" in problemas[0]["motivo"]


def test_blocos_cabem_no_groq_sem_cortar_evidencia_nem_resposta():
    from src import providers as pv
    s = _roteiro_grande(54)
    tamanhos = []
    original = pv.complete
    pv.complete = lambda messages, **kw: tamanhos.append(pv._too_big_for("groq", messages, kw["max_tokens"])) or \
        json.dumps(AUD(8)(messages[-1]["content"]))
    try:
        rd.auditar_ia(s, BANCO, TEMA_988)
    finally:
        pv.complete = original
    assert tamanhos and not any(tamanhos)


# ======================================================================= 20/11: leitura de espectador (roteiro inteiro)
def _roteiro_trajeto():
    """Duas frases verdadeiras que, juntas, sugerem algo falso (padrão genérico: o tempo de um trecho dito
    junto do ponto de partida de outro trajeto)."""
    banco = {**BANCO, "evidencias": [
        _ev("E1", "O trecho sob a montanha é atravessado em 20 minutos.", "fonte_unica"),
        _ev("E2", "Os trens partem da estação central da capital.", "fonte_unica")]}
    s = _roteiro([[F("E a viagem impressiona.", "narrativa")],
                  [F("O trecho sob a montanha é atravessado em 20 minutos.", evid=["E1"])],
                  [F("Os trens partem da estação central da capital.", evid=["E2"])]])
    return s, banco


ENGANO = {"frases": [2, 3], "tipo": "combinacao", "o_publico_conclui": "a viagem desde a capital leva 20 minutos",
          "por_que_engana": "os 20 minutos são só do trecho sob a montanha", "como_corrigir": "diga que é o tempo do trecho"}


def test_frases_certas_isoladas_mas_combinacao_enganosa_e_pega_na_leitura_inteira(llm):
    s, banco = _roteiro_trajeto()
    llm(AUD(9, enganos=[ENGANO]))
    probs, q = rd.auditar_ia(s, banco, TEMA_988)
    assert all(d["problema"] is None for d in q["decisoes"])                # cada frase, sozinha, está certa
    assert [(p["tipo"], p["cena"]) for p in probs] == [("leitura_enganosa", 2)]  # a que fecha a conclusão errada
    m = probs[0]["motivo"]
    assert "combinacao" in m and "Os trens" not in m and "trecho sob a montanha" in m and "20 minutos" in m
    assert q["factual"]["enganos"] and q["factual"]["nota"] < 10 and q["factual"]["conclusoes_do_publico"]


def test_leitura_sem_engano_nao_inventa_problema(llm):
    s, banco = _roteiro_trajeto()
    llm(AUD(9))
    probs, q = rd.auditar_ia(s, banco, TEMA_988)
    assert probs == [] and q["factual"]["nota"] == 10


@pytest.mark.parametrize("ruim", [
    {**ENGANO, "tipo": "inventado"}, {**ENGANO, "frases": [9]}, {**ENGANO, "por_que_engana": "não"}])
def test_engano_malformado_nao_conta(llm, ruim):
    s, banco = _roteiro_trajeto()
    llm(AUD(9, enganos=[ruim]))
    assert rd.auditar_ia(s, banco, TEMA_988)[0] == []


def test_sem_leitura_valida_a_auditoria_nao_sai(monkeypatch):
    s, banco = _roteiro_trajeto()
    from src import providers

    def falso(messages, **kw):
        p = messages[-1]["content"]
        if "ESPECTADOR atento" in p:
            raise RuntimeError("Nenhum provedor devolveu resposta válida")
        return json.dumps(AUD(9)(p))
    monkeypatch.setattr(providers, "complete", falso)
    assert rd.auditar_ia(s, banco, TEMA_988) is None


def test_prompt_do_espectador_cobre_os_sete_tipos():
    for tipo in rd.TIPOS_ENGANO:
        assert f'"{tipo}"' in rd._PROMPT_ESPECTADOR
    assert "não estão escritas" in rd._PROMPT_ESPECTADOR


# ======================================================================= 20/11: editorial separada da factual
def _cenas(*textos, tipo="factual"):
    return {"title": "Como funciona", "scenes": [{"narration": t, "frases": [F(t, tipo, ["E1"])]} for t in textos]}


def test_mesmo_fato_em_duas_cenas_e_trava_editorial():
    s = _cenas("Em 1990 os túneis se encontraram a 40 metros.", "Depois veio a obra.", "O encontro de 1990 foi a 40 metros.")
    travas = rd.travas_editoriais(s)
    assert travas and "1990" in travas[0] and "40 m" in travas[0]


def test_linha_do_tempo_e_trava_editorial_e_processo_nao():
    datas = _cenas("Em 1802 surgiu a ideia.", "Em 1882 uma máquina cavou.", "Em 1883 parou.", "Em 1922 voltou.",
                   "Em 1973 houve um teste.", "Em 1988 começou a obra.")
    processo = _cenas("Onze máquinas cavaram dos dois lados ao mesmo tempo.", "Cada máquina revestia o túnel com anéis.",
                      "A camada de giz escolhida não deixava a água entrar.", "As equipes se encontraram no meio em 1990.")
    assert any("linha do tempo" in t for t in rd.travas_editoriais(datas))
    assert rd.travas_editoriais(processo) == []


def test_nota_editorial_e_calculada_pelos_criterios_e_trava_derruba(llm):
    s = _cenas("Onze máquinas cavaram dos dois lados.", "A camada de giz não deixava a água entrar.")
    llm(AUD(9))
    r = rd.avaliar_editorial(s, "tema")
    assert r["nota"] == 9 and r["aprovado"] and r["travas"] == []
    from src import providers
    crit = {k: 9 for k in rd.CRITERIOS_EDITORIAIS}
    crit["titulo"] = 3
    providers.complete = lambda m, **kw: json.dumps({"criterios": crit, "problemas": [
        {"criterio": "titulo", "cenas": [1, 2], "descricao": "promete o como e entrega datas"}]})
    r = rd.avaliar_editorial(s, "tema")
    assert r["nota"] == rd.NOTA_COM_TRAVA and not r["aprovado"] and "titulo com nota 3" in r["travas"]
    assert any("promete o como" in x for x in r["problemas"])


def test_nota_7_sem_trava_aprova_e_com_trava_nao(monkeypatch):
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([], {"nota": 7.2, "problemas": [], "travas": [], "aprovado": True}))
    rd.auditar_e_corrigir(_roteiro([[F("Mas por quê?", "narrativa")]]), BANCO, TEMA_988)
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([], {"nota": 6.5, "problemas": ["linha do tempo"],
                                                                 "travas": ["linha do tempo"], "aprovado": False}))
    with pytest.raises(rd.RoteiroReprovado, match="trava editorial"):
        rd.auditar_e_corrigir(_roteiro([[F("Mas por quê?", "narrativa")]]), BANCO, TEMA_988)


def test_trava_editorial_pede_edicao_ate_duas_vezes(monkeypatch):
    avaliacoes = iter([(6.5, ["linha do tempo"]), (6.5, ["linha do tempo"]), (8.5, [])])
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: (lambda n, tr: ([], {
        "nota": n, "problemas": tr, "travas": tr, "aprovado": not tr,
        "alvos_travas": [{"criterio": "progressao", "cenas": [1], "descricao": "linha do tempo"}] if tr else []}))(
        *next(avaliacoes)))
    edicoes = []

    def editar(s, alvos, b, t, **k):
        edicoes.append(alvos)
        return _roteiro([[F("Agora sim, " + ("de novo." if len(edicoes) == 1 else "outra vez."), "narrativa")]]), \
            {"cenas_editadas": [1], "titulo": None}
    monkeypatch.setattr(rd, "editar_cenas", editar)
    final, rel = rd.auditar_e_corrigir(_roteiro([[F("Mas por quê?", "narrativa")]]), BANCO, TEMA_988)
    assert len(edicoes) == 2 and rel["edicoes"] == 2 and rel["qualidade"]["nota"] == 8.5
    assert edicoes[0] == {0: ["HISTÓRIA — progressao: linha do tempo"]}


# ======================================================================= 20/11: aprofundar as lacunas no artigo completo
def test_lacuna_e_procurada_no_artigo_completo_e_vira_evidencia(llm, monkeypatch):
    from src import web_search
    artigo = ("The tunnel opened in 1994. " * 20 + "Working from both sides, eleven tunnel boring machines cut through "
              "chalk marl to build the tunnels. Laser guidance kept the machines aligned so the two halves met in the middle. "
              + "Ticket prices changed in 2010. " * 20)
    monkeypatch.setattr(web_search, "_wiki_article", lambda lang, t: (artigo, "u", None))
    fonte = {"titulo": "Wikipedia (en): Tunnel", "url": "https://en.wikipedia.org/wiki/Tunnel", "trecho": "The tunnel opened in 1994."}
    banco = {"versao": 2, "tema": "Como o túnel foi escavado?", "fontes": [], "evidencias": [], "nao_lidos": [],
             "lacunas": ["Quantas máquinas escavaram e como o alinhamento foi garantido?"]}
    llm({"chaves": {"en": ["tunnel boring machines", "laser guidance", "aligned", "chalk marl"]}},
        {"evidencias": [{"afirmacao": "Onze tuneladoras escavaram o túnel a partir dos dois lados.",
                         "citacoes": [{"fonte": "S2", "trecho": "Working from both sides, eleven tunnel boring machines cut "
                                                                "through chalk marl to build the tunnels"}]}], "lacunas": []})
    web = [fonte]
    n = ev.aprofundar_lacunas(banco, banco["tema"], web)
    assert n == 1 and banco["evidencias"][0]["status"] == "fonte_unica" and banco["lacunas"] == []
    assert web[1]["url"].endswith("#lacunas") and "Ticket prices" not in web[1]["trecho"]  # sem bônus de número


@pytest.mark.parametrize("trecho,hip", [
    ("In 1802, a French engineer proposed a tunnel under the Channel with oil lamps", False),   # proposta: fato
    ("Some researchers have proposed that an outburst could explain the bubbles", True),         # hipótese
    ("It has been suggested that the enzyme may contribute to the effect", True),
    ("The report suggested a new route for the railway", False),
])
def test_proposta_historica_nao_e_hipotese(trecho, hip):
    assert bool(ev._HIPOTESE.search(trecho)) is hip


def test_travas_editoriais_voltam_ao_escritor_e_a_edicao_recebe_regras():
    s = _cenas("Em 1802 surgiu a ideia.", "Em 1882 uma máquina cavou.", "Em 1883 parou.", "Em 1922 voltou.",
               "Em 1973 houve um teste.", "Em 1988 começou a obra.")
    _, lista, _ = rd.defeitos(s, BANCO, TEMA_988, 1, 999, 1, 99)
    assert any("linha do tempo" in x for x in lista)
    alvos = rd.cenas_das_travas(s)
    assert alvos and alvos[0]["cenas"] == [1, 2, 3, 4, 5, 6] and "no máximo 3 anos" in alvos[0]["descricao"]


# ======================================================================= 20/11: o mesmo fato em outra forma
BANCO_FORMA = {**BANCO, "evidencias": [
    _ev("E1", "Entre 1882 e 1883, a máquina britânica perfurou 1.840 metros.", "fonte_unica",
        trecho="Between 1882 and 1883 the British machine bored 1,840 metres"),
    _ev("E2", "Dez trabalhadores morreram na obra.", "fonte_unica", trecho="Ten workers died during construction")]}


@pytest.mark.parametrize("texto,evid,passa", [
    ("A máquina britânica perfurou 1,84 km.", ["E1"], True),           # metro → quilômetro, como na fala
    ("A máquina britânica perfurou 1.840 metros.", ["E1"], True),
    ("A máquina britânica perfurou 2,5 km.", ["E1"], False),           # outro número
    ("Nos anos 1880, a máquina britânica cavou.", ["E1"], True),       # década que contém o ano
    ("Na década de 1950, a máquina britânica cavou.", ["E1"], False),  # década errada
    ("Dez trabalhadores morreram na obra.", ["E2"], True),             # número por extenso na evidência
    ("Doze trabalhadores morreram na obra.", ["E2"], False),
])
def test_mesmo_fato_em_outra_forma_passa_e_numero_novo_nao(texto, evid, passa):
    _, probs = rd.auditar_codigo(_uma(texto, evid), BANCO_FORMA, TEMA_988)
    assert (not probs) is passa, probs


def test_decada_com_dois_digitos():
    banco = {**BANCO, "evidencias": [_ev("E1", "Os levantamentos de 1964 e 1965 usaram 40 furos de sondagem.", "fonte_unica")]}
    assert rd.auditar_codigo(_uma("Nos anos 60, sondagens mapearam a rocha.", ["E1"]), banco, TEMA_988)[1] == []
    assert rd.auditar_codigo(_uma("Nos anos 70, sondagens mapearam a rocha.", ["E1"]), banco, TEMA_988)[1]


def test_fatos_e_historia_vao_juntos_para_a_edicao_por_cena(monkeypatch):
    """21/11: uma correção só. v1 com trava → edição; v2 sem trava mas com frase excedendo a evidência → a 2ª
    edição recebe a reprovação do auditor na cena dela; v3 limpa e aprovada. Nada de conserto frase a frase."""
    rodadas = iter([([], 6.5, ["trava"]), (["excede"], 8.5, []), ([], 8.5, [])])

    def auditar(s, b, t, *_):
        probs, nota, tr = next(rodadas)
        return ([{"tipo": p, "termo": "Editada uma vez agora.", "motivo": "a evidência não diz isso", "cena": 0, "frase": 0}
                 for p in probs],
                {"nota": nota, "problemas": tr, "travas": tr, "aprovado": not tr,
                 "alvos_travas": [{"criterio": "x", "cenas": [1], "descricao": "trava"}] if tr else []})
    monkeypatch.setattr(rd, "auditar_ia", auditar)
    assert not hasattr(rd, "consertar")
    n = iter(["uma vez agora.", "duas vezes agora."])
    pedidos = []

    def editar(s, a, b, t, **k):
        pedidos.append((a, k.get("problemas")))
        return _roteiro([[F(f"Editada {next(n)}", "narrativa")]]), {"cenas_editadas": [1], "titulo": None}
    monkeypatch.setattr(rd, "editar_cenas", editar)
    final, rel = rd.auditar_e_corrigir(_roteiro([[F("Mas por quê?", "narrativa")]]), BANCO, TEMA_988)
    assert rel["edicoes"] == 2 and rel["qualidade"]["nota"] == 8.5 and rel["versao_final"] == 3
    assert [v["origem"] for v in rel["versoes"]] == ["escrita", "edicao", "edicao"]
    assert pedidos[0][0] == {0: ["HISTÓRIA — x: trava"]}
    assert pedidos[1][0][0][0].startswith("FACTUAL — diz mais do que a evidência na frase \"Editada uma vez agora.\"")
    assert pedidos[1][1][0]["tipo"] == "excede"


def test_ordinal_nao_e_grau():
    """"1º de dezembro" é data, não 1 °C (a trava de repetição via "1 °C" em duas cenas)."""
    assert ft.temperaturas("Em 1º de dezembro de 1990 os túneis se encontraram.") == set()
    assert ft.temperaturas("A média é de 8 ºC.") == {("8", "°C")}
    assert ft.temperaturas("Fazia −8° naquela manhã.") == {("-8", "°C")}


# ======================================================================= 20/11: edição por cena, versões e custo
def _quatro_cenas():
    return _roteiro([[F("Yakutsk é uma das duas grandes cidades sobre permafrost contínuo.", evid=["E1"])],
                     [F("Os prédios ficam sobre estacas para não aquecer o solo congelado.", evid=["E3"])],
                     [F("E as estacas descem até 15 metros para os prédios não afundarem.", evid=["E2"])],
                     [F("No fim, é o frio que segura a cidade.", "narrativa")]])


def test_edicao_por_cena_devolve_so_a_cena_apontada_e_preserva_as_outras(llm):
    s = _quatro_cenas()
    falso = llm({"cenas": [
        {"cena": 2, "funcao": "explica por que estacas", "preserva": "o motivo", "corrige": "liga com a cena 3",
         "frases": [{"texto": "Por isso os prédios ficam sobre estacas, para não aquecer o solo congelado.",
                     "tipo": "factual", "evidencias": ["E3"]}]},
        {"cena": 4, "frases": [{"texto": "Uma conclusão nova que ninguém pediu.", "tipo": "narrativa"}]}]})
    novo, reg = rd.editar_cenas(s, {1: ["progressao: a cena 2 não se liga à 3"]}, BANCO, TEMA_988)
    pedido = falso.pedidos[0]
    assert ">> CENA 2" in pedido and "   CENA 1" in pedido and "No fim, é o frio que segura a cidade." in pedido
    assert "CENA 2: progressao: a cena 2 não se liga à 3" in pedido
    assert "pile foundations" not in pedido or "E2" in pedido          # evidência da vizinha (cena 3) com trecho
    assert "E3 [" in pedido
    assert reg["cenas_editadas"] == [2] and reg["recusadas"] == [{"cena": 4, "motivo": "cena não marcada sem justificativa"}]
    assert novo["scenes"][1]["narration"].startswith("Por isso")
    for k in (0, 2, 3):
        assert novo["scenes"][k] == s["scenes"][k]                     # intocadas, exatamente
    assert reg["detalhes"][0]["funcao"] == "explica por que estacas" and reg["detalhes"][0]["antes"]


def test_cena_fora_dos_alvos_so_entra_com_justificativa(llm):
    s = _quatro_cenas()
    llm({"cenas": [
        {"cena": 3, "frases": [{"texto": "As estacas descem até 15 metros para os prédios não afundarem.",
                                "tipo": "factual", "evidencias": ["E2"]}]},
        {"cena": 4, "justificativa": "sem o 'E' da cena 3 a conclusão precisa retomar o fio",
         "frases": [{"texto": "É o frio, bem guardado, que segura a cidade.", "tipo": "narrativa"}]}]})
    novo, reg = rd.editar_cenas(s, {2: ["linguagem: o 'E' inicial solto"]}, BANCO, TEMA_988)
    assert reg["cenas_editadas"] == [3, 4] and reg["extras"][0]["cena"] == 4
    assert novo["scenes"][0] == s["scenes"][0] and novo["scenes"][1] == s["scenes"][1]


def test_cena_editada_que_inventa_numero_volta_a_original(llm):
    s = _quatro_cenas()
    llm({"cenas": [{"cena": 3, "frases": [{"texto": "As estacas descem até 40 metros.", "tipo": "factual",
                                           "evidencias": ["E2"]}]}]})
    novo, reg = rd.editar_cenas(s, {2: ["novidade: repete"]}, BANCO, TEMA_988)
    assert reg["cenas_editadas"] == [] and reg["revertidas"][0]["cena"] == 3
    assert novo["scenes"] == s["scenes"]


def _julgadas(pedido):
    bloco = pedido.split("FRASES A JULGAR", 1)[1].split("Dê uma decisão", 1)[0]
    return [int(m) for m in re.findall(r"^(\d+)\) \[", bloco, re.M)]


def test_depois_da_edicao_so_as_frases_que_mudaram_voltam_ao_auditor(llm):
    s = _roteiro([[F(f"Narração de ligação número {k} da história.", "narrativa")] for k in range(1, 7)])
    previas = {}
    falso = llm(AUD(8))
    assert rd.auditar_ia(s, BANCO, TEMA_988, previas) is not None
    primeira = [p for p in falso.pedidos if "FRASES A JULGAR" in p]
    assert sorted(n for p in primeira for n in _julgadas(p)) == [1, 2, 3, 4, 5, 6]
    s["scenes"][5] = {"narration": "Um fim diferente.", "frases": [F("Um fim diferente.", "narrativa")]}
    falso.respostas.append(AUD(8))
    falso.pedidos.clear()
    _, q = rd.auditar_ia(s, BANCO, TEMA_988, previas)
    segunda = [p for p in falso.pedidos if "FRASES A JULGAR" in p]
    assert sorted(n for p in segunda for n in _julgadas(p)) == [5, 6]   # a que mudou e a vizinha dela
    assert any("ESPECTADOR atento" in p for p in falso.pedidos)         # a leitura do roteiro inteiro roda de novo
    assert any("Você é o EDITOR" in p for p in falso.pedidos)           # e a editorial também
    assert q["frases_reaproveitadas"] == 4 and len(q["decisoes"]) == 6


def test_banco_diferente_faz_toda_frase_voltar_ao_auditor(llm):
    s = _roteiro([[F(f"Narração de ligação número {k}.", "narrativa")] for k in range(1, 4)])
    previas = {}
    falso = llm(AUD(8), AUD(8))
    rd.auditar_ia(s, BANCO, TEMA_988, previas)
    outro = {**BANCO, "evidencias": BANCO["evidencias"] + [_ev("E9", "Fato novo encontrado.")]}
    falso.pedidos.clear()
    rd.auditar_ia(s, outro, TEMA_988, previas)
    assert sorted(n for p in falso.pedidos if "FRASES A JULGAR" in p for n in _julgadas(p)) == [1, 2, 3]


def test_versao_auditada_com_outro_banco_e_auditada_de_novo_antes_de_aprovar(monkeypatch):
    """v1 aprovada (7.5); a edição traz problema factual; a busca de evidência muda o banco. Volta a v1 —
    mas a aprovação dela era para o banco antigo: auditoria completa de novo, sem herdar nada."""
    banco = json.loads(json.dumps(BANCO))
    chamadas = []

    def auditar(s, b, t, *_):
        chamadas.append(rd._hash_banco(b))
        if len(chamadas) == 1 or len(chamadas) == 4:
            return [], {"nota": 7.5, "problemas": [], "aprovado": True, "alvos": [{"criterio": "x", "cenas": [1], "descricao": "y"}]}
        return ([{"tipo": "sem_suporte", "termo": "x", "motivo": "m", "cena": 0, "frase": 0}], {"nota": 8, "problemas": []})
    monkeypatch.setattr(rd, "auditar_ia", auditar)
    monkeypatch.setattr(rd, "editar_cenas", lambda s, a, b, t, **k: (_roteiro([[F("Editada.", "narrativa")]]),
                                                                      {"cenas_editadas": [1], "titulo": None}))
    monkeypatch.setattr(rd, "adicionar_fontes", lambda b, t, novas, web: b["evidencias"].append(_ev("E9", "Nova.")))
    final, rel = rd.auditar_e_corrigir(_roteiro([[F("Original.", "narrativa")]]), banco, TEMA_988,
                                       web_facts=[], buscar_evidencia=lambda p, t: [{"x": 1}])
    assert len(chamadas) == 4 and chamadas[0] != chamadas[3]               # a v1 foi auditada de novo, com o banco novo
    assert final["scenes"][0]["narration"] == "Original." and rel["versoes"][-1]["reauditada"]


def test_alvos_da_edicao_comecam_pelas_travas_e_tem_limite():
    s = _roteiro([[F(f"Cena {k}.", "narrativa")] for k in range(1, 9)])
    q = {"criterios": {"novidade": 6, "conclusao": 3, "linguagem": 7},
         "alvos_travas": [{"criterio": "sem_repeticao", "cenas": [2, 5], "descricao": "1990 repetido"}],
         "alvos": [{"criterio": "linguagem", "cenas": [7], "descricao": "frase dura"},
                   {"criterio": "novidade", "cenas": [3, 4], "descricao": "nada novo"}]}
    a = rd.alvos_da_edicao(s, q)
    assert list(a)[:2] == [1, 4]                                   # travas primeiro
    assert 7 in a and "HISTÓRIA — conclusao: conclusao com nota 3" in a[7]  # essencial baixo sem cena → a última
    assert len(a) <= 4 and 6 not in a                              # metade do roteiro: a linguagem (7) ficou de fora


def test_alvos_da_edicao_levam_toda_reprovacao_factual_mesmo_acima_do_limite():
    s = _roteiro([[F(f"Cena {k}.", "narrativa")] for k in range(1, 9)])
    probs = [{"tipo": "sem_suporte", "termo": f"Cena {k}.", "motivo": "nenhuma evidência", "cena": k - 1, "frase": 0}
             for k in (1, 3, 5, 6, 8)]
    q = {"nota": 6, "criterios": {"novidade": 5}, "alvos": [{"criterio": "novidade", "cenas": [2, 4], "descricao": "nada novo"}]}
    a = rd.alvos_da_edicao(s, q, probs)
    assert [0, 2, 4, 5, 7] == list(a)[:5]                          # as 5 reprovações, acima do limite de 4 cenas
    assert all(a[i][0].startswith("FACTUAL") for i in (0, 2, 4, 5, 7)) and 1 not in a
    # história no alvo: a edição só corrige os fatos
    assert list(rd.alvos_da_edicao(s, {"nota": 8.5, "aprovado": True, "alvos": q["alvos"]}, probs[:1])) == [0]


def test_custo_registra_chamadas_modelo_e_tempo_por_etapa(monkeypatch):
    from src import metricas, providers as pv

    def cascata(*a, **k):
        pv._ULTIMO_MODELO[0] = "groq/openai/gpt-oss-120b"
        return "ok"
    monkeypatch.setattr(pv, "_complete_cascata", cascata)
    metricas.zerar()
    with metricas.etapa("auditoria_factual"):
        pv.complete([{"role": "user", "content": "x"}])
        pv.complete([{"role": "user", "content": "y"}])
    with metricas.etapa("edicao_cenas"):
        pv.complete([{"role": "user", "content": "z"}])
    metricas.somar("cenas_modificadas", 2)
    r = metricas.resumo()
    assert r["total_chamadas"] == 3 and r["cenas_modificadas"] == 2
    assert r["etapas"]["auditoria_factual"]["chamadas"] == 2
    assert r["etapas"]["edicao_cenas"]["modelos"] == {"groq/openai/gpt-oss-120b": 1}
    assert "auditoria_factual: 2 chamada(s)" in metricas.texto()


def test_estimativa_nao_entra_dentro_de_nome_com_numero():
    """21/11 (JWST): "em torno de L2" virou "em torno de Lcerca de 2" — número colado a letra é nome."""
    t = "O telescópio entrou na órbita em torno de L2 após 29 dias."
    assert ft.estima_antes(t, {"2", "29"}) == "O telescópio entrou na órbita em torno de L2 após cerca de 29 dias."
    assert ft.estima_antes("O A380 levou 50 pessoas.", {"380"}) == "O A380 levou 50 pessoas."
