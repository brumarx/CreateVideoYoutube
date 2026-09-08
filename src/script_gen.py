"""Geração de roteiro: título, descrição, tags e cenas (texto de narração +
prompt de imagem por cena), via cascata de LLMs (providers.complete)."""
from __future__ import annotations

import json
import logging
import re

from .config import ChannelConfig
from .providers import complete

log = logging.getLogger("script_gen")

# edge-tts com rate="-15%" narra a ~130 palavras/minuto em pt-BR (medido nos
# vídeos já publicados). Pedir "um vídeo de X minutos" pro LLM não funciona:
# ele bate a CONTAGEM de cenas mas escreve frases curtas demais em cada uma
# (visto num vídeo longo que saiu com 4min em vez de 15-20). Por isso convertemos
# minutos-alvo em palavras-por-cena, uma meta concreta e verificável.
WORDS_PER_MINUTE = 130
MIN_WORDS_RATIO = 0.7  # abaixo disso, tenta de novo antes de aceitar

SYSTEM_PROMPT = (
    "Você é um roteirista de vídeos do YouTube no estilo 'faceless' "
    "(sem apresentador, só narração + imagens). Responda SEMPRE em JSON "
    "válido, sem markdown, sem texto fora do JSON. "
    "REGRA CRÍTICA para todo 'image_prompt': a IA de imagem SEMPRE erra "
    "letras/palavras e não deve imitar logos de marcas (YouTube, etc.) — "
    "por isso NUNCA peça texto, palavras, letras, números escritos, botões, "
    "ícones de interface, logos ou telas de 'inscreva-se'/'curta' na "
    "imagem. Descreva só cenário, objetos, pessoas e atmosfera visual. "
    "REGRA CRÍTICA sobre pessoas reais nomeadas (política, magistrado, "
    "empresário, atleta, figura histórica — QUALQUER pessoa real "
    "identificável pelo nome, não só política): se a história tem essa "
    "pessoa como protagonista de uma cena, o 'image_prompt' NUNCA pode ser "
    "um CLOSE NO ROSTO, mesmo descrito de forma genérica tipo 'a woman's "
    "face' ou 'a determined man' — isso NÃO livra a regra, porque o "
    "contexto (uniforme, número de peito, cargo, brasão, veste) deixa claro "
    "pra quem assiste que aquele rosto genérico É pra ser ela, e a IA de "
    "imagem não sabe gerar o rosto real dessa pessoa (pode sair errado, ou "
    "até confundir o nome com outra coisa, tipo gerar o animal 'lula' em "
    "vez do político Lula). Regra prática: quando o protagonista é uma "
    "pessoa real, a câmera NUNCA foca no rosto dela — descreva de costas, "
    "silhueta ao longe, mãos/pés/objetos pessoais em close (um crachá, "
    "número de peito, uniforme, uma caneta assinando), o ambiente ao redor, "
    "ou um prédio/símbolo relacionado — nunca um retrato ou close facial "
    "dramático que o espectador vá interpretar como sendo o rosto daquela "
    "pessoa. Isso vale pro 'image_prompt' de toda cena E pro "
    "'thumbnail_image_prompt'. "
    "REGRA CRÍTICA sobre lugares reais: se a cena menciona um país, cidade, "
    "monumento ou acidente geográfico REAL específico (ex.: Portugal, "
    "Amazônia, Holanda), o 'image_prompt' tem que descrever as "
    "características visuais REAIS e reconhecíveis daquele lugar exato "
    "(arquitetura, paisagem, clima, cor típica) — nunca um lugar genérico "
    "nem de outro país. Errar isso (ex.: pedir imagem de Nova York numa "
    "cena sobre o Rio de Janeiro) é o pior erro possível neste roteiro. "
    "REGRA CRÍTICA sobre vídeos de lista ('N fatos/coisas/dicas sobre...'): "
    "NUNCA sugira ou escreva um título/tema com mais de 10 itens — nada de "
    "'20 fatos', '30 coisas', '50 curiosidades'. O máximo é sempre 10; "
    "prefira listas menores (5, 7, 10) a forçar uma lista longa e repetitiva. "
    "REGRA CRÍTICA sobre o gancho inicial: mais da metade de quem assiste "
    "decide continuar ou não nos primeiros 3 segundos, então a PRIMEIRA "
    "frase da primeira cena tem que entregar o fato mais chocante/intrigante "
    "ou uma pergunta que gera curiosidade imediata — NUNCA comece com "
    "saudação, apresentação do canal, contexto histórico/geográfico ou "
    "'hoje vamos falar sobre...'. Vá direto ao ponto mais interessante e só "
    "depois explique o contexto."
)


def _prompt_for(
    channel: ChannelConfig,
    topic: str,
    facts: dict | None = None,
    scenes: int | None = None,
    min_minutes: float = 3,
    max_minutes: float = 6,
) -> str:
    facts_block = ""
    if facts:
        facts_block = f"""
DADOS REAIS (use SOMENTE estes números e nomes — não invente, não
extrapole, não cite nenhum nome/valor que não esteja aqui; atribua sempre à
fonte, ex.: "segundo declaração ao TSE" / "segundo dados abertos da
Câmara"):
{json.dumps(facts, ensure_ascii=False, indent=2)}
"""

    n_scenes = scenes or channel.scenes_per_video
    total_min_words = round(min_minutes * WORDS_PER_MINUTE)
    total_max_words = round(max_minutes * WORDS_PER_MINUTE)
    words_per_scene_min = max(total_min_words // n_scenes, 1)
    words_per_scene_max = max(total_max_words // n_scenes, words_per_scene_min + 1)

    return f"""{channel.prompt_base}

Tópico do vídeo: {topic}
Idioma: {channel.language}
Número de cenas: {n_scenes}
{facts_block}
Gere um JSON com exatamente este formato:
{{
  "title": "título chamativo, até 100 caracteres",
  "thumbnail_text": "gancho CURTÍSSIMO pra thumbnail, no máximo 4 palavras, tipo manchete de banca de jornal — não é o título, é a frase que faz alguém parar de rolar o feed",
  "thumbnail_image_prompt": "prompt em inglês pro momento MAIS visualmente marcante/dramático de toda a história (não precisa ser a cena 1) — close-up, alto contraste, cor vibrante, um único foco claro na imagem. Primeiro pergunte: essa história tem UM protagonista real específico e identificável (uma pessoa que existiu/existe de verdade, com nome — político, atleta, empresário, figura histórica, NÃO importa se o nome aparece literalmente neste prompt)? Se SIM: NUNCA um close no rosto, nem descrito de forma genérica ('a woman's face', 'a determined man') — o contexto (uniforme, número de peito, roupa de época, cargo) já entrega pra quem assiste que aquele rosto É pra ser ela, e a IA não sabe gerar o rosto real dela. Use em vez disso um close em mãos/objeto pessoal (crachá, número de peito, caneta, uniforme), uma silhueta de costas/longe, ou um símbolo/cenário forte relacionado à história. Se NÃO (a cena é sobre um personagem fictício, genérico, ou 'alguém' sem identidade real específica): aí sim pode descrever uma expressão facial EXAGERADA e genuína (chocada, olhos arregalados, boca aberta, maravilhada, com medo) — rosto humano com emoção forte é o maior fator isolado de clique em thumbnail. Se não tiver pessoa nenhuma, use um objeto/cenário com contraste visual forte numa composição que gere uma pergunta na cabeça de quem vê. Mesma regra das outras imagens: SEM texto, palavras, logos, botões ou UI",
  "description": "descrição para o YouTube, 2-3 parágrafos, com contexto e call-to-action",
  "tags": ["tag1", "tag2", "..."],
  "scenes": [
    {{"narration": "texto que o narrador vai falar nesta cena", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera — SEM texto, palavras, logos, botões ou UI"}}
  ]
}}

O array "scenes" deve ter exatamente {n_scenes} itens. META DE TAMANHO
OBRIGATÓRIA: cada cena precisa ter entre {words_per_scene_min} e
{words_per_scene_max} palavras de narração — isso é um parágrafo com
vários fatos/frases, NUNCA uma frase única de uma linha só. O roteiro
completo deve somar entre {total_min_words} e {total_max_words} palavras no
total (~{min_minutes:.0f} a {max_minutes:.0f} minutos narrados). Se não
tiver conteúdo real suficiente pra encher uma cena no tamanho pedido,
aprofunde com mais detalhes concretos (contexto, números, comparações,
consequências) em vez de encurtar — nunca encher linguiça repetindo a
mesma ideia com palavras diferentes só pra bater a contagem.

REGRA CRÍTICA sobre "thumbnail_text": thumbnail boa hoje em dia NÃO é o
título inteiro colado na imagem — é uma frase mínima (2 a 4 palavras) que
gera curiosidade sozinha, sem contexto nenhum, tipo "ELA FOI PROIBIDA",
"O ERRO DE R$ 2 MILHÕES", "ISSO É REAL?". Pode usar número, pode terminar
em pergunta, nunca uma frase completa com sujeito+verbo+complemento igual
o título."""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # Alguns modelos ainda envolvem em ```json ... ``` mesmo pedindo pra não fazer isso.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta do LLM não contém JSON: {raw[:300]}")
    return json.loads(match.group(0))


def generate_script(
    channel: ChannelConfig,
    topic: str,
    facts: dict | None = None,
    scenes: int | None = None,
    min_minutes: float = 3,
    max_minutes: float = 6,
) -> dict:
    """Gera o roteiro completo do vídeo. Se `facts` for passado (ex.: saída
    de politica_data.random_fact_set()), o roteiro é obrigado a usar só
    esses dados reais em vez de a IA inventar números/nomes. `scenes`,
    `min_minutes` e `max_minutes` permitem gerar roteiros bem mais longos
    (formato documentário) sem mudar a config padrão do canal — ver
    ChannelConfig.short_min_minutes/long_min_minutes etc.

    Levanta ValueError se o LLM não devolver um JSON parseável (raro, mas
    acontece)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _prompt_for(channel, topic, facts, scenes, min_minutes, max_minutes)},
    ]

    required = {"title", "description", "tags", "scenes"}
    min_total_words = round(min_minutes * WORDS_PER_MINUTE * MIN_WORDS_RATIO)
    n_scenes = scenes or channel.scenes_per_video
    total_max_words = round(max_minutes * WORDS_PER_MINUTE)
    # ~1.5 tokens/palavra em pt-BR + overhead de JSON/image_prompt (em
    # inglês, ~30-50 palavras por cena) + folga de 40% — sem isso a resposta
    # trunca no meio do JSON pra roteiros longos (visto na prática: 31
    # cenas cortou em ~3100 caracteres com o default baixo do provedor).
    max_tokens = min(int((total_max_words * 1.5 + n_scenes * 60 + 500) * 1.4), 16000)
    last_error: Exception | None = None
    best_short_script: dict | None = None
    best_short_words = -1
    for attempt in range(3):
        raw = complete(messages, max_tokens=max_tokens)
        try:
            script = _extract_json(raw)
            missing = required - script.keys()
            if missing:
                raise ValueError(f"JSON do roteiro sem campos {missing}: {script}")

            word_count = sum(len(s.get("narration", "").split()) for s in script["scenes"])
            if word_count >= min_total_words:
                return script

            log.warning(
                "tentativa %d/3: roteiro curto demais (%d palavras, mínimo %d), tentando de novo",
                attempt + 1, word_count, min_total_words,
            )
            if word_count > best_short_words:
                best_short_script, best_short_words = script, word_count
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            log.warning("tentativa %d/3: roteiro malformado (%s), tentando de novo", attempt + 1, exc)

    if best_short_script is not None:
        log.warning(
            "usando o melhor roteiro obtido mesmo abaixo da meta (%d palavras, mínimo %d)",
            best_short_words, min_total_words,
        )
        return best_short_script

    raise ValueError(f"LLM não devolveu roteiro válido após 3 tentativas: {last_error}")
