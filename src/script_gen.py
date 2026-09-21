"""Geração de roteiro: título, descrição, tags e cenas (texto de narração +
prompt de imagem por cena), via cascata de LLMs (providers.complete)."""
from __future__ import annotations

import json
import logging
import re

from .config import ChannelConfig
from .providers import complete

log = logging.getLogger("script_gen")

# edge-tts com rate="+0%" (ver src/tts.py — subiu de "-15%" por soar
# monótono/devagar demais) narra a ~150 palavras/minuto em pt-BR (medido:
# -15% dava ~130 wpm, +0% mede ~15% mais rápido). Pedir "um vídeo de X
# minutos" pro LLM não funciona: ele bate a CONTAGEM de cenas mas escreve
# frases curtas demais em cada uma (visto num vídeo longo que saiu com 4min
# em vez de 15-20). Por isso convertemos minutos-alvo em palavras-por-cena,
# uma meta concreta e verificável. Se a velocidade da narração mudar de
# novo, remedir e ajustar aqui junto — senão a duração real do vídeo
# desalinha do que o painel mostra.
WORDS_PER_MINUTE = 150
MIN_WORDS_RATIO = 0.7  # abaixo disso, tenta de novo antes de aceitar
MAX_WORDS_RATIO = 1.15  # acima disso, também tenta de novo (nunca tinha teto)

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
    "depois explique o contexto. "
    "REGRA CRÍTICA sobre reter até o fim (não só abrir bem): gancho forte só "
    "na cena 1 não basta — feedback direto de quem assistiu vários vídeos "
    "publicados foi que o meio 'fica chato', gente abandona antes do fim. "
    "TODA cena, não só a primeira, tem que terminar puxando pra próxima: uma "
    "pergunta em aberto, uma contradição ainda não explicada, uma promessa "
    "('mas o que aconteceu depois é ainda mais surpreendente'), nunca uma "
    "frase que soa como ponto final de assunto encerrado. Varie o ritmo das "
    "frases (curtas e diretas misturadas com uma mais longa, nunca a mesma "
    "estrutura sintática repetida cena após cena), fale direto com quem "
    "assiste ('você', 'imagina se...', perguntas retóricas) em vez de tom de "
    "verbete/enciclopédia, e nunca deixe duas cenas seguidas em tom "
    "puramente informativo sem reação, tensão ou surpresa nenhuma — isso é "
    "o que mais mata retenção no meio do vídeo."
)


def _prompt_for(
    channel: ChannelConfig,
    topic: str,
    facts: dict | None = None,
    scenes: int | None = None,
    min_minutes: float = 3,
    max_minutes: float = 6,
    web_facts: list[dict] | None = None,
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

    web_facts_block = ""
    if web_facts:
        trechos = "\n\n".join(f"- [{f['titulo']}]({f['url']}): {f['trecho']}" for f in web_facts)
        web_facts_block = f"""
PESQUISA REAL NA INTERNET (o tema foi digitado por uma pessoa — use estes
trechos como base factual; NÃO invente nome, número, data ou evento que
não apareça aqui ou nos DADOS REAIS acima; se os trechos não bastarem pra
cobrir o tema com segurança, foque no que ESTÁ confirmado neles em vez de
completar com suposição):
{trechos}
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
{facts_block}{web_facts_block}
Gere um JSON com exatamente este formato:
{{
  "title": "título chamativo, até 100 caracteres",
  "thumbnail_text": "gancho CURTÍSSIMO pra thumbnail, no máximo 4 palavras, tipo manchete de banca de jornal — não é o título, é a frase que faz alguém parar de rolar o feed",
  "thumbnail_image_prompt": "prompt em inglês pro momento MAIS visualmente marcante/dramático de toda a história (não precisa ser a cena 1) — close-up, alto contraste, cor vibrante, um único foco claro na imagem. Primeiro pergunte: essa história tem UM protagonista real específico e identificável (uma pessoa que existiu/existe de verdade, com nome — político, atleta, empresário, figura histórica, NÃO importa se o nome aparece literalmente neste prompt)? Se SIM: NUNCA um close no rosto, nem descrito de forma genérica ('a woman's face', 'a determined man') — o contexto (uniforme, número de peito, roupa de época, cargo) já entrega pra quem assiste que aquele rosto É pra ser ela, e a IA não sabe gerar o rosto real dela. Use em vez disso um close em mãos/objeto pessoal (crachá, número de peito, caneta, uniforme), uma silhueta de costas/longe, ou um símbolo/cenário forte relacionado à história. Se NÃO (a cena é sobre um personagem fictício, genérico, ou 'alguém' sem identidade real específica): aí sim pode descrever uma expressão facial EXAGERADA e genuína (chocada, olhos arregalados, boca aberta, maravilhada, com medo) — rosto humano com emoção forte é o maior fator isolado de clique em thumbnail. Se não tiver pessoa nenhuma, use um objeto/cenário com contraste visual forte numa composição que gere uma pergunta na cabeça de quem vê. Mesma regra das outras imagens: SEM texto, palavras, logos, botões ou UI",
  "description": "descrição para o YouTube, 2-3 parágrafos, com contexto e call-to-action",
  "tags": ["tag1", "tag2", "..."],
  "mood": "clima geral do vídeo pra escolher a música de fundo certa — exatamente um destes valores: 'tenso' (denúncia grave, algo errado/perigoso, suspense), 'animado' (conquista, vitória, dica animadora, virada positiva), 'melancolico' (derrota, perda, resultado triste/decepcionante), 'calmo' (reflexivo, sem tensão nem euforia), 'neutro' (explicativo/factual, sem carga emocional forte definida). Escolha 1 só, o que domina o vídeo como um todo, não cena a cena.",
  "scenes": [
    {{"narration": "texto que o narrador vai falar nesta cena", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera — SEM texto, palavras, logos, botões ou UI", "stock_query": "3 a 5 palavras em inglês pra buscar filmagem REAL de banco de vídeo — tem que amarrar num detalhe CONCRETO desta cena específica (o lugar, o objeto, a ação, o tipo de situação), nunca só a categoria genérica do assunto. Exemplo ruim (genérico demais, serve pra qualquer cena do tema): 'soccer stadium', 'empty stadium'. Exemplo bom (amarra no que ESSA cena narra): se a narração fala de gol sofrido no fim do primeiro tempo, 'goalkeeper diving missed save' ou 'soccer net ball entering'; se fala de posse de bola dividida, 'midfield players contesting ball'. NUNCA inclua nome de pessoa real aqui (troque pela ação/objeto/situação sem nomear ninguém). Só faz sentido se o que a cena descreve existe filmado de verdade — se for algo abstrato/conceitual que só dá pra ilustrar (um conceito, um gráfico, uma situação boa demais específica da história), deixe null (nunca uma frase vaga tipo 'related to the story' ou 'symbolic shot' — isso não é busca de vídeo, é ausência de busca, então é null mesmo).", "impact_beat": "true APENAS na cena do momento mais chocante/decisivo de todo o vídeo (o gol, o número mais absurdo, a virada) — no máximo 1 cena `true` no roteiro inteiro; todas as outras, false ou omita.", "stat_overlay": "OPCIONAL, deixe null na grande maioria das cenas — só preencha quando ESTA cena citar, no texto da narração, 2 valores comparáveis específicos vindos de DADOS REAIS (ex.: posse de bola de cada time, % de aprovação, participação em votos) — formato exato: {{\"label\": \"o que está sendo comparado\", \"a_label\": \"nome do 1º\", \"a_value\": numero_0_a_100, \"b_label\": \"nome do 2º\", \"b_value\": numero_0_a_100}}. NUNCA invente ou estime os valores — só use quando estiverem literalmente nos DADOS REAIS."}}
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

REGRA CRÍTICA sobre "stock_query": priorize filmagem REAL sobre imagem
gerada sempre que o assunto existe filmado de verdade — filmagem real de
banco de vídeo (animal, paisagem, cidade, objeto, natureza, situação do
dia a dia) fica muito mais viva na tela que qualquer imagem estática
gerada por IA com zoom. Só deixe null quando for algo que realmente não
existe filmado (um conceito abstrato, uma cena muito específica da
história que não tem como achar pronta).

REGRA CRÍTICA sobre "stock_query" ser ESPECÍFICO, não clichê: um vídeo
inteiro cheio de "stadium", "money", "office" repetidos cena após cena
fica genérico e entediante, e pior — se TODA cena do tema usar a mesma
palavra genérica, o banco de vídeo devolve sempre o mesmo clipezinho
manjado (isso já aconteceu de verdade: um clipe de "pilha de dinheiro"
virou a imagem de dezenas de vídeos diferentes só porque a busca nunca
saía do genérico). Cada cena tem um fato/ação diferente sendo narrado —
o "stock_query" tem que refletir ESSA diferença: qual objeto específico,
que ação, que tipo de lugar, aparece SÓ nessa cena e não nas outras do
mesmo vídeo. Duas cenas seguidas nunca devem ter "stock_query" parecido
demais.

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


def _is_parseable_script(raw: str, required: set[str]) -> bool:
    """Validador passado pra `complete()` — rejeita na hora qualquer
    resposta que não vira roteiro utilizável, pra cascata de provedores já
    pular pro próximo modelo/chave em vez de aceitar garbage (visto na
    prática: um modelo grátis instável respondia só `{` repetido sem nunca
    fechar o JSON, e as 3 tentativas de retry caíam nele de novo porque a
    cascata sempre parava no primeiro "não vazio")."""
    try:
        script = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return False
    return required <= script.keys()


# Trava de CÓDIGO (não só instrução de prompt) contra rosto de pessoa real
# nomeada — a regra no SYSTEM_PROMPT falha uma fração real das vezes na
# prática (visto: um vídeo sobre a Kathrine Switzer real saiu com uma cena
# de close no rosto dela mesmo com a regra no prompt). Detecta tema com
# nome próprio (2+ palavras capitalizadas seguidas, ex.: "Kathrine
# Switzer", "Nelson Mandela") e substitui qualquer image_prompt arriscado
# por um genérico seguro, sem depender só do LLM ter seguido a instrução.
#
# Bug crítico achado de verdade (2026-09-09): a versão original da regex
# não aceitava preposição minúscula no meio do nome ("de", "da", "do"),
# então "Alexandre de Moraes" não batia NADA — só "Alexandre" e "Moraes"
# isolados, cada um com 1 palavra só. Como o gate abaixo exige match pra
# sequer rodar a sanitização, um tema só com esse nome (sem mais nenhum
# nome próprio de 2+ palavras) passaria batido, sem proteção nenhuma.
# Nomes brasileiros com preposição são extremamente comuns (Luiz Inácio
# Lula da Silva, Maria de Fátima, etc.) — corrigido pra aceitar de/da/do/
# dos/das/e no meio da sequência.
_PROPER_NAME_RE = re.compile(
    r"\b[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:de|da|do|dos|das|e)\s+[A-ZÀ-Ý][a-zà-ÿ]+|\s+[A-ZÀ-Ý][a-zà-ÿ]+)+\b"
)
_SAFE_NO_FACE_RE = re.compile(
    r"\bno faces? (visible|shown)\b|\bface (is|are) not (shown|visible)\b|"
    r"\bface (hidden|obscured|not shown|not visible)\b|\bwithout (a|the|any) face\b|"
    r"\bno visible face\b",
    re.IGNORECASE,
)
_RISKY_FACE_RE = re.compile(
    r"\bface\b|\bfacial expression\b|\bher eyes\b|\bhis eyes\b|\beyes wide\b|"
    r"\bportrait\b|\bclose-?up of (a|the) (wom[ae]n|man|girl|boy|person)\b|"
    r"\bdetermined expression\b",
    re.IGNORECASE,
)
_SAFE_FALLBACK_IMAGE_PROMPT = (
    "symbolic wide shot related to the story, no identifiable face, focus on "
    "hands, objects, a silhouette seen from behind, or the surrounding "
    "environment, cinematic lighting, no text, no words, no letters, no "
    "numbers, no logos, no UI, no buttons, no watermark, no signs, no "
    "signage, no plaques, no banners, no billboards"
)


def _is_risky_face_prompt(prompt: str) -> bool:
    if not prompt or _SAFE_NO_FACE_RE.search(prompt):
        return False
    return bool(_RISKY_FACE_RE.search(prompt))


def _sanitize_person_images(topic: str, script: dict) -> dict:
    # Antes só olhava o TÓPICO do vídeo — suficiente quando nome de pessoa
    # real é exceção rara no tema, mas insuficiente pra um canal como o de
    # análise de futebol, que cita jogador real pelo nome em quase toda
    # cena sem esse nome necessariamente aparecer no tópico (ex.: tópico
    # "Botafogo 3 x 2 Mirassol", narração citando "Arthur Cabral marcou aos
    # 11 minutos"). Agora cada cena é checada pela própria narração também,
    # além do tópico — mais protetivo pra todo canal, sem custo (só troca
    # um image_prompt arriscado quando o texto daquela cena já nomeia
    # alguém real).
    topic_has_name = bool(_PROPER_NAME_RE.search(topic))
    # Rastreado à parte do loop pra decidir se a THUMBNAIL também precisa de
    # checagem: um vídeo sem pessoa real nenhuma pode (e deve, ver
    # SYSTEM_PROMPT) usar expressão facial exagerada e genérica na
    # thumbnail — sanitizar isso à toa derrubaria uma thumbnail boa e
    # legítima em todo canal sem gente real no roteiro.
    any_name_mentioned = topic_has_name
    for i, scene in enumerate(script.get("scenes", [])):
        scene_has_name = topic_has_name or bool(_PROPER_NAME_RE.search(scene.get("narration", "")))
        any_name_mentioned = any_name_mentioned or scene_has_name
        if not scene_has_name:
            continue
        prompt = scene.get("image_prompt", "")
        if _is_risky_face_prompt(prompt):
            log.warning("cena %d: image_prompt arriscado (rosto de pessoa real), substituindo: %s", i, prompt[:150])
            scene["image_prompt"] = _SAFE_FALLBACK_IMAGE_PROMPT
            # "" (não None) sinaliza "desativado de propósito" pro
            # run_pipeline.py — nunca busca filmagem real usando o nome da
            # pessoa (a busca cairia numa foto/vídeo de banco que não é
            # dela, mas o contexto arriscado já mostra que o LLM tratou essa
            # cena como sendo sobre ela especificamente). Antes usava None,
            # que o run_pipeline.py não distinguia de "LLM esqueceu de
            # preencher" — e nesse segundo caso ele monta uma busca de
            # reserva com as palavras do image_prompt, o que aqui reabriria
            # o buraco de segurança (e, na prática, jogava o próprio texto
            # do _SAFE_FALLBACK_IMAGE_PROMPT pro Pexels como busca real).
            scene["stock_query"] = ""
    if any_name_mentioned:
        thumb_prompt = script.get("thumbnail_image_prompt")
        if _is_risky_face_prompt(thumb_prompt or ""):
            log.warning("thumbnail_image_prompt arriscado (rosto de pessoa real), substituindo: %s", (thumb_prompt or "")[:150])
            script["thumbnail_image_prompt"] = _SAFE_FALLBACK_IMAGE_PROMPT
    return script


def generate_script(
    channel: ChannelConfig,
    topic: str,
    facts: dict | None = None,
    scenes: int | None = None,
    min_minutes: float = 3,
    max_minutes: float = 6,
    web_facts: list[dict] | None = None,
) -> dict:
    """Gera o roteiro completo do vídeo. Se `facts` for passado (ex.: saída
    de politica_data.random_fact_set()), o roteiro é obrigado a usar só
    esses dados reais em vez de a IA inventar números/nomes. `web_facts`
    (ex.: saída de web_search.search_topic_facts()) faz o mesmo papel pra
    tema digitado por uma pessoa — ancora em trecho real com fonte, em vez
    de só "conhecimento geral" do LLM (pode estar desatualizado/errado, e
    inventar fato sobre pessoa real é o pior erro possível). `scenes`,
    `min_minutes` e `max_minutes` permitem gerar roteiros bem mais longos
    (formato documentário) sem mudar a config padrão do canal — ver
    ChannelConfig.short_min_minutes/long_min_minutes etc.

    Levanta ValueError se o LLM não devolver um JSON parseável (raro, mas
    acontece)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _prompt_for(channel, topic, facts, scenes, min_minutes, max_minutes, web_facts)},
    ]

    required = {"title", "description", "tags", "scenes"}
    min_total_words = round(min_minutes * WORDS_PER_MINUTE * MIN_WORDS_RATIO)
    n_scenes = scenes or channel.scenes_per_video
    total_max_words = round(max_minutes * WORDS_PER_MINUTE)
    # tolerância de 15% acima do máximo — narração não bate a contagem de
    # palavras com precisão cirúrgica, rejeitar por 1 palavra a mais forçaria
    # retry à toa. Mas SEM teto nenhum, o vídeo podia sair bem mais longo que
    # o configurado (visto na prática: canal configurado pra 8min saiu com
    # 12min porque só existia checagem de mínimo, nunca de máximo).
    max_words_ceiling = round(total_max_words * MAX_WORDS_RATIO)
    # ~1.5 tokens/palavra em pt-BR + overhead de JSON/image_prompt (em
    # inglês, ~30-50 palavras por cena) + folga de 40% — sem isso a resposta
    # trunca no meio do JSON pra roteiros longos (visto na prática: 31
    # cenas cortou em ~3100 caracteres com o default baixo do provedor).
    max_tokens = min(int((total_max_words * 1.5 + n_scenes * 60 + 500) * 1.4), 16000)
    last_error: Exception | None = None
    best_script: dict | None = None
    best_distance = float("inf")
    for attempt in range(3):
        try:
            # `validate` já garante que `raw` é JSON parseável com os campos
            # obrigatórios — cascata inteira de provedores/modelos é
            # percorrida ANTES de aceitar qualquer coisa (ver providers.complete).
            raw = complete(messages, max_tokens=max_tokens, validate=lambda r: _is_parseable_script(r, required))
            script = _extract_json(raw)
            missing = required - script.keys()
            if missing:
                raise ValueError(f"JSON do roteiro sem campos {missing}: {script}")

            word_count = sum(len(s.get("narration", "").split()) for s in script["scenes"])
            if min_total_words <= word_count <= max_words_ceiling:
                return _sanitize_person_images(topic, script)

            distance = (
                min_total_words - word_count if word_count < min_total_words
                else word_count - max_words_ceiling
            )
            log.warning(
                "tentativa %d/3: roteiro fora da meta (%d palavras, faixa %d-%d), tentando de novo",
                attempt + 1, word_count, min_total_words, max_words_ceiling,
            )
            if distance < best_distance:
                best_script, best_distance = script, distance
        except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            log.warning("tentativa %d/3: roteiro malformado (%s), tentando de novo", attempt + 1, exc)

    if best_script is not None:
        log.warning(
            "usando o melhor roteiro obtido mesmo fora da faixa de %d-%d palavras",
            min_total_words, max_words_ceiling,
        )
        return _sanitize_person_images(topic, best_script)

    raise ValueError(f"LLM não devolveu roteiro válido após 3 tentativas: {last_error}")
