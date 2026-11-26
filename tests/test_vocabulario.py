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
    _, probs = rd.auditar_codigo(roteiro("Em 2015 o ICARDA fez a primeira e única retirada."), banco, "Svalbard")
    assert [p["tipo"] for p in probs] == ["escala_sem_suporte"]
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
