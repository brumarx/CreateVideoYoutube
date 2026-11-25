"""25/11: pesquisa de temas pelo que o público já provou (src/pesquisa_temas.py) e o gancho no pick_topic.
Sem rede: yt-dlp e HTTP são trocados por respostas montadas aqui."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import pesquisa_temas as pt
from src import topics

CANAL_A = "https://www.youtube.com/channel/UCaaa"
CANAL_B = "https://www.youtube.com/channel/UCbbb"


def _entrada(vid, titulo, views, canal="Canal A", url=CANAL_A, duracao=50):
    return {"id": vid, "title": titulo, "view_count": views, "channel": canal, "channel_url": url,
            "url": f"https://www.youtube.com/shorts/{vid}", "duration": duracao}


def _aba(views, canal="Canal A", inscritos=100_000):
    return {"channel": canal, "channel_follower_count": inscritos,
            "entries": [{"id": f"r{i}", "title": f"recente {i}", "view_count": v} for i, v in enumerate(views)]}


@pytest.fixture
def yt_falso(monkeypatch):
    chamadas = []
    respostas = {}

    def extrair(url, limite, flat=True):
        chamadas.append(url)
        for chave, resp in respostas.items():
            if chave in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise RuntimeError(f"url inesperada {url}")

    monkeypatch.setattr(pt, "_extrair", extrair)
    return respostas, chamadas


@pytest.fixture
def http_falso(monkeypatch):
    chamadas = []

    def get_json(url, params):
        chamadas.append((url, dict(params)))
        if "suggestqueries" in url:
            q = params["q"]
            base = ["megaprojetos dubai", "megaprojetos palm jumeirah"]
            return [q, base + ([f"{q} extra"] if q.endswith(" a") else [])]
        if "oembed" in url:
            raise RuntimeError("oembed fora")  # fica o título da busca
        if "wikipedia" in url:
            return {"query": {"search": [{"title": "Palm Jumeirah", "snippet": "ilha artificial em <b>Dubai</b>"}]}}
        raise RuntimeError(url)

    monkeypatch.setattr(pt, "_get_json", get_json)
    monkeypatch.setattr(pt.time, "sleep", lambda s: None)
    return chamadas


def test_busca_le_views_numericas_e_descarta_sem_contagem(yt_falso):
    respostas, chamadas = yt_falso
    respostas["results?search_query="] = {"entries": [
        _entrada("v1", "Palm Jumeirah", 500_000), {"id": "v2", "title": "sem views", "view_count": None}]}
    vids = pt.buscar_videos("megaprojetos", "shorts", 30)
    assert [v["id"] for v in vids] == ["v1"]
    assert vids[0]["views"] == 500_000
    # leitura em inglês (contagem numérica, nunca "1,3 mi") e filtro de Shorts
    assert all("hl=en" in c for c in chamadas)
    assert "sp=EgQIBRAB" in chamadas[0] and "sp=EgIQCQ" in chamadas[1]  # vídeos do último ano + reforço de Shorts
    assert pt._YDL_OPTS["http_headers"]["Accept-Language"].startswith("en")


def test_outliers_score_pela_mediana_do_canal_e_teto_de_canais(yt_falso, http_falso, monkeypatch):
    respostas, chamadas = yt_falso
    respostas["results?search_query="] = {"entries": [
        _entrada("v1", "COMO DUBAI CRIOU ILHAS NO MAR #shorts 🤯", 2_000_000),
        _entrada("v2", "Vídeo normal do canal A", 60_000),
        _entrada("v3", "Pouca view", 9_000),
        _entrada("v4", "Canal B grande", 900_000, "Canal B", CANAL_B),
    ]}
    respostas["UCaaa/shorts"] = _aba([50_000, 40_000, 60_000, 55_000, 45_000, 52_000])  # mediana 51.000
    respostas["UCbbb/shorts"] = _aba([800_000] * 6, "Canal B")
    respostas["watch?v="] = {"upload_date": (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y%m%d")}
    out = pt.outliers(["megaprojetos"], "shorts", 30, max_canais=5)
    assert [o["url"].rsplit("/", 1)[1] for o in out] == ["v1"]  # 39x; v2 ~1x; v3 < 10k; B ~1x
    o = out[0]
    assert o["score"] == pytest.approx(2_000_000 / 51_000, abs=0.1)
    assert o["muito_forte"] is True
    assert o["views_por_inscrito"] == 20.0
    assert o["views_por_dia"] == pytest.approx(200_000, rel=0.15)
    # o título do outlier fica guardado, mas o assunto vem limpo (sem hashtag, emoji e gritaria)
    assert o["titulo_outlier"].startswith("COMO DUBAI")
    assert o["assunto"] == "Como dubai criou ilhas no mar"
    # teto de canais: só o canal de maior alcance é lido
    chamadas.clear()
    monkeypatch.setattr(pt.disk_cache, "BASE", pt.disk_cache.BASE.parent / "outro_cache")
    pt.outliers(["megaprojetos"], "shorts", 30, max_canais=1, top_com_data=0)
    assert [c for c in chamadas if "/shorts" in c and "results" not in c] == [CANAL_A + "/shorts"]


def test_canal_sem_aba_de_shorts_usa_videos_e_mediana_fica_no_cache(yt_falso):
    respostas, chamadas = yt_falso
    respostas["UCaaa/shorts"] = RuntimeError("This channel does not have a shorts tab")
    respostas["UCaaa/videos"] = _aba([10, 20, 30, 40, 50])
    est = pt.estatistica_do_canal(CANAL_A + "/videos", "shorts")
    assert est["aba"] == "/videos" and est["mediana"] == 30
    n = len(chamadas)
    assert pt.estatistica_do_canal(CANAL_A, "shorts")["mediana"] == 30
    assert len(chamadas) == n  # segunda vez veio do disco


def test_canal_com_poucos_envios_nao_tem_mediana(yt_falso):
    respostas, _ = yt_falso
    respostas["UCaaa/videos"] = _aba([10, 20])
    assert pt.estatistica_do_canal(CANAL_A, "videos") is None


def test_demanda_conta_expansoes_e_rapido_pula_letras(http_falso, monkeypatch):
    mapa = pt.demanda("megaprojetos", rapido=False)
    assert mapa["megaprojetos dubai"] == 1 + 26 + 3
    assert mapa["megaprojetos a extra"] == 1
    n = len(http_falso)
    assert n == 30
    http_falso.clear()
    monkeypatch.setattr(pt.disk_cache, "BASE", pt.disk_cache.BASE.parent / "cache_rapido")
    pt.demanda("megaprojetos", rapido=True)
    assert [p["q"] for _, p in http_falso] == ["megaprojetos", "por que megaprojetos", "como megaprojetos",
                                               "o que megaprojetos"]
    assert pt.demanda_do_assunto("Como Dubai criou ilhas", "megaprojetos", mapa) == 30
    assert pt.demanda_do_assunto("Ponte na Noruega", "megaprojetos", mapa) == 0


def test_assunto_do_titulo():
    assert pt.assunto_do_titulo("O SEGREDO DO JAPÃO!!! #shorts #curiosidades") == "O segredo do japão"
    assert pt.assunto_do_titulo("Ponte Hong Kong-Macau | Mega Canal", "Mega Canal") == "Ponte Hong Kong-Macau"
    assert pt.assunto_do_titulo("Túnel Seikan (parte 2) 🚄") == "Túnel Seikan"


def _grava(tmp, canal, candidatos, dias=1):
    pt.DIR.mkdir(parents=True, exist_ok=True)
    gerado = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    (pt.DIR / f"{canal}.json").write_text(json.dumps({"gerado_em": gerado, "candidatos": candidatos}))


def _cand(assunto, wiki=None, score=10.0):
    return {"assunto": assunto, "titulo_outlier": assunto.upper() + "!!", "url": "u", "canal": "X", "score": score,
            "wikipedia": wiki}


def test_pesquisar_canal_grava_json_ordenado(yt_falso, http_falso):
    respostas, _ = yt_falso
    respostas["results?search_query="] = {"entries": [_entrada("v1", "Como Dubai criou ilhas no mar", 2_000_000)]}
    respostas["UCaaa/shorts"] = _aba([50_000] * 6)
    respostas["watch?v="] = RuntimeError("sem js")
    r = pt.pesquisar_canal("curiosidades", {"buscas": ["megaprojetos", "outra"], "formato": "shorts"},
                           max_buscas=1, rapido=True, comentarios={"lidos": 0, "pedidos": [], "palavras": []})
    gravado = json.loads((pt.DIR / "curiosidades.json").read_text())
    assert gravado["buscas"] == ["megaprojetos"]
    c = gravado["candidatos"][0]
    assert c["score"] == 40.0 and c["demanda"] > 0 and c["rank"] > c["score"]
    assert c["data"] is None and c["views_por_dia"] is None  # sem data, sem views/dia, sem quebrar
    assert c["wikipedia"] == "pt:Palm Jumeirah" or c["wikipedia"] is None
    assert "comentarios" in gravado and r["candidatos"] == gravado["candidatos"]


def test_candidatos_do_publico_validade_e_config(tmp_path):
    _grava(tmp_path, "curiosidades", [_cand("Palm Jumeirah em Dubai")])
    assert [c["assunto"] for c in pt.candidatos_do_publico("curiosidades")] == ["Palm Jumeirah em Dubai"]
    _grava(tmp_path, "curiosidades", [_cand("Palm Jumeirah em Dubai")], dias=15)
    assert pt.candidatos_do_publico("curiosidades") == []  # venceu
    _grava(tmp_path, "botafogo", [_cand("Palm Jumeirah em Dubai")])
    assert pt.candidatos_do_publico("botafogo") == []  # canal sem pesquisa_temas no yaml
    assert pt.candidatos_do_publico("canal_que_nao_existe") == []


@pytest.fixture
def pick_isolado(tmp_path, monkeypatch):
    monkeypatch.setattr(topics, "STATE_FILE", tmp_path / "used.json")
    monkeypatch.setattr(topics, "_append_topic_to_yaml", lambda *a: None)
    monkeypatch.setattr(topics.idea_sources, "load_ideas", lambda c: [])
    monkeypatch.setattr(topics, "topic_problem", lambda t, n, strict=True: None)
    monkeypatch.setattr(pt, "parcela_do_publico", lambda canal: 1.0)  # 25/11: o yaml real usa 0,3 (sorteio)


def test_pick_topic_prefere_o_publico_e_respeita_usados(tmp_path, pick_isolado):
    _grava(tmp_path, "curiosidades", [_cand("Como Dubai criou a Palm Jumeirah", score=40),
                                      _cand("O túnel Seikan sob o mar do Japão", score=8)])
    topics._save({"curiosidades": ["Por que a Palm Jumeirah de Dubai afunda"]})
    t = topics.pick_topic("curiosidades", ["tema da fila fixa qualquer"], "nicho")
    assert t == "O túnel Seikan sob o mar do Japão"  # o 1º é parecido com um usado
    assert topics._load()["curiosidades"][-1] == t
    # o seguinte já foi usado: volta pra fila fixa
    assert topics.pick_topic("curiosidades", ["tema da fila fixa qualquer"], "nicho") == "tema da fila fixa qualquer"


def test_pick_topic_serie_vem_antes_e_redacao_prefere_wikipedia(tmp_path, pick_isolado):
    _grava(tmp_path, "curiosidades", [_cand("Mistério sem artigo nenhum aqui", score=50),
                                      _cand("Como Dubai criou a Palm Jumeirah", wiki="pt:Palm Jumeirah", score=9)])
    assert topics.pick_topic("curiosidades", ["fila"], "nicho", priority=["Tema da série em teste"]) \
        == "Tema da série em teste"
    assert topics.pick_topic("curiosidades", ["fila"], "nicho", preferir_site_oficial=False) \
        == "Mistério sem artigo nenhum aqui"
    topics._save({})
    assert topics.pick_topic("curiosidades", ["fila"], "nicho", preferir_site_oficial=True) \
        == "Como Dubai criou a Palm Jumeirah"


def test_pick_topic_candidato_fora_do_nicho_cai_na_fila(tmp_path, pick_isolado, monkeypatch):
    _grava(tmp_path, "curiosidades", [_cand("Receita de bolo de cenoura fofinho")])
    monkeypatch.setattr(topics, "topic_problem", lambda t, n, strict=True: "fora do nicho")
    assert topics.pick_topic("curiosidades", ["tema da fila fixa qualquer"], "nicho") == "tema da fila fixa qualquer"


def test_pick_topic_sem_pesquisa_nao_muda_nada(pick_isolado, monkeypatch):
    monkeypatch.setattr(topics.random, "choice", lambda seq: seq[0])
    assert topics.pick_topic("curiosidades", ["tema da fila fixa qualquer"], "nicho") == "tema da fila fixa qualquer"


def test_pedidos_dos_comentarios():
    r = pt.extrair_pedidos([
        "Faz um vídeo sobre o túnel Seikan!", "Muito bom", "E o túnel do Canal da Mancha?",
        "podia falar do túnel Gotthard", "top"])
    assert len(r["pedidos"]) == 3
    assert ["tunel", 3] in r["palavras"]


def test_comentarios_sem_escopo_fica_desligado(monkeypatch):
    monkeypatch.setattr(pt, "_servico_youtube", lambda canal: None)
    assert pt.ideias_dos_comentarios("curiosidades") is None


def test_comentarios_le_threads_e_ignora_o_proprio_canal(monkeypatch, tmp_path):
    from src import yt_quota
    monkeypatch.setattr(yt_quota, "LEDGER", tmp_path / "quota.json")

    class Req:
        def __init__(self, r):
            self.r = r

        def execute(self):
            return self.r

    def thread(txt, autor):
        return {"snippet": {"topLevelComment": {"snippet": {"textDisplay": txt, "authorChannelId": {"value": autor}}}}}

    class YT:
        def channels(self):
            return type("C", (), {"list": lambda s, **k: Req({"items": [{"id": "UCnosso"}]})})()

        def commentThreads(self):
            return type("T", (), {"list": lambda s, **k: Req({"items": [
                thread("fala sobre o Seikan?", "UCfa"), thread("Qual seu túnel favorito?", "UCnosso")]})})()

    monkeypatch.setattr(pt, "_servico_youtube", lambda canal: YT())
    r = pt.ideias_dos_comentarios("curiosidades", paginas=1)
    assert r["lidos"] == 1 and r["pedidos"] == ["fala sobre o Seikan?"]
    assert json.loads((tmp_path / "quota.json").read_text())  # gastou (pouca) cota registrada


def test_c4_desligado():
    assert pt.mecanismo_com_objeto_trocado(_cand("x"), "nicho", []) is None
    assert "{titulo_outlier}" in pt.PROMPT_MECANISMO


def test_video_longo_usa_a_mediana_da_aba_videos_e_titulo_original(yt_falso, monkeypatch):
    respostas, chamadas = yt_falso
    respostas["results?search_query="] = {"entries": [
        _entrada("v1", "The $16 Billion Megaproject in Pará", 500_000, duracao=1200)]}
    respostas["UCaaa/videos"] = _aba([20_000] * 6)
    respostas["watch?v="] = RuntimeError("sem js")
    monkeypatch.setattr(pt, "_get_json", lambda url, params: {"title": "O Megaprojeto de US$ 16 bilhões no Pará"})
    out = pt.outliers(["megaprojetos"], "videos", 30)
    assert not any("/shorts" in c and "results" not in c for c in chamadas)
    assert out[0]["score"] == 25.0
    # o YouTube traduz o título com hl=en; o assunto sai do original (oEmbed)
    assert out[0]["titulo_outlier"] == "O Megaprojeto de US$ 16 bilhões no Pará"
    assert out[0]["assunto"] == "O Megaprojeto de US$ 16 bilhões no Pará"
