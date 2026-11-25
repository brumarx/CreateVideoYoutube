"""25/11: controle técnico do MP4 final (src/qc_video.py) — clipes sintéticos minúsculos do lavfi."""
import subprocess

import pytest

from src.qc_video import Medicoes, analisar_log, problemas, revisar_tecnica


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def clipes(tmp_path_factory):
    d = tmp_path_factory.mktemp("qc")
    bom, ruim = d / "bom.mp4", d / "ruim.mp4"
    # imagem em movimento + tom contínuo em volume de voz (~-16 LUFS)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=108x192:r=24:d=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-af", "volume=6dB", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(bom))
    # 1 s preto, 5 s de imagem parada, 2 s em movimento; áudio: tom 2 s, silêncio 2 s, tom 4 s
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=108x192:r=24:d=1", "-f", "lavfi", "-i", "color=c=red:s=108x192:r=24:d=5",
            "-f", "lavfi", "-i", "testsrc2=s=108x192:r=24:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-filter_complex", "[0:v]format=yuv420p[a0];[1:v]format=yuv420p[a1];[2:v]format=yuv420p[a2];"
            "[a0][a1][a2]concat=n=3:v=1:a=0[v];[3:a]aformat=channel_layouts=mono[b0];[5:a]aformat=channel_layouts=mono[b2];"
            "[b0][4:a][b2]concat=n=3:v=0:a=1[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", str(ruim))
    return bom, ruim


def test_video_bom_passa(clipes):
    assert revisar_tecnica(clipes[0]) == []


def test_video_ruim_aponta_cada_defeito(clipes):
    p = " | ".join(revisar_tecnica(clipes[1], max_segundos=5))
    assert "tela preta" in p
    assert "silêncio de 2.0 s" in p
    assert "congelada" in p
    assert "1º quadro quase preto" in p
    assert "duração 8 s acima" in p


def test_silencio_do_fim_nao_conta_e_volume_fora():
    m = Medicoes(duracao=60, silencios=[(58.9, 60)], lufs=-24.0, capa_clara=0.3)
    p = problemas(m)
    assert p == ["volume integrado -24.0 LUFS fora de -20..-10"]


def test_congelado_durante_pausa_nao_conta():
    m = Medicoes(duracao=60, silencios=[(10, 12)], congelados=[(9.5, 12.5)], lufs=-14, capa_clara=0.3)
    # o silêncio é apontado à parte; o congelamento (3 s, 2 s dele na pausa) não
    assert [x for x in problemas(m) if "congelada" in x] == []


def test_analisar_log():
    log = """[blackdetect @ 0x1] black_start:0 black_end:1 black_duration:1
[silencedetect @ 0x2] silence_start: 2.01
[silencedetect @ 0x2] silence_end: 4.0 | silence_duration: 1.99
[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 1.2
[freezedetect @ 0x3] lavfi.freezedetect.freeze_duration: 4.8
[freezedetect @ 0x3] lavfi.freezedetect.freeze_end: 6
[silencedetect @ 0x2] silence_start: 7.5
  Integrated loudness:
    I:         -14.9 LUFS
"""
    m = analisar_log(log, duracao=8)
    assert m.pretos == [(0, 1)] and m.silencios == [(2.01, 4.0), (7.5, 8)] and m.congelados == [(1.2, 6)]
    assert m.lufs == -14.9


def test_pipeline_reprova_antes_do_upload():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text()
    i = src.index("tecnicos = revisar_tecnica(final_video")
    assert src.index("raise ScriptRejected(\"vídeo reprovado na revisão antes do upload") < i < src.index("upload_video(")
