"""25/11: checagens determinísticas de texto com cara de IA e de título/descrição (src/qualidade_texto.py)."""
from src.qualidade_texto import (
    cara_de_ia, defeitos_de_texto, limitar_hashtags, palavra_chave, titulo_e_descricao,
)


def test_dois_chavoes_distintos_sao_erro():
    erros, avisos = cara_de_ia(["O túnel tem 53 km. Além disso, é crucial lembrar do custo."])
    assert erros and "alem disso" in erros[0] and "e crucial" in erros[0]


def test_um_chavao_e_aviso_e_acento_nao_importa():
    erros, avisos = cara_de_ia(["O túnel tem 53 km. Alem disso, custou caro."])
    assert not erros and avisos and "alem disso" in avisos[0]


def test_mundo_fascinante_conta_uma_vez_so():
    erros, avisos = cara_de_ia(["O túnel tem 53 km. No fascinante mundo das obras, ele lidera."])
    assert not erros and len(avisos) == 1


def test_nao_e_apenas_x_e_y_e_ingles():
    erros, _ = cara_de_ia(["O túnel não é apenas longo, é o mais fundo. Um testament to engenharia."])
    assert erros


def test_texto_direto_passa():
    erros, avisos = cara_de_ia(["O túnel Seikan tem 53 km e liga Honshu a Hokkaido.",
                                "Levou 17 anos para ficar pronto."])
    assert erros == [] and avisos == []


def test_travessao_demais_e_aviso():
    _, avisos = cara_de_ia(["O túnel — o maior — liga ilhas. Custou caro — muito caro. Abriu em 1988."])
    assert any("travessão" in a for a in avisos)


def test_abertura_generica_depois_do_bordao():
    erros, _ = cara_de_ia(["E aí, mente curiosa! Você sabia que existe um túnel de 53 km?"])
    assert any("abertura genérica" in e for e in erros)
    erros, _ = cara_de_ia(["Hoje vamos falar do túnel Seikan."])
    assert any("abertura genérica" in e for e in erros)


def test_bordao_com_ola_do_canal_nao_e_abertura_generica():
    # "Olá, alvinegros de plantão!" é o bordão do Botafogo (channels/botafogo.yaml), não a abertura
    erros, _ = cara_de_ia(["Olá, alvinegros de plantão! O Botafogo venceu por 2 a 0."])
    assert erros == []
    erros, _ = cara_de_ia(["Bordão Novo! Oi, gente."], saudacoes=["Bordão Novo!"])
    assert erros


def test_defeitos_de_texto_le_frases_e_narracao():
    script = {"_greeting": "E aí, mente curiosa!", "scenes": [
        {"frases": [{"texto": "E aí, mente curiosa!"}, {"texto": "Neste vídeo, o túnel."}], "narration": "x"},
        {"narration": "Vamos explorar e desvendar o mistério."},
    ]}
    d = defeitos_de_texto(script)
    assert any("abertura genérica" in x for x in d) and any("cara de texto de IA" in x for x in d)


def test_palavra_chave_e_titulo():
    assert palavra_chave("O túnel mais longo do arquipélago") == "arquipélago"
    narr = "O túnel Seikan liga as ilhas do arquipélago japonês."
    assert titulo_e_descricao("Seikan: o túnel do arquipélago", "Resumo curto.\n\n#a #b #c", narr) == []


def test_titulo_e_descricao_aponta_cada_problema():
    titulo = "Por que ninguém pode morrer na cidade gelada de Longyearbyen hoje"
    desc = "x" * 130 + "\n" + ", " * 26
    p = titulo_e_descricao(titulo, desc, "Svalbard é gelada.", ["#a", "#b"])
    texto = " | ".join(p)
    assert "título com" in texto and "primeiros 40" in texto and "não é falada" in texto
    assert "1ª linha" in texto and "2 hashtags" in texto and "vírgulas" in texto


def test_hashtags_lidas_da_descricao_e_limitadas():
    assert len(titulo_e_descricao("Túnel Seikan", "Linha.\n\n#a #b #c #d #e #f", "túnel seikan")) == 1
    assert limitar_hashtags(["#Shorts", "#IA", "ia", "#ia", "#A", "#B", "#C", "#D"]) == ["#Shorts", "#IA", "#A", "#B", "#C"]
