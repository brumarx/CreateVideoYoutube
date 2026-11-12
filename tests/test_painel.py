"""Regras do painel (web/app.py) e do comentário guardado pra aprovação."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))

import app as painel  # noqa: E402
from src import script_archive  # noqa: E402


def test_causas_novas_nao_caem_em_outros():
    casos = {
        "revisor de fatos indisponível — vídeo não sai sem revisão": "Revisor de fatos fora do ar (sem cota)",
        "roteiro final ficou magro depois das remoções: 87 palavras": "Roteiro ficou curto demais depois dos cortes",
        "sem correspondência segura e sem fato verificado para trocar": "Revisão de fatos reprovou o roteiro",
        "fatos verificados indisponíveis (extração das fontes falhou)": "Fontes não deram fatos verificados",
        "sequence item 1: expected str instance, NoneType found": "Erro de código (já corrigido ou a investigar)",
        "teste de roteiro (rodadas de validação do Fractal), não publicar": "Teste ou descartado à mão",
        "órfão: processo não existe mais": "Travou (tempo esgotado ou processo sumiu)",
        "": "Sem mensagem de erro",
    }
    for erro, causa in casos.items():
        assert painel._failure_cause(erro) == causa, erro


def test_reenviar_so_com_video_aprovado(tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x")
    job = SimpleNamespace(status="failed", video_path=str(video))
    assert not painel._has_video_to_send(job)  # sem upload.json = não passou da revisão do vídeo
    (tmp_path / "upload.json").write_text("{}")
    assert painel._has_video_to_send(job)
    assert not painel._has_video_to_send(SimpleNamespace(status="failed", video_path=None))  # falha de roteiro
    assert not painel._has_video_to_send(SimpleNamespace(status="abandoned", video_path=str(video)))


def test_arquivo_guarda_comentario_para_publicar_depois(tmp_path, monkeypatch):
    monkeypatch.setattr(script_archive, "ARCHIVE_DIR", tmp_path / "arq")
    work = tmp_path / "job"
    work.mkdir()
    (work / "script.json").write_text(json.dumps({"title": "T", "scenes": [{"narration": "Frase."}]}))
    (work / "upload.json").write_text(json.dumps({"comment": "Você teria coragem?"}))
    script_archive.archive("curiosidades", 1, work, "vid")
    assert script_archive.archived("curiosidades", 1)["comment"] == "Você teria coragem?"
