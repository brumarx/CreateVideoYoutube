"""Cascata resiliente (19/11): descanso pelo prazo do provedor, espera curta dentro da chamada,
indisponibilidade longa como ProvedoresIndisponiveis (retomada do job), resposta truncada nunca
aceita, cota por modelo e por organização; duração 2,5-5 min com formato pela duração; regressões
do job 996. Zero API e zero chamada paga: toda resposta HTTP é simulada."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import providers as pv  # noqa: E402
from src import redacao as rd  # noqa: E402
from src.config import LLMKeys  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
FORTE = "nvidia/nemotron-3-ultra-550b-a55b"
GROQ_FORTE = "openai/gpt-oss-120b"


def _chaves(**kw):
    base = dict(nvidia=[], xai=[], groq=[], cerebras=[], openrouter=[], mistral=[], gemini=[], cloudflare=[], deepseek=[])
    base.update(kw)
    return LLMKeys(**base)


@pytest.fixture(autouse=True)
def cascata_isolada(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "HEALTH_FILE", tmp_path / "health.json")
    monkeypatch.setattr(pv, "_rotators", {})
    monkeypatch.setattr(pv, "_fetch_models", lambda prov, url, key: list(pv._STRONG_MODELS.get(prov, ["m"])))
    esperas = []
    monkeypatch.setattr(pv.time, "sleep", esperas.append)
    return esperas


class Relogio:
    def __init__(self, monkeypatch):
        self.t = 1_800_000_000.0
        monkeypatch.setattr(pv.time, "time", lambda: self.t)

    def andar(self, s):
        self.t += s


# ======================================================================= descanso e espera
def test_chave_volta_depois_do_descanso_bug_do_relogio(monkeypatch):
    """O prazo (time.time) era comparado com time.monotonic: a chave que levou 429 ficava fora pelo
    resto do processo. Descanso vencido = chave usada de novo."""
    chamadas = []
    monkeypatch.setattr(pv, "_call_openai_compat", lambda ep, key, model, *a, **k: chamadas.append(key) or ("ok", 200, 0))
    k = _chaves(nvidia=["n1"])
    pv._rotator_for(k.nvidia)._resting["n1"] = time.time() - 5  # descanso já vencido
    assert pv.complete([{"role": "user", "content": "x"}], keys=k) == "ok"
    assert chamadas == ["n1"]


def test_429_curto_espera_o_prazo_e_tenta_de_novo(monkeypatch, cascata_isolada):
    rel = Relogio(monkeypatch)
    respostas = [(None, 429, 4), ("ok", 200, 0)]

    def falso(ep, key, model, *a, **k):
        r = respostas.pop(0)
        return r
    monkeypatch.setattr(pv, "_call_openai_compat", falso)
    monkeypatch.setattr(pv.time, "sleep", lambda s: (cascata_isolada.append(s), rel.andar(s)))
    assert pv.complete([{"role": "user", "content": "x"}], keys=_chaves(nvidia=["n1"])) == "ok"
    assert cascata_isolada and 4 <= cascata_isolada[0] <= 6  # nunca no mesmo segundo, nem 75 s fixos


def test_cota_diaria_nao_bloqueia_o_processo_e_informa_a_volta(monkeypatch, cascata_isolada):
    Relogio(monkeypatch)
    monkeypatch.setattr(pv, "_call_openai_compat", lambda *a, **k: (None, 429, 7200))
    with pytest.raises(pv.ProvedoresIndisponiveis) as exc:
        pv.complete([{"role": "user", "content": "x"}], keys=_chaves(nvidia=["n1"]))
    assert abs(exc.value.disponivel_em - (pv.time.time() + 7200)) < 2
    assert not cascata_isolada  # não dormiu duas horas


def test_indisponibilidade_simultanea_espera_o_primeiro_que_volta(monkeypatch, cascata_isolada):
    rel = Relogio(monkeypatch)
    monkeypatch.setattr(pv.time, "sleep", lambda s: (cascata_isolada.append(s), rel.andar(s)))
    estado = {"n1": [(None, 429, 30), ("ok-nvidia", 200, 0)], "g1": [(None, 429, 7200)]}
    monkeypatch.setattr(pv, "_call_openai_compat", lambda ep, key, model, *a, **k: estado[key].pop(0))
    r = pv.complete([{"role": "user", "content": "x"}], keys=_chaves(nvidia=["n1"], groq=["g1"]))
    assert r == "ok-nvidia" and 30 <= cascata_isolada[0] <= 32


def test_pedido_grande_demais_nao_vira_descanso_nem_espera(monkeypatch, cascata_isolada):
    monkeypatch.setattr(pv, "_call_openai_compat", lambda *a, **k: (None, 413, 0))
    with pytest.raises(RuntimeError) as exc:
        pv.complete([{"role": "user", "content": "x"}], keys=_chaves(nvidia=["n1"]))
    assert not isinstance(exc.value, pv.ProvedoresIndisponiveis)
    assert pv._rotator_for(["n1"]).ate("n1", FORTE) == 0 and not cascata_isolada


def test_groq_pulado_por_tamanho_aparece_no_diagnostico(monkeypatch):
    monkeypatch.setattr(pv, "_call_openai_compat", lambda *a, **k: (None, 500, 0))
    with pytest.raises(RuntimeError, match="pedido grande demais para groq"):
        pv._complete_cascata([{"role": "user", "content": "x" * 40000}], keys=_chaves(groq=["g1"], nvidia=["n1"]),
                             max_tokens=3000, _nova_tentativa=True)


@pytest.mark.parametrize("status,corpo,cab,esperado", [
    (429, "Rate limit reached (TPM): Limit 8000", {"retry-after": "7"}, 8),
    (429, "Rate limit reached ... on tokens per minute (TPM). Please try again in 2.5s.", {}, 4),
    (429, "Rate limit reached ... on tokens per minute (TPM)", {"x-ratelimit-reset-tokens": "547ms"}, 2),
    (429, "Rate limit reached ... on requests per day (RPD)", {"x-ratelimit-reset-requests": "1m26.4s"}, 88),
    (429, "Rate limit reached (TPM): Limit 8000", {}, 75),  # sem prazo do provedor: o padrão
])
def test_prazo_do_descanso_vem_do_provedor(status, corpo, cab, esperado):
    assert pv._cooldown_seconds(status, corpo, cab) == esperado


@pytest.mark.parametrize("status,corpo,classe", [
    (413, "Request too large for model x on tokens per minute (TPM)", "tamanho"),
    (429, "Request too large for model x on output tokens per minute (OTPM)", "tamanho"),
    (429, "Rate limit reached ... tokens per day (TPD)", "cota_diaria"),
    (429, "Rate limit reached ... tokens per minute (TPM)", "cota_minuto"),
    (401, "invalid api key", "config"), (404, "model not found", "config"),
    (402, "insufficient balance", "pagamento"), (503, "overloaded", "transitoria"), (None, "", "transitoria"),
])
def test_categorias_de_erro_nao_se_confundem(status, corpo, classe):
    assert pv.classe_do_erro(status, corpo) == classe


def test_diagnostico_registra_limites_sem_revelar_chave():
    corpo = json.dumps({"error": {"message": "Rate limit reached for model `openai/gpt-oss-120b` in organization "
                                             "`org_01kredea88fe19` on tokens per day (TPD). key gsk_SEGREDO123456789",
                                  "type": "tokens", "code": "rate_limit_exceeded"}})
    cab = {"retry-after": "120", "x-ratelimit-reset-tokens": "3m", "x-ratelimit-reset-requests": "1m26.4s",
           "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "0"}
    d = pv._diagnostico("https://api.groq.com/openai/v1/chat/completions", GROQ_FORTE, 429, corpo, cab, "gsk_SEGREDO123456789")
    texto = json.dumps({k: v for k, v in d.items() if not k.startswith("_")})
    assert "SEGREDO" not in texto and d["classe"] == "cota_diaria" and d["codigo"] == "rate_limit_exceeded"
    assert d["reset_tokens"] == "3m" and d["reset_requisicoes"] == "1m26.4s" and d["retry_after"] == "120"
    assert d["org"] == "org_01kr…" and d["provedor"] == "api.groq.com"


def test_cota_do_modelo_nao_derruba_outro_modelo_e_vale_para_a_organizacao():
    r = pv.KeyRotator(["a", "b", "c"])
    r.set_org("a", "org_X")
    r.set_org("b", "org_X")
    r.set_org("c", "org_Y")
    r.rest_model("a", GROQ_FORTE, 600)
    agora = time.time()
    assert r.ate("a", GROQ_FORTE) > agora and r.ate("b", GROQ_FORTE) > agora  # mesma organização
    assert r.ate("c", GROQ_FORTE) == 0                                        # outra organização
    assert r.ate("a", "openai/gpt-oss-20b") == 0                              # outro modelo segue
    assert pv.KeyRotator(["a", "b", "c"]).ate("b", GROQ_FORTE) > agora        # sobrevive a outro processo


class _Resp:
    def __init__(self, finish, conteudo='{"ok": 1}', status=200):
        self.status_code, self.text, self.headers = status, "", {}
        self._d = {"choices": [{"message": {"content": conteudo}, "finish_reason": finish}], "usage": {}}

    def json(self):
        return self._d


def test_resposta_truncada_nunca_e_aceita_e_tenta_com_mais_espaco(monkeypatch):
    pedidos = []
    monkeypatch.setattr(pv.httpx, "post", lambda url, headers, json, timeout: pedidos.append(json["max_tokens"]) or _Resp("length", '{"frases": ['))
    r = pv._call_openai_compat("https://api.groq.com/openai/v1/chat/completions", "k", GROQ_FORTE,
                               [{"role": "user", "content": "x" * 3000}], 2000, 0.1)
    assert r[0] is None and len(pedidos) == 2 and pedidos[1] > pedidos[0]
    assert pedidos[1] + 3000 / 3.7 <= pv._SMALL_TPM["groq"]  # a nova tentativa ainda cabe no limite do Groq


def test_truncada_no_provedor_pago_nao_repete(monkeypatch):
    monkeypatch.setattr(pv, "PROVEDOR_PAGO_LIGADO", True)  # comportamento se religada (hoje desligada)
    pedidos = []
    monkeypatch.setattr(pv.httpx, "post", lambda url, headers, json, timeout: pedidos.append(1) or _Resp("length"))
    monkeypatch.setattr(pv, "_registra_pago_uso", lambda *a: None)
    r = pv._call_openai_compat("https://api.deepseek.com/chat/completions", "k", "deepseek-flash",
                               [{"role": "user", "content": "x"}], 2000, 0.1)
    assert r[0] is None and len(pedidos) == 1


def test_auditor_e_extracao_cabem_no_groq_com_roteiro_de_5_minutos():
    """5 min = ~675 palavras: o pedido do auditor (frases + evidências citadas) + a resposta reservada
    cabe nos 8 mil tokens por pedido do Groq grátis."""
    frases = [[{"texto": "Palavra " * 14 + f"número {k}.", "tipo": "factual", "evidencias": [f"E{k}"]}] for k in range(1, 46)]
    banco = {"evidencias": [{"id": f"E{k}", "afirmacao": "Afirmação de tamanho médio sobre as estacas e o permafrost.",
                             "status": "oficial", "numeros": [], "conflitos": [],
                             "citacoes": [{"verificado": True, "dominio": "x.gov", "trecho": "t " * 100}]}
                            for k in range(1, 46)]}
    capturado = {}

    def medir(messages, **kw):
        capturado["grande"] = pv._too_big_for("groq", messages, kw["max_tokens"])
        return json.dumps({"frases": [], "qualidade": {"nota": 8, "problemas": []}})
    original = pv.complete
    pv.complete = medir
    try:
        rd.auditar_ia({"title": "t", "scenes": [{"frases": f, "narration": f[0]["texto"]} for f in frases]}, banco, "tema")
    finally:
        pv.complete = original
    assert capturado["grande"] is False


# ======================================================================= retomada
def test_rascunho_volta_do_cache_na_retomada_e_so_para_o_mesmo_banco():
    banco = {"evidencias": [{"id": "E1", "afirmacao": "a", "status": "oficial"}]}
    script = {"title": "t", "scenes": [{"narration": "x", "frases": []}], "_banco": banco}
    rd.guardar_rascunho("tema", banco, script)
    volta = rd.rascunho("tema", banco)
    assert volta["title"] == "t" and volta["_banco"] is banco
    outro = {"evidencias": [{"id": "E1", "afirmacao": "b", "status": "oficial"}]}
    assert rd.rascunho("tema", outro) is None


def _daily_run():
    spec = importlib.util.spec_from_file_location("dr_teste", RAIZ / "scripts" / "daily_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rodada_espera_volta_curta_e_retoma_o_mesmo_tema_sem_gastar_tentativa(monkeypatch):
    dr = _daily_run()
    agora = time.time()
    rcs = [dr.EXIT_INFRA, 0]
    cmds, esperas = [], []
    monkeypatch.setattr(dr, "_run_limited", lambda cmd, t: cmds.append(cmd) or rcs.pop(0))
    monkeypatch.setattr(dr, "_max_job_id", lambda: 0)
    monkeypatch.setattr(dr, "_last_job", lambda c, b: (999, "failed", None, "Yakutsk sobre estacas",
                                                        f"infraestrutura: x | retomar_em={int(agora + 120)}"))
    monkeypatch.setattr(dr, "update", lambda *a, **k: None)
    monkeypatch.setattr(dr.time, "sleep", esperas.append)
    dr._run_pipeline("curiosidades", [], "teste", max_total=1)
    assert len(cmds) == 2 and "--topic" in cmds[1] and "Yakutsk sobre estacas" in cmds[1]
    assert esperas and 100 <= esperas[0] <= 130


def test_rodada_agenda_volta_longa_para_o_fim_sem_bloquear(monkeypatch):
    dr = _daily_run()
    monkeypatch.setattr(dr, "_run_limited", lambda cmd, t: dr.EXIT_INFRA)
    monkeypatch.setattr(dr, "_max_job_id", lambda: 0)
    monkeypatch.setattr(dr, "_last_job", lambda c, b: (999, "failed", None, "Yakutsk",
                                                        f"infraestrutura | retomar_em={int(time.time() + 3 * 3600)}"))
    monkeypatch.setattr(dr, "update", lambda *a, **k: None)
    esperas = []
    monkeypatch.setattr(dr.time, "sleep", esperas.append)
    dr._run_pipeline("curiosidades", [], "teste")
    assert not esperas and dr.ADIADOS and dr.ADIADOS[0][1:] == ("curiosidades", "Yakutsk")


def test_pipeline_sai_com_codigo_de_infraestrutura_e_guarda_a_hora_de_retomar():
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    assert "EXIT_INFRA = 4" in src and "sys.exit(EXIT_INFRA)" in src
    assert "retomar_em={int(exc.disponivel_em or 0)}" in src
    i = src.index("isinstance(exc, ProvedoresIndisponiveis):\n            update(")
    assert "rmtree" not in src[i:i + 400]  # o trabalho do job não é apagado


# ======================================================================= duração 2,5-5 min
def test_meta_de_duracao_do_fractal():
    from src.config import ChannelConfig
    c = ChannelConfig.load("curiosidades")
    assert (c.short_min_minutes, c.short_max_minutes, c.formato_pela_duracao) == (2.5, 5, True)


def test_duracao_real_decide_e_nao_preenche():
    spec = importlib.util.spec_from_file_location("rp_dur", RAIZ / "scripts" / "run_pipeline.py")
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    ns: dict = {}
    inicio = src.index("def _duracao_fora(")
    exec(src[inicio:src.index("\n\n\ndef ", inicio)], ns)  # só a função pura
    fora = ns["_duracao_fora"]
    assert fora(100, 2.5, 5)            # 1min40: história curta demais (não se preenche)
    assert fora(160, 2.5, 5) is None    # 2min40: Short
    assert fora(290, 2.5, 5) is None    # 4min50: vídeo normal
    assert fora(400, 2.5, 5)            # 6min40: passou da meta
    assert spec is not None


def _pipeline_mod():
    spec = importlib.util.spec_from_file_location("rp_formato", RAIZ / "scripts" / "run_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("seg,formato,esperado", [
    (160, "vertical", (None, False, 1080, 1920)),  # 2min40: Short vertical
    (175, "vertical", (None, True, 1080, 1920)),   # previsão pela voz: normal, mas continua 9:16
    (290, "vertical", (None, True, 1080, 1920)),   # 4min50: vídeo normal vertical
    (171, "16:9", (None, True, 1920, 1080)),       # 16:9 só se o canal escolher de propósito
])
def test_formato_pela_duracao_real_e_configuravel(seg, formato, esperado):
    assert _pipeline_mod()._formato_pela_duracao(seg, 2.5, 5, formato) == esperado


# ======================================================================= 20/11: vertical em toda a faixa
def test_fractal_fica_vertical_em_toda_a_faixa_de_2_5_a_5_min():
    from src.config import ChannelConfig
    c = ChannelConfig.load("curiosidades")
    assert c.formato_acima_do_short == "vertical"
    mod = _pipeline_mod()
    for seg in range(150, 301, 5):  # 2,5 a 5 min de voz
        _, _, w, h = mod._formato_pela_duracao(seg, 2.5, 5, c.formato_acima_do_short)
        assert (w, h) == (1080, 1920), seg


def test_padrao_do_formato_acima_do_short_e_vertical(tmp_path, monkeypatch):
    mod = _pipeline_mod()
    assert mod._formato_pela_duracao(290, 2.5, 5)[2:] == (1080, 1920)  # sem argumento: vertical
    from src import config
    assert config.ChannelConfig.__dataclass_fields__["formato_acima_do_short"].default == "vertical"


@pytest.mark.parametrize("seg,w,h,normal", [
    (150.0, 1080, 1920, False),   # 2min30 vertical: Short
    (180.0, 1080, 1920, False),   # exatamente 3 min: ainda Short (critério do YouTube)
    (180.4, 1080, 1920, True),    # passou de 3 min: vídeo normal vertical
    (299.0, 1080, 1920, True),    # 4min59: vídeo normal vertical
    (150.0, 1920, 1080, True),    # horizontal nunca é Short
])
def test_classificacao_pela_duracao_do_arquivo_final(seg, w, h, normal):
    assert _pipeline_mod()._e_video_normal_final(seg, w, h) is normal


def test_classificacao_final_usa_o_arquivo_montado_e_decide_o_shorts():
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    i = src.index("dur_final = _ffprobe_duration(final_video)")
    j = src.index("normal_final = _e_video_normal_final(dur_final, width, height)")
    k = src.index('hashtags = (["#Shorts"] if not long_form else [])')
    c = src.index("if long_form:\n            # timestamps")
    assert i < j < c < k  # a duração REAL do arquivo final decide antes dos capítulos e do #Shorts
    assert "long_form = normal_final" in src[j:j + 600]
    # vertical acima de 3 min não é mais reprovado como "Short longo demais": vira vídeo normal vertical
    assert src.index("elif (dur_final > SHORTS_MAX_SECONDS + 10) and not long_form:") > j


def test_mais_de_3_minutos_nao_acelera_nem_corta_e_sai_sem_shorts():
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    # a decisão pela duração vem ANTES do ajuste do Short: acima de 170 s já é vídeo normal e o
    # _fit_short (aceleração da voz) não roda
    assert src.index("_formato_pela_duracao(\n                total_seconds") < src.index("_fit_short(narrations, total_seconds, job_id)")
    assert "if not long_form and total_seconds > SHORTS_MAX_SECONDS:\n            narrations = _fit_short" in src
    assert '(["#Shorts"] if not long_form else [])' in src


def test_escritor_pede_338_a_675_palavras(monkeypatch):
    pedidos = []

    def falso(messages, **kw):
        pedidos.append(messages[-1]["content"])
        return json.dumps({"titulo": "t", "cenas": [{"frases": [{"texto": "Mas por quê?", "tipo": "narrativa"}]}]})
    monkeypatch.setattr(pv, "complete", falso)
    from src.config import ChannelConfig
    rd.escrever(ChannelConfig.load("curiosidades"), "tema", {"evidencias": [], "lacunas": []},
                min_minutes=2.5, max_minutes=5, cenas_ref=12, tentativas=1)
    assert "entre 304 e 675\npalavras" in pedidos[0] and "entre 8 e 20 cenas" in pedidos[0]


# ======================================================================= regressão job 996
BANCO = {"evidencias": [
    {"id": "E1", "afirmacao": "Yakutsk possui 303.863 habitantes.", "status": "fonte_unica", "numeros": ["303863"],
     "conflitos": [], "citacoes": [{"verificado": True, "trecho": "Possui 303 863 habitantes", "dominio": "pt.wikipedia.org"}]}]}


def _s(*frases):
    return {"scenes": [{"frases": [{"texto": t, "tipo": "factual", "evidencias": ["E1"]} for t in frases]}]}


def test_996_numero_arredondado_para_a_fala_com_cerca_de_passa():
    _, p = rd.auditar_codigo(_s("Ali vivem cerca de 300 mil pessoas."), BANCO)
    assert p == []
    _, p = rd.auditar_codigo(_s("Ali vivem 300 mil pessoas."), BANCO)  # sem dizer que é aproximado
    assert p and p[0]["tipo"] == "numero_sem_evidencia"
    _, p = rd.auditar_codigo(_s("Ali vivem cerca de 200 mil pessoas."), BANCO)  # longe demais
    assert p


def test_996_populacao_repetida_e_defeito_do_escritor():
    s = {"scenes": [{"narration": "Vivem cerca de 300 mil pessoas ali.", "frases": [
        {"texto": "Vivem cerca de 300 mil pessoas ali.", "tipo": "factual", "evidencias": ["E1"]}]},
        {"narration": "Com seus cerca de 300 mil habitantes, é única.", "frases": [
            {"texto": "Com seus cerca de 300 mil habitantes, é única.", "tipo": "factual", "evidencias": ["E1"]}]}]}
    _, defeitos, _ = rd.defeitos(s, BANCO, "Yakutsk", 1, 999, 1, 99)
    assert any("aparece em 2 frases" in d for d in defeitos)


def test_996_historia_boa_com_frase_sem_prova_vai_a_edicao_so_da_cena_dela(monkeypatch):
    """21/11: a frase sem prova vai para a edição por cena (a única correção), só a cena dela e com a
    reprovação do auditor; a história no alvo (nota 9) não acrescenta motivo editorial."""
    edicoes = []
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([{"tipo": "excede", "termo": "Mas por quê?", "motivo": "m",
                                                                  "cena": 0, "frase": 0}], {"nota": 9, "problemas": []})
                        if not edicoes else ([], {"nota": 9, "problemas": []}))
    s = {"scenes": [{"narration": "Mas por quê?", "frases": [{"texto": "Mas por quê?", "tipo": "narrativa", "evidencias": []}]}]}
    novo = {"scenes": [{"narration": "E então?", "frases": [{"texto": "E então?", "tipo": "narrativa", "evidencias": []}]}]}
    monkeypatch.setattr(rd, "editar_cenas", lambda sc, alvos, *a, **k: edicoes.append((alvos, k)) or
                        (novo, {"cenas_editadas": [1], "titulo": None}))
    final, rel = rd.auditar_e_corrigir(s, BANCO, "Yakutsk")
    assert len(edicoes) == 1 and list(edicoes[0][0]) == [0] and edicoes[0][0][0][0].startswith("FACTUAL")
    assert edicoes[0][1]["problemas"][0]["tipo"] == "excede" and final["scenes"][0]["narration"] == "E então?"


# ======================================================================= job 997: escritor forte e edição
def test_997_escritor_so_com_modelo_forte_e_pedido_cabe_no_groq(monkeypatch):
    capt = {}

    def falso(messages, **kw):
        capt["filtro"] = kw["model_filter"]
        capt["grande"] = pv._too_big_for("groq", messages, kw["max_tokens"])
        return json.dumps({"titulo": "t", "cenas": [{"frases": [{"texto": "Mas por quê?", "tipo": "narrativa"}]}]})
    monkeypatch.setattr(pv, "complete", falso)
    from src.config import ChannelConfig
    banco = {"lacunas": [], "evidencias": [
        {"id": f"E{k}", "afirmacao": "Afirmação verificada de tamanho médio sobre Yakutsk e as estacas no permafrost.",
         "status": "oficial", "numeros": [], "conflitos": [],
         "citacoes": [{"verificado": True, "dominio": "x.gov", "trecho": "t " * 150}]} for k in range(1, 51)]}
    rd.escrever(ChannelConfig.load("curiosidades"), "tema", banco, min_minutes=2.5, max_minutes=5, cenas_ref=12, tentativas=1)
    assert capt["filtro"]("openai/gpt-oss-120b") and not capt["filtro"]("@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    assert capt["grande"] is False


def _roteiro_simples(txt):
    return {"scenes": [{"narration": txt, "frases": [{"texto": txt, "tipo": "narrativa", "evidencias": []}]}]}


def _editor_falso(novo, chamadas=None):
    def editar(s, alvos, b, t, **k):
        if chamadas is not None:
            chamadas.append((s, alvos))
        return novo, {"cenas_editadas": [1], "titulo": None}
    return editar


def test_997_nota_abaixo_do_alvo_faz_edicao_por_cena_e_fica_a_melhor(monkeypatch):
    notas = iter([7.0, 9.0])
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([], {"nota": next(notas), "problemas": ["repete"],
                                                                     "alvos": [{"criterio": "novidade", "cenas": [1],
                                                                                "descricao": "repete"}]}))
    chamadas = []
    monkeypatch.setattr(rd, "editar_cenas", _editor_falso(_roteiro_simples("Versão editada."), chamadas))
    final, rel = rd.auditar_e_corrigir(_roteiro_simples("Versão original."), {"evidencias": []}, "t")
    s, alvos = chamadas[0]
    assert s["scenes"][0]["narration"] == "Versão original." and alvos == {0: ["HISTÓRIA — novidade: repete"]}  # edita a cena 1
    assert final["scenes"][0]["narration"] == "Versão editada." and rel["qualidade"]["nota"] == 9.0
    assert [v["origem"] for v in rel["versoes"]] == ["escrita", "edicao"] and rel["versao_final"] == 2


def test_997_edicao_que_piora_fica_a_versao_anterior(monkeypatch):
    notas = iter([7.5, 6.0, 6.2])  # 20/11: até 2 edições; nenhuma melhora, fica a original (com a nota DELA)
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([], {"nota": next(notas), "problemas": [],
                                                                     "alvos": [{"criterio": "x", "cenas": [1], "descricao": "y"}]}))
    monkeypatch.setattr(rd, "editar_cenas", _editor_falso(_roteiro_simples("Pior.")))
    final, rel = rd.auditar_e_corrigir(_roteiro_simples("Original."), {"evidencias": []}, "t")
    assert final["scenes"][0]["narration"] == "Original." and rel["qualidade"]["nota"] == 7.5 and rel["versao_final"] == 1


def test_997_nota_abaixo_do_minimo_reprova(monkeypatch):
    monkeypatch.setattr(rd, "auditar_ia", lambda s, b, t, *_: ([], {"nota": 6.5, "problemas": ["rasa"]}))
    with pytest.raises(rd.RoteiroReprovado, match="qualidade narrativa"):
        rd.auditar_e_corrigir(_roteiro_simples("x."), {"evidencias": []}, "t")


# ======================================================================= 20/11: DeepSeek fora da cascata
def test_deepseek_esta_desligada_no_codigo():
    assert pv.PROVEDOR_PAGO_LIGADO is False
    # nenhuma variável de ambiente ou yaml religa: a constante não é lida de fora
    src = (RAIZ / "src" / "providers.py").read_text()
    assert "PROVEDOR_PAGO_LIGADO = False" in src
    assert "getenv(\"PROVEDOR_PAGO" not in src and "environ" not in src.split("PROVEDOR_PAGO_LIGADO = False")[1][:200]


def test_cascata_nunca_chama_a_deepseek_mesmo_com_os_gratis_falhando(monkeypatch):
    chamados = []

    def falso(ep, key, model, *a, **k):
        chamados.append(ep)
        return None, 500, 0  # todo grátis fora do ar
    monkeypatch.setattr(pv, "_call_openai_compat", falso)
    monkeypatch.setattr(pv, "_health_update", lambda *a, **k: None)
    k = _chaves(nvidia=["n1"], deepseek=["sk-deepseek-de-verdade"])
    with pytest.raises(RuntimeError):
        pv.complete([{"role": "user", "content": "x"}], keys=k)
    assert chamados and not any("deepseek" in ep for ep in chamados)


def test_so_com_chave_deepseek_a_cascata_falha_sem_chamar(monkeypatch):
    chamados = []
    monkeypatch.setattr(pv, "_call_openai_compat", lambda ep, *a, **k: chamados.append(ep) or ("pago", 200, 0))
    with pytest.raises(RuntimeError):
        pv.complete([{"role": "user", "content": "x"}], keys=_chaves(deepseek=["sk-deepseek"]))
    assert chamados == []
    assert pv.proxima_disponibilidade(_chaves(deepseek=["sk-deepseek"])) is None


def test_ultima_barreira_nenhuma_requisicao_http_para_a_api_paga(monkeypatch):
    posts = []
    monkeypatch.setattr(pv.httpx, "post", lambda *a, **k: posts.append(a) or None)
    with pytest.raises(pv.ProvedorPagoDesligado):
        pv._call_openai_compat("https://api.deepseek.com/chat/completions", "sk", "deepseek-flash",
                               [{"role": "user", "content": "x"}], 100, 0.1)
    assert posts == []


# ======================================================================= 20/11: respostas de erro REAIS (logs de 19/11)
@pytest.mark.parametrize("status,corpo,classe", [
    (429, '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization '
          '`org_01kredea88fe19qzvzfn0nzas6` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199505"}}',
     "cota_diaria"),
    (429, '{"error":{"message":"Request too large for model `qwen/qwen3.8-27b` in organization `org_01kn6t1e1veqvty4g9vzqsydx6`"}}',
     "tamanho"),
    (413, '{"error":{"message":"Request too large for model `openai/gpt-oss-120b` in organization `org_01m5a8shstejvvx13p8cct7206`"}}',
     "tamanho"),
    (400, '{"error":{"message":"Please reduce the length of the messages or completion.","type":"invalid_request_error"}}',
     "tamanho"),
    (429, '{"status":429,"title":"Too Many Requests"}', "cota_minuto"),
    (429, '{"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests"}}',
     "cota_diaria"),
    (429, '{"errors":[{"message":"AiError: AiError: you have used up your daily free allocation of 10,000 neurons"}]}',
     "cota_diaria"),
    (402, '{"message":"Payment required to access this resource.","type":"payment_required_error"}', "pagamento"),
    (503, '{"message":"Service temporarily overloaded","type":"Overloaded","code":503}', "transitoria"),
])
def test_classificacao_com_respostas_reais_dos_provedores(status, corpo, classe):
    assert pv.classe_do_erro(status, corpo) == classe


def test_organizacao_do_groq_sai_da_mensagem_real():
    corpo = ('{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization '
             '`org_01kredea88fe19qzvzfn0nzas6` service tier `on_demand` on tokens per day (TPD)"}}')
    d = pv._diagnostico("https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b", 429, corpo, {}, "k")
    assert d["_org_completa"] == "org_01kredea88fe19qzvzfn0nzas6" and d["classe"] == "cota_diaria"


# ======================================================================= 20/11: organização do Groq pela cascata real
class _RespErro:
    def __init__(self, status, corpo):
        self.status_code, self.text, self.headers = status, corpo, {}

    def json(self):
        return json.loads(self.text)


def _tpd(org):
    return ('{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization `' + org +
            '` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199505, Requested 900. '
            'Please try again in 7m30s."}}')


def test_organizacao_aprendida_da_resposta_agrupa_so_o_que_o_provedor_disse(monkeypatch, tmp_path):
    """Chaves a e b: o Groq diz que as duas são da org_X (cada uma no seu erro); c nunca deu erro: fica
    sozinha (organização desconhecida não é inventada). O descanso do modelo vale para a e b, não para c.
    O arquivo de saúde guarda organização e descanso por hash da chave — nenhuma chave aparece nele."""
    chaves = ["gsk_chave_A_secreta_000000000000", "gsk_chave_B_secreta_111111111111", "gsk_chave_C_secreta_222222222222"]
    respostas = {chaves[0]: _RespErro(429, _tpd("org_01kredea88fe19qzvzfn0nzas6")),
                 chaves[1]: _RespErro(429, _tpd("org_01kredea88fe19qzvzfn0nzas6"))}

    def post(url, headers, json, timeout):
        chave = headers["Authorization"].split()[-1]
        if json["model"] == GROQ_FORTE and chave in respostas:
            return respostas[chave]
        return _Resp("stop", "ok")
    monkeypatch.setattr(pv.httpx, "post", post)
    k = _chaves(groq=chaves)
    assert pv.complete([{"role": "user", "content": "x"}], keys=k, model_filter=lambda m: True) == "ok"
    rot = pv._rotator_for(k.groq)
    agora = time.time()
    assert rot._org.get(chaves[0]) == rot._org.get(chaves[1]) == "org_01kredea88fe19qzvzfn0nzas6"
    assert chaves[2] not in rot._org                                        # desconhecida: sem agrupamento
    assert rot.ate(chaves[0], GROQ_FORTE) > agora and rot.ate(chaves[1], GROQ_FORTE) > agora
    assert rot.ate(chaves[2], GROQ_FORTE) <= agora                          # c segue disponível
    assert rot.ate(chaves[0], "openai/gpt-oss-20b") <= agora                # o outro modelo da chave também
    saude = pv.HEALTH_FILE.read_text()
    assert not any(c in saude or c[-8:] in saude for c in chaves)           # nenhuma credencial no arquivo
    dados = json.loads(saude)
    a = dados[pv._key_id(chaves[0])]
    assert a["org"] == "org_01kredea88fe19qzvzfn0nzas6" and a["org_visto_em"] > 0 and GROQ_FORTE in a["modelos"]
    assert "org" not in dados.get(pv._key_id(chaves[2]), {})


def test_organizacao_desconhecida_descansa_so_a_propria_chave():
    r = pv._rotator_for(["k1", "k2"])
    r.rest_model("k1", GROQ_FORTE, 600)
    agora = time.time()
    assert r.ate("k1", GROQ_FORTE) > agora and r.ate("k2", GROQ_FORTE) <= agora
