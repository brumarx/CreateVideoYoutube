"""22/11 (Svalbard, jobs 999-1001): busca com tentativas e rodízio de motores, e histórico de reprovações
para o editor (o mesmo exagero não volta com outras palavras)."""
import json

import pytest

from src import busca_motores as bm
from src import fatos as ft
from src import providers
from src import redacao as rd


# ======================================================================= busca: motores falsos
class Relogio:
    def __init__(self):
        self.t = 1000.0
        self.dormidas: list[float] = []

    def agora(self):
        return self.t

    def dormir(self, s):
        self.dormidas.append(s)
        self.t += s


@pytest.fixture
def motores(monkeypatch):
    """Instala motores falsos: {nome: [respostas na ordem]}; cada resposta é (situação, urls, retry_after)."""
    rel = Relogio()
    monkeypatch.setattr(bm, "_relogio", rel.agora)
    monkeypatch.setattr(bm, "_dormir", rel.dormir)
    monkeypatch.setattr(bm, "_fora_ate", {})
    monkeypatch.setattr(bm, "falhas_definitivas", [])
    chamadas: list[str] = []

    def instalar(roteiro: dict[str, list]):
        monkeypatch.setattr(bm, "_motores", lambda: {n: type(n, (), {"priority": 1}) for n in roteiro})

        def consultar(nome, cls, query, region):
            chamadas.append(nome)
            rel.t += 1  # cada pedido gasta 1 s
            fila = roteiro[nome]
            situacao, urls, retry = fila.pop(0) if len(fila) > 1 else fila[0]
            return situacao, [{"titulo": u, "url": u, "trecho": f"texto de {u}"} for u in urls], retry
        monkeypatch.setattr(bm, "consultar_motor", consultar)
        return chamadas, rel
    return instalar


def test_busca_temporariamente_indisponivel_e_tentada_de_novo_respeitando_retry_after(motores):
    chamadas, rel = motores({"yahoo": [("limite", [], 5.0), ("ok", ["https://seedvault.no/a"], None)]})
    r = bm.buscar("Svalbard Global Seed Vault", max_results=1)
    assert [x["url"] for x in r] == ["https://seedvault.no/a"]
    assert chamadas == ["yahoo", "yahoo"]
    # esperou o Retry-After inteiro (contado da resposta), não bateu de novo antes
    assert rel.dormidas == [5.0]
    assert not bm.falhas_definitivas


def test_motor_bloqueado_nao_gera_ciclo_e_fica_de_fora_nas_consultas_seguintes(motores):
    chamadas, rel = motores({"duckduckgo": [("bloqueado", [], None)], "brave": [("vazio", [], None)]})
    assert bm.buscar("consulta qualquer", max_results=5) == []
    # o bloqueado é consultado uma vez só; o vazio, no máximo uma vez por rodada
    assert chamadas.count("duckduckgo") == 1 and chamadas.count("brave") <= bm.TENTATIVAS
    assert len(chamadas) <= 1 + bm.TENTATIVAS
    assert sum(rel.dormidas) <= bm.ORCAMENTO_S
    assert bm.falhas_definitivas and bm.falhas_definitivas[0]["motores"]["duckduckgo"] == "bloqueado"
    assert bm.falhas_definitivas[0]["motores"]["brave"].startswith("vazio")
    chamadas.clear()
    bm.buscar("outra consulta", max_results=5)
    assert "duckduckgo" not in chamadas  # 403/desafio: não insiste nas consultas seguintes


def test_todos_os_motores_com_espera_longa_nao_dorme_alem_do_orcamento(motores):
    chamadas, rel = motores({"yahoo": [("limite", [], 3600.0)], "mojeek": [("bloqueado", [], None)]})
    assert bm.buscar("consulta", max_results=3) == []
    assert chamadas == ["yahoo", "mojeek"]  # ninguém volta dentro do orçamento: para em vez de esperar 1 hora
    assert sum(rel.dormidas) == 0


def test_com_resultado_e_todos_os_motores_ja_consultados_nao_repete_a_rodada(motores):
    chamadas, _ = motores({"yahoo": [("ok", ["https://seedvault.no/a"], None)], "mojeek": [("vazio", [], None)]})
    assert len(bm.buscar("consulta", max_results=10)) == 1
    assert sorted(chamadas) == ["mojeek", "yahoo"]  # uma rodada só


def test_resultados_repetidos_entre_motores_e_rodadas_entram_uma_vez_com_origem_e_relevancia(motores):
    motores({"wikipedia": [("ok", ["https://en.wikipedia.org/wiki/Svalbard_Global_Seed_Vault"], None)],
             "yahoo": [("ok", ["https://www.seedvault.no/", "https://en.wikipedia.org/wiki/Svalbard_Global_Seed_Vault/"], None)],
             "mojeek": [("ok", ["https://seedvault.no", "https://www.croptrust.org/x"], None)]})
    r = bm.buscar("Svalbard Global Seed Vault", max_results=10)
    assert [x["url"] for x in r] == ["https://en.wikipedia.org/wiki/Svalbard_Global_Seed_Vault",
                                     "https://www.seedvault.no/", "https://www.croptrust.org/x"]
    assert [x["origem"] for x in r] == ["wikipedia", "yahoo", "mojeek"]
    assert [x["relevancia"] for x in r] == [1, 2, 3] and all(x["buscado_em"] for x in r)


def test_consultar_motor_le_o_status_real_403_e_429_com_retry_after():
    class Resp:
        def __init__(self, status, headers):
            self.status_code = status
            self._resp = type("R", (), {"headers": headers})()

    def motor_falso(status, headers=None):
        class Cliente:
            def request(self, *a, **k):
                return Resp(status, headers or {})

        class Motor:
            def __init__(self, timeout=None):
                self.http_client = Cliente()

            def search(self, query, region=None):
                self.http_client.request("GET", "u")
                return None
        return Motor
    assert bm.consultar_motor("ddg", motor_falso(202), "q", "br-pt")[0] == "bloqueado"
    assert bm.consultar_motor("google", motor_falso(403), "q", "br-pt")[0] == "bloqueado"
    sit, _, retry = bm.consultar_motor("brave", motor_falso(429, {"retry-after": "30"}), "q", "br-pt")
    assert sit == "limite" and retry == 30.0
    assert bm.consultar_motor("yahoo", motor_falso(200), "q", "br-pt")[0] == "vazio"


def test_web_search_usa_a_busca_com_tentativas(monkeypatch):
    from src import web_search as ws
    monkeypatch.setattr(bm, "buscar", lambda q, max_results=10, region="br-pt": [
        {"titulo": "t", "url": "https://seedvault.no/a", "trecho": "x" * 600, "origem": "yahoo", "relevancia": 1,
         "buscado_em": "2026-11-22"}])
    r = ws._duckduckgo("Svalbard", 3)
    assert r[0]["url"] == "https://seedvault.no/a" and r[0]["origem"] == "yahoo" and len(r[0]["trecho"]) == 500


# ======================================================================= histórico de reprovações
def _ev(id_, afirmacao, trecho):
    return {"id": id_, "afirmacao": afirmacao, "tipo": "qualitativo", "status": "oficial", "estimado": False,
            "numeros": sorted(ft.numeros(afirmacao)), "conflitos": [],
            "citacoes": [{"fonte": "S1", "url": "https://seedvault.no/a", "titulo": "x", "dominio": "seedvault.no",
                          "classe": "oficial", "trecho": trecho, "verificado": True}]}


BANCO = {"versao": 1, "tema": "Svalbard", "fontes": [], "lacunas": [], "nao_lidos": [], "evidencias": [
    _ev("E1", "Bancos de genes no Iraque e na Síria foram danificados por guerras.",
        "gene banks in Iraq and Syria were damaged by war"),
    _ev("E2", "Em 2016, água de degelo entrou no túnel de entrada.", "In 2016 meltwater entered the access tunnel"),
    _ev("E3", "As sementes são guardadas a 18 graus negativos.", "the seeds are always kept at minus 18 degrees"),
    _ev("E4", "Uma fonte diz que o túnel tem 100 metros.", "the tunnel is 100 metres long"),
    _ev("E5", "Outra fonte diz que o túnel tem 120 metros.", "a 120 metre tunnel"),
]}


def F(texto, tipo="factual", evid=()):
    return {"texto": texto, "tipo": tipo, "evidencias": list(evid)}


INTEIRAS = "Bancos de genes do Iraque e da Síria perderam coleções inteiras na guerra."
SEMPRE = "O degelo sempre trazia água para o túnel de entrada."
CORRETA = "As sementes ficam a 18 graus negativos."
FAIXA = "O túnel tem entre 100 e 120 metros."


def _roteiro():
    cenas = [[F("E aí, mente curiosa!", "narrativa")], [F(INTEIRAS, evid=["E1"])], [F(SEMPRE, evid=["E2"])],
             [F(CORRETA, evid=["E3"])], [F(FAIXA, evid=["E4", "E5"])]]
    return {"title": "O cofre", "description": "d", "tags": [], "_redacao": True, "_banco": BANCO,
            "scenes": [{"narration": " ".join(f["texto"] for f in fr), "frases": fr} for fr in cenas]}


def _problemas_v1():
    return [rd._p("excede", INTEIRAS, 1, 0, "a fonte diz danificados, não que coleções inteiras foram perdidas"),
            rd._p("excede", SEMPRE, 2, 0, "a fonte fala de um episódio em 2016, não de uma regra"),
            rd._p("leitura_enganosa", FAIXA, 4, 0, "divergência entre fontes apresentada como faixa")]


def _historico():
    h: list[dict] = []
    rd.registrar_historico(h, 1, _roteiro(), _problemas_v1(), BANCO)
    return h


def ED(*cenas):
    return json.dumps({"cenas": [{"cena": c, "funcao": "f", "preserva": "p", "corrige": "c", "justificativa": "",
                                  "frases": fr} for c, fr in cenas], "titulo": ""}, ensure_ascii=False)


class Editor:
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos: list[str] = []

    def __call__(self, messages, **kw):
        self.pedidos.append(messages[-1]["content"])
        return self.respostas.pop(0)


def test_historico_guarda_frase_cena_motivo_evidencias_e_a_escala_sem_sustentacao():
    h = _historico()
    assert [(x["cena"], x["tipo"]) for x in h] == [(1, "excede"), (2, "excede"), (4, "leitura_enganosa")]
    assert h[0]["escalas"] == {"extensao": "inteiras"}
    assert h[1]["escalas"] == {"frequencia": "sempre"} and h[1]["evidencias"] == ["E2"]
    assert h[2]["numeros"] == ["100", "120"]
    bloco = rd.bloco_historico(h, BANCO)
    assert INTEIRAS in bloco and "cena 2" in bloco and "danificados" in bloco
    assert "gene banks in Iraq and Syria were damaged by war" in bloco  # o que a fonte permite afirmar
    assert "extensao ('inteiras')" in bloco and "O mesmo erro dito com outras palavras é o MESMO erro" in bloco


@pytest.mark.parametrize("cena,disfarce,evid", [
    (2, "A guerra destruiu por completo os acervos do Iraque e da Síria.", ["E1"]),     # inteiras → por completo
    (3, "Com o degelo, a água entrava no túnel todo ano.", ["E2"]),                      # sempre → todo ano
    (3, "Com frequência o degelo levava água ao túnel de entrada.", ["E2"]),             # sempre → com frequência
    (5, "O túnel mede de 100 a 120 metros.", ["E4", "E5"]),                              # faixa com outras palavras
])
def test_exagero_reprovado_nao_volta_disfarcado_com_outra_formulacao(monkeypatch, cena, disfarce, evid):
    s = _roteiro()
    ed = Editor(ED((cena, [F(disfarce, evid=evid)])))
    monkeypatch.setattr(providers, "complete", ed)
    alvos = rd.alvos_da_edicao(s, {"nota": 9}, _problemas_v1())
    novo, reg = rd.editar_cenas(s, alvos, BANCO, "Svalbard", problemas=_problemas_v1(), historico=_historico())
    assert [r["cena"] for r in reg["revertidas"]] == [cena] and not reg["cenas_editadas"]
    assert novo["scenes"][cena - 1]["narration"] == s["scenes"][cena - 1]["narration"]


def test_correcao_restrita_entra_e_frases_corretas_nao_sao_prejudicadas(monkeypatch):
    s = _roteiro()
    ed = Editor(ED((2, [F("Bancos de genes do Iraque e da Síria foram danificados pela guerra.", evid=["E1"])]),
                   (3, [F("Em 2016, a água do degelo entrou no túnel de entrada.", evid=["E2"])]),
                   (5, [F("Uma fonte diz que o túnel tem 100 metros; outra fonte, 120.", evid=["E4", "E5"])]),
                   # marcador de escala SUSTENTADO pela evidência ("always") numa cena correta passa
                   (4, [F("As sementes são sempre guardadas a 18 graus negativos.", evid=["E3"])])))
    monkeypatch.setattr(providers, "complete", ed)
    alvos = rd.alvos_da_edicao(s, {"nota": 9}, _problemas_v1())
    alvos[3] = ["HISTÓRIA — teste"]
    novo, reg = rd.editar_cenas(s, alvos, BANCO, "Svalbard", problemas=_problemas_v1(), historico=_historico())
    assert reg["cenas_editadas"] == [2, 3, 4, 5] and not reg["revertidas"]
    assert novo["scenes"][0] == s["scenes"][0]  # cena fora dos alvos intacta


def test_causa_sustentada_em_portugues_na_evidencia_nao_e_exagero():
    """Job 1003: E108 diz "devido a temperaturas acima da média e chuvas fortes"; "causaram" foi recusado."""
    e = {"afirmacao": "Em outubro de 2016, houve intrusão de água devido a temperaturas acima da média e chuvas fortes.",
         "citacoes": []}
    assert rd.marcadores_sem_suporte("Calor e chuva causaram a entrada de água.", [e]) == {}
    assert rd.marcadores_sem_suporte("Calor e chuva causaram a entrada de água.",
                                     [{"afirmacao": "Em 2016 entrou água no túnel.", "citacoes": []}]) == {"causa": "causaram"}


def test_exagero_novo_sem_historico_tambem_e_recusado(monkeypatch):
    s = _roteiro()
    ed = Editor(ED((2, [F("Todos os bancos de genes do mundo foram danificados pela guerra.", evid=["E1"])])))
    monkeypatch.setattr(providers, "complete", ed)
    novo, reg = rd.editar_cenas(s, {1: ["FACTUAL — teste"]}, BANCO, "Svalbard", historico=[])
    assert reg["revertidas"] and "exagero novo" in reg["revertidas"][0]["motivo"]
    assert "quantificador" in reg["revertidas"][0]["motivo"]


def _qualidade(aprovado=True):
    return {"nota": 8.0, "aprovado": aprovado, "criterios": {}, "alvos": [], "travas": [], "problemas": [],
            "factual": {"nota": 5, "enganos": []}}


def _auditor_falso(sequencia):
    """_auditar_versao simulado: devolve os problemas da versão n (pela frase atual de cada cena)."""
    chamadas = {"n": 0}

    def auditar(script, banco, topic, previas):
        k = chamadas["n"]
        chamadas["n"] += 1
        probs = []
        for tipo, cena, motivo in sequencia[k]:
            texto = script["scenes"][cena]["frases"][0]["texto"]
            probs.append(rd._p(tipo, texto, cena, 0, motivo))
        return script, probs, _qualidade()
    return auditar


def test_editor_recebe_no_inicio_de_cada_rodada_os_problemas_das_versoes_anteriores(monkeypatch):
    monkeypatch.setattr(rd, "_auditar_versao", _auditor_falso([
        [("excede", 1, "coleções inteiras: a fonte diz danificados"), ("excede", 2, "a fonte não diz sempre")],
        [("excede", 2, "um episódio virou regra de novo")],
        []]))
    ed = Editor(ED((2, [F("Bancos de genes do Iraque e da Síria foram danificados pela guerra.", evid=["E1"])]),
                   (3, [F("O degelo levava água ao túnel de entrada.", evid=["E2"])])),
                ED((3, [F("Em 2016, a água do degelo entrou no túnel de entrada.", evid=["E2"])])))
    monkeypatch.setattr(providers, "complete", ed)
    final, rel = rd.auditar_e_corrigir(_roteiro(), BANCO, "Svalbard")
    p1, p2 = ed.pedidos
    for p in (p1, p2):
        assert p.index("HISTÓRICO DE REPROVAÇÕES") < p.index("ROTEIRO COMPLETO")  # no início da rodada
        assert INTEIRAS in p and SEMPRE in p
    # 2ª rodada: o que já foi tentado e o que voltou
    assert "correção já tentada: \"O degelo levava água ao túnel de entrada.\"" in p2
    assert "VOLTOU (já reprovado na v1)" in p2
    assert any(h["voltou_de"] == 1 and h["cena"] == 2 for h in rel["historico"])
    assert "Em 2016" in final["scenes"][2]["narration"]


def test_trava_de_numeros_so_vale_para_divergencia_e_nao_bloqueia_correcao_de_ordem(monkeypatch):
    """Job 1002: a trava recusou a correção de uma leitura enganosa de ORDEM só porque os dois números ficaram."""
    s = _roteiro()
    h: list[dict] = []
    rd.registrar_historico(h, 1, s, [rd._p("leitura_enganosa", FAIXA, 4, 0, "ordem: o público conclui que veio depois")],
                           BANCO)
    ed = Editor(ED((5, [F("Uma medição deu 100 metros e outra 120 metros.", evid=["E4", "E5"])])))
    monkeypatch.setattr(providers, "complete", ed)
    _, reg = rd.editar_cenas(s, {4: ["FACTUAL — ordem"]}, BANCO, "Svalbard", historico=h)
    assert reg["cenas_editadas"] == [5] and not reg["revertidas"]


def test_bloco_do_historico_poe_os_factuais_antes_dos_editoriais():
    h = _historico()
    rd.registrar_historico(h, 2, _roteiro(), [], BANCO, {"alvos": [
        {"criterio": f"c{k}", "cenas": [1, 2, 3, 4, 5], "descricao": f"editorial {k}"} for k in range(6)]})
    bloco = rd.bloco_historico(h, BANCO)
    assert INTEIRAS in bloco and SEMPRE in bloco and FAIXA in bloco
    assert bloco.count("editorial:") <= 4 and bloco.index(INTEIRAS) < bloco.index("editorial:")


def test_historico_de_um_job_nao_contamina_outro(monkeypatch):
    monkeypatch.setattr(rd, "_auditar_versao", _auditor_falso([[("excede", 1, "coleções inteiras")], []]))
    ed = Editor(ED((2, [F("Bancos de genes do Iraque e da Síria foram danificados pela guerra.", evid=["E1"])])))
    monkeypatch.setattr(providers, "complete", ed)
    rd.auditar_e_corrigir(_roteiro(), BANCO, "Svalbard")
    # outro job (outro tema): o primeiro pedido de edição não traz nada do job anterior
    monkeypatch.setattr(rd, "_auditar_versao", _auditor_falso([[("excede", 2, "a fonte não diz sempre")], []]))
    ed2 = Editor(ED((3, [F("Em 2016, a água do degelo entrou no túnel de entrada.", evid=["E2"])])))
    monkeypatch.setattr(providers, "complete", ed2)
    rd.auditar_e_corrigir(_roteiro(), BANCO, "Outro tema")
    assert INTEIRAS not in ed2.pedidos[0].split("ROTEIRO COMPLETO")[0]
    assert SEMPRE in ed2.pedidos[0].split("ROTEIRO COMPLETO")[0]


def test_prompts_do_escritor_e_do_editor_trazem_a_regra_de_precisao():
    assert "{regra_precisao}" in rd._PROMPT_FRASES and "{regra_precisao}" in rd._PROMPT_EDICAO
    for termo in ("quantificador", "frequência", "certeza", "extensão", "causa", "tempo", "unidades"):
        assert termo in rd._REGRA_PRECISAO
    assert "Nunca troque um exagero por outro equivalente" in rd._REGRA_PRECISAO
    assert "divergência não é faixa" in rd._REGRA_PRECISAO_CURTA


def test_trava_frequencia_job1008_reprovada_bloqueia_e_sustentada_pela_e38_passa():
    """Job 1008, cena 5: o "nunca correram risco" (E21, sem sustentação) entrou no histórico como exagero de
    frequência e a trava recusou a correção "vazamentos recorrentes", que a E38 sustenta ("recurrent minor leaks")."""
    e21 = _ev("E21", "Fortes chuvas em outubro de 2016 levaram a grande atenção da mídia na primavera de 2017.",
              "The heavy rainfall in October 2016 led to massive media attention in the spring 2017, and the partners "
              "decided to re-assure all seed depositors that the seeds in the Vault remained safe")
    e38 = _ev("E38", "Após 10 anos, o governo implementou melhorias devido a vazamentos menores no túnel no degelo.",
              "After 10 years of operation, the Norwegian Government decided to implement a number of improvements "
              "to the facility, due to recurrent minor leaks in the entrance tunnel during the snow melting season")
    banco = {**BANCO, "evidencias": [e21, e38]}
    nunca = F("A mídia explodiu em 2017, mas as sementes nunca correram risco.", evid=["E21"])
    s = {"title": "t", "scenes": [{"narration": nunca["texto"], "frases": [nunca]}]}
    h: list[dict] = []
    rd.registrar_historico(h, 1, s, [rd._p("leitura_enganosa", nunca["texto"], 0, 0, "ordem")], banco)
    assert h[0]["escalas"] == {"frequencia": "nunca"}
    # 1. a afirmação reprovada, sem sustentação, continua bloqueada (mesma frase com outras palavras)
    de_novo = F("Em 2017 a mídia explodiu, mas as sementes jamais correram risco.", evid=["E21"])
    assert "volta o exagero" in (rd.exagero_na_edicao([de_novo], [nunca], 0, banco, h) or "")
    # 2. a afirmação mais restrita, sustentada pela E38, passa
    restrita = F("Depois de 10 anos, o governo fez melhorias por causa de vazamentos menores e recorrentes no "
                 "túnel de entrada durante o degelo.", evid=["E38"])
    assert rd.exagero_na_edicao([restrita], [nunca], 0, banco, h) is None
