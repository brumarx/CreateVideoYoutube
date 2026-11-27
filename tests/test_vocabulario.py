"""26/11: vocabulário central (meses, lugares) e leitor único de quantidades (temperatura em faixa, sinal)."""
import pytest

from src import vocabulario as v
from src.unidades import quantidades


def test_datas_em_tres_idiomas_e_mes_sozinho_nao_e_data():
    assert v.datas("on 7 January 2015") == {(7, 1, 2015)}
    assert v.datas("em 7 de janeiro de 2015") == {(7, 1, 2015)}
    assert v.datas("January 7, 2015") == {(7, 1, 2015)}
    assert v.datas("07/01/2015") == {(7, 1, 2015)}
    assert v.datas("2015-01-07") == {(7, 1, 2015)}
    assert v.datas("em março de 2008") == {(None, 3, 2008)}
    assert v.datas("it may be maritime setting") == set()


def test_lugar_em_qualquer_idioma():
    assert v.lugar("Norway") == v.lugar("Noruega") == "NO"
    assert v.lugar("EUA") == v.lugar("United States") == "US"
    assert v.lugar("Ártico") and v.lugar("Minas Gerais")
    assert v.mesmo_lugar_no_texto("Síria", "the civil war in Syria")
    assert not v.mesmo_lugar_no_texto("Líbano", "in Morocco")
    assert "BR-Rio Grande do Sul" in v.lugares_no_texto("no Rio Grande do Sul")


@pytest.mark.parametrize("texto,esperado", [
    ("between minus 3 and 4°C", [-3.0, -4.0]),
    ("3-4 °C", [3.0, 4.0]),                       # hífen de faixa não é sinal
    ("a constant minus 18C.", [-18.0]),
    ("entre 3 e 4 graus negativos", [-3.0, -4.0]),
    ("de 10 a 15 km", [10.0, 15.0]),
    ("78° N", []),                                 # coordenada não é temperatura
    ("−64,4 °C", [-64.4]),
])
def test_leitor_unico_de_quantidades(texto, esperado):
    assert [q["valor"] for q in quantidades(texto)] == esperado


def test_nome_de_lugar_traduzido_nao_e_nome_novo_na_correcao():
    from src import redacao as rd
    banco = {"evidencias": [{"id": "E1", "afirmacao": "The war prevented access.", "status": "oficial",
                             "citacoes": [{"trecho": "The civil war in Syria prevented ICARDA from accessing Aleppo."}]}]}
    orig = [{"texto": "A guerra impediu o acesso.", "evidencias": ["E1"]}]
    nova = [{"texto": "A guerra na Síria impediu o acesso.", "evidencias": ["E1"]}]
    assert rd.nome_novo_na_correcao(nova, orig, ["FACTUAL x"], banco) is None
    nova2 = [{"texto": "A guerra no Líbano impediu o acesso.", "evidencias": ["E1"]}]
    assert rd.nome_novo_na_correcao(nova2, orig, ["FACTUAL x"], banco)


def test_nomes_da_narracao_sem_palavra_de_comeco_de_frase():
    from src.pronuncia import nomes_da_narracao
    t = "Em 2015, o ICARDA retirou sementes. Lá dentro, Dyveke Sanne fez a obra em Longyearbyen, no Rio de Janeiro."
    assert nomes_da_narracao(t) == ["ICARDA", "Dyveke Sanne", "Longyearbyen", "Rio de Janeiro"]


@pytest.mark.pronuncia
def test_pronuncia_central_aprende_e_reaproveita(monkeypatch):
    from src import pronuncia, providers
    chamadas = []

    def falso(messages, **k):
        chamadas.append(messages)
        return '{"nomes": {"Longyearbyen": "Lóngüierbüen", "Crop Trust": "en", "Noruega": "pt"}}'
    monkeypatch.setattr(providers, "complete", falso)
    s = {"scenes": [{"narration": "A Noruega e o Crop Trust levam caixas a Longyearbyen."}]}
    assert pronuncia.preparar(s) == ({"Longyearbyen": "Lóngüierbüen"}, {"Crop Trust": "en"})
    assert pronuncia.preparar(s) == ({"Longyearbyen": "Lóngüierbüen"}, {"Crop Trust": "en"})
    assert len(chamadas) == 1                      # 2ª vez sai da tabela, sem chamada
    pronuncia.confirmar_fala("Longyearbyen", "Lôngierbíen")
    assert pronuncia.preparar(s)[0] == {"Longyearbyen": "Lôngierbíen"}


@pytest.mark.parametrize("texto,unidade", [("funciona 24h por dia", "h"), ("cresceu 35%", "%"),
                                           ("usina de 300 megawatts", "MW"), ("gera 4,5 GWh", "GWh")])
def test_unidades_achadas_no_corpus(texto, unidade):
    """26/11: scripts/minerar_corpus.py passou todos os vídeos pelo leitor — horas, %, potência não eram lidos."""
    assert [q["unidade"] for q in quantidades(texto)] == [unidade]


def test_palavra_de_comeco_de_frase_nao_e_nome():
    from src.pronuncia import nomes_da_narracao
    assert nomes_da_narracao("Imagine um cofre. Primeira coisa: imagine a Noruega. primeira vez.") == ["Noruega"]


def test_classe_de_pronuncia_invalida_nao_entra():
    from src.pronuncia import _valida
    assert _valida("Gaudí", "ca") is None
    assert _valida("Hospital Xiaotangshan", "Shiáotángshan") is None
    assert _valida("Longyearbyen", "Lóngüierbüen") == "Lóngüierbüen"
    assert _valida("Crop Trust", "EN") == "en"


def test_unica_sem_suporte_reprovada_na_escrita():
    """Svalbard 1029: "Foi a primeira e única retirada" — a fonte dizia só "the first withdrawal"."""
    from src import redacao as rd
    banco = {"evidencias": [{"id": "E1", "afirmacao": "Em 2015 o ICARDA fez a primeira retirada de sementes.",
                             "status": "oficial", "numeros": ["2015"],
                             "citacoes": [{"trecho": "In 2015 ICARDA made the first withdrawal.", "verificado": True}]}]}
    def roteiro(t):
        return {"scenes": [{"frases": [{"texto": t, "tipo": "factual", "evidencias": ["E1"]}], "narration": t}]}
    s2, probs = rd.auditar_codigo(roteiro("Em 2015 o ICARDA fez a primeira e única retirada."), banco, "Svalbard")
    assert probs == [] and s2["scenes"][0]["frases"][0]["texto"] == "Em 2015 o ICARDA fez a primeira retirada."
    _, probs = rd.auditar_codigo(roteiro("Em 2015 o ICARDA fez sempre a retirada."), banco, "Svalbard")
    assert [p["tipo"] for p in probs] == ["escala_sem_suporte"]   # "sempre" não se remove: vai para o conserto
    _, probs = rd.auditar_codigo(roteiro("Em 2015 o ICARDA fez a primeira retirada."), banco, "Svalbard")
    assert probs == []


def test_pedido_do_fechamento_e_conferencia_usam_o_mesmo_lexico():
    from src import lexico, redacao as rd
    assert rd._ALEGA.pattern == lexico.garantia().pattern
    assert all(f'"{p}"' in rd._PALAVRAS_ALEGA for p in lexico.palavras_de_garantia())


def test_comeco_de_frase_no_meio_do_texto_nao_e_nome():
    from src import redacao as rd
    assert rd.afirmacao_escondida("Essa burocracia funciona na prática? Alguém precisou sacar o seguro?", set()) is None
    assert rd.afirmacao_escondida("Comenta SEMENTE se você achava que era gelo.", set()) is None
    assert rd.afirmacao_escondida("E foi assim que Svalbard virou notícia.", set()) == "nome próprio (Svalbard)"


def test_plano_nao_pede_mais_perguntas_do_que_a_regra_permite():
    """26/11: o plano em cadeia pedia uma pergunta por bloco; a regra anti-IA permite 1 no meio — 7 perguntas,
    escritor cobrado em toda tentativa. O pedido tem de dizer o mesmo limite da regra."""
    from src import redacao as rd
    from src.script_gen import MAX_PERGUNTAS_MEIO
    plano = {"angulo": {"pergunta": "p", "resposta": "r"}, "blocos": [{"funcao": "a", "conta": "c", "evidencias": []}]}
    texto = rd.material_do_plano(plano, {"evidencias": []})
    assert MAX_PERGUNTAS_MEIO == 1 and "no máximo UMA pergunta" in texto and "nunca com pergunta retórica" in texto


@pytest.mark.parametrize("texto,ok", [
    ("O cofre guarda mais de um milhão de amostras.", True),     # o próprio tema diz assim; a fonte, 1.417.799
    ("O cofre guarda mais de 1,4 milhão de amostras.", True),
    ("O cofre guarda quase 1,5 milhão de amostras.", True),
    ("O cofre guarda mais de 100 mil amostras.", False),         # folgada
    ("O cofre guarda mais de 2 milhões de amostras.", False),    # falsa
    ("O cofre guarda quase 2 milhões de amostras.", False),      # enganosa
    ("O cofre guarda 1 milhão de amostras.", False),             # seco e errado
])
def test_comparacao_justa_com_numero_redondo(texto, ok):
    """Varredura 26/11 pedido × código: o pedido manda arredondar, o código reprovava "mais de um milhão"."""
    from src import redacao as rd
    banco = {"evidencias": [{"id": "E1", "afirmacao": "O cofre guarda 1.417.799 amostras.", "status": "oficial",
                             "numeros": ["1417799"], "citacoes": []}]}
    s = {"scenes": [{"frases": [{"texto": texto, "tipo": "factual", "evidencias": ["E1"]}], "narration": texto}]}
    assert (rd.auditar_codigo(s, banco, "Svalbard")[1] == []) is ok


@pytest.mark.parametrize("texto,certo", [
    ("mais de 100 milhão de sementes", "mais de 100 milhões de sementes"),
    ("1,5 milhões de amostras", "1,5 milhão de amostras"),
    ("1 milhões", "1 milhão"),
    ("2 milhão", "2 milhões"),
    ("1,4 milhão", "1,4 milhão"),
    ("1.200 bilhão", "1.200 bilhões"),
    ("Mais de 300 Milhão", "Mais de 300 Milhões"),
])
def test_concordancia_de_escala(texto, certo):
    assert v.concordar_escalas(texto) == certo


@pytest.mark.parametrize("texto,certo", [
    ("cem milhão de pessoas", "cem milhões de pessoas"),
    ("dois milhão", "dois milhões"),
    ("um milhões", "um milhão"),
    ("uma milhões", "um milhão"),
    ("vinte e um milhão", "vinte e um milhões"),
    ("duzentas milhões de pessoas", "duzentos milhões de pessoas"),
    ("duzentas e duas milhões", "duzentos e dois milhões"),
    ("três bilhão", "três bilhões"),
    ("1.5 milhões", "1,5 milhão"),
    ("1.200 milhão", "1.200 milhões"),          # ponto de milhar, não decimal
    ("100 mi de pessoas", "100 milhões de pessoas"),
    ("2 bi de reais", "2 bilhões de reais"),
    ("500 mi", "500 mi"),                       # sem "de" pode ser milha: fica
    ("duzentas mil pessoas", "duzentas mil pessoas"),  # "mil" concorda com o substantivo: certo
])
def test_variantes_do_erro_de_escala(texto, certo):
    """26/11: o dono deu "mais de 100 milhão" como exemplo — a família inteira do erro, não só o caso."""
    assert v.concordar_escalas(texto) == certo


@pytest.mark.parametrize("texto,certo", [
    ("R$ 1,5 bi", "R$ 1,5 bilhão"),
    ("US$ 1,000,000", "US$ 1.000.000"),
    ("US$ 3.5 billion", "US$ 3,5 bilhões"),
    ("$ 5 million", "$ 5 milhões"),
    ("R$ 3 mil reais", "3 mil reais"),
    ("R$ 100 milhões de reais", "100 milhões de reais"),
    ("€ 2 milhão", "€ 2 milhões"),
    ("1 reais", "1 real"), ("2 real", "2 reais"), ("1 dólares", "1 dólar"), ("1 centavos", "1 centavo"),
    ("1 anos", "1 ano"), ("1 horas", "1 hora"), ("2 quilômetro", "2 quilômetros"), ("3 vez", "3 vezes"),
    # o que NÃO pode mudar
    ("1,50 reais", "1,50 reais"), ("21 anos", "21 anos"), ("1 milhão de anos", "1 milhão de anos"),
    ("1.500 metros", "1.500 metros"), ("R$ 5 dólares", "R$ 5 dólares"), ("1,000 graus", "1,000 graus"),
])
def test_familia_dinheiro_e_contaveis(texto, certo):
    """26/11: depois do "100 milhão", o dono: "e se temos outras opções" — moeda, formato inglês, abreviação,
    moeda repetida e concordância do número com o que ele conta."""
    assert v.normalizar_valores(texto) == certo


def test_voz_fala_moeda_da_tabela_unica():
    from src.tts import _for_speech
    assert _for_speech(v.normalizar_valores("¥ 100 milhões"))[0] == "100 milhões de ienes"
    assert _for_speech(v.normalizar_valores("US$ 1,000,000"))[0] == "1 milhão de dólares"


def test_siglas_da_tabela_central_na_voz_e_na_legenda(monkeypatch):
    """26/11: tabela de siglas — e os conflitos achados na voz antes de ligar (US$, PT-BA, SE, SAF, CEO)."""
    from src import siglas, tts
    monkeypatch.setattr(siglas, "_carregar", lambda: {"TCU": "soletra", "ONU": "palavra", "US": "soletra",
                                                      "SE": "soletra", "SAF": "soletra", "BA": "soletra"})
    def fala(t):
        return tts._for_speech(t, tts._state_abbreviation_pronunciations(t))[0]
    assert fala("o TCU e a ONU") == "o tê cê u e a Onu"
    assert fala("US$ 5 milhões") == "5 milhões de dólares"
    assert fala("QUE SE VIREM") == "Que Se Virem"
    assert fala("a SAF do Botafogo") == "a Sáfi do Botafogo"
    assert fala("o deputado do PT-BA") == "o deputado do pê tê Bahia"     # erro antigo: "pê tê-BA"
    assert fala("o Atlético-MG venceu") == "o Atlético Mineiro venceu"
    wb = [{"text": w, "start": i, "end": i + 1} for i, w in enumerate("do pê tê Bahia e o tê cê u".split())]
    assert [w["text"] for w in tts._join_multiword(wb, {"PT-BA": "PT Bahia"})] == ["do", "PT-BA", "e", "o", "TCU"]


def test_cidades_em_qualquer_idioma():
    """26/11: cidades do GeoNames (100 mil+ e capitais) — o corpus tinha Tóquio, Marrakech, Natal fora da tabela."""
    assert v.mesmo_lugar_no_texto("Tóquio", "The trains in Tokyo")
    assert v.mesmo_lugar_no_texto("Pequim", "Beijing hosted the games")
    assert v.mesmo_lugar_no_texto("Marrakech", "in Marrakesh")
    assert v.mesmo_lugar_no_texto("Alepo", "the genebank in Aleppo")
    assert not v.lugares_no_texto("o natal e a vitória")          # cidade só com maiúscula
    assert not any(f.isupper() for l in v._dados()["lugares"] if l["tipo"] == "cidade" for f in l["formas"])


@pytest.mark.parametrize("texto,esperado,problema", [
    ("Em 2015 o ICARDA fez a primeira e única retirada.", "Em 2015 o ICARDA fez a primeira retirada.", False),
    ("Em 2015 o ICARDA reformou totalmente o banco.", "Em 2015 o ICARDA reformou o banco.", False),
    ("Em 2015 o ICARDA nunca retirou sementes.", None, True),          # "nunca": não se remove (inverte)
])
def test_escala_removivel_corrigida_em_codigo(texto, esperado, problema):
    from src import redacao as rd
    banco = {"evidencias": [{"id": "E1", "afirmacao": "Em 2015 o ICARDA fez a primeira retirada e reformou o banco.",
                             "status": "oficial", "numeros": ["2015"], "citacoes": []}]}
    s = {"scenes": [{"frases": [{"texto": texto, "tipo": "factual", "evidencias": ["E1"]}], "narration": texto}]}
    s2, probs = rd.auditar_codigo(s, banco, "x")
    assert bool(probs) is problema
    if esperado:
        assert s2["scenes"][0]["frases"][0]["texto"] == esperado


@pytest.mark.parametrize("frases,problema", [
    (["Em 2026, o cofre passou de 1,4 milhão de amostras.", "Isso representa cerca de um terço da diversidade."], True),
    (["Em 2026, o cofre passou de 1,4 milhão de amostras, cerca de um terço da diversidade."], True),
    (["Em 2026, o cofre passou de 1,4 milhão de amostras.", "Segundo uma revisão, cerca de um terço da diversidade já estava lá em 2019."], False),
    (["Em 2026, o cofre passou de 1,4 milhão de amostras.", "O cofre fica numa montanha."], False),
])
def test_data_estendida_a_dado_sem_data(frases, problema):
    """Svalbard 1029/1033: o "um terço" (revisão antiga) lido como de 2026."""
    from src import redacao as rd
    banco = {"evidencias": [
        {"id": "E2", "afirmacao": "Em 2026, o cofre ultrapassou 1,4 milhão de amostras.", "status": "oficial",
         "numeros": ["2026", "1.4", "1400000"], "citacoes": []},
        {"id": "E106", "afirmacao": "Aproximadamente um terço da diversidade está no cofre.", "status": "oficial",
         "numeros": [], "citacoes": []},
        {"id": "E5", "afirmacao": "Uma revisão de 2019 estimou um terço da diversidade no cofre.", "status": "oficial",
         "numeros": ["2019"], "citacoes": []},
        {"id": "E9", "afirmacao": "O cofre fica numa montanha.", "status": "oficial", "numeros": [], "citacoes": []}]}
    evid = {0: ["E2"], 1: ["E106"]}
    if len(frases) == 1:
        evid = {0: ["E2", "E106"]}
    elif "2019" in frases[1]:
        evid = {0: ["E2"], 1: ["E5"]}
    elif "montanha" in frases[1]:
        evid = {0: ["E2"], 1: ["E9"]}
    s = {"scenes": [{"frases": [{"texto": t, "tipo": "factual", "evidencias": evid[k]} for k, t in enumerate(frases)],
                     "narration": " ".join(frases)}]}
    tipos = [p["tipo"] for p in rd.auditar_codigo(s, banco, "Svalbard")[1]]
    assert ("data_estendida" in tipos) is problema


@pytest.mark.parametrize("segunda,problema", [
    ("Uma revisão estimou cerca de um terço da diversidade no cofre.", False),           # diz a própria época
    ("Uma revisão de anos atrás estimava que isso cobria um terço da diversidade.", True),  # 1033 v2: "isso" amarra
])
def test_data_estendida_epoca_propria_e_amarracao(segunda, problema):
    from src import redacao as rd
    banco = {"evidencias": [
        {"id": "E2", "afirmacao": "Em 2026, o cofre ultrapassou 1,4 milhão de amostras.", "status": "oficial",
         "numeros": ["2026", "1.4", "1400000"], "citacoes": []},
        {"id": "E106", "afirmacao": "Aproximadamente um terço da diversidade está no cofre.", "status": "oficial",
         "numeros": [], "citacoes": []}]}
    fr = [{"texto": "Em 2026, o cofre passou de 1,4 milhão de amostras.", "tipo": "factual", "evidencias": ["E2"]},
          {"texto": segunda, "tipo": "factual", "evidencias": ["E106"]}]
    s = {"scenes": [{"frases": fr, "narration": " ".join(f["texto"] for f in fr)}]}
    assert ("data_estendida" in [p["tipo"] for p in rd.auditar_codigo(s, banco, "x")[1]]) is problema


def test_foto_real_ordenada_pelo_que_a_cena_mostra(monkeypatch):
    """26/11: fotos do próprio verbete; a que combina com a cena (caixas × entrada) vem primeiro."""
    from src import real_media as rm
    fotos = [{"title": "File:Entrance to Svalbard Global Seed Vault in 2008.jpg", "url": "u1", "credit": "c", "descricao": ""},
             {"title": "File:Storage containers in Svalbard Global Seed Vault 01.jpg", "url": "u2", "credit": "c",
              "descricao": "Seed boxes stored on shelves"}]
    monkeypatch.setattr(rm, "_search", lambda s, k: list(fotos))
    caixas = rm.real_photos("Svalbard Global Seed Vault", set(), contexto="seed boxes on shelves in a cold chamber")
    entrada = rm.real_photos("Svalbard Global Seed Vault", set(), contexto="the entrance of the vault in the snow")
    assert caixas[0]["url"] == "u2" and entrada[0]["url"] == "u1"
    assert rm.real_photos("Svalbard Global Seed Vault", set())[0]["url"] == "u1"   # sem contexto: ordem de antes


@pytest.mark.parametrize("titulo,certo", [
    ("1,4 milhão de sementes congeladas no Ártico", "1,4 milhão de amostras congeladas no Ártico"),
    ("1,4 milhão de amostras numa montanha", "1,4 milhão de amostras numa montanha"),
    ("O cofre do Ártico", "O cofre do Ártico"),
    ("134 países guardam sementes", "134 instituições guardam sementes"),
])
def test_numero_do_titulo_conta_o_mesmo_que_a_narracao(titulo, certo):
    narr = "Em 2026, o cofre passa de 1,4 milhão de amostras de 134 instituições."
    assert v.contagem_trocada(titulo, narr) == certo


def test_dia_da_semana_e_o_de_uma_data_dos_fatos():
    """27/11 (Botafogo 1049): "o resultado de sexta-feira" para o jogo de 28/11/2026, um sábado."""
    from datetime import date
    from src.vocabulario import corrigir_dia_da_semana as c
    ref = "Botafogo x Bahia 28/11/2026 às 21h00; Santos x Botafogo 02/12/2026; publicado 2026-11-26T16:50"
    h = date(2026, 11, 27)
    assert c("O resultado de sexta-feira define a semana.", ref, h)[0] == "O resultado de sábado define a semana."
    assert c("Nesta segunda-feira tem jogo.", ref, h)[0] == "Neste sábado tem jogo."  # quinta é a data da matéria
    # o certo vizinho e o que não é dia: nada muda
    for certo in ("Neste sábado o Botafogo joga.", "Na quarta-feira tem Santos.", "Ele está na sexta posição.",
                  "Hoje, sexta-feira, o elenco treinou.", "A sexta-feira foi tensa.", "Na quinta-feira o portal publicou."):
        assert c(certo, ref, h)[0] == certo, certo
    assert c("Na sexta-feira tem jogo.", "sem data", h)[0] == "Na sexta-feira tem jogo."
