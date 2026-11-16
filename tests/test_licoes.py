from src import licoes
from src.script_gen import _ERRO_PREFIXOS


def test_erro_vira_regra_permanente_so_depois_de_repetir(tmp_path):
    db = tmp_path / "q.db"
    licoes.registrar("aprendervia", "atribuicao_falsa", "Gemini reconhece sotaques (fonte não diz)", db)
    assert licoes.regras("aprendervia", db=db) == ""          # 1 vez: ainda não é regra
    licoes.registrar("aprendervia", "atribuicao_falsa", "Whisper roda no Gemini (fonte não diz)", db)
    r = licoes.regras("aprendervia", db=db)
    assert "atribuicao_falsa" in r and "NUNCA" not in r.split("atribuicao_falsa")[0]
    assert licoes.regras("curiosidades", db=db) == ""         # lição é por canal


def test_mesmo_erro_repetido_entra_na_lista_e_nao_expira(tmp_path):
    db = tmp_path / "q.db"
    for _ in range(3):
        licoes.registrar("botafogo", "video", "a mesma imagem aparece em 3 de 4 cenas", db)
    assert "(3x) a mesma imagem aparece em 3 de 4 cenas" in licoes.regras("botafogo", db=db)


def test_registrar_problemas_usa_tipo_e_motivo(tmp_path):
    db = tmp_path / "q.db"
    licoes.registrar_problemas("motivacional", [{"tipo": "fora_da_fonte", "termo": "X", "motivo": "sem fonte"},
                                                {"tipo": "fora_da_fonte", "termo": "Y", "motivo": "sem fonte"}], db)
    assert "fora_da_fonte" in licoes.regras("motivacional", db=db)


def test_importar_historico_so_uma_vez(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "q.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, channel TEXT, error TEXT)")
    c.execute("INSERT INTO jobs (channel, error) VALUES ('aprendervia', 'roteiro reprovado na revisão de fatos: A (m); B (n)')")
    c.commit(); c.close()
    assert licoes.importar_historico(db) == 2
    assert licoes.importar_historico(db) == 0


def test_todo_texto_de_descarte_do_pipeline_e_aprendido():
    for p in ("ressalva de fato sem resolver — roteiro descartado: ", "vídeo com ressalva não sobe: ",
              "bloqueio factual depois de uma correção: ", "áudio reprovado antes da montagem: "):
        assert p in _ERRO_PREFIXOS


def test_humano_pesa_mais_e_aparece_no_painel(tmp_path):
    db = tmp_path / "q.db"
    licoes.registrar_humano("aprendervia", "tema X", "apagado: ressalva", db)
    p = licoes.painel(db)
    assert p["canais"]["aprendervia"]["humanos"][0]["vezes"] == licoes.PESO_HUMANO
    assert "DONO" in licoes.regras("aprendervia", db=db)


def test_tema_que_reprovou_3x_nao_e_sorteado(tmp_path):
    import sqlite3
    db = tmp_path / "q.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, channel TEXT, topic TEXT, status TEXT, error TEXT, created_at TEXT)")
    for _ in range(3):
        c.execute("INSERT INTO jobs (channel, topic, status, error, created_at) VALUES "
                  "('aprendervia', 'Como usar IA para transcrever áudio com Whisper', 'retried', "
                  "'roteiro reprovado na revisão de fatos: x', datetime('now'))")
    c.commit(); c.close()
    assert licoes.tema_reprovado("aprendervia", "Como transcrever áudio com Whisper usando IA", db)
    assert licoes.tema_reprovado("aprendervia", "Receita de bolo de cenoura", db) is None
    assert licoes.tema_reprovado("curiosidades", "Como usar IA para transcrever áudio com Whisper", db) is None


def test_erro_que_volta_com_regra_pede_trava_uma_vez(tmp_path):
    db = tmp_path / "q.db"
    for _ in range(3):
        licoes.registrar("motivacional", "fora_da_fonte", "frase X sem fonte", db)
    assert len(licoes.pendentes_de_trava(db=db)) == 1
    assert licoes.pendentes_de_trava(db=db) == []
