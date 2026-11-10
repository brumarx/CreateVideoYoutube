"""Trends por canal: palavra inteira e sem fallback fora do nicho."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.trending_topics import topic_for_channel  # noqa: E402


# Exemplos reais do cache de 09/11 que estavam errados
REAL_TRENDS = [
    "anthropic",
    "romarinho",
    "danilo mesquita",
    "mateus solano",
    "superliga brasileira de voleibol masculino",
    "superliga masculina",
    "dira paes",
    "kiki ramos",
    "leo dias",
    "estoque de alimentos defesa civil",
]


@pytest.mark.parametrize("channel", ["aprendervia", "politica"])
def test_nicho_sem_match_nao_recebe_tema_aleatorio(channel):
    assert topic_for_channel(channel, REAL_TRENDS) is None


def test_leo_dias_nao_casa_ia_por_substring():
    assert topic_for_channel("aprendervia", ["leo dias"]) is None


def test_brasileira_nao_casa_lei():
    assert topic_for_channel("politica", ["superliga brasileira de voleibol masculino"]) is None


def test_aprendervia_pega_tema_tech_de_palavra_inteira():
    trends = ["leo dias", "ChatGPT lança agente de IA para empresas"]
    assert topic_for_channel("aprendervia", trends) == "ChatGPT lança agente de IA para empresas"


def test_politica_pega_tema_de_politica():
    trends = ["anthropic", "Câmara aprova reforma tributária"]
    assert topic_for_channel("politica", trends) == "Câmara aprova reforma tributária"


def test_curiosidades_sem_nicho_nao_recebe_tema():
    assert topic_for_channel("curiosidades", ["anthropic", "leo dias", "romarinho"]) is None


def test_curiosidades_pega_tema_de_lugar_ou_ciencia():
    trends = ["anthropic", "Usina nuclear de Akkuyu gera polêmica"]
    assert topic_for_channel("curiosidades", trends) == "Usina nuclear de Akkuyu gera polêmica"


def test_motivacional_exige_nicho():
    assert topic_for_channel("motivacional", REAL_TRENDS) is None
    assert topic_for_channel("motivacional", ["Atleta supera lesão e volta a treinar"]) == "Atleta supera lesão e volta a treinar"
