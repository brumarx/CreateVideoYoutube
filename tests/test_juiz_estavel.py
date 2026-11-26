"""23/11 (Svalbard, jobs 1003-1005): o mesmo roteiro recebeu editorial 6,0 a 7,5 e 0 a 3 leituras enganosas;
e a leitura cortada do Nemotron foi completada pelo gpt-oss sem aviso. Juiz fixo (mesmo provedor e modelo,
no máximo 2 pedidos, sem outro modelo no lugar), 3 avaliações por versão (mediana por critério no editorial;
interpretação enganosa bloqueia com 2 de 3, com 1 vira alerta), versão sem 3 avaliações válidas não é
aprovada, e divergência entre fontes nunca vira faixa."""
import json
from types import SimpleNamespace

import pytest

from src import fatos as ft
from src import providers as pv
from src import redacao as rd

NOME, MODELO = pv.JUIZ
CHAVES = SimpleNamespace(nvidia=["nvapi-teste-1", "nvapi-teste-2"])


@pytest.fixture
def rede(monkeypatch):
    """_call_openai_compat simulado: respostas na ordem; guarda (modelo, saída) de cada pedido. A cascata não
    pode ser chamada."""
    pedidos = []

    def instalar(*respostas):
        fila = list(respostas)

        def chamar(endpoint, api_key, model, messages, max_tokens, temperature=0.8, _fitted=False, _ampliado=False,
                   pensar=False):
            pedidos.append({"endpoint": endpoint, "modelo": model, "saida": max_tokens, "ampliado": _ampliado,
                            "pensar": pensar})
            return fila.pop(0)
        monkeypatch.setattr(pv, "_call_openai_compat", chamar)
        monkeypatch.setattr(pv, "_complete_cascata", lambda *a, **k: pytest.fail("juiz caiu na cascata"))
        return pedidos
    return instalar


# ======================================================================= juiz fixo
def test_resposta_truncada_do_juiz_e_pedida_de_novo_no_mesmo_modelo_com_mais_saida(rede):
    pedidos = rede((None, 200, 0), ('{"ok": 1}', 200, 0))
    r = pv.complete([{"role": "user", "content": "x"}], keys=CHAVES, max_tokens=8000, temperature=0.1, juiz=True)
    assert r == '{"ok": 1}'
    assert [p["modelo"] for p in pedidos] == [MODELO, MODELO] and all("nvidia" in p["endpoint"] for p in pedidos)
    assert [p["saida"] for p in pedidos] == [8000, 16000] and all(p["ampliado"] for p in pedidos)


def test_juiz_fixo_sem_resposta_valida_fica_nao_julgado_depois_de_duas_tentativas(rede):
    pedidos = rede((None, 200, 0), ("isto não é json", 200, 0), ('{"ok": 1}', 200, 0))
    with pytest.raises(pv.JuizSemResposta, match="cortada.*inválida"):
        pv.complete([{"role": "user", "content": "x"}], keys=CHAVES, max_tokens=8000, temperature=0.1,
                    validate=lambda r: r.startswith("{"), juiz=True)
    assert len(pedidos) == pv.JUIZ_TENTATIVAS == 2  # a 3ª resposta nunca é pedida


def test_juiz_nunca_passa_do_teto_de_saida(rede):
    pedidos = rede((None, 200, 0), (None, 200, 0))
    with pytest.raises(pv.JuizSemResposta):
        pv.complete([{"role": "user", "content": "x"}], keys=CHAVES, max_tokens=20000, temperature=0.1, juiz=True)
    assert [p["saida"] for p in pedidos] == [20000, pv.JUIZ_SAIDA_MAX]


def test_sem_fallback_silencioso_nenhum_outro_modelo_completa_a_avaliacao(rede, monkeypatch):
    """A falha do juiz chega à leitura de espectador como "não julgada"; a cascata (gpt-oss, Groq…) não é
    chamada (a fixture falha o teste se for)."""
    pedidos = rede(*[(None, 200, 0)] * 2 * (rd.LEITURAS + rd.LEITURAS_REPOSICAO))
    monkeypatch.setattr(pv, "LLMKeys", lambda: CHAVES)
    s = {"scenes": [{"narration": "A. B.", "frases": [{"texto": "A frase um.", "tipo": "narrativa", "evidencias": []},
                                                      {"texto": "A frase dois.", "tipo": "narrativa", "evidencias": []}]}]}
    assert rd.leitura_espectador(s, {"evidencias": []}, "t") is None
    # 26/11: as que falham são repostas (LEITURAS_REPOSICAO) antes de virar "não julgada"
    assert {p["modelo"] for p in pedidos} == {MODELO} and len(pedidos) == 2 * (rd.LEITURAS + rd.LEITURAS_REPOSICAO)


# ======================================================================= três avaliações por versão
def _editor(*notas):
    """complete simulado para o editor: uma resposta por leitura, com as notas dadas (dict parcial)."""
    fila = [json.dumps({"criterios": {**{k: 8 for k in rd.CRITERIOS_EDITORIAIS}, **n},
                        "problemas": [{"criterio": "conclusao", "cenas": [2], "descricao": "fim fraco"}]})
            for n in notas]
    chamadas = []

    def complete(messages, **kw):
        chamadas.append(kw)
        r = fila.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return complete, fila, chamadas


S = {"title": "t", "scenes": [{"narration": "Uma.", "frases": []}, {"narration": "Duas.", "frases": []}]}


def test_tres_notas_editoriais_ficam_com_a_mediana_de_cada_criterio(monkeypatch):
    complete, _, chamadas = _editor({"titulo": 5, "conclusao": 6}, {"titulo": 9, "conclusao": 7}, {"titulo": 7, "conclusao": 9})
    monkeypatch.setattr(pv, "complete", complete)
    q = rd.avaliar_editorial(S, "t")
    assert q["criterios"]["titulo"] == 7 and q["criterios"]["conclusao"] == 7 and q["criterios"]["abertura"] == 8
    assert q["nota"] == round((8 * 6 + 7 + 7) / 8, 1) and q["aprovado"] and not q["nao_julgada"]
    assert len(q["leituras"]) == 3 and all(k.get("juiz") for k in chamadas)  # as 3 no juiz fixo
    assert len(q["alvos"]) == 1  # o mesmo problema das 3 leituras entra uma vez


def test_sem_tres_avaliacoes_editoriais_validas_a_versao_nao_e_aprovada(monkeypatch):
    complete, fila, _ = _editor({"titulo": 9}, {"titulo": 9})
    fila += [pv.JuizSemResposta("cortada")] * (1 + rd.LEITURAS_REPOSICAO)   # a falta e as reposições falham
    monkeypatch.setattr(pv, "complete", complete)
    q = rd.avaliar_editorial(S, "t")
    assert q["nota"] >= rd.NOTA_MINIMA and q["aprovado"] is False and "2 de 3" in q["nao_julgada"]


LISTA = [{"texto": f"Frase número {k}.", "tipo": "factual", "evidencias": ["E1"]} for k in range(1, 7)]


def _espectador(*leituras):
    fila = [json.dumps({"conclusoes_do_publico": ["a coisa um", "a coisa dois"], "enganos": e}) for e in leituras]

    def complete(messages, **kw):
        assert kw.get("juiz") is True
        return fila.pop(0)
    return complete


def E(frases, tipo="combinacao", conclui="o público conclui uma coisa errada"):
    return {"frases": frases, "tipo": tipo, "o_publico_conclui": conclui, "por_que_engana": "as fontes não dizem isso",
            "como_corrigir": "diga só o que a fonte diz"}


def test_interpretacao_enganosa_vista_em_duas_leituras_bloqueia_sem_contar_duplicado(monkeypatch):
    monkeypatch.setattr(pv, "complete", _espectador(
        [E([2, 3]), E([2, 3], "causal")],      # duas vezes a MESMA interpretação na leitura 1: um voto só
        [E([3, 2])],
        []))
    s = {"scenes": [{"narration": "x", "frases": LISTA}]}
    r = rd.leitura_espectador(s, {"evidencias": []}, "t")
    assert len(r["enganos"]) == 1 and r["enganos"][0]["votos"] == 2 and r["enganos"][0]["frases"] == [2, 3]
    assert r["alertas"] == [] and not r["nao_julgada"]


def test_interpretacao_vista_por_uma_leitura_so_vira_alerta_para_revisao_humana(monkeypatch):
    monkeypatch.setattr(pv, "complete", _espectador([E([5], "sujeito_trocado")], [], [E([1, 2], "ordem")]))
    s = {"scenes": [{"narration": "x", "frases": LISTA}]}
    r = rd.leitura_espectador(s, {"evidencias": []}, "t")
    assert r["enganos"] == [] and [a["frases"] for a in r["alertas"]] == [[5], [1, 2]]
    assert all(a["votos"] == 1 for a in r["alertas"])


def test_alerta_nao_e_problema_da_versao_mas_fica_registrado(monkeypatch):
    s = {"title": "t", "scenes": [{"narration": "x", "frases": LISTA}]}
    banco = {"evidencias": [{"id": "E1", "afirmacao": "Frase número 1. Frase número 2.", "status": "oficial",
                             "tipo": "qualitativo", "estimado": False, "numeros": ["1", "2", "3", "4", "5", "6"],
                             "conflitos": [], "citacoes": []}]}
    monkeypatch.setattr(rd, "leitura_espectador", lambda *a: {"conclusoes": ["c"], "enganos": [], "leituras": 3,
                                                               "alertas": [E([5])], "nao_julgada": ""})
    monkeypatch.setattr(rd, "avaliar_editorial", lambda *a: {"nota": 8.0, "criterios": {}, "travas": [], "alvos": [],
                                                              "problemas": [], "aprovado": True, "nao_julgada": ""})
    aud = json.dumps({"frases": [{"n": k, "decisao": "sustentada", "evidencias": ["E1"], "justificativa": "a evidência E1 traz isso"}
                                 for k in range(1, 7)], "qualidade": {"nota": 0, "problemas": []}})
    monkeypatch.setattr(pv, "complete", lambda *a, **k: aud)
    probs, q = rd.auditar_ia(s, banco, "t")
    assert probs == [] and q["factual"]["alertas"][0]["frases"] == [5] and q["aprovado"]


def test_versao_nao_julgada_nunca_e_aprovada_nem_sem_problema_factual(monkeypatch):
    q_ok = {"nota": 9.0, "criterios": {}, "travas": [], "alvos": [], "problemas": [], "aprovado": False,
            "nao_julgada": ["só 2 de 3 leituras de espectador válidas"], "factual": {"nota": 10, "enganos": []}}
    monkeypatch.setattr(rd, "_auditar_versao", lambda s, b, t, p: (s, [], q_ok))
    s = {"title": "t", "scenes": [{"narration": "Mas por quê?", "frases": [{"texto": "Mas por quê?", "tipo": "narrativa",
                                                                            "evidencias": []}]}]}
    with pytest.raises(rd.RoteiroReprovado, match="qualidade"):
        rd.auditar_e_corrigir(s, {"evidencias": []}, "t")
    inst = rd.instantaneo(1, "escrita", [], s, [], q_ok, {"evidencias": []})
    assert inst["aprovado"] is False and inst["nao_julgada"]


# ======================================================================= divergência entre fontes
def _ev(id_, afirmacao):
    return {"id": id_, "afirmacao": afirmacao, "tipo": "quantitativo", "status": "oficial", "estimado": False,
            "numeros": sorted(ft.numeros(afirmacao)), "conflitos": [],
            "citacoes": [{"fonte": id_, "url": f"https://x{id_}.gov/a", "titulo": "x", "dominio": "x.gov",
                          "classe": "oficial", "trecho": afirmacao, "verificado": True}]}


BANCO = {"versao": 1, "tema": "t", "fontes": [], "lacunas": [], "nao_lidos": [], "evidencias": [
    _ev("E1", "Uma fonte diz que o túnel tem 100 metros."),
    _ev("E2", "Outra fonte diz que o túnel tem 120 metros."),
    _ev("E3", "As câmaras ficam entre 3 e 6 graus negativos sem refrigeração.")]}


def _uma(texto, evid):
    return {"scenes": [{"narration": texto, "frases": [{"texto": texto, "tipo": "factual", "evidencias": evid}]}]}


@pytest.mark.parametrize("texto", ["O túnel tem entre 100 e 120 metros.", "O túnel tem de 100 a 120 metros.",
                                   "O túnel tem cerca de 100 a 120 metros."])
def test_fontes_divergentes_sobre_a_mesma_medida_nao_viram_faixa(texto):
    _, probs = rd.auditar_codigo(_uma(texto, ["E1", "E2"]), BANCO, "t")
    assert [p["tipo"] for p in probs] == ["divergencia_como_faixa"]
    assert "E1" in probs[0]["motivo"] and "E2" in probs[0]["motivo"]


def test_divergencia_dita_identificando_as_fontes_passa():
    texto = "Uma fonte diz que o túnel tem 100 metros; outra fonte diz 120 metros."
    assert rd.auditar_codigo(_uma(texto, ["E1", "E2"]), BANCO, "t")[1] == []


def test_faixa_dada_por_uma_fonte_continua_permitida():
    texto = "Sem refrigeração, as câmaras ficam entre 3 e 6 graus negativos."
    assert rd.auditar_codigo(_uma(texto, ["E3"]), BANCO, "t")[1] == []


def test_anos_seguidos_nao_sao_faixa_de_medida():
    assert rd.faixa_de_fontes_divergentes("Houve retiradas em 2017 e 2019.", [], BANCO) is None


def test_so_o_juiz_fixo_pensa_no_nemotron(monkeypatch):
    # 24/11: com raciocínio, 19 de 26 respostas da rodada de Svalbard saíram cortadas; só o juiz pensa
    enviados = []

    class _R:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(pv.httpx, "post", lambda url, **kw: enviados.append(kw["json"]) or _R())
    ep = "https://integrate.api.nvidia.com/v1/chat/completions"
    pv._call_openai_compat(ep, "k", "nvidia/nemotron-3-ultra-550b-a55b", [{"role": "user", "content": "x"}], 1000, 0.1)
    pv._call_openai_compat(ep, "k", "nvidia/nemotron-3-ultra-550b-a55b", [{"role": "user", "content": "x"}], 1000, 0.1,
                           pensar=True)
    assert enviados[0]["chat_template_kwargs"] == {"enable_thinking": False} and enviados[0]["max_tokens"] == 1000
    assert "chat_template_kwargs" not in enviados[1] and enviados[1]["max_tokens"] == 1000 + 4096


def test_juiz_reveza_chaves_e_espera_o_descanso_como_o_groq(rede, monkeypatch):
    # 24/11: 429 não gasta tentativa — a chave descansa, a outra tenta; todas descansando, espera e volta
    from src.config import LLMKeys
    chaves = LLMKeys(nvidia=["nvapi-reveza-1", "nvapi-reveza-2"])  # chaves novas: rotação sem descanso herdado
    esperas = []
    import time as _t
    agora = [_t.time()]  # relógio real: o descanso guardado no rotador é medido nele
    monkeypatch.setattr(pv.time, "time", lambda: agora[0])
    monkeypatch.setattr(pv.time, "sleep", lambda s: (esperas.append(s), agora.__setitem__(0, agora[0] + s)))
    pedidos = rede((None, 429, 30), (None, 429, 30), ('{"ok": 1}', 200, 0))
    r = pv.complete([{"role": "user", "content": "x"}], keys=chaves, max_tokens=8000, temperature=0.1, juiz=True)
    assert r == '{"ok": 1}' and len(pedidos) == 3
    assert esperas and all(0 < s <= pv.ESPERA_CURTA_MAX for s in esperas) and sum(esperas) <= pv.ESPERA_TOTAL_MAX


def test_nota_perto_do_minimo_ganha_mais_duas_leituras(monkeypatch):
    # 26/11: 3 leituras dão 6,8 (faixa de dúvida) → mais 2, mediana de 5
    baixo = {k: 7 for k in rd.CRITERIOS_EDITORIAIS}
    baixo["conclusao"] = 5
    baixo["novidade"] = 6
    complete, fila, chamadas = _editor(baixo, baixo, baixo, {}, {})
    monkeypatch.setattr(pv, "complete", complete)
    q = rd.avaliar_editorial(S, "t")
    assert len(chamadas) == 5 and len(q["leituras"]) == 5
    assert q["criterios"]["conclusao"] == 5 and not q["nao_julgada"]


def test_nota_longe_do_minimo_fica_com_tres_leituras(monkeypatch):
    complete, _, chamadas = _editor({}, {}, {})  # 8 em tudo
    monkeypatch.setattr(pv, "complete", complete)
    rd.avaliar_editorial(S, "t")
    assert len(chamadas) == 3


def test_leitura_que_cai_e_reposta_e_a_versao_e_julgada(monkeypatch):
    """26/11 (Svalbard 1033 v5: 2 problemas, editorial 7,1 — "não julgada" porque a NVIDIA caiu numa leitura)."""
    import json as _json
    from src import providers, redacao as rd
    chamadas = {"n": 0}
    resposta = _json.dumps({"conclusoes_do_publico": ["a", "b"], "enganos": []})

    def complete(msgs, **k):
        chamadas["n"] += 1
        if chamadas["n"] == 2:
            raise RuntimeError("Server disconnected without sending a response")
        return resposta
    monkeypatch.setattr(providers, "complete", complete)
    monkeypatch.setattr(rd, "complete", complete, raising=False)
    s = {"scenes": [{"frases": [{"texto": "O cofre fica numa montanha.", "tipo": "factual", "evidencias": []}],
                     "narration": "O cofre fica numa montanha."}]}
    r = rd.leitura_espectador(s, {"evidencias": []}, "Svalbard")
    assert r is not None and not r.get("nao_julgada") and chamadas["n"] == 4   # 3 válidas + 1 reposta


@pytest.mark.reconexao
def test_queda_do_servidor_tenta_o_mesmo_modelo_de_novo(monkeypatch):
    """26/11 (o dono): "se nosso modelo cai com a NVIDIA, ele deve voltar a tentar e não abandonar"."""
    from src import providers
    respostas = [(None, None, None), (None, 502, None), ("ok", 200, None)]
    chamadas = []
    monkeypatch.setattr(providers, "_call_openai_compat", lambda *a, **k: chamadas.append(a[2]) or respostas.pop(0))
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    r = providers._chamar_com_reconexao("url", "chave", "nemotron", [], 100, 0.1)
    assert r[0] == "ok" and chamadas == ["nemotron"] * 3


@pytest.mark.reconexao
def test_resposta_errada_nao_e_queda(monkeypatch):
    from src import providers
    chamadas = []
    monkeypatch.setattr(providers, "_call_openai_compat", lambda *a, **k: chamadas.append(1) or (None, 400, None))
    providers._chamar_com_reconexao("url", "chave", "m", [], 100, 0.1)
    assert chamadas == [1]   # 400 é erro do pedido: não adianta repetir
