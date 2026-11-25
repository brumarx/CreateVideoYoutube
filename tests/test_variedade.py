"""25/11: variedade entre vídeos do canal (src/variedade.py) — música em rodízio, item de banco sem repetir em
30 vídeos, crédito na descrição e subtema em 14 dias."""
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import variedade


@pytest.fixture(autouse=True)
def historico(tmp_path, monkeypatch):
    monkeypatch.setattr(variedade, "HISTORICO", tmp_path / "variedade.json")
    yield
    variedade.encerrar_video()


def test_musica_em_rodizio_pela_menos_recente():
    faixas = [Path(f"{n}.mp3") for n in "abc"]
    rng = random.Random(1)
    usadas = [variedade.escolher_musica("canal", v, faixas, rng).name for v in range(6)]
    assert sorted(usadas[:3]) == ["a.mp3", "b.mp3", "c.mp3"] and usadas[3:] == usadas[:3]  # nenhuma repete antes das outras


def test_remontagem_do_mesmo_video_mantem_a_faixa_e_canais_sao_separados():
    faixas = [Path(f"{n}.mp3") for n in "ab"]
    primeira = variedade.escolher_musica("canal", 7, faixas)
    assert variedade.escolher_musica("canal", 7, faixas) == primeira
    assert variedade.escolher_musica("canal", 8, faixas) != primeira
    assert variedade.escolher_musica("outro", 1, faixas) in faixas


def test_item_de_banco_bloqueado_por_30_videos_do_mesmo_canal():
    variedade.iniciar_video("canal", 1)
    variedade.registrar_midia("video:123", variedade.credito("Ana, Silva", "Pexels", "licença Pexels", "https://p/1"))
    assert variedade.creditos_do_video() == ["Ana Silva — Pexels (licença Pexels): https://p/1"]
    assert not variedade.midia_bloqueada("video:123")  # o próprio vídeo (remontagem) pode repetir
    variedade.iniciar_video("outro", 2)
    assert not variedade.midia_bloqueada("video:123")  # outro canal
    for v in range(2, 2 + variedade.JANELA_MIDIA):
        variedade.iniciar_video("canal", v)
        assert variedade.midia_bloqueada("video:123")
        variedade.registrar_midia(f"video:x{v}")
    variedade.iniciar_video("canal", 999)
    assert not variedade.midia_bloqueada("video:123")  # já passou de 30 vídeos


def test_sem_video_iniciado_nada_e_gravado(tmp_path):
    variedade.registrar_midia("video:1", "crédito")
    assert not variedade.midia_bloqueada("video:1") and variedade.creditos_do_video() == []
    assert not variedade.HISTORICO.exists()


def test_busca_de_banco_pula_item_bloqueado_e_guarda_credito(monkeypatch):
    from src import stock_media
    monkeypatch.setattr(stock_media, "_RECENT_STATE_FILE", variedade.HISTORICO.parent / "recent.json")
    variedade.iniciar_video("canal", 1)
    variedade.registrar_midia("pixabay_photo:1")
    variedade.iniciar_video("canal", 2)
    hits = [{"id": 1, "tags": "tunnel"}, {"id": 2, "tags": "tunnel"}]
    assert [h["id"] for h in stock_media._rank_candidates("tunnel", hits, "pixabay_photo", lambda h: h["tags"])] == [2]

    class Resp:
        content = b"img"
        def raise_for_status(self): pass
    monkeypatch.setattr(stock_media, "_pixabay_hits", lambda *a, **k: [
        {"id": 1, "tags": "tunnel", "largeImageURL": "u1"},
        {"id": 2, "tags": "tunnel", "largeImageURL": "u2", "user": "Bea", "pageURL": "https://pixabay.com/p-2"}])
    monkeypatch.setattr(stock_media.httpx, "get", lambda *a, **k: Resp())
    assert stock_media._pixabay_photo("tunnel", 1080, 1920, None) == b"img"
    assert variedade.creditos_do_video() == ["Bea — Pixabay (licença Pixabay): https://pixabay.com/p-2"]


def test_add_background_music_usa_o_rodizio(tmp_path):
    from src import assemble
    musicas = tmp_path / "musicas"
    musicas.mkdir()
    for n in ("a.mp3", "b.mp3"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(musicas / n)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=1", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    for job in (1, 2):
        assemble.add_background_music(video, tmp_path / f"s{job}.mp4", music_dir=musicas, canal="canal", video=job)
    import json
    usadas = [e["musica"] for e in json.loads(variedade.HISTORICO.read_text())["canal"]]
    assert sorted(usadas) == ["a.mp3", "b.mp3"]


def test_subtema_repetido_em_14_dias():
    agora = datetime(2026, 11, 25, tzinfo=timezone.utc)
    recentes = [("Svalbard: a cidade onde é proibido morrer", (agora - timedelta(days=3)).isoformat()),
                ("O túnel Seikan, o mais longo do Japão", (agora - timedelta(days=20)).isoformat())]
    assert variedade.subtema_repetido("Por que é proibido morrer em Svalbard", recentes, agora=agora)
    assert variedade.subtema_repetido("O túnel Seikan por dentro", recentes, agora=agora) is None  # 20 dias
    assert variedade.subtema_repetido("Vulcões da Islândia", recentes, agora=agora) is None


def test_pipeline_inicia_o_video_e_passa_canal_para_a_musica():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py").read_text()
    assert "variedade.iniciar_video(channel_name, job_id)" in src
    assert "canal=channel_name, video=job_id" in src
    assert src.index("variedade.creditos_do_video()") < src.index("hashtags = limitar_hashtags(")
