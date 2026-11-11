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
                   check=True, capture_output=True)
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
