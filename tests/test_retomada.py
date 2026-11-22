"""Retomada de job no modo redação (20/11), de ponta a ponta e sem rede nem IA de verdade:
pesquisa → banco de evidências → roteiro → provedor cai na auditoria → o job sai → o processo
reinicia → pesquisa, banco e roteiro voltam do cache → a auditoria (só ela) roda e termina.
O cache em disco é o real (src/disk_cache), isolado num diretório temporário pelo conftest."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from src import providers, web_search
from src.providers import ProvedoresIndisponiveis
from tests.auditor_falso import AUD

RAIZ = Path(__file__).resolve().parent.parent
TEMA = "Como Yakutsk funciona sobre o permafrost"
FRASES = [f"Em Yakutsk, o fato verificado número {k} vale {k + 10} metros de estaca." for k in range(1, 13)]
FONTE = {"titulo": "Yakutsk", "url": "https://en.wikipedia.org/wiki/Yakutsk", "trecho": " ".join(FRASES)}


def _pipeline():
    """Uma importação NOVA do run_pipeline: o equivalente a reiniciar o processo."""
    spec = importlib.util.spec_from_file_location("rp_retomada", RAIZ / "scripts" / "run_pipeline.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IAFalsa:
    """Responde por etapa e conta as chamadas. `cair` = etapa em que o provedor fica indisponível."""

    def __init__(self):
        self.chamadas = {"historia": 0, "extracao": 0, "juiz": 0, "escritor": 0, "auditor": 0, "juiz_final": 0}
        self.cair = None
        self.rede = {"buscas": 0}

    def __call__(self, messages, **_kw):
        p = messages[-1]["content"]
        etapa = ("historia" if "defina a HISTÓRIA" in p else "juiz" if "Você confere se o TRECHO" in p else "escritor" if "Você é o roteirista" in p else
                 "auditor" if "Você é o auditor" in p else "juiz_final" if ("Você é o EDITOR" in p or "ESPECTADOR" in p)
                 else "extracao")
        self.chamadas[etapa] += 1
        if etapa == self.cair:
            raise ProvedoresIndisponiveis("todos os provedores fortes em descanso", disponivel_em=1_900_000_000)
        if etapa == "historia":
            r = {}  # 22/11: sem história (resposta inválida) — o banco segue pelo tema, sem segunda passagem
        elif etapa == "extracao":
            r = {"evidencias": [{"afirmacao": f, "citacoes": [{"fonte": "S1", "trecho": f}]} for f in FRASES],
                 "lacunas": []}
        elif etapa == "juiz":
            itens = re.findall(r"TRECHO: \"(.+?)\"\n", p)
            r = {"julgamentos": [{"n": n, "decisao": "sustenta", "sujeito": "ok", "medida": "ok",
                                  "trecho_chave": " ".join(t.split()[:4]), "justificativa": "o trecho diz exatamente isso"}
                                 for n, t in enumerate(itens, 1)]}
        elif etapa == "escritor":
            r = {"titulo": "Yakutsk", "descricao": "d", "tags": [],
                 "cenas": [{"frases": [{"texto": FRASES[k], "tipo": "factual", "evidencias": [f"E{k + 1}"]}]}
                           for k in range(6)]}
        else:
            r = AUD(9)(p)
        return json.dumps(r, ensure_ascii=False)


@pytest.fixture
def ia(monkeypatch):
    falsa = IAFalsa()
    monkeypatch.setattr(providers, "complete", falsa)
    rede = falsa.rede

    def tavily(_q, _n):
        rede["buscas"] += 1
        return [dict(FONTE)]
    monkeypatch.setattr(web_search, "_tavily", tavily)
    monkeypatch.setattr(web_search, "_duckduckgo", lambda _q, _n: None)
    monkeypatch.setattr(web_search, "_enrich", lambda facts, _q, _n: facts)
    for nome in ("wikipedia_facts", "wikidata_facts", "openalex_facts"):
        monkeypatch.setattr(web_search, nome, lambda _q: [])
    return falsa


def _rodada(ia):
    """O trecho do run() do modo redação: pesquisa, escrita (ou retomada) e auditoria."""
    from src.config import ChannelConfig
    mod = _pipeline()
    canal = ChannelConfig.load("curiosidades")
    fontes = web_search.search_topic_facts(TEMA)
    script, banco = mod._escrever_redacao(canal, TEMA, fontes, 1, 0.1, 5, 6)
    script = canal.with_greeting(script)
    return mod._auditar_redacao(canal, script, banco, TEMA, fontes, 1, 0.1, 5, 6)


def test_provedor_cai_na_auditoria_e_a_retomada_recupera_tudo_sem_refazer(ia):
    # 1ª execução: pesquisa, banco e roteiro prontos; a auditoria cai
    ia.cair = "auditor"
    with pytest.raises(ProvedoresIndisponiveis) as exc:
        _rodada(ia)
    assert exc.value.disponivel_em == 1_900_000_000
    antes = dict(ia.chamadas)
    assert ia.rede["buscas"] == 1 and antes["extracao"] == 1 and antes["juiz"] >= 1 and antes["escritor"] >= 1

    # o processo reinicia (nova importação do pipeline, rodízio de chaves zerado) e o provedor voltou
    providers._rotators.clear()
    ia.cair = None
    final = _rodada(ia)

    assert ia.rede["buscas"] == 1                                   # pesquisa veio do cache
    assert ia.chamadas["extracao"] == antes["extracao"]             # banco de evidências veio do cache
    assert ia.chamadas["juiz"] == antes["juiz"]                     # a sustentação não foi julgada de novo
    assert ia.chamadas["escritor"] == antes["escritor"]             # o roteiro não foi reescrito
    assert ia.chamadas["auditor"] == antes["auditor"] + 1           # só a etapa pendente rodou
    assert final["_auditoria"]["qualidade"]["nota"] == 9
    from src.redacao import frases_de
    assert len(final["_auditoria"]["qualidade"]["decisoes"]) == len(frases_de(final))  # toda frase (e o bordão)
    textos = [f["texto"] for _, _, f in frases_de(final)]
    assert textos[1:] == FRASES[:6]                                 # é o mesmo roteiro (depois do bordão)

    # 3ª execução (caiu depois, na voz ou nas imagens): o roteiro já auditado não é auditado de novo
    providers._rotators.clear()
    auditor = ia.chamadas["auditor"]
    de_novo = _rodada(ia)
    assert ia.chamadas["auditor"] == auditor and de_novo["_auditoria"] == final["_auditoria"]


def test_o_job_que_cai_sai_com_codigo_de_infraestrutura_e_hora_de_retomar():
    src = (RAIZ / "scripts" / "run_pipeline.py").read_text()
    assert "retomar_em={int(exc.disponivel_em or 0)}" in src
    i = src.index("if isinstance(exc, ProvedoresIndisponiveis):\n            sys.exit(EXIT_INFRA)")
    assert i > 0
    rodada = (RAIZ / "scripts" / "daily_run.py").read_text()
    assert "EXIT_INFRA" in rodada or "retomar_em" in rodada
