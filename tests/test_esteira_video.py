"""Esteira do vídeo do Fractal (11/11): render de roteiro aprovado, nomes na revisão de áudio,
ordem da checagem de áudio, música sem risco de Content ID e assunto da foto real. Zero API."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RAIZ = Path(__file__).resolve().parent.parent
PIPELINE = (RAIZ / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")


# ---------- 1. renderizar o roteiro aprovado não refaz a revisão nem o título
def test_comando_de_render_leva_approved():
    bloco = PIPELINE[PIPELINE.index('"--topic", topic, "--script-file", str(work_dir / "script.json"), "--no-upload",'):]
    assert '"--approved",' in bloco[:400]


def test_roteiro_aprovado_com_titulo_e_imagens_nao_e_reempacotado():
    assert "ja_empacotado = bool(approved and script_file and script.get(\"title\")" in PIPELINE
    assert "empacotado = script if ja_empacotado else packaging_from_final(" in PIPELINE
    assert "if channel_name in FATOS_CANAIS and not ja_empacotado:" in PIPELINE


# ---------- 2. nomes na revisão de áudio, e o áudio conferido antes da montagem
FRASE = "O Túnel Seikan liga Honshu a Hokkaido, passando por Aomori. As estações Tappi-Kaitei e Yoshioka-Kaitei ficam dentro dele."


def test_nomes_japoneses_ouvidos_com_pequenas_diferencas_passam():
    from src.video_qa import name_problems
    ouvido = "O túnel Seikan liga Onshua Hokkaido, passando por Aomori. As estações TAP Kaitei e Yoshioka Kaitei ficam dentro dele."
    assert name_problems([FRASE], ouvido) == []


@pytest.mark.parametrize("texto,ouvido", [
    ("O presidente Textor falou.", "O presidente Tech Store falou."),
    ("O túnel liga Honshu a Hokkaido.", "O túnel liga a ilha principal ao norte."),
])
def test_nome_trocado_de_verdade_continua_marcado(texto, ouvido):
    from src.video_qa import name_problems
    assert name_problems([texto], ouvido)


def test_audio_e_conferido_antes_das_imagens_e_da_montagem():
    checa = PIPELINE.index("audio_problems, audio_names = review_audio_full(")
    assert checa < PIPELINE.index('visual_context = f"Canal: {channel.channel_title}')
    assert checa < PIPELINE.index("concat_scenes(")
    assert PIPELINE.count("review_audio_full(") == 1


# ---------- 3. música sem risco de Content ID
def _video_curto(destino: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(destino)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)  # ffmpeg sem ler o terminal
    return destino


def test_canal_sem_risco_nao_usa_faixa_marcada(tmp_path):
    from src import assemble
    musicas = tmp_path / "musicas"
    musicas.mkdir()
    for nome in assemble.CONTENT_ID_RISCO:
        (musicas / nome).write_bytes(b"x")  # nunca deveria ser lida
    video = _video_curto(tmp_path / "v.mp4")
    _, atribuicao = assemble.add_background_music(video, tmp_path / "saida.mp4", mood="tenso", music_dir=musicas, sem_risco=True)
    assert atribuicao is None and (tmp_path / "saida.mp4").exists()  # sem faixa segura: sem música


def test_todas_as_faixas_atuais_estao_classificadas():
    from src import assemble
    faixas = {p.name for p in (RAIZ / "assets" / "music").glob("*.mp3")}
    assert faixas <= set(assemble.CONTENT_ID_RISCO) | set(assemble.TRACK_MOOD)


def test_fractal_esta_configurado_sem_risco():
    from src.config import ChannelConfig
    assert ChannelConfig.load("curiosidades").musica_sem_risco is True
    assert ChannelConfig.load("botafogo").musica_sem_risco is False  # os outros canais não mudam


def test_pipeline_passa_a_configuracao_para_a_musica():
    assert "sem_risco=channel.musica_sem_risco" in PIPELINE


# ---------- 4. assunto da foto real sai do texto final
def test_assunto_da_foto_real_vem_da_narracao_final(monkeypatch):
    from src import script_gen as sg

    def fake(messages, **kw):
        return json.dumps({"cenas": [
            {"image_prompt": "Tunnel entrance", "stock_query": "rail tunnel", "real_subject": "Seikan Tunnel"},
            {"image_prompt": "Ocean", "stock_query": "ocean", "real_subject": None},
        ]})

    monkeypatch.setattr(sg, "complete", fake)
    roteiro = {"scenes": [{"narration": "O Túnel Seikan liga duas ilhas.", "real_subject": "VELHO"},
                          {"narration": "O mar é profundo.", "real_subject": "VELHO"}]}
    out = sg.image_prompts_from_final(roteiro, "Túnel Seikan")
    assert [sc["real_subject"] for sc in out["scenes"]] == ["Seikan Tunnel", ""]


def test_assunto_antigo_e_apagado_antes_de_gerar():
    assert '"real_subject": ""} for sc in script["scenes"]]' in PIPELINE


# ---------- rodada 25: miniatura insistente com número fora de contexto não derruba o roteiro validado
NARR = "O Túnel Seikan tem 53,85 km de extensão. Sob o mar ficam 23,3 km sob o leito marinho."


def _pacote(monkeypatch, respostas):
    from src import script_gen as sg
    import src.fact_check as fcm
    pedidos = []
    seq = iter(respostas)
    monkeypatch.setattr(sg, "complete", lambda messages, **k: pedidos.append(messages[0]["content"]) or next(seq))
    monkeypatch.setattr(fcm, "_title_problems", lambda s: [])
    from src.config import ChannelConfig
    roteiro = {"title": "", "_fatos_verificados": ["f"], "scenes": [{"narration": NARR}]}
    return sg.packaging_from_final(roteiro, ChannelConfig.load("curiosidades"), "Túnel Seikan: como o Japão fez"), pedidos


def test_pedido_traz_os_numeros_com_o_contexto_da_narracao(monkeypatch):
    bom = json.dumps({"titulo": "Túnel Seikan: 53,85 km de extensão", "thumbnail_text": "53,85 KM DE EXTENSÃO", "thumbnail_image_prompt": "p"})
    _, pedidos = _pacote(monkeypatch, [bom])
    assert '"53,85 km de extensão"' in pedidos[0] and '"23,3 km sob o leito marinho"' in pedidos[0]


def test_miniatura_errada_duas_vezes_sai_sem_texto_e_o_titulo_certo_fica(monkeypatch):
    ruim = json.dumps({"titulo": "Túnel Seikan: 53,85 km de extensão", "thumbnail_text": "53,85 KM SOB O MAR", "thumbnail_image_prompt": "p"})
    out, _ = _pacote(monkeypatch, [ruim, ruim])
    assert out is not None
    assert out["title"] == "Túnel Seikan: 53,85 km de extensão" and out["thumbnail_text"] == "TÚNEL SEIKAN"


def test_titulo_errado_duas_vezes_vira_o_assunto(monkeypatch):
    ruim = json.dumps({"titulo": "53,85 km sob o mar", "thumbnail_text": "TÚNEL SEIKAN", "thumbnail_image_prompt": "p"})
    out, _ = _pacote(monkeypatch, [ruim, ruim])
    assert out["title"] == "Túnel Seikan" and out["thumbnail_text"] == "TÚNEL SEIKAN"



# ---------- título e miniatura são CÓPIA do texto aprovado (palavras seguidas), conferido em código
@pytest.mark.parametrize("trecho,ok", [
    ("53,85 KM SOB O MAR", False), ("23,3 KM SOB O MAR", False), ("53,85 km submersos", False),
    ("23,3 km sob o leito marinho", True), ("53,85 KM DE EXTENSÃO", True), ("Túnel Seikan", True),
])
def test_copiado_do_texto(trecho, ok):
    from src.fatos import copiado_do_texto
    assert copiado_do_texto(trecho, NARR) is ok


@pytest.mark.parametrize("titulo,ok", [
    ("Túnel Seikan: 53,85 km de extensão", True), ("Túnel Seikan: 53,85 km sob o mar", False),
    ("Túnel Seikan", True), ("Túnel Seikan: o maior do mundo", False),
])
def test_titulo_copiado(titulo, ok):
    from src.fatos import titulo_copiado
    assert titulo_copiado(titulo, NARR) is ok


def test_pedido_manda_copiar_da_narracao(monkeypatch):
    bom = json.dumps({"titulo": "Túnel Seikan: 53,85 km de extensão", "thumbnail_text": "53,85 KM DE EXTENSÃO", "thumbnail_image_prompt": "p"})
    out, pedidos = _pacote(monkeypatch, [bom])
    assert "COPIADOS da NARRAÇÃO FINAL" in pedidos[0]
    assert out["title"] == "Túnel Seikan: 53,85 km de extensão" and out["thumbnail_text"] == "53,85 KM DE EXTENSÃO"


# ---------- rodada 26: proporção errada, miniatura cortada, imagem com escrita
def _rot(*narr):
    return {"scenes": [{"narration": n} for n in narr]}


def test_maior_parte_que_nao_bate_com_a_conta_sai():
    from src.fatos import ajustar_texto_final, proporcao_errada
    r = _rot("Existe um caminho de 53,85 km que atravessa o mar.",
             "Do total, 23,3 km ficam sob o leito. Ou seja: a maior parte da viagem acontece debaixo da água.")
    assert proporcao_errada(r) == ["Ou seja: a maior parte da viagem acontece debaixo da água."]
    out = ajustar_texto_final(r)
    assert "maior parte" not in out["scenes"][1]["narration"]


@pytest.mark.parametrize("frase", ["Ou seja: menos da metade do caminho fica debaixo da água.",
                                   "Cerca de metade do caminho, 23,3 km, fica debaixo da água."])
def test_proporcao_que_bate_com_a_conta_fica(frase):
    from src.fatos import proporcao_errada
    assert proporcao_errada(_rot("O caminho tem 53,85 km.", f"Sob o mar ficam 23,3 km. {frase}")) == []


def test_maioria_certa_fica():
    from src.fatos import proporcao_errada
    assert proporcao_errada(_rot("O túnel tem 53,85 km.", "Desses, 40 km ficam em terra: a maior parte.")) == []


@pytest.mark.parametrize("texto,solto", [("23,3 km ficam sob", True), ("53,85 KM DE", True), ("TÚNEL SEIKAN", False),
                                         ("23,3 KM SOB O LEITO MARINHO", False), ("Túnel Seikan: 53,85 km que atravessa o mar", False)])
def test_texto_curto_cortado_no_meio(texto, solto):
    from src.fatos import termina_solto
    assert termina_solto(texto) is solto


def test_miniatura_cortada_e_refeita_ou_sai_sem_texto(monkeypatch):
    cortada = json.dumps({"titulo": "Túnel Seikan: 53,85 km de extensão", "thumbnail_text": "23,3 km sob o", "thumbnail_image_prompt": "p"})
    out, _ = _pacote(monkeypatch, [cortada, cortada])
    assert out["thumbnail_text"] == "TÚNEL SEIKAN" and out["title"] == "Túnel Seikan: 53,85 km de extensão"


@pytest.mark.parametrize("prompt,busca,esperado", [
    ("construction site sign displaying \"JRCC\" at the entrance", "JRCC tunnel construction sign", "tunnel construction"),
    ("sign at tunnel entrance indicating 53.85 km length", "53.85 km underwater tunnel sign", "underwater tunnel"),
])
def test_imagem_que_pede_escrita_vira_busca_limpa(prompt, busca, esperado):
    from src.script_gen import sem_escrita_na_imagem
    out = sem_escrita_na_imagem({"scenes": [{"narration": "x", "image_prompt": prompt, "stock_query": busca}]})
    novo = out["scenes"][0]["image_prompt"]
    assert novo.startswith(esperado) and "sign" not in novo and "JRCC" not in novo and "53" not in novo


def test_imagem_sem_escrita_nao_muda():
    from src.script_gen import sem_escrita_na_imagem
    r = {"scenes": [{"narration": "x", "image_prompt": "stormy sea above a railway tunnel", "stock_query": "stormy sea"}]}
    assert sem_escrita_na_imagem(r) == r


def test_render_reempacota_quando_a_miniatura_aprovada_esta_cortada():
    assert "and not termina_solto(str(script[\"thumbnail_text\"]))" in PIPELINE
    assert PIPELINE.index("script = sem_escrita_na_imagem(script)") < PIPELINE.index("script = art_direction(script, channel)")


# ---------- job 855: Short longo demais é encurtado tirando frases, antes do título e da voz
def _roteiro_longo():
    cenas = [{"narration": "E aí, mente curiosa! O túnel tem 53,85 km. Isso mesmo, é muito longo e impressiona qualquer pessoa que pensa nisso."}]
    cenas += [{"narration": f"Cena {i}: o trecho tem 23,3 km sob o mar. Pense no tamanho enorme do desafio que isso representava naquela época distante. Quer saber mais?"}
              for i in range(1, 10)]
    cenas.append({"narration": "Você teria coragem de atravessar? Deixe sua resposta nos comentários e compartilhe com quem ama engenharia."})
    return {"scenes": cenas}


def test_short_longo_tira_enchimento_e_cabe_no_limite():
    from src.fatos import encurtar_roteiro
    r = _roteiro_longo()
    total = sum(len(sc["narration"].split()) for sc in r["scenes"])
    out = encurtar_roteiro(r, total - 60)
    texto = " ".join(sc["narration"] for sc in out["scenes"])
    assert sum(len(sc["narration"].split()) for sc in out["scenes"]) <= total - 60
    assert texto.count("23,3 km") == 9 and "53,85 km" in texto          # números ficam
    assert out["scenes"][-1] == r["scenes"][-1]                           # fechamento intacto
    assert all(sc["narration"].split(".")[0] == orig["narration"].split(".")[0] for sc, orig in zip(out["scenes"], r["scenes"]))


def test_short_dentro_do_limite_nao_muda():
    from src.fatos import encurtar_roteiro
    r = _roteiro_longo()
    assert encurtar_roteiro(r, 10_000) == r


def test_perguntas_e_primeira_frase_nunca_saem():
    from src.fatos import encurtar_roteiro
    out = encurtar_roteiro(_roteiro_longo(), 50)
    for sc in out["scenes"][1:-1]:
        assert sc["narration"].startswith("Cena") and sc["narration"].endswith("Quer saber mais?")


def test_pipeline_encurta_antes_do_titulo_e_da_voz():
    encurta = PIPELINE.index("script = encurtar_roteiro(script, int(SHORTS_MAX_SECONDS * PALAVRAS_POR_MINUTO_VOZ / 60))")
    assert encurta < PIPELINE.index("packaging_from_final(\n            script, channel, topic")
    assert encurta < PIPELINE.index("audio_problems, audio_names = review_audio_full(")


# ---------- job 856: sotaque brasileiro ("te" → "tchi") não é erro de pronúncia; gráfico com vírgula; foto por palavra solta
def test_sotaque_brasileiro_no_nome_japones_passa():
    from src.video_qa import name_problems
    texto = "Em março de 2016, a linha ligou Shin-Aomori e Shin-Hakodate-Hokuto."
    ouvido = "Em março de 2016, a linha ligou Shin Aomori e Shin Hakodachi Okuto."
    assert name_problems([texto], ouvido) == []


def test_troca_de_verdade_no_fim_continua_marcada():
    from src.video_qa import name_problems
    assert name_problems(["O porto de Hakodate fica no norte."], "O porto de Hakomachi fica no norte.")


@pytest.mark.parametrize("valor,esperado", [("53.85", "53,85"), ("6.900", "6.900"), ("1988", "1988"), ("3.6 bi", "3,6 bi"), ("90%", "90%")])
def test_numero_do_grafico_em_portugues(valor, esperado):
    from src.infographic import numero_pt
    assert numero_pt(valor) == esperado


def test_assunto_vazio_do_texto_final_nao_vira_busca_por_nome():
    assert 'if channel.real_media and not real_subject and not script.get("_imagens_do_texto_final"):' in PIPELINE
    from src import script_gen as sg
    import json as _j

    def fake(messages, **kw):
        return _j.dumps({"cenas": [{"image_prompt": "calm sea", "stock_query": "calm sea", "real_subject": None}]})

    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(sg, "complete", fake)
    try:
        out = sg.image_prompts_from_final({"scenes": [{"narration": "Hoje, atravessar o Estreito de Tsugaru virou algo banal."}]}, "T")
    finally:
        mp.undo()
    assert out["_imagens_do_texto_final"] is True and out["scenes"][0]["real_subject"] == ""



# ---------- job 857: miniatura termina onde a frase termina; revisão visual pelo assunto do vídeo
@pytest.mark.parametrize("trecho,ok", [("53,85 km que atravessa", False), ("53,85 km de extensão", True),
                                       ("23,3 km sob o leito", False), ("23,3 km sob o leito marinho", True)])
def test_miniatura_termina_onde_a_frase_termina(trecho, ok):
    from src.fatos import termina_em_fronteira
    assert termina_em_fronteira(trecho, NARR) is ok


def test_miniatura_cortada_no_meio_vira_o_nome_do_assunto(monkeypatch):
    cortada = json.dumps({"titulo": "Túnel Seikan: 53,85 km de extensão", "thumbnail_text": "53,85 km que atravessa", "thumbnail_image_prompt": "p"})
    out, _ = _pacote(monkeypatch, [cortada, cortada])
    assert out["thumbnail_text"] == "TÚNEL SEIKAN"


def test_revisao_visual_considera_o_assunto_do_video():
    assert "A imagem tem de combinar com o ASSUNTO do vídeo, não só com a frase da cena" in PIPELINE
    assert "and termina_em_fronteira(str(script[\"thumbnail_text\"]), _narr_final)" in PIPELINE


def test_comentario_do_fractal_e_a_pergunta_final_da_narracao():
    import importlib
    rp = importlib.import_module("scripts.run_pipeline")
    roteiro = {"scenes": [{"narration": "Como isso é possível? O túnel tem 53,85 km."},
                          {"narration": "Você teria coragem de atravessar esse túnel? Deixe nos comentários."}]}
    assert rp._comentario_do_texto(roteiro, 2) == "Você teria coragem de atravessar esse túnel?"
    assert "_comentario_do_texto(script, original_scene_count) if channel_name in FATOS_CANAIS" in PIPELINE
