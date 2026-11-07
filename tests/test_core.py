"""Suíte mínima: travas de código que seguram número, tema, roteiro e fala."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fact_check, script_gen, topics, tts  # noqa: E402


# ---------- fact_check: números ----------

@pytest.mark.parametrize("raw, expected", [
    ("3.812", 3812.0),
    ("3,812", 3812.0),
    ("3 812", 3812.0),
    ("3,5", 3.5),
    ("3.5", 3.5),
    ("353.248,04", 353248.04),   # pt-BR com milhar e decimal (job 754-759)
    ("12,345.67", 12345.67),     # en
])
def test_source_number_formats(raw, expected):
    assert fact_check._source_number(raw) == pytest.approx(expected)


def test_numbers_not_in_facts_flags_invented_value():
    facts = {"total": 10_800_000}
    script = {"scenes": [{"narration": "a soma chega a R$ 11,8 milhões"}]}
    problems = fact_check.numbers_not_in_facts(script, facts)
    assert len(problems) == 1 and problems[0]["tipo"] == "fora_da_fonte"


def test_numbers_not_in_facts_accepts_value_from_data():
    facts = {"total": 10_800_000}
    script = {"scenes": [{"narration": "a soma chega a R$ 10,8 milhões"}]}
    assert fact_check.numbers_not_in_facts(script, facts) == []


def test_numbers_not_in_facts_ignores_loose_numbers_and_no_facts():
    script = {"scenes": [{"narration": "em 1998 foram 7 jogos"}]}
    assert fact_check.numbers_not_in_facts(script, {"x": 1}) == []
    assert fact_check.numbers_not_in_facts(script, None) == []


def test_numbers_not_in_sources_uses_two_percent_tolerance():
    web = [{"titulo": "Estádio", "trecho": "o estádio tem 4.800 metros quadrados"}]
    ok = {"scenes": [{"narration": "são 4.800 metros quadrados"}]}
    bad = {"scenes": [{"narration": "são 5.000 metros quadrados"}]}
    assert fact_check.numbers_not_in_sources(ok, web) == []
    assert len(fact_check.numbers_not_in_sources(bad, web)) == 1


def test_numbers_not_in_sources_without_sources_is_empty():
    assert fact_check.numbers_not_in_sources({"scenes": []}, None) == []


# ---------- topics ----------

def test_topic_problem_rejects_garbage_without_llm():
    # tema quebrado é recusado antes de qualquer chamada ao LLM
    assert topics.topic_problem("<think>", "tecnologia") is not None
    assert topics.topic_problem("Claro, aqui estão alguns temas:", "tecnologia") is not None


def test_idea_pool_loads_and_is_deduplicated():
    from src import idea_sources

    ideas = idea_sources.load_ideas("aprendervia")
    assert ideas, "pool de ideias do aprendervia não pode estar vazio"
    assert len(ideas) == len(set(ideas))


def test_github_topic_and_filter():
    from src.idea_sources import filter_repos, github_topic

    assert "188 mil estrelas" in github_topic("Significant-Gravitas/AutoGPT", 187932)
    repos = filter_repos([
        {"full_name": "a/b", "description": "ok"},
        {"full_name": "a/b", "description": "duplicado"},
        {"full_name": "usestrix/strix", "description": "pentest"},
    ])
    assert [r["full_name"] for r in repos] == ["a/b"]


# ---------- script_gen: roteiro parseável ----------

def test_is_parseable_script_requires_all_keys():
    raw = json.dumps({"titulo": "x", "scenes": []})
    assert script_gen._is_parseable_script(raw, {"titulo", "scenes"})
    assert not script_gen._is_parseable_script(raw, {"titulo", "scenes", "descricao"})


def test_is_parseable_script_rejects_unclosed_json():
    assert not script_gen._is_parseable_script("{", {"titulo"})
    assert not script_gen._is_parseable_script("{{{{", {"titulo"})


# ---------- tts: normalização de moeda e números ----------

def test_money_is_spoken_in_words():
    out = tts._money("R$ 44.695,96")
    assert "R$" not in out and "reais" in out.lower()


def test_money_reals_scale():
    out = tts._money("R$ 3 bi")
    assert "bilh" in out and "reais" in out


# ---------- video_qa: mesma imagem ----------

def test_same_visual_share_ignores_none_frames():
    from PIL import Image

    from src import video_qa

    img = Image.new("RGB", (64, 64), "black")
    assert video_qa.same_visual_share([None, None]) == (0, 0)
    assert video_qa.same_visual_share([img, None]) == (1, 1)


def test_same_visual_share_groups_identical_images():
    from PIL import Image, ImageDraw

    from src import video_qa

    a = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(a).rectangle([0, 0, 31, 63], fill="black")
    b = a.copy()
    assert video_qa.same_visual_share([a, b, None]) == (2, 2)


def test_same_visual_share_separates_different_images():
    from PIL import Image, ImageDraw

    from src import video_qa

    a = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(a).rectangle([0, 0, 31, 63], fill="black")
    b = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(b).rectangle([32, 0, 63, 63], fill="black")
    c = Image.new("RGB", (64, 64), "gray")
    assert video_qa.same_visual_share([a, b, c]) == (1, 3)


# ---------- tts: falas que a voz lia errado ----------

@pytest.mark.parametrize("texto, esperado", [
    ("Tite alivia a pressão", "Tchitchi alivia"),
    ("na zona de descenso", "zona de rebaixamento"),
    ("Chelsea vs Arsenal", "Chelsea contra Arsenal"),
    ("saiu de St. John's", "Saint John's"),
])
def test_spoken_fixes(texto, esperado):
    assert esperado in tts._for_speech(texto)[0]


def test_tite_caption_keeps_written_name():
    # a legenda volta a grafia escrita (Tite), não a fonética falada
    assert tts._SPELLING_BACK.get("tchitchi") == "Tite"
