"""Geração de roteiro: título, descrição, tags e cenas (texto de narração +
prompt de imagem por cena), via cascata de LLMs (providers.complete)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from .config import ChannelConfig
from .providers import complete

log = logging.getLogger("script_gen")

# edge-tts com rate="-8%" (ver src/tts.py — meio-termo entre "-15%", que
# soava monótono/devagar demais, e "+0%", que ficou rápido demais) narra a
# ~140 palavras/minuto em pt-BR (medido: -15% dava ~130 wpm, +0% ~150). Pedir "um vídeo de X
# minutos" pro LLM não funciona: ele bate a CONTAGEM de cenas mas escreve
# frases curtas demais em cada uma (visto num vídeo longo que saiu com 4min
# em vez de 15-20). Por isso convertemos minutos-alvo em palavras-por-cena,
# uma meta concreta e verificável. Se a velocidade da narração mudar de
# novo, remedir e ajustar aqui junto — senão a duração real do vídeo
# desalinha do que o painel mostra.
WORDS_PER_MINUTE = 140
MIN_WORDS_RATIO = 0.7  # abaixo disso, tenta de novo antes de aceitar
MAX_WORDS_RATIO = 1.15  # acima disso, também tenta de novo (nunca tinha teto)

SYSTEM_PROMPT = (
    "Você é um roteirista de vídeos do YouTube no estilo 'faceless' "
    "(sem apresentador, só narração + imagens). Responda SEMPRE em JSON "
    "válido, sem markdown, sem texto fora do JSON. "
    "REGRA CRÍTICA de voz: o roteiro é a voz do CANAL — nunca copie trecho "
    "de matéria/coluna palavra por palavra, e ironia ou deboche de colunista "
    "(\"esse eu não lembro quem é\", \"aguardando a aposentadoria\") nunca vira "
    "fala do canal. Nunca ridicularize, apelide ou zombe de pessoa real "
    "(idade, aparência, desempenho), mesmo que a fonte faça; aviso de rodapé "
    "de site (\"os comentários não representam...\") nunca entra. "
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
    "frase que soa como ponto final de assunto encerrado. A pergunta ou "
    "promessa do fim de uma cena TEM que ser respondida logo na 1ª frase da "
    "cena seguinte, sobre o MESMO assunto — se a próxima cena fala de outra "
    "coisa, a ponte anuncia esse outro assunto (nunca pergunte sobre o "
    "adversário e comece a cena seguinte falando de dívida). Varie o ritmo das "
    "frases (curtas e diretas misturadas com uma mais longa, nunca a mesma "
    "estrutura sintática repetida cena após cena), fale direto com quem "
    "assiste ('você', 'imagina se...', perguntas retóricas) em vez de tom de "
    "verbete/enciclopédia, e nunca deixe duas cenas seguidas em tom "
    "puramente informativo sem reação, tensão ou surpresa nenhuma — isso é "
    "o que mais mata retenção no meio do vídeo."
)


# Regras anti-erro que valem pra todo roteiro de todo canal — cada uma saiu
# de um motivo real de reprovação na revisão de fatos (src/fact_check.py):
# título prometendo o que o roteiro não entrega, evento futuro narrado como
# passado ("o clássico já passou" com o jogo marcado pra dali 9 dias),
# número/nome sem fonte.
ACCURACY_RULES = """REGRAS DE PRECISÃO (obrigatórias — o roteiro passa por revisão de fatos e é descartado se violar):
- O título, o thumbnail_text e a descrição só podem prometer o que as cenas realmente narram. Nada de título sobre um assunto que o roteiro não cobre.
- Tempo verbal de eventos: compare cada data com a DATA DE HOJE acima. Se a data de um jogo/evento é futura, fale no futuro ("vai enfrentar", "no dia 07/10"); se não houver data completa (dia E mês) nos dados, NÃO afirme se já aconteceu ou não — cite só o que a fonte diz.
- Números, valores, placares, nomes e cargos: só os que aparecem nos DADOS REAIS/fontes. Na dúvida, deixe de fora em vez de arredondar, estimar ou completar.
- Possibilidade não é fato: se a fonte diz "pode", "deve", "negocia", "avalia", narre como possibilidade.
- Não tire conclusão, consequência ou impacto que a fonte não diz. Leia a relação de cada pessoa com o assunto exatamente como está: "emprestado pelo clube X" = joga em OUTRO clube (não desfalca X agora); "ex-X" = já saiu antes (não é saída de agora).
- Nunca acuse ninguém de irregularidade sem que a fonte diga isso com todas as letras."""


def current_date_rule() -> str:
    """O LLM não sabe a data de hoje e escrevia "em 2024" / "este ano de
    2025" como se fosse o presente — isso envelhece o vídeo na hora e passa
    desleixo. Vai em todo prompt que gera tema ou roteiro."""
    today = date.today()
    return (
        f"DATA DE HOJE: {today.strftime('%d/%m/%Y')} (ano atual: {today.year}). Nunca trate "
        f"{today.year - 1} ou anos anteriores como o presente ou como \"este ano\"; não diga "
        "\"recentemente\", \"acabou de lançar\" ou \"a novidade\" sobre algo que você não sabe se "
        "ainda é atual. Prefira não citar ano quando não for necessário."
    )


_FOREIGN_FIELDS = """  "idioma": {"OPCIONAL — termo da narração em INGLÊS, ESPANHOL, FRANCÊS, ALEMÃO, ITALIANO ou HOLANDÊS (nome de ferramenta, empresa, pessoa, lugar, expressão), exatamente como está escrito na narração": "código do idioma: en, es, fr, de, it ou nl — ex.: \"Claude Code\": \"en\", \"Runway\": \"en\", \"Palm Jumeirah\": \"en\", \"Hindenburg\": \"de\", \"Mont Saint-Michel\": \"fr\". A voz fala esse termo com a pronúncia nativa. Palavra que já existe em português (Havana, Londres, Nova York, internet) NUNCA entra. Sem termo assim, {}"},
  "pronuncia": {"OPCIONAL — só nome de OUTRO alfabeto/idioma (grego, russo, japonês, árabe, chinês etc.) que uma voz brasileira leria errado — inglês, espanhol, francês, alemão, italiano e holandês vão em \"idioma\", NUNCA aqui": "como se fala, escrito à portuguesa com acento na sílaba tônica — ex.: \"Pavlopetri\": \"Pavlopétri\", \"Dyatlov\": \"Diátlov\", \"Aokigahara\": \"Aokigahára\". Palavra portuguesa nunca entra. Sem nome assim, {}"},
"""
_PRONUNCIATION_FIELD = """  "pronuncia": {"OPCIONAL — só nomes estrangeiros/difíceis da narração (lugar, pessoa, espécie) que uma voz brasileira leria errado": "como se fala, escrito à portuguesa com acento na sílaba tônica — ex.: \"Pavlopetri\": \"Pavlopétri\", \"Wegener\": \"Véguener\", \"Mesosaurus\": \"Mesossáurus\". Palavra portuguesa comum nunca entra. Sem nome assim, {}"},
"""


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
{json.dumps(facts, ensure_ascii=False, separators=(",", ":"))}
"""
        if _news_count(facts):
            facts_block += NEWS_STRUCTURE.format(n=_news_count(facts))

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

    # foto/vídeo de arquivo do Commons (src/real_media.py) — só nos canais
    # com real_media, pra não mexer no roteiro de quem não usa (Botafogo)
    real_subject_field = (
        ', "real_subject": "nome próprio EM INGLÊS, como no título da Wikipédia em inglês, da coisa real '
        'específica que ESTA cena mostra (lugar, objeto, evento, pessoa, navio, documento) — ex.: '
        '\'Antikythera mechanism\', \'Dyatlov Pass incident\', \'Centralia, Pennsylvania\', \'Voynich manuscript\'. '
        'Serve pra buscar FOTO/VÍDEO REAL de arquivo do assunto. Cenas seguidas sobre o mesmo caso podem variar '
        'o aspecto concreto (\'Antikythera wreck\', \'National Archaeological Museum, Athens\'). null quando a cena '
        'não cita nada concreto com nome (reflexão, transição, conceito)."'
    ) if channel.real_media else ""
    # canal sobre um país: busca de banco sem o país devolve Capitólio/Canadá
    country_rule = (
        f"\nREGRA CRÍTICA sobre o país: o canal é sobre o {channel.country}. Em \"stock_query\" de lugar, "
        f"governo, cidade, polícia ou dinheiro, inclua o país (ex.: 'Brazil congress building', 'Brasilia aerial', "
        f"'Brazilian real banknotes') — sem isso o banco devolve bandeira do Canadá e Capitólio americano."
        + (" Em \"real_subject\", prefira o nome do lugar/órgão brasileiro ('National Congress of Brazil', "
           "'Palácio do Planalto', 'Supreme Federal Court')." if channel.real_media else "")
        + "\n"
    ) if channel.country else ""

    # termo estrangeiro: voz nativa ("idioma") ou reescrita à portuguesa
    # ("pronuncia") — ver ChannelConfig.native_foreign
    pronunciation_fields = _FOREIGN_FIELDS if channel.native_foreign else _PRONUNCIATION_FIELD

    return f"""{channel.prompt_base}

{current_date_rule()}

{ACCURACY_RULES}

Tópico do vídeo: {topic}
Idioma: {channel.language}
Número de cenas: {n_scenes}
{facts_block}{web_facts_block}
Gere um JSON com exatamente este formato:
{{
  "title": "título chamativo, até 100 caracteres",
  "thumbnail_text": "gancho CURTÍSSIMO pra thumbnail, no máximo 4 palavras, em português correto (concordância certa, nenhuma palavra em inglês — saiu \"DOR É FUEL\" e \"A ÁLCOOL VERDADE\"), tipo manchete de banca de jornal — não é o título, é a frase que faz alguém parar de rolar o feed",
{pronunciation_fields}  "thumbnail_image_prompt": "prompt em inglês pro momento MAIS marcante da história (não precisa ser a cena 1) — close-up, alto contraste, cor vibrante, um único foco. Se a história tem protagonista REAL (pessoa com nome, mesmo que o nome não esteja no prompt): NUNCA close no rosto, nem genérico ('a determined man') — o contexto entrega quem é e a IA erra o rosto; use mãos/objeto pessoal (crachá, número de peito, caneta), silhueta de costas ou símbolo/cenário. Se o personagem é fictício/genérico: rosto com emoção EXAGERADA (chocado, olhos arregalados) — o que mais gera clique. Sem pessoa: objeto/cenário de contraste forte que gere uma pergunta. SEM texto, palavras, logos, botões ou UI",
  "description": "descrição para o YouTube, 2-3 parágrafos, com contexto e call-to-action",
  "tags": ["tag1", "tag2", "..."],
  "mood": "clima geral do vídeo pra escolher a música de fundo certa — exatamente um destes valores: 'tenso' (denúncia grave, algo errado/perigoso, suspense), 'animado' (conquista, vitória, dica animadora, virada positiva), 'melancolico' (derrota, perda, resultado triste/decepcionante), 'calmo' (reflexivo, sem tensão nem euforia), 'neutro' (explicativo/factual, sem carga emocional forte definida). Escolha 1 só, o que domina o vídeo como um todo, não cena a cena.",
  "scenes": [
    {{"narration": "texto que o narrador vai falar nesta cena", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera — SEM texto, palavras, logos, botões ou UI, e SEM jogador/atleta de uniforme ou escudo de time (vira jogador falso que parece real)", "stock_query": "3 a 5 palavras em inglês pra buscar filmagem REAL — amarrada num detalhe CONCRETO desta cena (lugar, objeto, ação), nunca a categoria genérica (ruim: 'soccer stadium'; bom, se a cena fala de gol sofrido: 'goalkeeper diving missed save'). NUNCA nome de pessoa real, NUNCA escudo/camisa/uniforme/logo/bandeira de time (o banco só tem clubes estrangeiros); futebol é 'soccer', nunca 'football'. Cena abstrata/conceitual que não existe filmada: null (nunca 'symbolic shot' ou 'related to the story').", "impact_beat": "true APENAS na cena do momento mais chocante/decisivo de todo o vídeo (o gol, o número mais absurdo, a virada) — no máximo 1 cena `true` no roteiro inteiro; todas as outras, false ou omita.", "stat_overlay": "OPCIONAL, deixe null na grande maioria das cenas — só preencha quando ESTA cena citar, no texto da narração, 2 valores comparáveis específicos vindos de DADOS REAIS (ex.: posse de bola de cada time, % de aprovação, participação em votos) — formato exato: {{\"label\": \"o que está sendo comparado\", \"a_label\": \"nome do 1º\", \"a_value\": numero, \"b_label\": \"nome do 2º\", \"b_value\": numero, \"unit\": \"% se for porcentagem, senão a unidade por extenso (pontos, gols, finalizações)\"}}. NUNCA invente ou estime os valores — só use quando estiverem literalmente nos DADOS REAIS."{real_subject_field}}}
  ]
}}

O array "scenes" deve ter exatamente {n_scenes} itens. META DE TAMANHO
OBRIGATÓRIA: cada cena precisa ter entre {words_per_scene_min} e
{words_per_scene_max} palavras de narração — isso é um parágrafo com
vários fatos/frases, NUNCA uma frase única de uma linha só. O roteiro
completo deve somar entre {total_min_words} e {total_max_words} palavras no
total (~{min_minutes:.0f} a {max_minutes:.0f} minutos narrados). Se não
tiver conteúdo real suficiente pra encher uma cena no tamanho pedido,
aprofunde com detalhes concretos QUE VOCÊ TEM CERTEZA que são reais
(contexto, números, comparações, consequências). Sem mais fato real,
termine antes do tamanho pedido — vídeo mais curto é melhor que detalhe
inventado (data, origem, "habitantes que sobreviveram"). Nunca encha
linguiça repetindo a mesma ideia com outras palavras, nunca termine com
conselho de turismo ("não deixe de visitar", "experiência única") e não
repita a mesma palavra frase após frase — troque por pronome ou sinônimo
(o nome do assunto 12 vezes e "cidade" 16 vezes num vídeo de 3 minutos,
dry-run de 06/10). Nunca narre em primeira pessoa como se fosse uma pessoa
real com história própria ("durante meus 8 anos como sargento") nem invente
personagem com nome ("Maria, uma jovem profissional") — o narrador é o
canal. Se o título ou o gancho promete um método, passos ou N casos, o
roteiro ENTREGA cada um, concreto e na ordem — nunca só repete a promessa.

{country_rule}
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
o título. Tem que ter UM elemento CONCRETO desta história (número, valor,
nome, objeto, lugar) — gancho genérico que serviria pra qualquer vídeo
("A VERDADE OUSADA", "ISSO É CHOCANTE", "O SEGREDO REVELADO", "INCRÍVEL")
é proibido. Concordância certa: número com plural ("9 MORTES", nunca "9
MORTE"). Marque com *asteriscos* a palavra (ou número) de maior impacto —
ela sai numa faixa amarela na thumbnail, ex.: "O ERRO DE *R$ 2 MILHÕES*"."""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # Alguns modelos ainda envolvem em ```json ... ``` mesmo pedindo pra não fazer isso.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta do LLM não contém JSON: {raw[:300]}")
    return json.loads(match.group(0))


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# vídeo de várias notícias (portal do Botafogo, 15/10): o tema era a 1ª
# manchete e o roteiro pulava de uma pra outra e voltava — "começa com o
# Zorro e muda de assunto". Agora é um noticiário: abertura, uma notícia por
# bloco na ordem da lista, sem misturar. O campo "noticia" de cada cena diz
# qual ela narra (foto e manchete na tela saem dele, ver run_pipeline.py).
NEWS_STRUCTURE = """
ESTRUTURA OBRIGATÓRIA (são {n} notícias SEPARADAS, formato noticiário):
- Cena 1 = abertura curta: diga quantas notícias tem hoje e cite as
  principais numa frase só. "noticia": 0.
- Depois, cada notícia no SEU bloco de cenas, NA ORDEM da lista
  "noticias" (1 = a primeira da lista). Cada cena ganha o campo extra
  "noticia" com esse número. Uma cena NUNCA fala de duas notícias, e
  depois que um bloco termina ele não volta mais.
- A 1ª cena de cada bloco começa anunciando a notícia ("Primeira notícia:",
  "Segunda notícia:", ... "Última notícia:") e a manchete em poucas
  palavras; não ligue uma notícia na outra como se fosse a mesma história.
- O título do vídeo é a manchete MAIS forte do dia, curta (até 50
  caracteres), escrita como notícia única: "Júnior Santos Deixa o
  Botafogo", "Justiça multa Textor". Nunca "X notícias", "hoje",
  "notícias do dia" nem lista de assuntos.
"""

# título de noticiário genérico: "5 notícias do Botafogo hoje" e "Botafogo
# hoje: ..." caíram de 85 pra 3 views em 5 dias; manchete única no título
# fez 124-240 (out/26)
_GENERIC_NEWS_TITLE_RE = re.compile(r"\bnot[íi]cias?\b|\bhoje\b|\bdo dia\b", re.IGNORECASE)


def _news_title_problem(script: dict, facts: dict | None) -> str | None:
    title = str(script.get("title") or "")
    if _GENERIC_NEWS_TITLE_RE.search(title):
        return f"título genérico de noticiário ({title!r})"
    if len(title) > 60:
        return f"título longo demais ({len(title)} caracteres)"
    # dia de jogo: "Botafogo x Vasco" (487) e "Botafogo x Coritiba" (374)
    # foram os dois vídeos mais vistos do canal
    hoje = (facts or {}).get("hoje")
    for jogo in (facts or {}).get("proximos_jogos") or []:
        if hoje and str(jogo.get("data") or "").startswith(hoje) and " x " not in title.lower():
            return f"dia de jogo e o título não traz o confronto ({jogo.get('jogo')})"
    return None


def _news_count(facts: dict | None) -> int:
    return len([n for n in (facts or {}).get("noticias") or [] if isinstance(n, dict)])


def _news_order_problem(script: dict, n: int) -> str | None:
    """Roteiro de várias notícias que mistura ou volta pra uma notícia já
    encerrada — o vídeo vira uma história só, sem pé nem cabeça."""
    vistos: list[int] = []
    for scene in script.get("scenes", []):
        k = scene.get("noticia")
        if not isinstance(k, int) or not 0 <= k <= n:
            return "cena sem o campo \"noticia\" (número da notícia que ela narra)"
        if k == 0:
            continue
        if vistos and k != vistos[-1] and k in vistos:
            return f"a notícia {k} voltou depois de outra já ter começado"
        if not vistos or k != vistos[-1]:
            vistos.append(k)
    if vistos != sorted(vistos):
        return "as notícias saíram fora da ordem da lista"
    return None


def _norm_sentence(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _drop_repeated_scenes(script: dict) -> dict:
    """Tira frase que repete frase de cena anterior (e a cena que ficar
    vazia). Com pouca notícia e meta de minutos, o modelo copiava a 1ª
    metade do roteiro na 2ª (job 222) ou fechava recapitulando a tabela
    palavra por palavra (job 236, reprovado na revisão final por isso) —
    vídeo mais curto é melhor que vídeo repetido."""
    seen: set[str] = set()
    kept = []
    for scene in script.get("scenes", []):
        out = []
        for sentence in _SENTENCE_SPLIT.split(scene.get("narration", "")):
            key = _norm_sentence(sentence)
            if len(key.split()) >= 6 and key in seen:
                log.warning("frase repetida cortada: %s", sentence[:80])
                continue
            seen.add(key)
            out.append(sentence)
        narration = " ".join(out).strip()
        if len(narration.split()) >= 4:
            kept.append({**scene, "narration": narration})
    return {**script, "scenes": kept}


def _filler_phrase(script: dict, topic: str = "", min_scenes: int = 4, min_ratio: float = 0.3) -> str | None:
    """Sequência de 4 palavras que aparece em várias cenas — bordão de
    enchimento ("isso mostra a importância", "o desempenho do elenco").
    Ignora nome próprio repetido sozinho porque exige 4 palavras seguidas.
    O limite cresce com o roteiro (30% das cenas, mínimo 4): num longo de 15-30
    cenas, 4 repetições é normal. Expressão feita só de termos do tema ("jogo
    contra o vasco", "emendas parlamentares em 2023") não é enchimento — falsos
    positivos assim derrubaram 4 jobs em 2026-10-04."""
    scenes = script.get("scenes", [])
    threshold = max(min_scenes, math.ceil(len(scenes) * min_ratio))
    topic_words = set(re.findall(r"\w+", topic.lower()))
    count: dict[tuple[str, ...], int] = {}
    for scene in scenes:
        words = re.findall(r"\w+", scene.get("narration", "").lower())
        for gram in {tuple(words[i:i + 4]) for i in range(len(words) - 3)}:
            count[gram] = count.get(gram, 0) + 1
    for gram, n in sorted(count.items(), key=lambda kv: kv[1], reverse=True):
        if n < threshold:
            break
        content = [w for w in gram if len(w) > 3 or w.isdigit()]
        if content and all(w in topic_words for w in content):
            continue
        return " ".join(gram)
    return None


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CTA_RE = re.compile(
    r"\b(inscrev\w*|deix\w* (o |seu |um )?like|curt\w* (o |este |esse )?vídeo|sininho|"
    r"ative as notificações|obrigad[oa] por assistir|até a próxima)\b",
    re.IGNORECASE,
)


def _drop_llm_cta(script: dict) -> dict:
    """O pedido de like/inscrição é gerado por código no fim
    (run_pipeline.py); o do LLM nas últimas cenas virava CTA dobrado
    ("Obrigado por assistir... deixe seu like e se inscreva" + o nosso)."""
    scenes = list(script.get("scenes", []))
    for idx in range(max(0, len(scenes) - 2), len(scenes)):
        frases = _SENTENCE_RE.split(scenes[idx].get("narration", ""))
        keep = [f for f in frases if not _CTA_RE.search(f)]
        if len(keep) != len(frases):
            log.info("CTA do roteiro cortado: %s", " ".join(f for f in frases if _CTA_RE.search(f))[:120])
            scenes[idx] = {**scenes[idx], "narration": " ".join(keep).strip()}
    return {**script, "scenes": [sc for sc in scenes if sc.get("narration", "").strip()]}


_STOPWORDS = set(
    "para pela pelo pelos pelas como mais mas isso essa esse esta este está "
    "foram eram será seria tinha temos você vocês ele ela eles elas seus suas "
    "também ainda quando onde porque sobre entre depois antes muito muita "
    "cada todo toda todos todas outro outra outros outras mesmo mesma desta "
    "deste dessa desse aqui hoje sempre nunca apenas quem qual quais".split()
)


# número por extenso e moeda não são "palavra repetida": valor dito por
# extenso ("quarenta e quatro mil reais") repetia "quarenta"/"reais" à toa
_NUMBER_WORDS = {
    "zero", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "onze", "doze", "treze",
    "quatorze", "catorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove", "vinte", "trinta",
    "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa", "cento", "duzentos",
    "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos",
    "milhão", "milhões", "bilhão", "bilhões", "trilhão", "trilhões", "real", "reais", "centavo",
    "centavos", "dólar", "dólares", "euro", "euros", "libra", "libras", "vírgula",
}


def _overused_word(script: dict, ignore: str = "") -> str | None:
    """Palavra de conteúdo repetida demais no roteiro todo (mais de ~2,2% das
    palavras, mínimo 9 vezes): "cidade" 16x e "Pavlopetri" 12x em 476
    palavras no dry-run de 06/10 — soa repetitivo de ouvir."""
    words = [w for s in script.get("scenes", []) for w in re.findall(r"\w+", s.get("narration", "").lower())]
    if not words:
        return None
    # "salário"/"salários" contam juntos; teto ~1 a cada 60 palavras (mín. 6):
    # com 9 de mínimo, o vídeo dos salários do STF (12/10) falava "salário"
    # frase sim, frase não — soava robô
    limit = max(6, len(words) / 60)
    skip = _STOPWORDS | set(re.findall(r"\w+", ignore.lower()))  # nome do canal ("Botafogo")
    counts: dict[str, int] = {}
    for w in words:
        if len(w) > 3 and not w.isdigit() and w not in skip and w not in _NUMBER_WORDS:
            w = w[:-1] if w.endswith("s") and len(w) > 4 else w
            counts[w] = counts.get(w, 0) + 1
    word, n = max(counts.items(), key=lambda kv: kv[1], default=("", 0))
    return f"{word} ({n}x)" if n > limit else None


# começo de frase que vira muleta rápido (vídeo dos salários, 12/10)
_MULETAS = {"enquanto isso", "será que", "mas será", "isso mostra", "isso significa", "mas o", "e o", "além disso",
           "você acha", "isso sai", "isso é", "quem paga", "é dinheiro"}
_VALUE = re.compile(r"(?:R\$|US\$|€|£|\$)?\s?\d+(?:\.\d{3})*(?:,\d+)?(?:\s?%)?")


def _repetition_problem(script: dict) -> str | None:
    """Vídeo que repete o mesmo valor ou a mesma muleta: o dos salários do
    STF (12/10) disse "R$ 43.028,79" 3 vezes e abriu 7 frases com "Enquanto
    isso" — soa como enrolação. Valor (4+ dígitos) em mais de 2 frases, ou o
    mesmo começo de frase (2 palavras) 3+ vezes, reprova."""
    frases = [f for sc in script.get("scenes", []) for f in _SENTENCE_SPLIT.split(sc.get("narration", "")) if f.strip()]
    valores: dict[str, int] = {}
    for f in frases:
        for v in {re.sub(r"\D", "", m) for m in _VALUE.findall(f)}:
            if len(v) >= 4 and not (len(v) == 4 and v.startswith(("19", "20"))):  # ano não conta
                valores[v] = valores.get(v, 0) + 1
    v, n = max(valores.items(), key=lambda kv: kv[1], default=("", 0))
    if n > 2:
        return f"o mesmo valor ({v}) aparece em {n} frases — cada valor no máximo 2 vezes"
    inicios: dict[str, int] = {}
    for f in frases:
        w = re.findall(r"\w+", f.lower())[:2]
        if len(w) == 2:
            inicios[" ".join(w)] = inicios.get(" ".join(w), 0) + 1
    ini, n = max(inicios.items(), key=lambda kv: kv[1], default=("", 0))
    if n >= (3 if ini in _MULETAS else 5):
        return f"{n} frases começam com \"{ini}\" — varie o começo das frases"
    return None


_CONECTIVO = re.compile(r"^(enquanto isso|além disso|ao mesmo tempo|por outro lado|nesse sentido|dessa forma),\s*", re.I)


def _trim_muletas(script: dict) -> dict:
    """Conectivo-muleta no começo de frase ("Enquanto isso, ...") fica só na
    1ª vez; nas outras sai e a frase começa direto. Roteiro de reserva
    (3 tentativas com repetição) saía com 4 "Enquanto isso" (15/10)."""
    vistos: set[str] = set()
    scenes = []
    for sc in script.get("scenes", []):
        frases = []
        for f in _SENTENCE_SPLIT.split(sc.get("narration", "")):
            m = _CONECTIVO.match(f)
            if m:
                chave = m[1].lower()
                if chave in vistos and len(f) > m.end():
                    resto = f[m.end():]
                    f = resto[:1].upper() + resto[1:]
                vistos.add(chave)
            frases.append(f)
        scenes.append({**sc, "narration": " ".join(frases)})
    return {**script, "scenes": scenes}


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


QUEUE_DB = Path(__file__).resolve().parent.parent / "data" / "queue.db"
_FACT_PREFIX = "roteiro reprovado na revisão de fatos: "
_VIDEO_PREFIX = "vídeo reprovado na revisão antes do upload: "


def _split_items(text: str) -> list[str]:
    """Separa 'A (motivo; x); B (motivo)' no ';' de fora dos parênteses."""
    items, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == ";" and depth <= 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return [i for i in [*items, cur.strip()] if i]


def recent_mistakes(channel_name: str, jobs: int = 8, max_items: int = 12) -> str:
    """Aprende com o que já foi reprovado: os motivos das últimas revisões
    (fatos e vídeo) deste canal, guardados no erro do job, entram no prompt
    pro LLM não repetir o mesmo tipo de erro (botafogo 09/10: 'jogo em casa'
    sendo fora, 'zona de rebaixamento' sem fonte, jobs 350-353)."""
    try:
        with closing(sqlite3.connect(QUEUE_DB)) as conn:
            rows = conn.execute(
                "SELECT error FROM jobs WHERE channel = ? AND status IN ('retried', 'failed') "
                "AND (error LIKE ? OR error LIKE ?) ORDER BY id DESC LIMIT ?",
                (channel_name, _FACT_PREFIX + "%", _VIDEO_PREFIX + "%", jobs),
            ).fetchall()
    except sqlite3.Error:
        return ""
    lines: list[str] = []
    for (error,) in rows:
        if error.startswith(_FACT_PREFIX):
            items = [f"- fato: {i[:220]}" for i in _split_items(error[len(_FACT_PREFIX):])]
        else:
            items = [f"- imagem/vídeo: {i[:220]}" for i in _split_items(error[len(_VIDEO_PREFIX):])]
        lines += [i for i in items if i not in lines]
    if not lines:
        return ""
    return (
        "\n\nERROS RECENTES DESTE CANAL (roteiros/vídeos já reprovados — NÃO repita o mesmo tipo de erro; "
        "na dúvida, deixe a afirmação de fora; mando/local de jogo, próximo adversário, posição na tabela "
        "e números só se estiverem nas fontes; image_prompt de cada cena tem que mostrar exatamente o "
        "assunto daquela cena e variar entre as cenas):\n" + "\n".join(lines[:max_items])
    )


def generate_script(
    channel: ChannelConfig,
    topic: str,
    facts: dict | None = None,
    scenes: int | None = None,
    min_minutes: float = 3,
    max_minutes: float = 6,
    web_facts: list[dict] | None = None,
    revision_feedback: str | None = None,
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
        {"role": "user", "content": _prompt_for(channel, topic, facts, scenes, min_minutes, max_minutes, web_facts)
         + recent_mistakes(channel.name) + (revision_feedback or "")},
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
    filler_fallback: dict | None = None
    for attempt in range(3):
        try:
            # `validate` já garante que `raw` é JSON parseável com os campos
            # obrigatórios — cascata inteira de provedores/modelos é
            # percorrida ANTES de aceitar qualquer coisa (ver providers.complete).
            from .topics import _SMALL_MODEL_RE  # import tardio: topics importa este módulo

            # roteiro de modelo pequeno (cota dos grandes esgotada) saía
            # colagem sem sentido das fontes (job 318) — sem modelo grande,
            # melhor o dia sem vídeo
            raw = complete(
                messages, max_tokens=max_tokens, validate=lambda r: _is_parseable_script(r, required),
                model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
            )
            script = _extract_json(raw)
            missing = required - script.keys()
            if missing:
                raise ValueError(f"JSON do roteiro sem campos {missing}: {script}")

            script = _trim_muletas(_drop_llm_cta(_drop_repeated_scenes(script)))
            news_problem = _news_order_problem(script, _news_count(facts)) if _news_count(facts) > 1 else None
            news_problem = news_problem or (_news_title_problem(script, facts) if _news_count(facts) > 1 else None)
            if news_problem:
                filler_fallback = script
                last_error = ValueError(news_problem)
                log.warning("tentativa %d/3: notícias misturadas (%s), tentando de novo", attempt + 1, news_problem)
                messages = [messages[0], {"role": "user", "content": messages[1]["content"]
                                          + f"\n\nO roteiro anterior errou: {news_problem}. Uma notícia "
                                          "por bloco, na ordem da lista, cada cena com o campo \"noticia\"; título = a "
                                          "manchete mais forte, curta, sem \"notícias\"/\"hoje\" (em dia de jogo, "
                                          "\"Botafogo x Adversário\" + gancho curto)."}]
                continue
            repeated = _repetition_problem(script)
            if repeated:
                filler_fallback = script
                last_error = ValueError(repeated)
                log.warning("tentativa %d/3: repetição (%s), tentando de novo", attempt + 1, repeated)
                messages = [messages[0], {"role": "user", "content": messages[1]["content"]
                                          + f"\n\nO roteiro anterior se repetiu: {repeated}. Cada dado é dito uma vez "
                                          "(no máximo citado de novo numa comparação); sem frase-muleta."}]
                continue
            filler = _filler_phrase(script, topic)
            overused = None if filler else _overused_word(script, channel.channel_title)
            if overused:
                filler_fallback = script
                last_error = ValueError(f"palavra repetida demais ({overused})")
                log.warning("tentativa %d/3: palavra repetida demais (%s), tentando de novo", attempt + 1, overused)
                word = overused.split()[0]
                messages = [messages[0], {"role": "user", "content": messages[1]["content"]
                                          + f"\n\nA palavra \"{word}\" apareceu demais: varie com pronome/sinônimo, "
                                          "e não repita a mesma palavra frase após frase."}]
                continue
            if filler:
                # bordão em toda cena ("isso mostra a importância", job 239)
                # = enchimento de modelo fraco: tenta de novo, pedindo pra não.
                # Guarda o roteiro: bordão é defeito de estilo, não de fato —
                # melhor publicá-lo do que perder o dia (revisões seguem valendo)
                filler_fallback = script
                last_error = ValueError(f"bordão repetido ({filler!r})")
                log.warning("tentativa %d/3: bordão repetido (%r), tentando de novo", attempt + 1, filler)
                messages = [messages[0], {"role": "user", "content": messages[1]["content"]
                                          + f"\n\nNÃO use a expressão \"{filler}\" nem frases de enchimento sem fato novo."}]
                continue
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
    if filler_fallback is not None:
        log.warning("3 tentativas com bordão repetido — usando a última mesmo assim")
        return _sanitize_person_images(topic, filler_fallback)

    raise ValueError(f"LLM não devolveu roteiro válido após 3 tentativas: {last_error}")


_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def fix_dangling_hooks(script: dict) -> dict:
    """Pergunta de fim de cena que a cena seguinte não responde (job 363:
    "E quem vem do outro lado?" seguida do prazo da recuperação judicial)
    sai do roteiro. Um LLM só aponta quais estão soltas; o corte é por
    código e só tira a pergunta — nunca acrescenta fato. Sem LLM, segue."""
    scenes = script.get("scenes") or []
    pairs: list[tuple[int, str, str]] = []
    for i in range(len(scenes) - 1):
        sentences = _SENTENCES.split(scenes[i].get("narration", "").strip())
        if len(sentences) > 1 and sentences[-1].endswith("?"):
            nxt = " ".join(_SENTENCES.split(scenes[i + 1].get("narration", "").strip())[:2])
            pairs.append((i, sentences[-1], nxt))
    if not pairs:
        return script
    listing = "\n".join(f"{k}) PERGUNTA: {q}\n   COMEÇO DA CENA SEGUINTE: {n}" for k, (_, q, n) in enumerate(pairs))
    prompt = (
        "Em cada item, a PERGUNTA fecha uma cena de vídeo e puxa pra seguinte. "
        "Ela está CONECTADA se o começo da cena seguinte responde ou continua "
        "exatamente o assunto da pergunta; DESCONECTADA se fala de outra coisa.\n\n"
        f"{listing}\n\nResponda só JSON: {{\"desconectadas\": [números]}}"
    )

    def parse(raw: str) -> list[int] | None:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
            ids = data.get("desconectadas") if isinstance(data, dict) else None
            return [int(x) for x in ids] if isinstance(ids, list) else None
        except (ValueError, TypeError):
            return None

    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=200, validate=lambda r: parse(r) is not None)
    except RuntimeError as exc:
        log.warning("checagem de ganchos: nenhum provedor respondeu (%s) — segue sem", exc)
        return script
    new_scenes = [dict(sc) for sc in scenes]
    for k in parse(raw) or []:
        if 0 <= k < len(pairs):
            i, question, _ = pairs[k]
            narration = new_scenes[i]["narration"].strip()
            new_scenes[i]["narration"] = narration[: len(narration) - len(question)].rstrip()
            log.warning("gancho solto cortado da cena %d: %s", i + 1, question)
    return {**script, "scenes": new_scenes}


# Pergunta do comentário que o canal posta logo depois do upload (ver
# src/upload.py -> post_comment). Chamada separada e curta — não vira campo
# novo obrigatório no JSON do roteiro (que já teve problema de robustez em
# roteiro longo). Falhou a cascata, cai numa pergunta genérica por código.
_MAX_QUESTION_LEN = 220


def _is_valid_question(text: str) -> bool:
    return 20 <= len(text) <= _MAX_QUESTION_LEN and text.rstrip().endswith("?") and "\n" not in text.strip()


def engagement_question(title: str, scenes: list[dict]) -> str:
    """Pergunta curta que puxa comentário, sobre o conteúdo real do vídeo
    (ex.: qual das consequências surpreendeu mais)."""
    fallback = "Qual parte desse vídeo mais te surpreendeu? Conta aqui nos comentários 👇"
    resumo = " ".join(s.get("narration", "") for s in scenes)[:3000]
    prompt = (
        f"Vídeo do YouTube: \"{title}\".\nResumo da narração: {resumo}\n\n"
        "Escreva UMA pergunta curta (máx. 2 frases), em português do Brasil, "
        "para o próprio canal postar como comentário e puxar conversa. Cite "
        "algo concreto do vídeo, de preferência pedindo pra pessoa escolher "
        "entre 2-3 opções ou dar a opinião dela. Termine com ponto de "
        "interrogação. Sem aspas, sem hashtag, sem explicação — só a pergunta."
    )

    def clean(raw: str) -> str:
        line = next((ln.strip() for ln in raw.strip().splitlines() if ln.strip()), "")
        return line.strip('"“”').strip()

    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=200,
            validate=lambda r: _is_valid_question(clean(r)),
        )
        return clean(raw) + " 👇"
    except RuntimeError as exc:
        log.warning("pergunta de engajamento: nenhum provedor respondeu (%s) — usando a genérica", exc)
        return fallback


def _parse_titles(raw: str, n: int) -> list[str] | None:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        titles = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    if not isinstance(titles, list) or len(titles) != n:
        return None
    titles = [" ".join(str(t).split()).strip(" .") for t in titles]
    return titles if all(1 <= len(t.split()) <= 7 and len(t) <= 60 for t in titles) else None


def chapter_titles(chapters: list[str]) -> list[str] | None:
    """Título curto de cada capítulo, a partir do que ele narra. Antes era o
    começo da narração cortado no meio ("Wegener usou a Glossopteris e o").
    None = sem provedor: quem chama cai pro corte antigo."""
    trechos = "\n".join(f"{i + 1}. {c[:500]}" for i, c in enumerate(chapters))
    prompt = (
        "Capítulos de um vídeo do YouTube (narração de cada um):\n"
        f"{trechos}\n\n"
        f"Dê um título curto (2 a 5 palavras, português do Brasil, frase "
        "completa, sem cortar no meio, sem número, sem aspas) para cada um dos "
        f"{len(chapters)} capítulos, dizendo do que ele trata. Responda SÓ uma "
        'lista JSON de strings, ex.: ["A descoberta", "Como funciona"].'
    )
    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=400,
            validate=lambda r: _parse_titles(r, len(chapters)) is not None,
        )
    except RuntimeError as exc:
        log.warning("títulos dos capítulos: nenhum provedor respondeu (%s)", exc)
        return None
    return _parse_titles(raw, len(chapters))
