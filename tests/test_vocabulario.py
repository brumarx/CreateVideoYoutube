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
