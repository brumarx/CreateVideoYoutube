"""22/11 — história antes da extração e segunda passagem do banco (diagnóstico + busca dirigida): a passagem mede o
banco contra as perguntas da HISTÓRIA, lê só frases sem uso que falam das perguntas abertas, e as evidências novas
passam pelas MESMAS conferências da extração. Teto de evidências proporcional ao material. Nenhuma chamada real."""
import json

import pytest

from src import evidencias as ev
from src import metricas, providers

TEMA = "Como o telescópio James Webb se desdobrou no espaço depois do lançamento?"

# frases guardadas da pesquisa de 20/11 (S13 = NASA "Webb Observatory", S14 = NASA Ciencia)
S13 = ("Webb's tennis court-sized sunshield protects it from stray heat and light from the Sun, while its large segmented "
       "mirror — 18 segments covering 6.5 meters (21.3 feet) at its widest point — enables it to effectively capture "
       "infrared light. Launching such a large mirror into space was another huge challenge. An origami-inspired folding "
       "telescope. Both the sunshield and the mirrors were designed to unfold after the telescope separates from its "
       "launch vehicle. Optical Telescope Element (OTE) The OTE gathers the light coming from space and provides it to the "
       "science instruments located in the ISIM.")
S14 = ("Webb es el principal observatorio de la próxima década, al servicio de miles de astrónomos de todo el mundo. "
       "Begoña Vila, astrofísica de la NASA e ingeniera de sistemas de instrumentos de los telescopios espaciales Nancy "
       "Grace Roman y James Webb, te explica más. Recibe en tu buzón de correo electrónico nuestro boletín semanal.")
FONTES_WEB = [{"titulo": "Webb Observatory - NASA Science", "url": "https://science.nasa.gov/mission/webb/spacecraftoverview/",
               "trecho": S13},
              {"titulo": "Telescopio espacial James Webb - NASA Ciencia", "url": "https://ciencia.nasa.gov/mission/webb/",
               "trecho": S14}]
FONTES = ev._fontes_do_banco(FONTES_WEB)
SOMBRA = "Both the sunshield and the mirrors were designed to unfold after the telescope separates from its launch vehicle."
JA_NO_BANCO = [{"afirmacao": "O protetor solar e os espelhos foram projetados para se desdobrar depois que o telescópio se "
                "separa do foguete.", "numeros": [], "citacoes": [{"fonte": "S1", "trecho": SOMBRA, "verificado": True}]}]
HISTORIA = {"historia": "Como o Webb saiu dobrado do foguete e se abriu sozinho no espaço.", "angulo": "",
            "perguntas": [{"id": "P1", "pergunta": "Por que o telescópio precisou ir dobrado?"},
                          {"id": "P2", "pergunta": "Que partes se desdobraram depois da separação do foguete?"},
                          {"id": "P3", "pergunta": "Por que o espelho grande foi um desafio de lançar?"}],
            "fora_do_escopo": ["descobertas científicas", "equipe e entrevistas"]}

ESPELHO = {"afirmacao": "Lançar um espelho tão grande ao espaço foi outro enorme desafio.",
           "citacoes": [{"fonte": "S1", "trecho": "Launching such a large mirror into space was another huge challenge."}]}
OTE = {"afirmacao": "O Elemento de Telescópio Óptico (OTE) reúne a luz do espaço e a entrega aos instrumentos científicos no ISIM.",
       "citacoes": [{"fonte": "S1", "trecho": "The OTE gathers the light coming from space and provides it to the science "
                                              "instruments located in the ISIM."}]}


def _diag(*abertas, suficiente=False):
    perguntas = [{"id": p["id"], "situacao": "respondida", "evidencias": ["E1"], "falta": "", "chaves": []}
                 for p in HISTORIA["perguntas"] if p["id"] not in {a[0] for a in abertas}]
    perguntas += [{"id": i, "situacao": "falta", "evidencias": [], "falta": falta, "chaves": chaves} for i, falta, chaves in abertas]
    return {"suficiente": suficiente, "perguntas": perguntas}


def _dirigida(*respostas):
    return {"respostas": [{"id": i, "evidencias": evs} for i, evs in respostas]}


@pytest.fixture(autouse=True)
def sem_cache(monkeypatch):
    from src import disk_cache
    monkeypatch.setattr(disk_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(disk_cache, "put", lambda *a, **k: None)
    metricas.zerar()


@pytest.fixture
def ia(monkeypatch):
    pedidos = []

    def instalar(*respostas):
        fila = list(respostas)

        def chamar(prompt, max_tokens=ev.RESPOSTA_EXTRACAO, validar=None):
            pedidos.append(prompt)
            if not fila:
                return None
            r = fila.pop(0)
            if r is not None and validar and not validar(json.dumps(r)):
                return None
            return r
        monkeypatch.setattr(ev, "_chamar", chamar)
        return pedidos
    return instalar


# ---------------------------------------------------------------- história antes da extração
def test_historia_valida_vira_perguntas_numeradas_e_entra_na_extracao(ia, monkeypatch):
    pedidos = ia({"historia": HISTORIA["historia"], "perguntas": [{"pergunta": p["pergunta"]} for p in HISTORIA["perguntas"]],
                  "fora_do_escopo": ["descobertas científicas"]}, {"evidencias": [], "lacunas": []})
    monkeypatch.setattr(ev, "_recuperar_por_llm", lambda *a: None)
    h = ev.planejar_historia(TEMA)
    assert [p["id"] for p in h["perguntas"]] == ["P1", "P2", "P3"] and h["fora_do_escopo"] == ["descobertas científicas"]
    assert "Não use fatos" in pedidos[0] and "sem números, nomes ou afirmações" in pedidos[0]
    ev._extrair(TEMA, FONTES_WEB, FONTES, h)
    assert "HISTÓRIA que o vídeo vai contar: Como o Webb saiu dobrado" in pedidos[1]
    assert "P3) Por que o espelho grande" in pedidos[1] and "Fora do escopo desta história: descobertas" in pedidos[1]


def test_historia_invalida_ou_ausente_nao_inventa_plano(ia):
    ia({"historia": "x", "perguntas": [{"pergunta": "só uma"}]})
    assert ev.planejar_historia(TEMA) is None
    ia(None)
    assert ev.planejar_historia(TEMA) is None


def test_angulo_pedido_vai_no_pedido_da_historia(ia):
    pedidos = ia(None)
    ev.planejar_historia(TEMA, angulo="as pessoas por trás do desdobramento")
    assert "Ângulo pedido: as pessoas por trás do desdobramento" in pedidos[0]


# ---------------------------------------------------------------- teto proporcional, não quota
def test_teto_cresce_com_o_volume_e_com_as_fontes_e_tem_limites():
    assert ev.limite_de_evidencias(9000, 8) == 20
    assert ev.limite_de_evidencias(2000, 1) == 5
    assert ev.limite_de_evidencias(1200, 6) == 6           # muitas fontes curtas: uma vaga por fonte
    assert ev.limite_de_evidencias(300, 1) == 3            # piso
    assert ev.limite_de_evidencias(300, 1, minimo=1) == 1
    assert ev.limite_de_evidencias(40000, 30) == ev.MAX_POR_CHAMADA


def test_extracao_inicial_recebe_teto_proporcional_ao_lote(monkeypatch):
    pedidos = []
    monkeypatch.setattr(ev, "_chamar", lambda p, max_tokens=0, validar=None: pedidos.append(p) or {"evidencias": [], "lacunas": []})
    monkeypatch.setattr(ev, "_recuperar_por_llm", lambda *a: None)
    ev._extrair(TEMA, FONTES_WEB, FONTES)
    maximo = ev.limite_de_evidencias(len(S13) + len(S14), 2)
    assert f"até {maximo} (o teto cresce" in pedidos[0] and "Entre 10 e 20" not in pedidos[0]
    assert "é teto, não meta" in pedidos[0] and "não crie evidência fraca" in pedidos[0]


# ---------------------------------------------------------------- frases sem uso
def test_frases_sem_uso_excluem_as_citadas():
    sobra = ev.trechos_nao_cobertos(JA_NO_BANCO, FONTES_WEB)
    assert SOMBRA not in [f for _, f in sobra[0]]
    assert any("huge challenge" in f for _, f in sobra[0]) and len(sobra[1]) == 3


def test_frases_juntadas_marcam_os_pulos():
    assert ev._juntar([(1, "B."), (3, "D.")], 5) == "[…] B. […] D. […]"
    assert ev._juntar([(0, "A."), (1, "B.")], 2) == "A. B."


# ---------------------------------------------------------------- diagnóstico
def test_banco_suficiente_encerra_no_diagnostico_sem_outra_chamada(ia):
    pedidos = ia(_diag(suficiente=True))
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert novas == [] and rel["decisao"] == "suficiente" and rel["suficiente"] is True and len(pedidos) == 1
    assert "BANCO DE EVIDÊNCIAS (id: afirmação):\nE1: O protetor solar" in pedidos[0]
    assert "Launching such a large mirror" not in pedidos[0]               # o diagnóstico não leva texto de fonte
    assert "curiosidade, pessoa ou assunto que\na história não pede" in pedidos[0]


def test_respondida_sem_evidencia_que_exista_nao_conta_como_respondida(ia):
    diag = {"suficiente": True, "perguntas": [{"id": p["id"], "situacao": "respondida", "evidencias": ["E99"]}
                                              for p in HISTORIA["perguntas"]]}
    ia(diag, _dirigida())
    _, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert rel["suficiente"] is False and {p["situacao"] for p in rel["perguntas"]} == {"nao_avaliada"}


def test_sem_historia_ou_sem_diagnostico_nada_entra_e_nao_e_dado_como_suficiente(ia):
    novas, rel = ev.cobrir_lacunas(TEMA, None, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert novas == [] and rel["decisao"] == "sem_historia" and rel["suficiente"] is None
    ia({"evidencias": []})                                              # JSON sem "perguntas": inválido
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert novas == [] and rel["decisao"] == "sem_resposta" and rel["suficiente"] is None


def test_sem_texto_sem_uso_nao_chama(ia):
    pedidos = ia(_diag())
    tudo = [{"afirmacao": "x", "citacoes": [{"fonte": f"S{i + 1}", "trecho": f["trecho"]}]} for i, f in enumerate(FONTES_WEB)]
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, tudo, FONTES_WEB, FONTES)
    assert novas == [] and rel["decisao"] == "nada_a_revisar" and pedidos == []


# ---------------------------------------------------------------- busca dirigida
def test_busca_dirigida_leva_so_frases_das_perguntas_abertas_e_recupera_com_trecho_conferido(ia):
    pedidos = ia(_diag(("P3", "por que o espelho foi difícil de lançar", ["mirror", "challenge", "launching"])),
                 _dirigida(("P3", [ESPELHO])))
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    dirigido = pedidos[1]
    assert "Launching such a large mirror" in dirigido
    assert "Begoña" not in dirigido and "boletín" not in dirigido          # frases que não falam da pergunta não vão
    assert "BANCO DE EVIDÊNCIAS" not in dirigido                          # o banco foi uma vez só (no diagnóstico)
    assert "P3) Por que o espelho grande foi um desafio de lançar? — falta:" in dirigido
    assert [e["afirmacao"] for e in novas] == [ESPELHO["afirmacao"]] and novas[0]["citacoes"][0]["verificado"]
    assert novas[0]["origem"] == "cobertura" and novas[0]["lacuna_cobertura"] == "P3"
    assert rel["decisao"] == "avaliada" and rel["chamadas"] == 2 and rel["lotes"] == 1


def test_teto_da_busca_dirigida_corta_o_excesso(ia):
    """Uma frase relevante (~70 caracteres) só comporta uma evidência: a segunda resposta não entra."""
    ia(_diag(("P3", "desafio do espelho", ["challenge"])), _dirigida(("P3", [ESPELHO, dict(ESPELHO, afirmacao="Outra.")])))
    novas, _ = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert len(novas) == 1


def test_lacuna_sem_frase_relevante_nao_gasta_chamada(ia):
    pedidos = ia(_diag(("P2", "o que aconteceu com o radiador criogênico", ["cryocooler", "radiator", "criogenico"])))
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert novas == [] and rel["decisao"] == "lacunas_sem_material" and len(pedidos) == 1


def test_resposta_a_pergunta_que_nao_estava_aberta_nao_entra(ia):
    ia(_diag(("P3", "desafio do espelho", ["mirror", "challenge"])), _dirigida(("P1", [OTE]), ("P3", [ESPELHO])))
    novas, _ = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert [e["afirmacao"] for e in novas] == [ESPELHO["afirmacao"]]


def test_trecho_inventado_e_numero_errado_nao_sao_verificados(ia, monkeypatch):
    monkeypatch.setattr(ev, "CARACTERES_POR_EVIDENCIA", 50)
    inventado = {"afirmacao": "O espelho foi o maior desafio de toda a missão.",
                 "citacoes": [{"fonte": "S1", "trecho": "The mirror was the biggest challenge of the whole mission."}]}
    numero = {"afirmacao": "O espelho tem 25 segmentos e foi um enorme desafio.",
              "citacoes": [{"fonte": "S1", "trecho": "Launching such a large mirror into space was another huge challenge."}]}
    ia(_diag(("P3", "desafio do espelho", ["mirror", "challenge"])), _dirigida(("P3", [inventado, numero])))
    novas, _ = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert [ev._status(e)["status"] for e in novas] == ["nao_verificado", "nao_verificado"]


def test_o_que_o_banco_ja_tem_e_descartado_como_repetido(ia, monkeypatch):
    monkeypatch.setattr(ev, "CARACTERES_POR_EVIDENCIA", 50)
    repetida = {"afirmacao": "O protetor solar e os espelhos foram projetados para se desdobrar depois que o telescópio se "
                             "separa do foguete lançador.", "citacoes": [{"fonte": "S1", "trecho": SOMBRA}]}
    ia(_diag(("P2", "partes que se desdobraram", ["sunshield", "mirrors", "unfold", "mirror"])),
       _dirigida(("P2", [repetida, ESPELHO])))
    novas, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert [e["afirmacao"] for e in novas] == [ESPELHO["afirmacao"]] and rel["descartadas_repetidas"][0]["igual_a"] == "E1"


def test_texto_relevante_maior_que_um_lote_vai_em_no_maximo_dois(ia, monkeypatch):
    monkeypatch.setattr(ev, "LIMITE_LOTE", 400)
    enchimento = [{"titulo": f"Webb {k}", "url": f"https://science.nasa.gov/webb/{k}",
                   "trecho": " ".join(f"The mirror challenge number {k}.{j} was solved in space." for j in range(8))}
                  for k in range(3)]
    fontes_web = FONTES_WEB + enchimento
    pedidos = ia(_diag(("P3", "desafio do espelho", ["mirror", "challenge"])), _dirigida(), _dirigida(), _dirigida())
    _, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, fontes_web, ev._fontes_do_banco(fontes_web))
    assert rel["lotes"] == ev.MAX_LOTES_DIRIGIDOS == len(pedidos) - 1 and rel["nao_enviadas"] > 0


def test_pedido_identico_vem_do_cache(ia, monkeypatch):
    from src import disk_cache
    guardado = {}
    monkeypatch.setattr(disk_cache, "get", lambda nome, chave: guardado.get((nome, json.dumps(chave))))
    monkeypatch.setattr(disk_cache, "put", lambda nome, chave, v: guardado.__setitem__((nome, json.dumps(chave)), v))
    pedidos = ia(_diag(suficiente=True))
    ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    _, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, FONTES_WEB, FONTES)
    assert len(pedidos) == 1 and rel["decisao"] == "suficiente"
    assert metricas.resumo().get("cache_cobertura_diagnostico_acertos") == 1


# ---------------------------------------------------------------- no banco: juiz, termos, ordem e status iguais
class JuizPorAfirmacao:
    def __init__(self, recusar=()):
        self.recusar, self.vistas = recusar, []

    def __call__(self, messages, **kw):
        p = messages[0]["content"]
        julg = []
        for bloco in p.split("\n\n"):
            if ") AFIRMAÇÃO:" not in bloco:
                continue
            n = int(bloco.split(")")[0].strip())
            afirmacao = bloco.split("AFIRMAÇÃO: ", 1)[1].split("\n", 1)[0]
            trecho = bloco.split('TRECHO: "', 1)[1].split('"\n', 1)[0]
            self.vistas.append(afirmacao)
            ruim = any(r in afirmacao for r in self.recusar)
            julg.append({"n": n, "decisao": "irrelevante" if ruim else "sustenta", "sujeito": "outro" if ruim else "ok",
                         "medida": "nao_se_aplica", "trecho_chave": " ".join(trecho.split()[:5]),
                         "justificativa": "o trecho fala de outra coisa" if ruim else "o trecho diz isso", "termos": []})
        return json.dumps({"julgamentos": julg})


def test_no_banco_as_novas_passam_pelo_juiz_pela_ordem_e_ganham_status(monkeypatch, ia):
    monkeypatch.setattr(ev, "CARACTERES_POR_EVIDENCIA", 50)
    juiz = JuizPorAfirmacao(recusar=("Hubble",))
    monkeypatch.setattr(providers, "complete", juiz)
    recusada = {"afirmacao": "Pôr o telescópio Hubble em órbita foi um enorme desafio.", "citacoes": ESPELHO["citacoes"]}
    com_ordem = {"afirmacao": "Primeiro, o OTE reúne a luz do espaço e a entrega aos instrumentos científicos no ISIM.",
                 "citacoes": OTE["citacoes"]}
    inicial = {"evidencias": [{"afirmacao": JA_NO_BANCO[0]["afirmacao"], "citacoes": [{"fonte": "S1", "trecho": SOMBRA}]}],
               "lacunas": []}
    hist = {"historia": HISTORIA["historia"], "perguntas": HISTORIA["perguntas"], "fora_do_escopo": []}
    ia(hist, inicial, _diag(("P3", "desafio do espelho", ["mirror", "challenge", "light", "instruments"])),
       _dirigida(("P3", [ESPELHO, recusada, com_ordem])))
    banco = ev.montar_banco(TEMA, FONTES_WEB)
    por_texto = {e.get("afirmacao_com_ordem") or e["afirmacao"]: e for e in banco["evidencias"]}
    assert ESPELHO["afirmacao"] in juiz.vistas and recusada["afirmacao"] in juiz.vistas  # o juiz viu as novas
    assert por_texto[ESPELHO["afirmacao"]]["status"] in ev.STATUS_USAVEIS
    assert por_texto[recusada["afirmacao"]]["status"] == "nao_verificado"                # o juiz recusou: não entra
    ordem = por_texto[com_ordem["afirmacao"]]                                             # trava de ordem também vale
    assert ordem["ordem"]["comprovada"] is False and not ordem["afirmacao"].startswith("Primeiro")
    assert banco["historia"]["perguntas"][2]["id"] == "P3" and banco["cobertura"]["decisao"] == "avaliada"


def test_segundo_lote_so_com_frases_de_duas_palavras_das_perguntas(ia, monkeypatch):
    """Rodada 11: 69 frases com UMA palavra das perguntas abertas viraram 2 lotes. O segundo lote só sai com frases
    mais relevantes (2+ palavras); as outras ficam registradas como não enviadas."""
    monkeypatch.setattr(ev, "LIMITE_LOTE", 400)
    fracas = [{"titulo": f"Webb {k}", "url": f"https://science.nasa.gov/webb/{k}",
               "trecho": " ".join(f"The mirror of unit {k}.{j} was polished in a clean room." for j in range(8))}
              for k in range(3)]
    fontes_web = FONTES_WEB + fracas
    pedidos = ia(_diag(("P3", "desafio do espelho", ["mirror", "challenge"])), _dirigida(), _dirigida())
    _, rel = ev.cobrir_lacunas(TEMA, HISTORIA, JA_NO_BANCO, fontes_web, ev._fontes_do_banco(fontes_web))
    assert rel["lotes"] == 1 == len(pedidos) - 1 and rel["nao_enviadas"] > 0


def test_mesmo_trecho_mesmos_numeros_e_parafrase_e_repetida():
    """Rodada 11 (E70 × E73): a mesma frase da S11, dita de dois jeitos para duas perguntas."""
    trecho = ("Depois de cumprir o principal marco de alinhar o telescópio ao NIRCam em março de 2022, a equipe do Webb "
              "começou a estender o alinhamento do telescópio ao FGS e aos outros três instrumentos científicos.")
    cit = [{"fonte": "S11", "trecho": trecho}]
    a = "Após alinhar o telescópio ao NIRCam em março de 2022, a equipe começou a estender o alinhamento ao sensor de orientação fina (FGS) e aos outros três instrumentos científicos."
    b = "Após o marco principal de alinhar o telescópio ao NIRCam em março de 2022, a equipe estendeu o alinhamento ao guia (FGS) e aos outros três instrumentos."
    assert ev._ja_no_banco(b, [{"id": "E70", "afirmacao": a, "citacoes": cit}], cit) == "E70"
    # mesmo trecho, fatos diferentes (números diferentes): não é repetida — "port" em 7/1 × "starboard" em 8/1
    esa = "with the first (port) side of the mirror deployed on 7 January and the second (starboard) side deployed on 8 January"
    c74 = [{"fonte": "S3", "trecho": esa}]
    assert ev._ja_no_banco("O lado de bombordo do espelho foi desdobrado em 7 de janeiro.",
                           [{"id": "E1", "afirmacao": "O lado de estibordo do espelho foi desdobrado em 8 de janeiro.",
                             "citacoes": c74}], c74) is None
