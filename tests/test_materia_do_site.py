"""24/11: Botafogo e Política — 1 vídeo = o resumo de UMA matéria escrita no site do canal."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import botafogo_data as bd  # noqa: E402
from src import script_gen as sg  # noqa: E402
from src.config import ChannelConfig  # noqa: E402

UMA = {"tipo": "noticias", "noticias": [{"titulo": "T", "resumo": "", "texto": "x " * 400, "veiculo": "Portal"}]}
DUAS = {"tipo": "noticias", "noticias": [UMA["noticias"][0], {**UMA["noticias"][0], "titulo": "U"}]}


def test_uma_materia_vira_resumo_e_nao_noticiario():
    p = sg._prompt_for(ChannelConfig.load("botafogo"), "T", UMA)
    assert "RESUMO de UMA matéria" in p and "Primeira notícia:" not in p.split("ESTRUTURA OBRIGATÓRIA")[1][:600]
    assert "são 2 notícias SEPARADAS" in sg._prompt_for(ChannelConfig.load("botafogo"), "T", DUAS)


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _portal(paginas, corpos):
    def get(url, params=None, timeout=None):
        if url.endswith("/articles"):
            return _Resp({"items": paginas.get(params["page"], [])})
        return _Resp({"body_html": corpos[url.rsplit("/", 1)[1]]})
    return get


def test_botafogo_pega_so_materia_propria_com_texto_e_pagina():
    art = lambda i, tipo, slug: {"id": i, "title": f"Matéria {slug} do Botafogo de hoje", "slug": slug,  # noqa: E731
                                 "source_type": tipo, "published_at": "2026-11-24T10:00:00Z", "excerpt": "e"}
    paginas = {1: [art(1, "aggregated", "agregada"), art(2, "authored", "video-curto")],
               2: [art(3, "authored", "materia-longa")]}
    corpos = {"video-curto": "<p>Veja os gols.</p>", "materia-longa": "<p>" + "texto da matéria " * 60 + "</p>"}
    with mock.patch.object(bd.httpx, "get", _portal(paginas, corpos)), \
            mock.patch.object(bd, "_load_state", return_value={}), mock.patch.object(bd, "_save_state"), \
            mock.patch.object(bd, "_agenda", return_value={}):
        t = bd.materia_task()
    assert t["titulo"] == "Matéria materia-longa do Botafogo de hoje"
    assert len(t["facts"]["noticias"]) == 1 and t["fontes"][0]["url"].endswith("/noticias/materia-longa")


def test_daily_run_do_botafogo_so_roda_a_materia():
    texto = (Path(__file__).resolve().parent.parent / "scripts" / "daily_run.py").read_text()
    assert 'for task in ("portal",)' in texto


def test_politica_sem_banco_cai_nos_rankings_e_nao_repete_materia():
    from src import politica_data as pd
    with mock.patch.object(pd, "_connect", side_effect=RuntimeError("fora")):
        assert pd.materia_task() is None  # o chamador usa random_fact_set

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=()):
            from datetime import datetime, timezone
            agora = datetime(2026, 11, 24, tzinfo=timezone.utc)
            if "FROM public.users" in sql:
                return mock.Mock(fetchall=lambda: [("marina-couto", "Marina Couto"), ("augusto-valente", "Augusto Valente")])
            # 25/11: ordem do dono — Tiago, Bruno, Lúcia, Augusto, Marina por último; debate entre colunistas fora
            return mock.Mock(fetchall=lambda: [(7, "Da Marina", "", "marina", "t" * 700, "", agora, "marina-couto"),
                                               (5, "Resposta a Augusto Valente: x", "", "r", "t" * 700, "", agora,
                                                "marina-couto"),
                                               (4, "Sobre bets", "", "b", "Marina Couto erra " + "t" * 700, "", agora,
                                                "tiago-rezende"),
                                               (6, "Do Augusto", "", "augusto", "t" * 700, "", agora, "augusto-valente"),
                                               (9, "Usada", "", "usada", "t" * 700, "", agora, "tiago-rezende"),
                                               (8, "Nova", "sub", "nova", "t" * 700, "", agora, "bruno-marques")])
    salvo = {}
    with mock.patch.object(pd, "_connect", return_value=_C()), \
            mock.patch.object(pd, "_load_used", return_value={"materias": ["9"]}), \
            mock.patch.object(pd, "_save_used", side_effect=salvo.update):
        t = pd.materia_task()
    assert t["titulo"] == "Nova" and t["fontes"][0]["url"] == "https://politica-br.org/materias/nova"
    assert t["fotos"] == [] and salvo["materias"] == ["8", "9"]
    from src import politica_data as pd2
    assert pd2.debate_entre_colunistas("Resposta a Augusto Valente: x", "", "marina-couto", {}) == "resposta"
    assert pd2.debate_entre_colunistas("Visita a Augusto Heleno", "o general Augusto Heleno",
                                       "marina-couto", {"augusto-valente": "Augusto Valente"}) is None
