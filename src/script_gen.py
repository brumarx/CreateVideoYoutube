"""Geração de roteiro: título, descrição, tags e cenas (texto de narração +
prompt de imagem por cena), via cascata de LLMs (providers.complete)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from .db import connect_db
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

SYSTEM_PROMPT = "Você é um roteirista de vídeos do YouTube no estilo 'faceless' (sem apresentador, só narração + imagens). Responda SEMPRE em JSON válido, sem markdown, sem texto fora do JSON. REGRA CRÍTICA de voz: o roteiro é a voz do CANAL — nunca copie trecho de matéria/coluna palavra por palavra, e ironia ou deboche de colunista (\"esse eu não lembro quem é\", \"aguardando a aposentadoria\") nunca vira fala do canal. Nunca ridicularize, apelide ou zombe de pessoa real (idade, aparência, desempenho), mesmo que a fonte faça; aviso de rodapé de site (\"os comentários não representam...\") nunca entra. REGRA CRÍTICA para todo 'image_prompt': a IA de imagem SEMPRE erra letras/palavras e não deve imitar logos de marcas (YouTube, etc.) — por isso NUNCA peça texto, palavras, letras, números escritos, botões, ícones de interface, logos ou telas de 'inscreva-se'/'curta' na imagem. Descreva só cenário, objetos, pessoas e atmosfera visual. REGRA CRÍTICA sobre pessoas reais nomeadas (política, magistrado, empresário, atleta, figura histórica — QUALQUER pessoa real identificável pelo nome, não só política): se a história tem essa pessoa como protagonista de uma cena, o 'image_prompt' NUNCA pode ser um CLOSE NO ROSTO, mesmo descrito de forma genérica tipo 'a woman's face' ou 'a determined man' — isso NÃO livra a regra, porque o contexto (uniforme, número de peito, cargo, brasão, veste) deixa claro pra quem assiste que aquele rosto genérico É pra ser ela, e a IA de imagem não sabe gerar o rosto real dessa pessoa (pode confundir o nome com outra coisa). Regra prática: quando o protagonista é uma pessoa real, a câmera NUNCA foca no rosto dela — descreva de costas, silhueta ao longe, mãos/pés/objetos pessoais em close (um crachá, número de peito, uniforme, uma caneta assinando), o ambiente ao redor, ou um prédio/símbolo relacionado — nunca um retrato ou close facial dramático que o espectador vá interpretar como sendo o rosto daquela pessoa. Isso vale pro 'image_prompt' de toda cena E pro 'thumbnail_image_prompt'. REGRA CRÍTICA sobre lugares reais: se a cena menciona um país, cidade, monumento ou acidente geográfico REAL específico (ex.: Portugal, Amazônia, Holanda), o 'image_prompt' tem que descrever as características visuais REAIS e reconhecíveis daquele lugar exato (arquitetura, paisagem, clima, cor típica) — nunca um lugar genérico nem de outro país. Errar isso (ex.: pedir imagem de Nova York numa cena sobre o Rio de Janeiro) é o pior erro possível neste roteiro. REGRA CRÍTICA sobre vídeos de lista ('N fatos/coisas/dicas sobre...'): NUNCA sugira ou escreva um título/tema com mais de 10 itens — nada de '20 fatos', '30 coisas', '50 curiosidades'. O máximo é sempre 10; prefira listas menores (5, 7, 10) a forçar uma lista longa e repetitiva. REGRA CRÍTICA sobre o gancho inicial: mais da metade de quem assiste decide continuar ou não nos primeiros 3 segundos, então a PRIMEIRA frase da primeira cena tem que entregar o fato mais chocante/intrigante ou uma pergunta que gera curiosidade imediata — NUNCA comece com saudação, apresentação do canal, contexto histórico/geográfico ou 'hoje vamos falar sobre...'. Vá direto ao ponto mais interessante e só depois explique o contexto. REGRA CRÍTICA sobre reter até o fim (não só abrir bem): gancho forte só na cena 1 não basta — feedback direto de quem assistiu vários vídeos publicados foi que o meio 'fica chato', gente abandona antes do fim. TODA cena, não só a primeira, tem que terminar puxando pra próxima: uma pergunta em aberto, uma contradição ainda não explicada, uma promessa ('mas o que aconteceu depois é ainda mais surpreendente'), nunca uma frase que soa como ponto final de assunto encerrado. A pergunta ou promessa do fim de uma cena TEM que ser respondida logo na 1ª frase da cena seguinte, sobre o MESMO assunto — se a próxima cena fala de outra coisa, a ponte anuncia esse outro assunto (nunca pergunte sobre o adversário e comece a cena seguinte falando de dívida). Varie o ritmo das frases (curtas e diretas misturadas com uma mais longa, nunca a mesma estrutura sintática repetida cena após cena), fale direto com quem assiste ('você', 'imagina se...', perguntas retóricas) em vez de tom de verbete/enciclopédia, e nunca deixe duas cenas seguidas em tom puramente informativo sem reação, tensão ou surpresa nenhuma — isso é o que mais mata retenção no meio do vídeo. REGRA CRÍTICA de narrativa em CADEIA (o molde do Short 'Por que a Coreia do Norte se apaga à noite?', 530 mil views em 4 dias): 1) abre com uma PERGUNTA ou um paradoxo que se VÊ; 2) dá o contexto em 1-2 frases; 3) solta a VIRADA, o dado que inverte o que a pessoa esperava; 4) no meio, repete a pergunta com outras palavras ('Então, como...?'); 5) responde com UMA causa principal, às vezes numa frase de uma palavra só; 6) encadeia as consequências como dominós, cada uma com data ou número; 7) mostra o contraste (o outro lado, o vizinho, o antes); 8) fecha com um número concreto que dá pra imaginar ('1 sul-coreano gasta a luz de 14 norte-coreanos') e um detalhe humano; 9) a ÚLTIMA frase retoma o começo com uma ironia ou virada ('Em 1948 o Norte cortou a luz do Sul. Agora não consegue manter as próprias acesas'). Cada frase precisa trazer um fato NOVO que leve à próxima: se dá pra tirar a frase sem perder nada, ela não entra. Nunca termine com 'o futuro permanece indefinido', 'só o tempo dirá', 'resta saber' ou parecido: isso é admitir que a história não tem final. Molde de DILEMA ('X ou Y?') também serve, com a virada documentada no fim. ADAPTE a cadeia ao formato: NOTÍCIAS: cada notícia é uma mini-cadeia (fato, por que surpreende, consequência com número), na ordem pedida; PASSO A PASSO: gancho é o problema de quem assiste, virada é o erro comum ou o atalho, fim mostra o resultado; PESSOA: obstáculo com número, virada, fim que retoma o começo; DADOS PÚBLICOS: número que choca, comparação que dá pra imaginar, fim que volta ao bolso de quem assiste."


# Regras anti-erro que valem pra todo roteiro de todo canal — cada uma saiu
# de um motivo real de reprovação na revisão de fatos (src/fact_check.py):
# título prometendo o que o roteiro não entrega, evento futuro narrado como
# passado ("o clássico já passou" com o jogo marcado pra dali 9 dias),
# número/nome sem fonte.
ACCURACY_RULES = """REGRAS DE PRECISÃO (obrigatórias: roteiro que violar é reprovado na revisão de fatos):
- Título, miniatura e descrição prometem só o que a narração diz, no mesmo sentido. Capacidade, meta ou previsão nunca vira "fixou", "fez" ou "entregou".
- Números, datas, nomes, cargos, valores, medidas e recordes: só os que estão nas fontes, nunca estimados ou completados. Sem conta feita pelo roteiro (diferença, soma, "o dobro", "X vezes mais").
- Superlativo, exclusividade ou generalização ("a maior", "a primeira", "a única", "o mundo inteiro"): só se a fonte disser exatamente isso; senão use o dado concreto.
- Quem afirma: se a fonte é autoridade ou empresa, diga quem ("segundo a autoridade do condado"). Possibilidade ("pode", "avalia") fica como possibilidade. Nunca acuse sem a fonte dizer com todas as letras.
- Relações de pessoas e clubes como estão na fonte ("emprestado" não é saída agora; "ex-X" já saiu). Não tire conclusão, consequência ou impacto que a fonte não diz.
- A virada é um fato da fonte, não contraste inventado: negação ("não foi", "nunca houve") só se a fonte disser isso. "Imagine..." como hipótese de quem assiste é permitido.
- Datas: compare com a DATA DE HOJE. Futuro se ainda não aconteceu; sem dia e mês completos na fonte, não afirme se aconteceu. Fatos passados no passado, sem presente histórico.
- Termo técnico só com explicação em palavra comum na mesma frase; sigla explicada na primeira vez; no máximo um número por cena; uma ideia por cena.
- Cada cena traz um fato novo que nenhuma outra diz. Explique o COMO e o PORQUÊ em pelo menos uma cena, com palavras simples.
- Sem enchimento ("tecnologia avançada", "um marco", "revoluciona o setor") e sem tautologia (mesma palavra na mesma frase).
- Termine com o que o espectador leva (consequência ou comparação que ele entende), não com um resumo da abertura.
- Detalhe que não está na fonte não entra, mesmo que você ache que sabe. Menos detalhe certo vale mais que muito detalhe sem fonte."""


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


def _get_opening_instruction(channel: str) -> str:
    """Retorna instrução de abertura para o LLM baseado no canal."""
    instructions = {
        "politica": (
            "GANCHO INICIAL (VARIAÇÃO): Comece com um NÚMERO CHOCANTE relacionado ao tema "
            "(salário, gastos, doações, multa, crescimento) — sem contexto antes, pura e simples. "
            "Exemplo: 'R$ 2,3 bilhões em um dia', 'cresceu 4.700%'. O número é a 1ª frase."
        ),
        "motivacional": (
            "GANCHO INICIAL (VARIAÇÃO): Comece com um NÚMERO QUE DEFINE O OBSTÁCULO "
            "(rejeitado 100 vezes, tinha R$ 5, perdeu 3 campanhas, trabalhou 50 anos). "
            "O número é concreto e humano — não seja saudação nem contexto histórico."
        ),
        "aprendervia": (
            "GANCHO INICIAL (VARIAÇÃO): Comece direto no RESULTADO PRÁTICO: 'Copie isto na IA "
            "e economize R$ 500/mês' ou 'Use essa ferramenta grátis em 2 minutos'. "
            "Resultado concreto em português, números, nada de saudação."
        ),
        "curiosidades": (
            "GANCHO INICIAL (VARIAÇÃO): Comece com uma CONTRADIÇÃO IMPOSSÍVEL que desafie a expectativa, direto ao choque. NUNCA abra com data ou ano; abra com a coisa concreta que o espectador vê ou não acredita. ESTRUTURA DE LUGAR OU FENÔMENO: 1) gancho com o fato mais surpreendente, com número ou comparação que o espectador entende; 2) onde e o que existe lá hoje (quem vive, o que se faz, o que se vê); 3) o COMO ou PORQUÊ, em palavras simples, com imagem que explique; 4) o que isso causa de concreto; 5) fecho com pergunta ou comparação com o cotidiano, nunca resumo. Cada cena pede UMA imagem diferente; se duas pediriam a mesma, uma está repetindo."
        ),
        "botafogo": (
            "GANCHO INICIAL (VARIAÇÃO): Comece com um PLACAR ou NÚMERO DO JOGO "
            "(Vitória 3-0, Classificação com 3 jogos de sobra, 5 gols em 30 minutos). "
            "Número direto e emotivo — not saudação."
        ),
    }

    return instructions.get(channel, instructions.get("curiosidades", ""))


_FOREIGN_FIELDS = """  "idioma": {"OPCIONAL — termo da narração em INGLÊS, ESPANHOL, FRANCÊS, ALEMÃO, ITALIANO ou HOLANDÊS (nome de ferramenta, empresa, pessoa, lugar, expressão), exatamente como está escrito na narração": "código do idioma: en, es, fr, de, it ou nl — ex.: \"Claude Code\": \"en\", \"Runway\": \"en\", \"Palm Jumeirah\": \"en\", \"Hindenburg\": \"de\", \"Mont Saint-Michel\": \"fr\". A voz fala esse termo com a pronúncia nativa. Palavra que já existe em português (Havana, Londres, Nova York, internet) NUNCA entra. Sem termo assim, {}"},
  "pronuncia": {"OPCIONAL — só nome de OUTRO alfabeto/idioma (grego, russo, japonês, árabe, chinês etc.) que uma voz brasileira leria errado — inglês, espanhol, francês, alemão, italiano e holandês vão em \"idioma\", NUNCA aqui": "como se fala, escrito à portuguesa com acento na sílaba tônica — ex.: \"Pavlopetri\": \"Pavlopétri\", \"Dyatlov\": \"Diátlov\", \"Aokigahara\": \"Aokigahára\". Palavra portuguesa nunca entra. Sem nome assim, {}"},
"""
_PRONUNCIATION_FIELD = """  "pronuncia": {"OPCIONAL — só nomes estrangeiros/difíceis da narração (lugar, pessoa, espécie) que uma voz brasileira leria errado": "como se fala, escrito à portuguesa com acento na sílaba tônica — ex.: \"Pavlopetri\": \"Pavlopétri\", \"Wegener\": \"Véguener\", \"Mesosaurus\": \"Mesossáurus\". Palavra portuguesa comum nunca entra. Sem nome assim, {}"},
"""


# cena desenhada por código (src/infographic.py) — o que deixa o vídeo com
# cara de canal de mapa/história, em vez de banco de imagem genérico
_GRAFICO_FIELD = json.dumps(
    "OPCIONAL, em boa parte das cenas (metade ou mais) que falam de LUGAR real, DATA marcante, UM número forte ou comparação de 2 números. Desenhado por código (mapa NASA, relevo, ícones). Um objeto, ou lista de até 3 batidas (cada uma com 'palavra' da narração em que entra). Tipos: mapa {'lugares': [país em inglês], 'destaques': [{'pais', 'cor': vermelho|azul|dourado|verde|cinza|roxo|laranja, 'rotulo': país em português, 'icone': emoji, 'quantidade': 1-6}], 'cidade', 'ano', 'ano_rotulo', 'bandeira', 'rota': {'de','para','icone'}, 'nota' (documento, até 6 palavras), 'personagens' (só figura histórica morta antes de 1950, nunca viva ou recente)}; numero {'valor', 'rotulo' (o que mede, até 8 palavras)}; comparacao {'a','a_rotulo','b','b_rotulo','icone'} (1 a 20); ranking {'titulo','itens':[{'nome','valor','valor_rotulo'}] (2 a 6, na ordem da narração), 'destaque'} (use para 3 ou mais lugares); corte {'esquerda','direita','legenda','fluxo'} (use para COMO algo passa entre dois lugares); perfil {'titulo','niveis':[{'prof','valor','rotulo'}] (2 a 5, falados na cena)} (use para medida que muda com a profundidade); termometro {'valor','unidade','rotulo'} (temperatura falada); cartao {'titulo','linhas':[2 a 4 linhas com palavras que a cena FALA]} (use para passo, ferramenta ou problema com solução); ano {'de','ate' (opcional),'rotulo'}. Todo número ou ano do gráfico TEM que estar em algarismos na narração DESTA cena. Mesma cor = mesmo lado da história no vídeo. Aspas duplas no JSON. null quando a cena não tem lugar, data nem número.",
    ensure_ascii=False,
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
{json.dumps(facts, ensure_ascii=False, separators=(",", ":"))}
"""
        if _news_count(facts):
            facts_block += NEWS_STRUCTURE.format(n=_news_count(facts))

    web_facts_block = ""
    if web_facts:
        trechos = "\n\n".join(f"- [{f['titulo']}]({f['url']}): {f['trecho']}" for f in web_facts)
        # teto do texto de fontes no pedido: Wikipédia + Wikidata + OpenAlex passaram de 4000 tokens e o
        # Groq recusou com 413 (09/11, job 799); sem fonte de sobra, o roteiro cai em todos os provedores
        # 10/11: Nemotron (contexto grande) passou a escrever; 5.500 mantém os números do
        # corpo do artigo (53,85 km, 23,3 km, 538,4 bilhões) visíveis ao escritor
        trechos = trechos[:5500]
        web_facts_block = f"""
PESQUISA REAL NA INTERNET (o tema foi digitado por uma pessoa — use estes
trechos como base factual; NÃO invente nome, número, data ou evento que
não apareça aqui ou nos DADOS REAIS acima; se os trechos não bastarem pra
cobrir o tema com segurança, foque no que ESTÁ confirmado neles em vez de
completar com suposição). TRAVA DE FATOS: todo número, data e método da
narração e do título tem de constar LITERALMENTE nestes trechos (mesmo valor,
mesma unidade). Sem arredondar e sem completar de memória; se o número exato
não estiver nos trechos, não cite número nenhum sobre aquilo):
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

    # Instruções de abertura variada por canal (melhorias)
    opening_instruction = _get_opening_instruction(channel.name)

    return f"""{channel.prompt_base}

{current_date_rule()}

{ACCURACY_RULES}

{opening_instruction}

Tópico do vídeo: {topic}
Idioma: {channel.language}
Número de cenas: {n_scenes}
{facts_block}{web_facts_block}
Gere um JSON com exatamente este formato:
{{
  "title": "título chamativo, até 60 caracteres (título curto rende mais)",
  "thumbnail_text": "gancho CURTÍSSIMO pra thumbnail, no máximo 4 palavras, em português correto (concordância certa, nenhuma palavra em inglês — saiu \"DOR É FUEL\" e \"A ÁLCOOL VERDADE\"), tipo manchete de banca de jornal — não é o título, é a frase que faz alguém parar de rolar o feed",
{pronunciation_fields}  "thumbnail_image_prompt": "prompt em inglês pro momento MAIS marcante da história (não precisa ser a cena 1) — close-up, alto contraste, cor vibrante, um único foco. Se a história tem protagonista REAL (pessoa com nome, mesmo que o nome não esteja no prompt): NUNCA close no rosto, nem genérico ('a determined man') — o contexto entrega quem é e a IA erra o rosto; use mãos/objeto pessoal (crachá, número de peito, caneta), silhueta de costas ou símbolo/cenário. Se o personagem é fictício/genérico: rosto com emoção EXAGERADA (chocado, olhos arregalados) — o que mais gera clique. Sem pessoa: objeto/cenário de contraste forte que gere uma pergunta. SEM texto, palavras, logos, botões ou UI",
  "description": "descrição para o YouTube, 2-3 parágrafos, com contexto e call-to-action",
  "tags": ["tag1", "tag2", "..."],
  "mood": "clima geral do vídeo pra escolher a música de fundo certa — exatamente um destes valores: 'tenso' (denúncia grave, algo errado/perigoso, suspense), 'animado' (conquista, vitória, dica animadora, virada positiva), 'melancolico' (derrota, perda, resultado triste/decepcionante), 'calmo' (reflexivo, sem tensão nem euforia), 'neutro' (explicativo/factual, sem carga emocional forte definida). Escolha 1 só, o que domina o vídeo como um todo, não cena a cena.",
  "scenes": [
    {{"narration": "texto que o narrador vai falar nesta cena", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera — SEM texto, palavras, logos, botões ou UI, e SEM jogador/atleta de uniforme ou escudo de time (vira jogador falso que parece real)", "stock_query": "3 a 5 palavras em inglês pra buscar filmagem REAL — amarrada num detalhe CONCRETO desta cena (lugar, objeto, ação), nunca a categoria genérica (ruim: 'soccer stadium'; bom, se a cena fala de gol sofrido: 'goalkeeper diving missed save'). NUNCA nome de pessoa real, NUNCA escudo/camisa/uniforme/logo/bandeira de time (o banco só tem clubes estrangeiros); futebol é 'soccer', nunca 'football'. Cena abstrata/conceitual que não existe filmada: null (nunca 'symbolic shot' ou 'related to the story').", "item": "SÓ em vídeo de LISTA numerada (5 ferramentas, 7 dicas...): o número do item (1, 2, 3...) na cena que ABRE cada item, na ordem; null nas demais cenas e em vídeos que não são lista", "impact_beat": "true APENAS na cena do momento mais chocante/decisivo de todo o vídeo (o gol, o número mais absurdo, a virada) — no máximo 1 cena `true` no roteiro inteiro; todas as outras, false ou omita.", "stat_overlay": "OPCIONAL, deixe null na grande maioria das cenas — só preencha quando ESTA cena citar, no texto da narração, 2 valores comparáveis específicos vindos de DADOS REAIS (ex.: posse de bola de cada time, % de aprovação, participação em votos) — formato exato: {{\"label\": \"o que está sendo comparado\", \"a_label\": \"nome do 1º\", \"a_value\": numero, \"b_label\": \"nome do 2º\", \"b_value\": numero, \"unit\": \"% se for porcentagem, senão a unidade por extenso (pontos, gols, finalizações)\"}}. NUNCA invente ou estime os valores — só use quando estiverem literalmente nos DADOS REAIS. Nunca posição/ranking (lugar menor é melhor e a barra mostraria o contrário). Rótulos em português com acento (\"posição\", \"milhões\").", "grafico": {_GRAFICO_FIELD}{real_subject_field}}}
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
- Notícia de jogo/transmissão: diga o CANAL de TV ou streaming e o horário
  que a fonte traz (quem assiste quer saber onde ver). Rádio não interessa:
  não cite. Sem canal na fonte, conte só o dia, o horário e o local.
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
    noticias = [n for n in (facts or {}).get("noticias") or [] if isinstance(n, dict)]
    if any(not n.get("modalidade") for n in noticias):
        from src.botafogo_data import modalidade_do_texto

        if modalidade := modalidade_do_texto(title):
            # job 554: "Flamengo vence Botafogo no NBB e ..." num canal de futebol
            return f"título tirado da notícia de {modalidade}; use a manchete de futebol mais forte"
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


def _word_cap_problem(script: dict, caps: dict[str, int]) -> str | None:
    """Bordão do canal (channels/<nome>.yaml -> bordao_max) em mais frases
    que o teto: conta frases que contêm a palavra (prefixo: "imposto" pega
    "impostos")."""
    frases = [f.lower() for sc in script.get("scenes", []) for f in _SENTENCE_SPLIT.split(sc.get("narration", ""))]
    for palavra, teto in caps.items():
        n = sum(1 for f in frases if re.search(rf"\b{re.escape(palavra)}", f))
        if n > teto:
            return f"\"{palavra}\" aparece em {n} frases — no máximo {teto}"
    return None


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


# fórmula de planilha / referência de célula ditada na narração: "=SOMA($C$2:C2)"
# saiu "igual soma cifrão cê" (Aprender vIA, 30/10) — o pedido é pra IA escrever
_FORMULA = re.compile(r"=\s*[A-ZÀ-Ý]{2,}\s*\(|\$[A-Z]{1,2}\$?\d|\b[A-Z]{1,2}\d+:[A-Z]{1,2}\d+\b")


def _echo_problem(script: dict) -> str | None:
    """Trecho de 6+ palavras dito em duas frases diferentes: o roteiro do
    cubo saudita (job 696) falou "citando revisão de prioridades e custos
    inesperados" duas vezes — soa como enchimento."""
    seen: dict[tuple[str, ...], int] = {}
    frases = [f for sc in script.get("scenes", []) for f in _SENTENCE_SPLIT.split(sc.get("narration", "")) if f.strip()]
    for idx, f in enumerate(frases):
        words = re.findall(r"\w+", f.lower())
        for j in range(len(words) - 5):
            gram = tuple(words[j:j + 6])
            if seen.setdefault(gram, idx) != idx:
                return f"o trecho \"{' '.join(gram)}\" aparece em duas frases"
    return None


# fim que admite que a história não tem final (job 696: "O futuro do
# projeto permanece indefinido")
_WEAK_ENDING = re.compile(
    r"permanece(m)? (indefinid|incert|em aberto)|s[óo] o tempo dir[áa]|resta saber|futuro (é |segue |continua )?incerto"
    r"|continua(m)? a (intrigar|fascinar)|ningu[ée]m sabe (ao certo )?o que o futuro"
    r"|(permanece|segue|continua)(m)? (em|sob) (debate|discuss[ãa]o|negocia[çc])",
    re.I,
)


# promessa no fim que o vídeo não cumpre (job 702: "A resposta está no
# mesmo tratado que a privou do litoral." — e acabou)
_TEASER_ENDING = re.compile(
    r"\b(a resposta (est[áa]|pode|vem|fica)|descubra|vamos ver|voc[êe] vai (ver|descobrir)|mas isso [ée] (outra|assunto)"
    r"|s[óo] o tempo|ser[áa] que|o que vem (a seguir|depois)|fica a pergunta)", re.I)


def _teaser_sentence(f: str) -> bool:
    return f.strip().endswith("?") or bool(_TEASER_ENDING.search(f)) or bool(_WEAK_ENDING.search(f))


def trim_teaser_ending(script: dict, n_scenes: int | None = None) -> dict:
    """Corta do FIM da última cena as frases de pergunta/promessa sem
    resposta ("Mas será que...? A resposta pode estar...", job 703) — o
    conserto da revisão de fatos reescreve frases depois da checagem do
    roteiro, então isto roda no roteiro FINAL. Fica pelo menos 1 frase."""
    scenes = list(script.get("scenes") or [])
    idx = (n_scenes or len(scenes)) - 1
    if idx < 0 or idx >= len(scenes):
        return script
    frases = [f for f in _SENTENCE_SPLIT.split(scenes[idx].get("narration", "")) if f.strip()]
    cut = len(frases)
    while cut > 1 and _teaser_sentence(frases[cut - 1]):
        cut -= 1
    if cut == len(frases):
        return script
    if cut == 1 and _teaser_sentence(frases[0]) and idx > 0:
        # a cena inteira era pergunta: sai, o fim vira a cena anterior
        log.warning("cena final só de pergunta/promessa, removida: %r", scenes[idx].get("narration", "")[:80])
        return {**script, "scenes": scenes[:idx] + scenes[idx + 1:]}
    log.warning("final sem resposta cortado: %r", " ".join(frases[cut:])[:100])
    scenes[idx] = {**scenes[idx], "narration": " ".join(frases[:cut])}
    return {**script, "scenes": scenes}


def _ending_problem(script: dict) -> str | None:
    scenes = script.get("scenes") or []
    if not scenes:
        return None
    last = [f for f in _SENTENCE_SPLIT.split(scenes[-1].get("narration", "")) if f.strip()][-2:]
    for f in last:
        if _WEAK_ENDING.search(f):
            return f"final sem desfecho (\"{f.strip()[:70]}\")"
    if last and _teaser_sentence(last[-1]):
        return f"final que pergunta/promete e não responde (\"{last[-1].strip()[:70]}\")"
    return None


def _formula_problem(script: dict) -> str | None:
    for sc in script.get("scenes", []):
        if m := _FORMULA.search(sc.get("narration", "")):
            return f"fórmula ditada na narração ({m[0]}...)"
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


def _script_max_tokens(total_max_words: int, n_scenes: int) -> int:
    """Orçamento de saída do roteiro: ~1,5 token por palavra em pt-BR, mais o JSON de
    cada cena (narração, image_prompt, stock_query, gráfico e campos extras, cerca de
    200 tokens por cena), mais folga de 40%. Em 10/11 a conta usava 60 tokens por cena e
    o Nemotron cortou o JSON de 12 cenas em 9.171 caracteres (2.443 tokens)."""
    return min(int((total_max_words * 1.5 + n_scenes * 220 + 500) * 1.4), 16000)


def _is_parseable_script(raw: str, required: set[str]) -> bool:
    """Validador passado pra `complete()` — rejeita na hora qualquer
    resposta que não vira roteiro utilizável, pra cascata de provedores já
    pular pro próximo modelo/chave em vez de aceitar garbage (visto na
    prática: um modelo grátis instável respondia só `{` repetido sem nunca
    fechar o JSON, e as 3 tentativas de retry caíam nele de novo porque a
    cascata sempre parava no primeiro "não vazio")."""
    try:
        script = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("roteiro recusado: não é JSON (%s); %d caracteres; início %r; fim %r",
                    type(exc).__name__, len(raw), raw[:120], raw[-120:])
        return False
    missing = required - script.keys()
    if missing:
        log.warning("roteiro recusado: faltam campos %s; campos presentes %s",
                    sorted(missing), sorted(script.keys()))
        return False
    return True


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


# Escudo/brasão de clube: a IA de imagem inventa um escudo que não é o do clube
# (botafogo, 07/11). Quando o prompt pede escudo, a cena vira só uma estrela
# branca de cinco pontas num fundo preto — nunca um escudo inventado.
_CREST_RE = re.compile(
    r"\b(escudo|brasão|brasao|emblema do clube|escudo do clube|crest|badge|shield|coat of arms|club emblem|team emblem|club crest|team crest|club logo|team logo|logo do clube|logo do time)\b",
    re.IGNORECASE,
)
_CREST_SAFE_PROMPT = (
    "plain solid black background, a single white five-pointed star centered, "
    "minimalist, no text, no logos, no emblem, no shield"
)


def _sanitize_club_crests(script: dict) -> dict:
    """Troca o prompt de imagem que pede escudo/brasão de clube pela estrela
    branca em fundo preto (regra do canal botafogo, 07/11). Vale pra cena e
    pro prompt da thumbnail; a busca de estoque dessa cena é desligada pra não
    trazer um escudo de banco de imagens."""
    for i, scene in enumerate(script.get("scenes", [])):
        prompt = scene.get("image_prompt", "") or ""
        if _CREST_RE.search(prompt):
            log.warning("cena %d: image_prompt pede escudo de clube, trocando pela estrela: %s", i, prompt[:120])
            scene["image_prompt"] = _CREST_SAFE_PROMPT
            scene["stock_query"] = ""
    thumb = script.get("thumbnail_image_prompt") or ""
    if _CREST_RE.search(thumb):
        log.warning("thumbnail pede escudo de clube, trocando pela estrela")
        script["thumbnail_image_prompt"] = _CREST_SAFE_PROMPT
    return script


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


def recent_mistakes(channel_name: str, jobs: int = 8, max_items: int = 6) -> str:
    """Aprende com o que já foi reprovado: os motivos das últimas revisões
    (fatos e vídeo) deste canal, guardados no erro do job, entram no prompt
    pro LLM não repetir o mesmo tipo de erro (botafogo 09/10: 'jogo em casa'
    sendo fora, 'zona de rebaixamento' sem fonte, jobs 350-353)."""
    try:
        with closing(connect_db(QUEUE_DB)) as conn:
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


def _story_scenes(entry: dict) -> list[dict]:
    """Cenas de um Short guardado: as do arquivo (com a busca de imagem) ou,
    pros guardados antes de 05/11 (só narração), a narração em blocos de 2
    frases, com a busca de imagem a preencher."""
    if entry.get("scenes"):
        return [{**sc, "_arquivo": True} for sc in entry["scenes"]]
    sentences = [x for x in _SENTENCE_SPLIT.split(entry.get("narration", "").strip()) if x]
    return [{"narration": " ".join(sentences[i:i + 2]), "_arquivo": True} for i in range(0, len(sentences), 2)]


def compose_compilation(channel: ChannelConfig, facts: dict, revision_feedback: str | None = None) -> dict:
    """Longo semanal montado com a narração dos Shorts EXATAMENTE como foi
    publicada (já passou na revisão); o LLM escreve só abertura, transições,
    fechamento, título e a busca de imagem das cenas sem uma. Pedir pro LLM
    reescrever as 6 histórias fazia a revisão de fatos reprovar o que estava
    no próprio Short e o conserto em laço tirava nomes e travava o texto
    ("bólido rival", "pretensões francesas", job 739; "força de vontade" 9x,
    job 741). Cenas do arquivo levam "_arquivo" (ver fact_check.review_script)."""
    from .script_archive import archived
    from .spellcheck import portuguese_words
    from .topics import _SMALL_MODEL_RE  # import tardio: topics importa este módulo

    stories = []
    for item in facts.get("dados") or []:
        entry = archived(channel.name, item.get("job_id")) if item.get("job_id") else None
        entry = entry or {"title": item.get("titulo", ""), "narration": item.get("narracao", "")}
        scenes = _story_scenes(entry)
        if scenes:
            stories.append({"title": entry.get("title", ""), "scenes": scenes})
    if len(stories) < 2:
        raise ValueError("compilação sem histórias guardadas suficientes")
    everything = " ".join(sc["narration"] for st in stories for sc in st["scenes"])
    allowed = _fact_tokens(everything, every_capital=True)
    missing = [(i, j) for i, st in enumerate(stories) for j, sc in enumerate(st["scenes"]) if not sc.get("image_prompt")]
    blocks = "\n\n".join(
        f"HISTÓRIA {i + 1} — {st['title']}\n" + "\n".join(
            (f"[{i + 1}.{j + 1}] " if not sc.get("image_prompt") else "") + sc["narration"]
            for j, sc in enumerate(st["scenes"]))
        for i, st in enumerate(stories)
    )
    n = len(stories)
    visual = ('{"narration": "...", "image_prompt": "prompt em inglês, só cenário/objetos/atmosfera, sem texto, '
              'sem rosto de pessoa real", "stock_query": "3 a 5 palavras em inglês pra filmagem real"}')
    prompt = (
        f"{channel.prompt_base}\n\n"
        f"Vídeo LONGO que junta {n} Shorts da semana do canal. A narração de cada história JÁ ESTÁ PRONTA e entra "
        "como está — você NÃO reescreve nada dela. Escreva só:\n"
        "- abertura: 1 ou 2 frases que prometem as histórias sem contar o final;\n"
        f"- transicoes: exatamente {n} frases, UMA para cada história, na ordem; cada uma apresenta a história "
        "que vem (quem é, o assunto em uma frase, por que importa) e é o início dela. NÃO escreva o número "
        "da história: o código põe \"História N de {n}\" antes de cada uma;\n"
        f"- perguntas: exatamente {n}, UMA por história, na ordem. Cada uma fecha a história em uma frase "
        "(o desfecho ou a lição, sem fato novo) e termina com uma pergunta direta ao público sobre "
        "aquela história (ex.: \"Você acha justo?\", \"Qual dessas te surpreendeu mais?\"), que convide a comentar. "
        "A última palavra de cada uma é a pergunta;\n"
        "- fechamento: 1 ou 2 frases com a lição comum das histórias, sem pergunta.\n"
        "Nesses textos novos, NENHUM fato, número, data ou nome que não esteja nas narrações abaixo; sem "
        "pergunta que fica sem resposta, sem pedir inscrição.\n"
        "Título (até 70 caracteres) e thumbnail (até 4 palavras): o fio comum das histórias com o número "
        f"delas (use o assunto REAL das histórias, sem exemplo pronto: o título tem que bater com elas), nunca uma história só e nunca "
        "\"compilação\".\n"
        + (f"Para cada trecho marcado [h.c], dê a busca de imagem em \"visuais\".\n" if missing else "")
        + (revision_feedback or "")
        + f"\n\n{blocks}\n\n"
        "Responda só com JSON:\n"
        f'{{"title": "...", "thumbnail_text": "...", "thumbnail_image_prompt": "prompt em inglês, sem rosto", '
        f'"description": "2 parágrafos", "tags": ["..."], "mood": "animado|calmo|neutro|tenso|melancolico", '
        f'"abertura": {visual}, "transicoes": [{visual}, ...], "perguntas": [{visual}, ...], "fechamento": {visual}'
        + (', "visuais": {"1.1": {"image_prompt": "...", "stock_query": "..."}, ...}' if missing else "")
        + "}"
    )

    def parse(raw: str) -> dict | None:
        try:
            data = _extract_json(raw)
        except ValueError:
            return None
        parts = [data.get("abertura"), *(data.get("transicoes") or []), *(data.get("perguntas") or []), data.get("fechamento")]
        if len(data.get("transicoes") or []) != n or len(data.get("perguntas") or []) != n or not all(
                isinstance(p, dict) and str(p.get("narration") or "").strip() and p.get("image_prompt") for p in parts):
            return None
        # cada história fecha com pergunta ao público (termina com "?")
        if any(not str(p.get("narration", "")).strip().endswith("?") for p in data.get("perguntas") or []):
            return None
        new_text = " ".join(str(p["narration"]) for p in parts)
        if _fact_tokens(new_text) - allowed:
            return None
        title, thumb = str(data.get("title") or ""), str(data.get("thumbnail_text") or "")
        if not (8 <= len(title) <= 60 and 0 < len(thumb.split()) <= 5):
            return None
        # título leva o número de histórias; palavra em Title Case ("Histórias
        # Reais") só reprova se não for palavra comum do português
        odd = _fact_tokens(f"{title} {thumb}") - allowed - {str(n)}
        if odd and (any(t.isdigit() for t in odd) or odd - portuguese_words(sorted(odd))):
            return None
        vis = data.get("visuais") or {}
        if any(not isinstance(vis.get(f"{i + 1}.{j + 1}"), dict) or not vis[f"{i + 1}.{j + 1}"].get("image_prompt")
               for i, j in missing):
            return None
        return data

    raw = complete(
        [{"role": "user", "content": prompt}], max_tokens=600 + 80 * len(missing) + 120 * n,
        validate=lambda r: parse(r) is not None, model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        temperature=0.6,
    )
    data = parse(raw)
    for i, j in missing:
        v = data["visuais"][f"{i + 1}.{j + 1}"]
        stories[i]["scenes"][j].update({k: v[k] for k in ("image_prompt", "stock_query") if v.get(k)})

    def new_scene(p: dict) -> dict:
        return {k: str(p[k]).strip() for k in ("narration", "image_prompt", "stock_query") if p.get(k)}

    # cada história: "História N de n" (código, não LLM), o início, as cenas
    # (narração intacta), o desfecho e a pergunta ao público
    scenes = [new_scene(data["abertura"])]
    for i, st in enumerate(stories):
        intro = new_scene(data["transicoes"][i])
        intro["narration"] = f"História {i + 1} de {n}. " + intro["narration"]
        scenes.append(intro)
        scenes.extend(st["scenes"])
        scenes.append(new_scene(data["perguntas"][i]))
    scenes.append(new_scene(data["fechamento"]))
    log.info("compilação montada: %d histórias guardadas, %d cenas (%d novas)", n, len(scenes), n + 1)
    return {
        "title": data["title"].strip(), "thumbnail_text": data["thumbnail_text"].strip(),
        "thumbnail_image_prompt": data.get("thumbnail_image_prompt") or scenes[1].get("image_prompt", ""),
        "description": data.get("description") or "", "tags": data.get("tags") or [],
        "mood": data.get("mood") if data.get("mood") in {"animado", "calmo", "neutro", "tenso", "melancolico"} else "animado",
        "scenes": scenes,
    }


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
    if facts and facts.get("tipo") == "compilacao":
        try:
            return compose_compilation(channel, facts, revision_feedback)
        except RuntimeError as exc:
            raise ValueError(f"compilação: {exc}") from exc
    # etapa 1: o plano (estrutura e fatos escolhidos) antes de escrever a narração
    plan = plan_story(topic, facts, web_facts)
    writer_sources = web_facts
    if web_facts and channel.name in FATOS_CANAIS:
        # 10/11: o escritor não lê o texto bruto das fontes (misturava números e completava de
        # memória, job 824); recebe só a lista de fatos literais extraída e checada
        fatos = extract_verified_facts(topic, web_facts)
        if not fatos:
            raise ValueError("fatos verificados indisponíveis (extração das fontes falhou)")
        writer_sources = [{"titulo": "FATOS VERIFICADOS (única base; cada cena usa um destes)", "url": "",
                           "trecho": "\n".join(f"F{i + 1}: {f}" for i, f in enumerate(fatos))}]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (plan + "\n\n" if plan else "")
         + _prompt_for(channel, topic, facts, scenes, min_minutes, max_minutes, writer_sources)
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
    max_tokens = _script_max_tokens(total_max_words, n_scenes)
    last_error: Exception | None = None
    best_script: dict | None = None
    best_distance = float("inf")
    filler_fallback: dict | None = None
    for attempt in range(3):
        # 09/11: o plano e as outras chamadas gastaram a janela de 60 s do Groq; espera ela fechar
        # antes do roteiro, em vez de queimar as 3 tentativas em 429
        from .providers import _LAST_GROQ_CALL
        since = time.monotonic() - _LAST_GROQ_CALL["at"]
        if _LAST_GROQ_CALL["at"] and since < 62:
            log.info("esperando %.0f s a janela de 60 s do Groq antes do roteiro", 62 - since)
            time.sleep(62 - since)
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
            # TODOS os defeitos de estilo de uma vez: antes só o 1º ia pra
            # tentativa seguinte, que podia voltar com o 2º (3 defeitos
            # esgotavam as 3 tentativas)
            issues: list[tuple[str, str]] = []  # (pro log, pro pedido de correção)
            news_problem = _news_order_problem(script, _news_count(facts)) if _news_count(facts) > 1 else None
            news_problem = news_problem or (_news_title_problem(script, facts) if _news_count(facts) > 1 else None)
            if news_problem:
                issues.append((f"notícias misturadas ({news_problem})",
                               f"errou: {news_problem}. Uma notícia por bloco, na ordem da lista, cada cena com o campo "
                               "\"noticia\"; título = a manchete mais forte, curta, sem \"notícias\"/\"hoje\" (em dia de "
                               "jogo, \"Botafogo x Adversário\" + gancho curto)."))
            repeated = _repetition_problem(script) or _word_cap_problem(script, channel.word_caps)
            if repeated:
                issues.append((f"repetição ({repeated})", f"se repetiu: {repeated}. Cada dado é dito uma vez (no máximo "
                               "citado de novo numa comparação); sem frase-muleta."))
            echo = _echo_problem(script)
            if echo:
                issues.append((echo, f"repetiu: {echo}. Cada frase traz um fato novo que leva à próxima; nada dito duas vezes."))
            ending = _ending_problem(script)
            if ending:
                issues.append((ending, f"terminou com {ending}. A última frase retoma o gancho do começo com uma virada "
                               "ou ironia — um fato, não uma dúvida. Ex.: 'Em 1948 o Norte cortou a luz do Sul. Agora não "
                               "consegue manter as próprias acesas.' Pergunta, 'será que', 'a resposta está' no fim: proibido."))
            formula = _formula_problem(script)
            if formula:
                issues.append((formula, f"tinha {formula}. A voz não lê fórmula: em vez de ditar, mostre o pedido pra IA "
                               "escrever a fórmula (ex.: \"Peça: Escreva a fórmula que soma a coluna de valores\") e onde "
                               "colar a resposta."))
            filler = _filler_phrase(script, topic)
            if filler:
                # bordão em toda cena ("isso mostra a importância", job 239)
                issues.append((f"bordão repetido ({filler!r})",
                               f"usou a expressão \"{filler}\" — NÃO use, nem frases de enchimento sem fato novo."))
            overused = _overused_word(script, channel.channel_title)
            if overused:
                word = overused.split()[0]
                issues.append((f"palavra repetida demais ({overused})", f"repetiu demais a palavra \"{word}\": varie "
                               "com pronome/sinônimo, e não repita a mesma palavra frase após frase."))
            if issues:
                # defeito de estilo, não de fato: guarda o roteiro — melhor
                # publicá-lo do que perder o dia (as revisões de fato seguem)
                filler_fallback = script
                last_error = ValueError("; ".join(i[0] for i in issues))
                log.warning("tentativa %d/3: %d defeito(s) — %s; tentando de novo", attempt + 1, len(issues),
                            "; ".join(i[0] for i in issues))
                fix = "\n".join(f"- O roteiro anterior {i[1]}" for i in issues)
                messages = [messages[0], {"role": "user", "content": messages[1]["content"]
                                          + f"\n\nCorrija TODOS estes defeitos do roteiro anterior de uma vez:\n{fix}"}]
                continue
            word_count = sum(len(s.get("narration", "").split()) for s in script["scenes"])
            if min_total_words <= word_count <= max_words_ceiling:
                return _sanitize_club_crests(_sanitize_person_images(topic, script))

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
            if "Nenhum provedor" in str(exc) and attempt < 2:
                # 09/11: as 3 tentativas caíam em 2 s, dentro do descanso de 75 s das chaves (429).
                # Espera o descanso acabar antes de tentar de novo.
                log.info("esperando 80 s o descanso das chaves antes de tentar de novo")
                time.sleep(80)

    if best_script is not None:
        log.warning(
            "usando o melhor roteiro obtido mesmo fora da faixa de %d-%d palavras",
            min_total_words, max_words_ceiling,
        )
        return _sanitize_club_crests(_sanitize_person_images(topic, best_script))
    if filler_fallback is not None:
        log.warning("3 tentativas com bordão repetido — usando a última mesmo assim")
        return _sanitize_club_crests(_sanitize_person_images(topic, filler_fallback))

    raise ValueError(f"LLM não devolveu roteiro válido após 3 tentativas: {last_error}")


_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def _repair_parse(raw: str, n: int) -> dict | None:
    try:
        data = _extract_json(raw)
    except ValueError:
        return None
    cenas = data.get("cenas")
    if not isinstance(cenas, list) or len(cenas) != n or not all(isinstance(c, str) for c in cenas):
        return None
    return data


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PROPER_RE = re.compile(r"(?<![.!?:]\s)(?<!^)\b[A-ZÀ-Ý][a-zà-ÿ]{3,}")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


_CAPITAL_RE = re.compile(r"\b[A-ZÀ-Ý][a-zà-ÿ]{3,}")


def _fact_tokens(text: str, every_capital: bool = False) -> set[str]:
    """Números e nomes próprios do texto — o que conta como "fato".
    every_capital: conta também palavra maiúscula em começo de frase (pro
    lado do que é PERMITIDO, onde sobrar é melhor que faltar)."""
    nums = {n.replace(".", "").replace(",", "") for n in _NUMBER_RE.findall(text)}
    names = {w.lower() for w in (_CAPITAL_RE if every_capital else _PROPER_RE).findall(text)}
    return nums | names


def _drop_new_facts(scenes: list[str], allowed: set[str]) -> tuple[list[str], set[str]]:
    """Apaga do conserto a frase que traz número ou nome próprio que não
    estava no roteiro, nas fontes nem no tema: o conserto só pode tirar ou
    generalizar, nunca inventar — senão o laço conserta/revisa não acaba."""
    novos: set[str] = set()
    out = []
    for text in scenes:
        kept = []
        for sentence in _SENTENCE_RE.split(text):
            extra = _fact_tokens(sentence) - allowed
            if extra:
                novos |= extra
            else:
                kept.append(sentence)
        out.append(" ".join(kept).strip())
    return out, novos


def repair_script(
    script: dict, problems: list[dict], facts: dict | None = None, web_facts: list[dict] | None = None,
    topic: str = "",
) -> dict | None:
    """Conserta SÓ as frases que a revisão de fatos apontou, com base nas
    fontes, e mantém o resto do roteiro igual. Reescrever do zero trocava um
    detalhe sem fonte por outros (curiosidades 555-557, 21/10); cortar
    deixava buraco na história. None se o modelo não devolver o formato."""
    narrations = [str(sc.get("narration", "")) for sc in script["scenes"]]
    if facts or web_facts:
        sources = json.dumps(facts, ensure_ascii=False, separators=(",", ":")) if facts else ""
        sources += "\n".join(f"- {f['titulo']}: {f['trecho']}" for f in web_facts or [])
    else:
        sources = "(sem fontes: só conhecimento geral verdadeiro e amplamente conhecido)"
    apontados = "\n".join(f"- {p.get('termo', '')}: {p.get('motivo', '')}" for p in problems)
    prompt = f"""Um roteiro de vídeo foi reprovado na revisão de fatos. Conserte SÓ os
pontos apontados, usando as FONTES. Para cada ponto:
- "fora_da_fonte" ou "fato_nao_confirmado": APAGUE o detalhe não comprovado
  (a frase, ou só o trecho com o número, nome ou causa sem fonte). Não reformule
  o detalhe e não o troque por outro. Se a frase ficar sem sentido, apague a frase.
- "titulo_nao_confere": o título tem de dizer exatamente o número e o fato que a
  narração diz (mesmo valor, mesma unidade).
Não acrescente número, medida, data, nome ou detalhe que não esteja nas
FONTES. Ao generalizar, não deixe frase vaga que não diz nada ("Ele
participou das partidas", "teve papel importante") — apague a frase. Não mexa no que não foi apontado: mesmo tom, mesmo tamanho, mesmo
número de cenas. Se o TÍTULO foi apontado, devolva um título novo, alinhado à narração.

TÍTULO: {script.get("title", "")}

PONTOS APONTADOS:
{apontados}

FONTES:
{sources}

CENAS (narração, na ordem):
{json.dumps(narrations, ensure_ascii=False)}

Responda só com JSON: {{"titulo": "título (o mesmo, se não foi apontado)", "cenas": ["narração da cena 1", "..."]}}
— exatamente {len(narrations)} cenas, na mesma ordem."""
    from .topics import _SMALL_MODEL_RE  # import tardio: topics importa este módulo

    try:
        raw = complete(
            # temperatura 0.3 liga o raciocínio do Nemotron, que come o orçamento (+4096):
            # conserto de 12 cenas saía cortado em 7.889 caracteres (job 819)
            [{"role": "user", "content": prompt}], max_tokens=12000,
            validate=lambda r: _repair_parse(r, len(narrations)) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m), temperature=0.3,
        )
    except RuntimeError as exc:
        log.warning("conserto do roteiro: nenhum provedor respondeu (%s)", exc)
        return None
    data = _repair_parse(raw, len(narrations))
    allowed = _fact_tokens(
        " ".join(narrations) + " " + sources + " " + topic + " " + str(script.get("title", "")), every_capital=True,
    )
    cenas, novos = _drop_new_facts(data["cenas"], allowed)
    if novos:
        log.warning("conserto tentou acrescentar fato novo (%s) — frase apagada", ", ".join(sorted(novos)))
    scenes = [{**sc, "narration": text} for sc, text in zip(script["scenes"], cenas) if text]
    if len(scenes) < 2:
        return None
    title = str(data.get("titulo") or "").strip()
    if not title or _fact_tokens(title) - allowed:
        title = script.get("title")
    return {**script, "title": title, "scenes": scenes}


def refresh_description(script: dict) -> str:
    """Descrição refeita só com o que as cenas narram — depois de conserto
    ou corte, a gerada junto com o roteiro prometia o que saiu (job 558,
    21/10: "a política de felicidade, a constituição verde e os números"
    num vídeo cortado pra 3 cenas). Sem IA, as 2 primeiras frases."""
    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", []))
    prompt = (
        "Escreva a descrição de um vídeo do YouTube em português, 2 a 3 frases, SÓ com o que esta narração "
        "conta — nada que ela não diga, sem hashtags, sem pedir like.\n\n"
        f"TÍTULO: {script.get('title', '')}\nNARRAÇÃO: {narration}\n\nResponda só com a descrição."
    )
    try:
        text = complete([{"role": "user", "content": prompt}], max_tokens=400, temperature=0.3,
                        validate=lambda r: 40 <= len(r.strip()) <= 600)
        allowed = _fact_tokens(narration + " " + str(script.get("title", "")), every_capital=True)
        if not _fact_tokens(text) - allowed:
            return text.strip()
        log.warning("descrição nova trouxe fato fora da narração — usando o começo da narração")
    except RuntimeError as exc:
        log.warning("descrição: nenhum provedor respondeu (%s)", exc)
    return " ".join(_SENTENCE_RE.split(narration)[:2]).strip()


_TITLE_RULE_RE = re.compile(r"T[IÍ]TULO:(.*?)(?:\n\s*\n|$)", re.DOTALL)


def _packaging_ok(data: dict, allowed: set[str]) -> bool:
    title, thumb = str(data.get("titulo") or "").strip(), str(data.get("thumbnail_text") or "").strip()
    return (
        8 <= len(title) <= 60 and 0 < len(thumb.split()) <= 5
        and not (_fact_tokens(title) | _fact_tokens(thumb)) - allowed
    )


def refresh_packaging(script: dict, channel: ChannelConfig, topic: str = "", extra_rule: str = "") -> dict:
    """Depois de conserto ou corte (ver run_pipeline._repair_loop): descrição
    refeita do que as cenas narram e, se o título ou o texto da thumbnail
    citam número/nome que saiu da narração, os dois refeitos com a regra de
    TÍTULO do canal — título e thumb são o que faz o vídeo ser clicado, e
    prometer o que foi cortado engana quem clicou."""
    script = {**script, "description": refresh_description(script)}
    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", []))
    allowed = _fact_tokens(narration + " " + topic, every_capital=True)
    stale = [k for k in ("title", "thumbnail_text") if _fact_tokens(str(script.get(k) or "")) - allowed]
    if not stale:
        return script
    rule = _TITLE_RULE_RE.search(channel.prompt_base or "")
    prompt = (
        "Crie o TÍTULO e o texto da THUMBNAIL de um vídeo do YouTube, em português. São o que faz a "
        "pessoa clicar: título curto (até 55 caracteres) com o fato ou número mais forte, em português "
        "correto e com palavras comuns (ex.: \"Catar: estádio gelado no meio do deserto\"); thumbnail com "
        "no máximo 4 palavras, tipo manchete de banca, diferente do título (ex.: \"AR FRIO NO DESERTO\"). "
        "Use SÓ fatos, números e nomes que estão na NARRAÇÃO.\n"
        + (f"Regra de título do canal:{rule.group(1).strip()}\n" if rule else "")
        + (f"Regra deste vídeo (vale mais que a do canal): {extra_rule}\n" if extra_rule else "")
        + f"\nTEMA: {topic}\nTÍTULO ANTERIOR (citava algo que saiu do vídeo): {script.get('title', '')}\n"
        f"NARRAÇÃO: {narration}\n\n"
        'Responda só com JSON: {"titulo": "...", "thumbnail_text": "..."}'
    )
    from .topics import _SMALL_MODEL_RE  # import tardio: topics importa este módulo

    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=300, temperature=0.5,
            validate=lambda r: _repair_parse_packaging(r, allowed) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
        data = _repair_parse_packaging(raw, allowed)
        log.warning("título/thumb citavam o que saiu do vídeo (%s) — refeitos: %r / %r",
                    ", ".join(stale), data["titulo"], data["thumbnail_text"])
        return {**script, "title": data["titulo"].strip(), "thumbnail_text": data["thumbnail_text"].strip()}
    except RuntimeError as exc:
        # troca só o que cita o que saiu: título vira o tema, thumb sem texto
        log.warning("título/thumb: nenhum provedor respondeu (%s) — trocando só %s", exc, ", ".join(stale))
        fallback = {"title": (topic or script.get("title", ""))[:60], "thumbnail_text": None}
        return {**script, **{k: fallback[k] for k in stale}}


def _repair_parse_packaging(raw: str, allowed: set[str]) -> dict | None:
    try:
        data = _extract_json(raw)
    except ValueError:
        return None
    return data if _packaging_ok(data, allowed) else None


def _close_repeats(script: dict, ignore: str = "") -> list[str]:
    """Palavra de conteúdo repetida na MESMA frase ou na frase seguinte
    ("participou das partidas... papel nas partidas", "decidiu seu futuro,
    ele decidiu seu futuro" — Ronaldo, 22/10). Nome próprio e termo do
    tema/título não contam. Devolve as palavras, na ordem."""
    import unicodedata

    def fold(w: str) -> str:
        return unicodedata.normalize("NFKD", w.lower()).encode("ascii", "ignore").decode()

    skip = {fold(w) for w in re.findall(r"\w+", ignore)}
    found: list[str] = []
    prev: set[str] = set()
    for sc in script.get("scenes", []):
        for sentence in _SENTENCE_SPLIT.split(sc.get("narration", "")):
            cur: set[str] = set()
            for w in re.findall(r"\w+", sentence):
                lw = w.lower()
                if (len(lw) < 5 or w[:1].isupper() or lw in _STOPWORDS or lw in _NUMBER_WORDS
                        or lw.isdigit() or fold(lw) in skip):
                    continue
                stem = fold(lw)[:6]  # "partidas"/"partida", "recuperação"/"recuperar"
                if stem in cur or stem in prev:
                    found.append(lw)
                cur.add(stem)
            prev = cur
    return found


MAX_CLOSE_REPEATS = 2


def vary_close_repeats(script: dict, topic: str = "") -> dict:
    """Roteiro com palavra repetida em frases vizinhas (comum depois do
    conserto da revisão de fatos, que generaliza: "participou das
    partidas", "papel importante nas partidas") passa por UMA reescrita
    leve: troca a palavra repetida por sinônimo/pronome ou apaga a frase
    vaga. Mesma trava do conserto (_drop_new_facts): nenhum número ou nome
    novo. Sem LLM, segue como está."""
    repeats = _close_repeats(script, topic + " " + str(script.get("title", "")))
    if len(repeats) <= MAX_CLOSE_REPEATS:
        return script
    narrations = [str(sc.get("narration", "")) for sc in script["scenes"]]
    prompt = f"""Este roteiro de vídeo em português repete palavras em frases vizinhas
e soa robótico ao ser narrado. Palavras repetidas: {", ".join(dict.fromkeys(repeats))}.
Reescreva SÓ as frases com repetição: troque a palavra repetida por sinônimo,
pronome ou outra construção, junte duas frases curtas, ou apague a frase vaga
que não acrescenta nada (ex.: "Ele participou das partidas do torneio").
Não acrescente número, data, nome ou fato; não mude o sentido; mantenha o resto
igual, o mesmo tom e o mesmo número de cenas.

CENAS (narração, na ordem):
{json.dumps(narrations, ensure_ascii=False)}

Responda só com JSON: {{"cenas": ["narração da cena 1", "..."]}} — exatamente {len(narrations)} cenas."""
    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=12000, temperature=0.3,
            validate=lambda r: _repair_parse(r, len(narrations)) is not None,
        )
    except RuntimeError as exc:
        log.warning("variação de palavras repetidas: nenhum provedor respondeu (%s) — segue sem", exc)
        return script
    allowed = _fact_tokens(" ".join(narrations) + " " + topic + " " + str(script.get("title", "")), every_capital=True)
    cenas, novos = _drop_new_facts(_repair_parse(raw, len(narrations))["cenas"], allowed)
    if novos:
        log.warning("variação tentou acrescentar fato novo (%s) — frase apagada", ", ".join(sorted(novos)))
    scenes = [{**sc, "narration": text} for sc, text in zip(script["scenes"], cenas) if text.strip()]
    if len(scenes) < max(2, len(narrations) - 1):
        return script
    new = {**script, "scenes": scenes}
    log.info("palavras repetidas em frases vizinhas: %d -> %d (%s)", len(repeats),
             len(_close_repeats(new, topic + " " + str(script.get("title", "")))), ", ".join(dict.fromkeys(repeats)))
    return new


# posição/ranking: lugar menor é MELHOR, a barra maior mostrava o pior
# ("2024 57 posicao" x "2025 158 posicao", vídeo do Nikolas, 22/10)
_RANKING = re.compile(r"posi[cç][aã]o|coloca[cç][aã]o|ranking|\blugar|\d+\s*[ºª°]", re.I)
_NUM_FALADO = re.compile(r"(\d+(?:\.\d{3})*(?:,\d+)?)\s*(milh(?:ão|ões)|bilh(?:ão|ões)|mil\b)?", re.I)
_ESCALA = {"mil": 1e3, "milhão": 1e6, "milhões": 1e6, "bilhão": 1e9, "bilhões": 1e9}


def _spoken_numbers(text: str) -> list[float]:
    nums = []
    for m in _NUM_FALADO.finditer(text):
        n = float(m[1].replace(".", "").replace(",", "."))
        nums += [n, n * _ESCALA.get((m[2] or "").lower(), 1)]
    return nums


def clean_stat_overlays(script: dict) -> dict:
    """Barra comparativa só com o que a cena FALA: os 2 valores têm de
    estar na narração da cena (o cartão do Nikolas, 22/10, mostrou Eduardo
    Bolsonaro 1,8 milhão sem a narração citar), nada de ranking, e rótulo
    com acento. Fora disso a cena sai sem barra — nunca inventada."""
    from .spellcheck import restore_accents

    for scene in script.get("scenes") or []:
        ov = scene.get("stat_overlay")
        if not ov:
            continue
        try:
            values = [float(ov["a_value"]), float(ov["b_value"])]
            textos = [str(ov.get(k) or "") for k in ("label", "a_label", "b_label", "unit")]
        except (KeyError, TypeError, ValueError):
            scene["stat_overlay"] = None
            continue
        spoken = _spoken_numbers(scene.get("narration", ""))

        def dito(v: float) -> bool:
            return any(abs(n - v * k) <= 0.02 * max(abs(v * k), 1) for n in spoken for k in (1, 1e3, 1e6, 1e9))

        motivo = (
            "ranking/posição" if any(_RANKING.search(t) for t in textos)
            else "valor que a cena não fala" if not all(dito(v) for v in values) else None
        )
        if motivo:
            log.warning("barra comparativa tirada (%s): %s", motivo, ov)
            scene["stat_overlay"] = None
            continue
        label, a_label, b_label, unit = restore_accents(textos)
        scene["stat_overlay"] = {**ov, "label": label, "a_label": a_label, "b_label": b_label, "unit": unit}
    return script


def fix_dangling_hooks(script: dict) -> dict:
    """Pergunta de fim de cena que a cena seguinte não responde (job 363:
    "E quem vem do outro lado?" seguida do prazo da recuperação judicial)
    sai do roteiro. Um LLM só aponta quais estão soltas; o corte é por
    código e só tira a pergunta — nunca acrescenta fato. Sem LLM, segue."""
    scenes = script.get("scenes") or []
    pairs: list[tuple[int, str, str]] = []
    # a pergunta da 1ª cena é o GANCHO do vídeo, respondida ao longo dele —
    # não na frase seguinte (job 704 perdeu a abertura "Como a Bolívia...
    # acabou ficando sem acesso ao oceano?")
    for i in range(1, len(scenes) - 1):
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


# ---------------------------------------------------------------- diretor de arte

ART_PROMPT = """Você é o DIRETOR DE ARTE de um Short do canal "{canal}" ({nicho}). O roteiro abaixo já foi
aprovado — NÃO mude nenhuma palavra. Sua única tarefa: escolher o gráfico desenhado de cada cena, no estilo
dos canais de mapa/história que viralizam (satélite da NASA, país em relevo de papel ou pintado com a
bandeira, rota tracejada, bilhete de documento, personagem caricato, número gigante, ano com régua).

REGRAS:
- O vídeo MESCLA desenho com foto/vídeo real: dê gráfico a cerca de METADE das cenas — as que foto não
  explica (lugar no mapa, viagem/rota, guerra, tratado/documento, data, número, comparação). Cena com
  assunto concreto e fotografável (uma pessoa, um prédio, um animal, um objeto, uma paisagem) fica com
  null, pra entrar a foto/vídeo real. Nunca mais de 3 cenas desenhadas seguidas.
- VARIE de uma cena pra outra: não repita o mesmo mapa igual; alterne mapa, rota, número, ano, bilhete.
- País que é PERSONAGEM da história (França x Inglaterra, Chile x Bolívia): "bandeira": true.
- Movimento (fuga, invasão, viagem, comércio, exportação): "rota" de/para com ícone.
- Tratado, carta, lei, decreto, contrato citado: "nota" curta EM PORTUGUÊS com o nome e a data que a cena
  diz ("Acordo secreto · out/1807"). Rótulos e notas sempre em português; só nomes de país/cidade/pessoa
  nos campos de busca ("pais", "lugares", "de", "para", "nome") vão em inglês.
- Figura HISTÓRICA citada (morta antes de 1950): "personagens" com nome em inglês, "fala": o nome EXATO
  como a narração da cena escreve ("Dom João", "Napoleão"), roupa da época e a expressão da cena. NUNCA
  pessoa viva ou recente (político, atleta, empresário de hoje).
- Só mostre o que a CENA diz: país, cidade, rota, ano, número e nome que não estão na narração daquela
  cena são descartados pelo código.
- Use "lista" de 2-3 batidas com "palavra" quando a cena muda de assunto no meio (ex.: mapa e, quando a voz
  fala "1904", o ano).
- Todo número/ano do gráfico TEM que estar escrito em algarismos na narração DAQUELA cena.
- A mesma cor = o mesmo lado da história no vídeo inteiro.

Formato de cada gráfico: {formato}

ROTEIRO:
{cenas}

Responda SÓ com JSON: {{"cenas": [{{"cena": 1, "grafico": ...}}, ...]}}"""


def art_direction(script: dict, channel: ChannelConfig) -> dict:
    """Segunda chamada, só pra escolher o visual de cada cena do roteiro JÁ
    aprovado: misturado nos ~10 campos do roteiro o LLM quase nunca pedia
    gráfico (0 de 8 cenas no job 703), e o conserto da revisão de fatos
    reescrevia frases depois. Cena que já tem gráfico fica como está; cada
    um passa pelas travas de src/infographic.sanitize (número fora da
    narração sai). Sem LLM, segue sem — o automático cobre."""
    from .infographic import sanitize

    scenes = script.get("scenes") or []
    todo = [i for i, sc in enumerate(scenes) if not sc.get("grafico")]
    if not todo:
        return script
    listing = "\n".join(f"{i + 1}) {sc.get('narration', '')}" for i, sc in enumerate(scenes))
    prompt = ART_PROMPT.format(canal=channel.channel_title, nicho=channel.niche[:200], formato=json.loads(_GRAFICO_FIELD),
                               cenas=listing)
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=3000, validate=lambda r: '"cenas"' in r)
        data = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — sem diretor de arte, o automático cobre
        log.warning("diretor de arte indisponível: %s", exc)
        return script
    picked = {}
    for item in data.get("cenas") or []:
        try:
            idx = int(item.get("cena")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if idx in todo:
            g = sanitize(item.get("grafico"), scenes[idx].get("narration", ""))
            if g:
                picked[idx] = g
    if picked:
        log.info("diretor de arte: gráfico em %d de %d cenas (%s)", len(picked), len(todo),
                 ", ".join(sorted({b["tipo"] for g in picked.values() for b in g})))
    return {**script, "scenes": [{**sc, "grafico": picked[i]} if i in picked else sc for i, sc in enumerate(scenes)]}


# ---------------------------------------------------------------- plano antes de escrever
PLAN_KEYS = ("gancho", "premissa", "virada_1", "virada_2", "climax_factual", "fechamento_reflexivo")


def _plan_valid(raw: str) -> bool:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k].strip() for k in PLAN_KEYS)


# canais que escrevem a partir de uma lista de fatos verificados (passo de extração)
FATOS_CANAIS = {"curiosidades"}


def _numeros(texto: str) -> set[str]:
    """Números do texto sem separadores: '65,23' e '65.23' viram '6523'; '1.155' vira '1155'."""
    return {re.sub(r"[.,]", "", m) for m in re.findall(r"\d+(?:[.,]\d+)*", texto)}


def fatos_com_numeros_da_fonte(fatos: list[str], fontes: str) -> list[str]:
    """Fica só o fato cujos números aparecem nas fontes. Fato sem número passa; fato com
    número que não está nas fontes é descartado (é como o 1.430 e o 53,85 saíram do texto)."""
    na_fonte = _numeros(fontes)
    return [f for f in fatos if _numeros(f) <= na_fonte]


def extract_verified_facts(topic: str, web_facts: list[dict] | None) -> list[str] | None:
    """Passo A: lista fechada de fatos literais das FONTES (Wikipédia primeiro). Cada número
    tem de constar nas fontes (checado em código). None se sobrarem menos de 8 fatos."""
    fontes = "\n".join(f"[{f.get('titulo', '')}]: {f.get('trecho', '')}" for f in (web_facts or []))[:9000]
    if not fontes.strip():
        return None
    prompt = (
        f"Tema: {topic}\n\nFONTES:\n{fontes}\n\n"
        "Extraia de 10 a 12 FATOS que estão LITERALMENTE nas FONTES, com o mesmo número, data, "
        "nome e unidade. Prefira o que a Wikipédia diz. Cada fato numa frase curta. Não acrescente "
        "nada que não esteja nas fontes e não junte fontes diferentes num número novo. "
        "Responda SÓ JSON: {\"fatos\": [\"fato 1\", \"fato 2\", \"...\"]}"
    )
    from .providers import is_strong_model
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=12000, temperature=0.1,
                       validate=lambda r: '"fatos"' in r, model_filter=is_strong_model)
    except RuntimeError as exc:
        log.warning("extração de fatos indisponível: %s", exc)
        return None
    try:
        fatos = [str(x).strip() for x in _extract_json(raw).get("fatos", []) if str(x).strip()]
    except (ValueError, json.JSONDecodeError):
        log.warning("extração de fatos: resposta sem JSON válido (%d caracteres)", len(raw))
        return None
    ok = fatos_com_numeros_da_fonte(fatos, fontes)
    if len(fatos) != len(ok):
        log.warning("extração de fatos: %d de %d descartados (número fora das fontes)", len(fatos) - len(ok), len(fatos))
    return ok if len(ok) >= 8 else None


def plan_story(topic: str, facts: dict | None, web_facts: list[dict] | None) -> str | None:
    """Etapa 1 (só estrutura, sem narração): escolhe os 4 fatos mais surpreendentes
    das FONTES e monta o arco (gancho, premissa, duas viradas, clímax factual,
    fechamento). Devolve um bloco de texto para o pedido do roteiro, ou None se
    o modelo não responder com o JSON (aí o roteiro segue como antes)."""
    sources = ""
    if facts:
        sources += json.dumps(facts, ensure_ascii=False, separators=(",", ":"))[:6000] + "\n"
    sources += "\n".join(f"- {f.get('titulo', '')}: {f.get('trecho', '')}" for f in (web_facts or []))[:2500]
    if not sources.strip():
        return None
    prompt = (
        f"Tema: {topic}\n\nFONTES (use SÓ o que está aqui):\n{sources}\n\n"
        "Não escreva narração. Monte o PLANO do vídeo em JSON estrito com as chaves: "
        "gancho (a coisa concreta que o espectador não acredita, em 1 frase), premissa (onde estamos e "
        "por que importa, 1 frase), virada_1 e virada_2 (fatos que mudam o que se achava saber, 1 frase "
        "cada), climax_factual (o fato mais surpreendente de todos, 1 frase), fechamento_reflexivo "
        "(consequência ou comparação com o cotidiano, 1 frase). Cada item tem que ser um fato que está nas "
        "FONTES. ANTES de escrever, confirme o ÂNGULO do tema (a afirmação central: método, causa, "
        "consequência): ele tem de aparecer nas FONTES com as mesmas palavras e números. Se não aparecer, "
        "não use esse ângulo: escolha o que as FONTES dizem (ex.: datas, custo, comparação). "
        "Responda SÓ o JSON."
    )
    try:
        raw = complete([{"role": "user", "content": prompt}], max_tokens=700, validate=_plan_valid, temperature=0.2)
    except RuntimeError as exc:
        log.warning("plano do roteiro indisponível (%s) — segue sem plano", exc)
        return None
    data = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
    lines = [f"{k.replace('_', ' ').upper()}: {data[k].strip()}" for k in PLAN_KEYS]
    return "PLANO APROVADO (siga esta ordem e use só estes fatos; cada item vira uma parte do roteiro):\n" + "\n".join(lines)
