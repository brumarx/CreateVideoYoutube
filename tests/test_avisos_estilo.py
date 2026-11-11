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
    monkeypatch.setattr(sg, "extract_facts_e_outliers", lambda topic, web: ([
        {"id": f"F{i + 1}", "texto": f"Fato {i} verificado", "status": "oficial", "fontes": []} for i in range(8)], []))
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


def test_frase_sem_correspondencia_sai_e_entra_fato_nao_usado():
    lista = [f"Fato verificado número {i} sobre a obra do túnel" for i in range(1, 10)]
    usados = " ".join(f"Fato verificado número {i} sobre a obra do túnel." for i in range(1, 9))
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": usados + " A obra durou 40 anos."}]}
    problemas = [{"tipo": "fora_da_fonte", "termo": "A obra durou 40 anos", "motivo": "x"}]
    out, n = fc.cortar_e_trocar_por_fato(roteiro, problemas)
    assert n == 1
    texto = " ".join(sc["narration"] for sc in out["scenes"])
    assert "40 anos" not in texto
    assert "Fato verificado número 9" in texto  # o único fato verificado não usado entrou no lugar


def test_sem_fatos_suficientes_nao_troca():
    lista = ["Fato verificado número 1 sobre a obra do túnel"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "A obra durou 40 anos. Fato verificado número 1 sobre a obra do túnel."}]}
    assert fc.cortar_e_trocar_por_fato(roteiro, [{"tipo": "fora_da_fonte", "termo": "A obra durou 40 anos", "motivo": "x"}]) is None


def test_numero_ja_na_lista_com_outro_substantivo_e_resolvido():
    lista = ["Estima-se que 34 operários tenham morrido durante a construção do túnel"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "Durante a obra, 34 pessoas morreram."}]}
    out, restantes = fc.substituir_pela_lista(roteiro, [{"tipo": "fora_da_fonte", "termo": "34 pessoas morreram", "motivo": "x"}])
    assert restantes == []


def test_titulo_nao_e_revisado_antes_da_validacao(revisor):
    roteiro = _roteiro("O túnel tem 53,85 km de extensão, com 23,3 km sob o mar.", title="53,85 km sob o mar")
    res = fc.review_script_result(roteiro, "Túnel Seikan", None, None)
    assert not any(p.get("tipo") in ("titulo_nao_confere", "titulo_enganoso") for p in res.blocking)


def test_numero_da_lista_com_outro_assunto_nao_resolve():
    lista = ["Túnel Seikan: extensão total de 53,85 km", "Trecho submarino de 23,3 km sob o leito marinho"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "O túnel tem 23,3 km de extensão total."}]}
    problema = [{"tipo": "fato_nao_confirmado", "termo": "23,3 km de extensão total", "motivo": "x"}]
    _, restantes = fc.substituir_pela_lista(roteiro, problema)
    assert restantes == problema  # o número existe na lista, mas é o trecho submarino, não a extensão


def test_frase_sem_numero_nunca_e_resolvida_pela_substituicao():
    lista = ["Túnel Seikan: extensão total de 53,85 km"]
    roteiro = {"title": "t", "thumbnail_text": "", "_fatos_verificados": lista,
               "scenes": [{"narration": "Elas serviam de refúgio."}]}
    problema = [{"tipo": "fora_da_fonte", "termo": "Elas serviam de refúgio", "motivo": "x"}]
    _, restantes = fc.substituir_pela_lista(roteiro, problema)
    assert restantes == problema


def test_fato_com_japones_ou_nota_de_fonte_nao_entra_na_narracao():
    assert fc._frase_narravel("Custo total de 690 bilhões de ienes (約6,900億円)") == "Custo total de 690 bilhões de ienes."
    assert fc._frase_narravel("Estima-se que 34 operários morreram (fonte secundária; usar com estima-se)") == \
        "Estima-se que 34 operários morreram."
    assert fc._frase_narravel("Custo de 約6,900億円") is None


def test_fato_reescrito_conta_como_usado_pelos_numeros():
    assert fc._fato_usado("Trecho submarino de 23,3 km sob o leito marinho", "Sob o mar ficam 23,3 km do túnel.")


def test_titulo_continua_revisado_nos_outros_canais(revisor, monkeypatch):
    monkeypatch.setattr(fc, "_title_problems", lambda s: [{"tipo": "titulo_nao_confere", "termo": "t", "motivo": "m"}])
    roteiro = _roteiro("Texto normal sem problema.", title="Título que não confere")  # sem _fatos_verificados
    res = fc.review_script_result(roteiro, "Tema", None, None)
    assert any(p["tipo"] == "titulo_nao_confere" for p in res.blocking)


def test_miniatura_nao_pode_usar_numero_do_tema_que_nao_esta_na_narracao(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig
    respostas = iter([
        json.dumps({"titulo": "Túnel Seikan: 53,85 km e 34 vidas", "thumbnail_text": "53,85 km submersos",
                    "thumbnail_image_prompt": "undersea tunnel, no text"}),
        json.dumps({"titulo": "Túnel Seikan: 23,3 km sob o mar", "thumbnail_text": "23,3 km sob o mar",
                    "thumbnail_image_prompt": "undersea tunnel, no text"}),
    ])

    def fake(messages, validate=None, **kw):
        for r in respostas:  # devolve a primeira resposta que passa na validação
            if validate is None or validate(r):
                return r
        raise RuntimeError("nenhuma válida")

    monkeypatch.setattr(sg, "complete", fake)
    import src.fact_check as fcm
    monkeypatch.setattr(fcm, "_title_problems", lambda s: [])
    roteiro = {"title": "x", "thumbnail_text": "x", "_fatos_verificados": ["Trecho submarino de 23,3 km"],
               "scenes": [{"narration": "Debaixo do mar, o Túnel Seikan tem 23,3 km sob o mar."}]}
    out = sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"),
                                  "Túnel Seikan: túnel submarino de 53,85 km")
    assert "53,85" not in out["title"] and "53,85" not in (out["thumbnail_text"] or "")


def _fonte_pipeline():
    return (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")


def test_titulo_e_imagens_so_depois_da_ultima_mudanca_no_texto():
    src = _fonte_pipeline()
    ultima_mudanca = max(src.index("script = fix_dangling_hooks(script)"), src.index("script = vary_close_repeats(script, topic)"),
                         src.index("script = fix_script(script, channel.language)"), src.index("script = clean_stat_overlays(script)"))
    assert src.index("empacotado = packaging_from_final(") > ultima_mudanca
    assert src.index("com_imagens = image_prompts_from_final(") > ultima_mudanca
    assert src.count("packaging_from_final(script") == 1  # não existe outra geração de título antes


def test_campos_antigos_sao_apagados_antes_de_gerar_no_fractal():
    src = _fonte_pipeline()
    apaga = src.index('script = {**script, "title": "", "thumbnail_text": None, "thumbnail_image_prompt": ""')
    assert apaga < src.index("empacotado = packaging_from_final(")


def test_prompts_das_imagens_vem_da_narracao_final(monkeypatch):
    from src import script_gen as sg
    recebido = {}

    def fake(messages, **kw):
        recebido["prompt"] = messages[0]["content"]
        return json.dumps({"cenas": [{"image_prompt": "Undersea rail tunnel carved in rock", "stock_query": "undersea rail tunnel"},
                                     {"image_prompt": "Train inside a long tunnel", "stock_query": "train tunnel"}]})

    monkeypatch.setattr(sg, "complete", fake)
    roteiro = {"title": "", "scenes": [{"narration": "O túnel passa sob o mar.", "image_prompt": "VELHO", "stock_query": "VELHO"},
                                       {"narration": "Trens cruzam o túnel.", "image_prompt": "VELHO", "stock_query": "VELHO"}]}
    out = sg.image_prompts_from_final(roteiro, "Túnel Seikan")
    assert "O túnel passa sob o mar." in recebido["prompt"] and "VELHO" not in recebido["prompt"]
    assert [sc["image_prompt"] for sc in out["scenes"]] == ["Undersea rail tunnel carved in rock", "Train inside a long tunnel"]


def test_falha_na_geracao_no_fractal_nao_reaproveita_o_antigo(monkeypatch):
    from src import script_gen as sg
    from src.config import ChannelConfig

    def falha(messages, **kw):
        raise RuntimeError("sem provedor")

    monkeypatch.setattr(sg, "complete", falha)
    roteiro = {"title": "ANTIGO", "thumbnail_text": "ANTIGO", "_fatos_verificados": ["f"],
               "scenes": [{"narration": "Texto validado.", "image_prompt": "ANTIGO"}]}
    assert sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"), "Tema") is None
    assert sg.image_prompts_from_final(roteiro, "Tema") is None


def test_regra_roda_antes_do_titulo_e_das_imagens():
    src = _fonte_pipeline()
    assert src.index("script = _normalize_currency(ajustar_texto_final(script))") < src.index("empacotado = packaging_from_final(")


