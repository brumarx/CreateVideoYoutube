"""Erros bloqueantes × avisos de estilo (review_script_result). Zero API: complete mockado."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fact_check as fc  # noqa: E402

MARCA_REVISOR = "Você é revisor de fatos de um canal do YouTube"
ESTILO = "Os operários enfrentaram o solo instável e o solo instável travou as máquinas."


def _roteiro(*narracoes, title="Túnel Seikan"):
    return {"title": title, "thumbnail_text": "", "scenes": [{"narration": n} for n in narracoes]}


@pytest.fixture
def revisor(monkeypatch):
    """O revisor LLM devolve a lista `problemas` que o teste preparar."""
    estado = {"problemas": []}

    def fake(messages, **kw):
        if MARCA_REVISOR in messages[0]["content"]:
            return json.dumps({"problemas": estado["problemas"]})
        raise RuntimeError("sem LLM nesta checagem")

    monkeypatch.setattr(fc, "complete", fake)
    monkeypatch.setattr(fc, "unknown_names", lambda *a, **k: [])
    return estado


def test_repeticao_estilistica_gera_so_aviso(revisor):
    res = fc.review_script_result(_roteiro(ESTILO), "Túnel Seikan", None, None)
    assert res.style, "a repetição próxima devia virar aviso de estilo"
    assert res.blocking == []


def test_roteiro_sem_bloqueantes_e_aprovado_mesmo_com_estilo(revisor):
    assert fc.review_script(_roteiro(ESTILO), "Túnel Seikan", None, None) == []


def test_erro_factual_continua_reprovando_com_estilo_presente(revisor):
    revisor["problemas"] = [{"tipo": "fato_nao_confirmado", "termo": "60 km", "motivo": "a extensão é 53,85 km"}]
    res = fc.review_script_result(_roteiro(ESTILO, "O túnel tem 60 km."), "Túnel Seikan", None, None)
    assert [p["termo"] for p in res.blocking] == ["60 km"]
    assert res.style
    assert fc.review_script(_roteiro(ESTILO, "O túnel tem 60 km."), "Túnel Seikan", None, None) != []


def test_checagem_factual_roda_quando_so_ha_estilo(revisor, monkeypatch):
    chamadas = {"ungrounded": 0, "verify": 0}

    def fake_ungrounded(*a, **k):
        chamadas["ungrounded"] += 1
        return []

    def fake_verify(*a, **k):
        chamadas["verify"] += 1
        return []

    monkeypatch.setattr(fc, "ungrounded_sentences", fake_ungrounded)
    monkeypatch.setattr(fc, "verify_full_script", fake_verify)
    fontes = [{"titulo": "Fonte", "trecho": "o túnel foi aberto em 1988"}]
    res = fc.review_script_result(_roteiro(ESTILO), "Túnel Seikan", None, fontes)
    assert res.style
    assert chamadas == {"ungrounded": 1, "verify": 1}
    assert res.blocking == []


def test_tema_recorrente_nao_e_penalizado(revisor):
    roteiro = _roteiro("O túnel submarino liga duas ilhas.", "Cada parte do submarino foi escavada à mão.",
                       "Hoje o submarino é usado por trens.")
    assert not any("submarin" in p["motivo"] for p in fc._vague_problems(roteiro, "Túnel Seikan"))


def test_palavra_repetida_em_cenas_diferentes_nao_penaliza(revisor):
    roteiro = _roteiro("A obra começou em 1971.", "Os trabalhadores cavaram por anos.",
                       "O trabalho terminou em 1985.", "A inauguração veio em 1988.")
    assert fc._vague_problems(roteiro, "Túnel Seikan") == []


def test_numeros_e_unidades_nao_geram_falso_positivo(revisor):
    assert fc._vague_problems(_roteiro("O túnel tem 53,85 quilômetros de extensão, mas apenas 23,3 quilômetros ficam sob o mar."),
                              "Túnel Seikan") == []
    assert fc._vague_problems(_roteiro("O custo foi de 538,4 bilhões de ienes, ou 3,6 bilhões de dólares."),
                              "Túnel Seikan") == []


def test_retry_de_variacao_e_uma_so_chamada(monkeypatch):
    import src.script_gen as sg
    chamadas = []

    def fake(messages, **kw):
        chamadas.append(1)
        n = messages[0]["content"].count('"')  # só para devolver algo válido
        frases = ["A usina bombeia água e a água passa por filtros de água."] * 4
        return json.dumps({"cenas": frases})

    monkeypatch.setattr(sg, "complete", fake)
    roteiro = _roteiro(*["A usina bombeia água e a água passa por filtros de água."] * 4)
    sg.vary_close_repeats(roteiro, "usina")
    assert len(chamadas) == 1  # a variação roda UMA vez, sem laço


def test_regressao_frases_da_rodada_6_so_geram_estilo(revisor):
    frases = [
        "O túnel tem 53,85 quilômetros de extensão, mas apenas 23,3 quilômetros ficam sob o mar.",
        "O nível dos trilhos fica cerca de 100 metros abaixo do fundo do mar e 240 metros abaixo da superfície.",
        "Duas estações ficam dentro do túnel, Tappi-Kaitei e Yoshioka-Kaitei.",
    ]
    res = fc.review_script_result(_roteiro(*frases), "Túnel Seikan", None, None)
    assert res.blocking == []


def test_titulo_de_fallback_nao_corta_palavra():
    from src.script_gen import _corta_titulo
    t = _corta_titulo("Túnel Seikan: como o Japão construiu o túnel ferroviário submarino de 53,85 km entre 1971 e 1988")
    assert len(t) <= 60
    assert t == "Túnel Seikan: como o Japão construiu o túnel ferroviário"
    assert _corta_titulo("Título curto") == "Título curto"


def test_nos_canais_de_fatos_o_plano_so_le_a_lista_fechada(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig
    recebido = {}

    def fake_plan(topic, facts, web_facts):
        recebido["web_facts"] = web_facts
        raise RuntimeError("parar aqui: só queremos ver o que o plano recebeu")

    monkeypatch.setattr(sg, "plan_story", fake_plan)
    monkeypatch.setattr(sg, "extract_verified_facts", lambda topic, web: [f"Fato {i} verificado" for i in range(8)])
    fontes = [{"titulo": "Fonte bruta", "url": "https://x", "trecho": "causa de morte dos operários não listada"}]
    canal = ChannelConfig.load("curiosidades")
    try:
        sg.generate_script(canal, "Túnel Seikan", None, web_facts=fontes)
    except Exception:
        pass
    assert "web_facts" in recebido
    texto = " ".join(f.get("trecho", "") for f in recebido["web_facts"])
    assert "causa de morte" not in texto
    assert "Fato 0 verificado" in texto


def test_lista_verificada_vale_sobre_fonte_secundaria_com_outro_numero():
    roteiro = {"title": "t", "thumbnail_text": "", "scenes": [{"narration": "O túnel tem 53,85 km."}],
               "_fatos_verificados": ["Extensão de 53,85 km (fonte oficial JR Hokkaido)"]}
    prompt = fc._review_prompt(roteiro, "Túnel Seikan", None, [{"titulo": "Wiki", "trecho": "65,23 km"}])
    assert "a fonte primária manda sobre a secundária" in prompt


def test_titulo_miniatura_e_prompt_saem_do_roteiro_final(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig
    recebido = {}

    def fake(messages, **kw):
        recebido["prompt"] = messages[0]["content"]
        return json.dumps({"titulo": "Túnel Seikan: 53,85 km sob o mar", "thumbnail_text": "53,85 KM",
                           "thumbnail_image_prompt": "Undersea rail tunnel boring machine, dark blue tones, no text"})

    monkeypatch.setattr(sg, "complete", fake)
    roteiro = {"title": "título antigo", "thumbnail_text": "antigo", "thumbnail_image_prompt": "antigo",
               "scenes": [{"narration": "O túnel tem 53,85 km e 23,3 km sob o leito marinho."}]}
    out = sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"), "Túnel Seikan")
    assert "O túnel tem 53,85 km" in recebido["prompt"]  # foi pedido a partir da narração final
    assert out["title"] == "Túnel Seikan: 53,85 km sob o mar"
    assert out["thumbnail_image_prompt"].startswith("Undersea rail tunnel")


def test_titulo_miniatura_mantem_atuais_se_ia_falha(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig

    def falha(messages, **kw):
        raise RuntimeError("sem provedor")

    monkeypatch.setattr(sg, "complete", falha)
    roteiro = {"title": "atual", "thumbnail_text": "atual", "thumbnail_image_prompt": "atual",
               "scenes": [{"narration": "Texto."}]}
    assert sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"), "Tema") == roteiro


def test_numero_errado_troca_pela_lista_sem_reprovar():
    lista = ["Túnel Seikan: extensão total de 53,85 km (fonte oficial)", "Trecho submarino de 23,3 km sob o leito marinho"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "O túnel tem 65,23 km de extensão e 23,3 km sob o mar."}]}
    out, restantes = fc.substituir_pela_lista(roteiro, [{"tipo": "fato_nao_confirmado", "termo": "65,23 km de extensão", "motivo": "x"}])
    assert restantes == []
    assert "53,85 km de extensão" in out["scenes"][0]["narration"]
    assert "23,3 km sob o mar" in out["scenes"][0]["narration"]  # o que já estava certo não muda


def test_sem_correspondencia_segura_continua_bloqueando():
    lista = ["Trecho submarino de 23,3 km sob o leito marinho"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "A obra durou 40 anos."}]}
    problema = [{"tipo": "fato_nao_confirmado", "termo": "A obra durou 40 anos", "motivo": "x"}]
    out, restantes = fc.substituir_pela_lista(roteiro, problema)
    assert restantes == problema
    assert out["scenes"][0]["narration"] == "A obra durou 40 anos."


def test_tipos_fora_dos_numeros_nao_sao_substituidos():
    lista = ["Túnel Seikan: extensão total de 53,85 km"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "O túnel tem 65,23 km de extensão."}]}
    problema = [{"tipo": "frase_quebrada", "termo": "O túnel tem 65,23 km de extensão", "motivo": "x"}]
    _, restantes = fc.substituir_pela_lista(roteiro, problema)
    assert restantes == problema
