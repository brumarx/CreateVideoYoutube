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


# ---------- redação, promessas e unidades (roteiros publicados) ----------

def _scene(*texts):
    return {"scenes": [{"narration": t} for t in texts]}


def test_broken_sentence_flags_terry_fox_and_svalbard():
    from src.fact_check import broken_sentences

    terry = _scene("Em 12 de abril de 1980, Terry Fox saiu de St. Ele havia perdido uma perna.")
    svalbard = _scene("Svalbard, a milhas ao norte da Noruega, é um laboratório natural.")
    assert any("St." in p["termo"] for p in broken_sentences(terry))
    assert any("milhas" in p["termo"] for p in broken_sentences(svalbard))


def test_broken_sentence_accepts_clean_text():
    from src.fact_check import broken_sentences

    ok = _scene("Terry Fox saiu de St. John's, no Canadá, em 1980.", "Ele correu 42 km por dia.")
    assert broken_sentences(ok) == []


def test_unfulfilled_promise_flags_missing_prompt():
    from src.fact_check import unfulfilled_promises

    nlm = _scene("Copie este prompt no NotebookLM e transforme seus PDFs em tutor.", "Acesse o site.")
    assert len(unfulfilled_promises(nlm)) == 1


def test_promise_ok_when_prompt_is_given():
    from src.fact_check import unfulfilled_promises

    nlm = _scene(
        "Copie este prompt no NotebookLM: “Você é um tutor. Faça perguntas sobre o material e corrija as respostas.”",
    )
    assert unfulfilled_promises(nlm) == []


def test_unit_mismatch_miles_vs_km():
    from src.fact_check import numbers_not_in_sources

    web = [{"titulo": "Terry Fox", "trecho": "Terry percorreu cerca de 5.373 km até parar."}]
    wrong = _scene("Terry completou cerca de 5.300 milhas antes de parar.")
    right_km = _scene("Terry completou cerca de 5.373 km antes de parar.")
    right_mi = _scene("Terry completou cerca de 3.339 milhas antes de parar.")
    assert any(p["tipo"] == "unidade_errada" for p in numbers_not_in_sources(wrong, web))
    assert numbers_not_in_sources(right_km, web) == []
    assert numbers_not_in_sources(right_mi, web) == []


def test_greeting_is_added_once_before_review():
    from src.config import ChannelConfig

    channel = ChannelConfig.load("motivacional")
    script = _scene("Nove vezes ele ouviu não.", "Ele seguiu.")
    once = channel.with_greeting(script)
    twice = channel.with_greeting(once)
    assert once["scenes"][0]["narration"].startswith(once["_greeting"])
    assert twice["scenes"][0]["narration"] == once["scenes"][0]["narration"]


def test_domain_is_not_repeated_word():
    from src.fact_check import broken_sentences

    assert broken_sentences(_scene("Acesse notebooklm.google.com com sua conta Google.")) == []


def test_title_limit_is_60_chars():
    # título acima de 60 caracteres rende menos (dados de 07/11): reprova
    from src.script_gen import _packaging_ok

    ok = {"titulo": "Baleia Rossi: R$ 377 mil de cota em 2025", "thumbnail_text": "R$ 377 mil"}
    longo = {"titulo": "Baleia Rossi gastou R$ 377 mil da cota parlamentar em 2025 e isso é muito", "thumbnail_text": "R$ 377 mil"}
    allowed = {"baleia", "rossi", "377", "mil", "2025", "cota"}
    assert _packaging_ok(ok, allowed)
    assert not _packaging_ok(longo, allowed)


# ---------- revisão: correções da revisão de 07/11 ----------

@pytest.mark.parametrize("raw, expected", [
    ("0.125", 0.125), ("0,125", 0.125), ("-5", -5.0), ("-3.812", -3812.0),
    ("3.812", 3812.0), ("353.248,04", 353248.04), ("12,345.67", 12345.67), ("3,5", 3.5),
])
def test_source_number_zero_decimal_and_sign(raw, expected):
    assert fact_check._source_number(raw) == pytest.approx(expected)


def test_st_proper_names_are_not_broken():
    from src.fact_check import broken_sentences

    for text in ["Ele visitou St. Patrick's Cathedral.", "A igreja de St. Peter fica no Vaticano."]:
        assert broken_sentences(_scene(text)) == [], text


def test_st_abbreviation_before_sentence_start_is_broken():
    from src.fact_check import broken_sentences

    assert broken_sentences(_scene("Terry saiu de St. Ele havia perdido uma perna."))


def test_ordinary_milhas_are_not_broken():
    from src.fact_check import broken_sentences

    for text in ["Ela falou de milhas e quilômetros.", "O projeto de milhas aéreas cresceu."]:
        assert broken_sentences(_scene(text)) == [], text


def test_distance_without_source_distance_is_fora_da_fonte():
    from src.fact_check import numbers_not_in_sources

    web = [{"titulo": "Terry Fox", "trecho": "Terry foi um atleta canadense."}]
    problems = numbers_not_in_sources(_scene("Terry completou 5.300 milhas."), web)
    assert [p["tipo"] for p in problems] == ["fora_da_fonte"]


def test_claim_sources_keeps_units_and_sourceless_results(monkeypatch):
    from src import fact_check

    queries = []

    def fake_search(query, max_results=5):
        queries.append(query)
        return [{"titulo": "x", "trecho": "y", "url": None}, {"titulo": "z", "trecho": "w", "url": "https://a"}]

    monkeypatch.setattr("src.web_search.search_topic_facts", fake_search)
    fact_check._claim_cache.clear()
    out = fact_check.claim_sources(_scene("Terry percorreu 5.300 milhas em 1980."), "Terry Fox")
    assert any("milhas" in q for q in queries)
    assert len(out) == 2  # resultado sem URL não é descartado


def test_club_crest_prompt_becomes_white_star_on_black():
    from src.script_gen import _CREST_SAFE_PROMPT, _sanitize_club_crests

    script = {
        "thumbnail_image_prompt": "Botafogo crest on a shield",
        "scenes": [
            {"image_prompt": "close-up of the Botafogo escudo on a jersey", "stock_query": "botafogo"},
            {"image_prompt": "a crowd cheering in a stadium at night", "stock_query": "stadium"},
        ],
    }
    out = _sanitize_club_crests(script)
    assert out["scenes"][0]["image_prompt"] == _CREST_SAFE_PROMPT
    assert out["scenes"][0]["stock_query"] == ""
    assert out["scenes"][1]["image_prompt"] == "a crowd cheering in a stadium at night"
    assert out["thumbnail_image_prompt"] == _CREST_SAFE_PROMPT
    assert "black background" in _CREST_SAFE_PROMPT and "five-pointed star" in _CREST_SAFE_PROMPT


# ---------- revisão (segunda rodada): intervalos, sinal, merge, veredito ----------

@pytest.mark.parametrize("texto, esperado", [
    ("COVID-19", [19.0]),
    ("2020-2021", [2020.0, 2021.0]),
    ("-5 °C", [-5.0]),
    ("-5 milhões", [-5_000_000.0]),
])
def test_source_numbers_hyphen_is_not_sign(texto, esperado):
    assert fact_check._source_numbers(texto) == pytest.approx(esperado)


def test_range_with_shared_unit_counts_both_ends():
    from src.fact_check import numbers_not_in_sources

    web = [{"trecho": "A trilha tem entre 10-20 km."}]
    assert numbers_not_in_sources({"scenes": [{"narration": "A trilha tem 20 km."}]}, web) == []
    web2 = [{"trecho": "trilha de 5-10 km"}]
    assert numbers_not_in_sources({"scenes": [{"narration": "5 km"}]}, web2) == []


def test_a_milhas_flagged_only_after_comma_or_start():
    from src.fact_check import broken_sentences

    assert broken_sentences(_scene("Ele ficou a milhas de distância.")) == []
    assert broken_sentences(_scene("A casa fica a metros da praia.")) == []
    assert broken_sentences(_scene("Svalbard, a milhas ao norte.")) != []


def test_merge_web_facts_dedupes_by_url():
    from src.fact_check import merge_web_facts

    tema = [{"url": "https://a", "trecho": "1"}]
    extra = [{"url": "https://a", "trecho": "dup"}, {"url": "https://b", "trecho": "2"}, {"url": None, "trecho": "3"}]
    out = merge_web_facts(tema, extra)
    assert [f["trecho"] for f in out] == ["1", "2", "3"]
    assert merge_web_facts(None, None) is None


def test_compilacao_merges_claim_sources_without_losing_theme_sources():
    from src.fact_check import merge_web_facts

    compil = [{"url": "https://wiki", "trecho": "tema"}]
    claims = [{"url": "https://wiki", "trecho": "x"}, {"url": "https://news", "trecho": "y"}]
    assert [f["url"] for f in merge_web_facts(compil, claims)] == ["https://wiki", "https://news"]


def test_unfulfilled_promise_accepts_code_block_prompt():
    from src.fact_check import unfulfilled_promises

    script = _scene("Copie este prompt no NotebookLM: `Você é um tutor que faz perguntas sobre o material.`")
    assert unfulfilled_promises(script) == []


def test_topic_problem_valid_and_invalid_verdicts(monkeypatch):
    from src import topics

    monkeypatch.setattr(topics, "complete", lambda *a, **k: '{"ok": true, "motivo": ""}')
    assert topics.topic_problem("Como usar IA para estudar mais rápido", "IA e carreira", strict=False) is None
    monkeypatch.setattr(topics, "complete", lambda *a, **k: '{"ok": false, "motivo": "fora do nicho"}')
    assert topics.topic_problem("Como usar IA para estudar mais rápido", "IA e carreira", strict=False) == "fora do nicho"


def test_is_parseable_script_accepts_code_fence_and_surrounding_text():
    raw = 'Aqui está:\n```json\n{"titulo": "x", "scenes": []}\n```\nObrigado.'
    assert script_gen._is_parseable_script(raw, {"titulo", "scenes"})


def test_money_currencies_and_scales():
    assert "3 reais e 50 centavos" in tts._money("R$ 3,50")
    assert "dólares" in tts._money("US$ 10")
    assert "euros" in tts._money("€39 milhões")
    assert "milhões" in tts._money("R$ 5 milhões")


def _dark_scene(seed: int):
    import random

    from PIL import Image, ImageDraw, ImageFilter

    rnd = random.Random(seed)
    im = Image.new("RGB", (480, 854), (12, 14, 20))
    d = ImageDraw.Draw(im)
    for _ in range(3):
        x, y = rnd.randint(0, 300), rnd.randint(300, 700)
        d.rectangle([x, y, x + rnd.randint(80, 180), y + rnd.randint(80, 200)],
                    fill=(rnd.randint(60, 200), rnd.randint(60, 200), rnd.randint(60, 200)))
    return im.filter(ImageFilter.GaussianBlur(3))


def test_same_visual_two_dark_different_scenes_not_grouped():
    from src import video_qa

    assert video_qa.same_visual_share([_dark_scene(1), _dark_scene(2)]) == (1, 2)


def test_same_visual_recompressed_and_zoomed_still_grouped():
    import io

    from PIL import Image

    from src import video_qa

    base = _dark_scene(3)
    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=55)
    rec = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    zoom = base.crop((24, 43, 456, 811)).resize((480, 854))
    assert video_qa.same_visual_share([base, rec, zoom]) == (3, 3)


def test_description_fits_youtube_limit_keeping_start():
    from src.youtube_text import YOUTUBE_DESCRIPTION_LIMIT, fit_description

    head = "Descubra oito histórias.\n" * 100
    sources = "\n\nFontes consultadas:\n" + "\n".join(f"- fonte {i}: https://exemplo.com/" + "x" * 120 for i in range(30))
    out = fit_description(head + sources)
    assert len(out) <= YOUTUBE_DESCRIPTION_LIMIT
    assert out.startswith("Descubra oito histórias.")
    assert "- fonte 0:" in out  # cabe pelo menos o início das fontes


def test_description_without_sources_is_hard_cut_on_line():
    from src.youtube_text import YOUTUBE_DESCRIPTION_LIMIT, fit_description

    out = fit_description("linha\n" * 2000)
    assert len(out) <= YOUTUBE_DESCRIPTION_LIMIT and out.endswith("linha")


def test_short_description_untouched():
    from src.youtube_text import fit_description

    assert fit_description("curta") == "curta"


# ---------- fala: unidades, símbolos e termo estrangeiro (relatório de 07/11) ----------

@pytest.mark.parametrize("texto, esperado", [
    ("100 km/h", "100 quilômetros por hora"),
    ("30 °C", "30 graus Celsius"),
    ("abre 24/7", "24 horas por dia, 7 dias por semana"),
    ("entre 2020-2021", "2020 a 2021"),
    ("no século XXI", "século vinte e um"),
    ("placar 3-0", "3 a 0"),
    ("cabe 2 GB", "2 gigabytes"),
    ("5 m²", "5 metros quadrados"),
    ("6,4h/semana", "6,4 horas por semana"),
])
def test_units_and_symbols_are_spoken_in_words(texto, esperado):
    assert esperado in tts._for_speech(texto)[0]


def test_foreign_term_is_not_rewritten_by_fixed_pronunciation():
    # Textor tem reescrita fixa (Técstor), mas marcado como estrangeiro fica como está
    spoken, _ = tts._for_speech("o Textor e o Botafogo", {}, {"textor"})
    assert "Textor" in spoken and "Técstor" not in spoken
    # sem marcação, a reescrita fixa continua valendo
    spoken2, _ = tts._for_speech("o Textor e o Botafogo", {})
    assert "Técstor" in spoken2


# ---------- áudio: nome e sigla (relatório de 07/11) ----------

def test_name_mispronounced_is_flagged_but_correct_name_is_not():
    from src.video_qa import name_problems

    assert name_problems(["O Textor investiu no Botafogo."], "O Tech Store investiu no Botafogo.")
    assert name_problems(["O arquipélago de Svalbard fica no Ártico."], "O arquipélago de Esvalbard fica no Ártico.")
    assert name_problems(["O arquipélago de Svalbard está no Ártico."], "o arquipélago de svalbard está no ártico") == []


def test_acronyms_have_explicit_spoken_form():
    from src import tts

    assert "Estados Unidos" in tts._for_speech("o EUA")[0]
    assert "cê pê efe" in tts._for_speech("o CPF")[0]
    assert "ésse tê efe" in tts._for_speech("o STF")[0]


def test_person_name_found_for_photo_search():
    from src.real_media import person_in_narration

    assert person_in_narration("Em 2018, Nadia Murad recebeu o Nobel da Paz.") == "Nadia Murad"
    assert person_in_narration("Gleisi Hoffmann, do PT, falou.") == "Gleisi Hoffmann"
    assert person_in_narration("O Congresso aprovou a lei.") is None


def test_lead_image_requires_exact_person_page(monkeypatch):
    from src import real_media

    class FakeWiki:
        pass

    def fake_get_json(url, params):
        return {"query": {"pages": {"1": {"title": "Outra Pessoa", "pageimage": "Foto.jpg"}}}}

    monkeypatch.setattr("src.wiki_cache.get_json", fake_get_json)
    assert real_media._lead_image("Simone Biles") == []  # verbete de outra pessoa: não serve


def test_single_name_mid_sentence_is_found():
    from src.real_media import person_in_narration

    assert person_in_narration("Ele começou jogando com Neymar no Santos.") == "Neymar"


def test_topic_name_covers_sentence_start_name():
    from src.real_media import person_in_narration, topic_person

    tema = "Neymar: da periferia de Mogi Cruzes ao Barcelona"
    assert topic_person(tema) == "Neymar"
    assert person_in_narration("Neymar começou aos 11 anos.", tema) == "Neymar"
    assert person_in_narration("O Congresso aprovou a lei.", tema) is None
    assert topic_person("Como funciona a cota parlamentar") is None


def test_evidence_search_per_rejected_claim(monkeypatch):
    from src import fact_check

    queries = []

    def fake_search(query, max_results=5):
        queries.append(query)
        return [{"url": f"https://ex/{len(queries)}", "titulo": "t", "trecho": "730 dias"}]

    monkeypatch.setattr("src.web_search.search_topic_facts", fake_search)
    fact_check._claim_cache.clear()
    problems = [{"tipo": "fora_da_fonte", "termo": "730 dias sem lar fixo"},
                {"tipo": "fato_nao_confirmado", "termo": "escola em casa aos 13 anos"}]
    out = fact_check.evidence_for_problems(problems, "Simone Biles: de abrigo a campeã")
    assert len(queries) == 2 and "730 dias" in queries[0]
    assert len(out) == 2
