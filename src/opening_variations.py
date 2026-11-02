"""Aberturas e encerramentos variados por canal — aumenta retenção.

Cada canal tem 3-4 padrões de abertura (instead of sempre o mesmo) que rodam em sequência.
O LLM escolhe qual tipo no roteiro baseado no assunto."""
from __future__ import annotations

from dataclasses import dataclass

@dataclass
class OpeningVariation:
    """Variação de abertura para um roteiro."""
    style: str  # "hook_number", "hook_question", "hook_contradiction", "hook_consequence"
    instructions: str


def get_opening_rules(channel: str) -> list[OpeningVariation]:
    """Retorna as variações de abertura para cada canal."""

    rules = {
        "politica": [
            OpeningVariation(
                style="hook_number",
                instructions=(
                    "Comece com um NÚMERO CHOCANTE relacionado ao tema — salário, "
                    "gastos, doações, multa, crescimento. Exemplo: 'R$ 2,3 bilhões em um "
                    "dia', 'cresceu 4.700%', 'o terceiro maior gasto foi...'. O número "
                    "é a primeira frase, sem contexto antes, e gera curiosidade imediata "
                    "sobre COMO isso aconteceu."
                )
            ),
            OpeningVariation(
                style="hook_question",
                instructions=(
                    "Comece com uma PERGUNTA RETÓRICA que toda pessoa faz sobre política: "
                    "'Como um deputado pode...?', 'Por que não...?', 'Você sabe que existe "
                    "uma lei que...?'. A pergunta gera curiosidade sobre a resposta que vem "
                    "em seguida, nunca é saudação."
                )
            ),
            OpeningVariation(
                style="hook_contradiction",
                instructions=(
                    "Comece com uma CONTRADIÇÃO ou IRONIA: 'O deputado que prometeu combater "
                    "corrupção foi condenado por...', 'O senador que grita contra gastos públicos "
                    "tem a maior cota de...'. A contradição gera tensão e curiosidade sobre como "
                    "isso aconteceu."
                )
            ),
            OpeningVariation(
                style="hook_consequence",
                instructions=(
                    "Comece com a CONSEQUÊNCIA mais grave ou surpreendente: 'Isso custou "
                    "R$ X aos cofres públicos', 'Afetou Y milhões de brasileiros', 'Levou a "
                    "Z demissões'. A consequência gera impacto imediato e faz assistir virar "
                    "importância do tema."
                )
            ),
        ],
        "motivacional": [
            OpeningVariation(
                style="hook_number",
                instructions=(
                    "Comece com um NÚMERO que define o obstáculo ou o feito: 'Foi rejeitado "
                    "100 vezes', 'Tinha R$ 5', 'Perdeu 3 campanhas', 'Trabalhou 50 anos'. "
                    "O número é concreto e humano, faz assistir imaginar a dificuldade real."
                )
            ),
            OpeningVariation(
                style="hook_question",
                instructions=(
                    "Comece com uma PERGUNTA EXISTENCIAL: 'O que você faria se perdesse tudo?', "
                    "'Você é capaz de recomeçar do zero?', 'Sabe que uma rejeição pode levar a "
                    "glória?'. A pergunta faz assistir se identificar pessoalmente com o assunto."
                )
            ),
            OpeningVariation(
                style="hook_consequence",
                instructions=(
                    "Comece com o RESULTADO MAIS SURPREENDENTE: 'Virou lenda com isso', "
                    "'Inspirou milhões', 'Ganhou o prêmio mais importante da vida'. "
                    "O resultado cria curiosidade sobre COMO alguém chegou lá partindo do nada."
                )
            ),
            OpeningVariation(
                style="hook_contradiction",
                instructions=(
                    "Comece com a CONTRADIÇÃO entre a pessoa e o que ela se tornou: "
                    "'Era um migrante e virou o maior empresário', 'Falava errado e virou "
                    "orador famoso', 'Ninguém dava um centavo de chance e fez fortuna'. "
                    "A contradição gera curiosidade sobre a transformação."
                )
            ),
        ],
        "aprendervia": [
            OpeningVariation(
                style="hook_consequence",
                instructions=(
                    "Comece direto no RESULTADO PRÁTICO: 'Copie isto na IA e economize "
                    "R$ 500/mês', 'Faça isso em 2 minutos e aumente sua nota em X', 'Use "
                    "essa ferramenta grátis e trabalhe 4 horas a menos por dia'. "
                    "O resultado concreto em pt-BR e números motivam clicar."
                )
            ),
            OpeningVariation(
                style="hook_question",
                instructions=(
                    "Comece com uma PERGUNTA QUE TODO MUNDO FAZ: 'Como aprender IA de graça?', "
                    "'Qual é a melhor ferramenta para...?', 'Dá pra substituir essa ferramenta "
                    "paga por outra grátis?'. A pergunta comum cria identificação imediata."
                )
            ),
            OpeningVariation(
                style="hook_number",
                instructions=(
                    "Comece com um NÚMERO DE ECONOMIA OU GANHO: 'Grátis', '500% mais rápido', "
                    "'Economiza 30 horas por mês', 'Disponível em 5 idiomas'. O número é sobre "
                    "valor ou tempo economizado, nunca sobre complexidade."
                )
            ),
            OpeningVariation(
                style="hook_contradiction",
                instructions=(
                    "Comece com o CONTRASTE entre a ferramenta paga vs gratuita: "
                    "'Faz a mesma coisa que o app de R$ 300/mês e é grátis', "
                    "'Melhor que o software que você paga R$ 1000 de licença'. "
                    "O contraste gera incredulidade que se transforma em curiosidade."
                )
            ),
        ],
        "curiosidades": [
            OpeningVariation(
                style="hook_contradiction",
                instructions=(
                    "Comece com uma CONTRADIÇÃO IMPOSSÍVEL: 'O país mais rico do mundo gasta "
                    "com...', 'O país que não tem problema resolve...' Diferente de qualquer "
                    "expectativa que assistir tem sobre o assunto/país."
                )
            ),
            OpeningVariation(
                style="hook_question",
                instructions=(
                    "Comece com uma PERGUNTA QUE NINGUÉM FAZ, mas é curiosa: 'Você sabe por que "
                    "existe...?', 'Qual animal consegue...?', 'Qual país nunca...?'. "
                    "A pergunta incomum gera curiosidade imediata."
                )
            ),
            OpeningVariation(
                style="hook_number",
                instructions=(
                    "Comece com um NÚMERO QUE CHOCA: '4.000 anos', 'a profundidade de "
                    "10 km', 'maior que 100 cidades juntas', 'custou R$ 2 trilhões'. "
                    "O número é tão grande ou pequeno que parece ficção."
                )
            ),
            OpeningVariation(
                style="hook_consequence",
                instructions=(
                    "Comece com o RESULTADO MAIS ABSURDO OU FANTÁSTICO: 'Isso matou X pessoas', "
                    "'Mudou completamente a história do mundo', 'Criou uma espécie inteira', "
                    "'Resolveu um mistério de séculos'. O resultado é dramático e atrai clique."
                )
            ),
        ],
        "botafogo": [
            OpeningVariation(
                style="hook_number",
                instructions=(
                    "Comece com um PLACAR ou NÚMERO DO JOGO: 'Vitória 3-0', 'Classificação "
                    "com 3 jogos de sobra', 'O gol do milênio', '5 gols em 30 minutos'. "
                    "O número é direto e emotivo para torcedor."
                )
            ),
            OpeningVariation(
                style="hook_consequence",
                instructions=(
                    "Comece com o SIGNIFICADO HISTÓRICO: 'Garantiu a vaga na final', "
                    "'Tirou a maldição de 30 anos', 'Classificou pra Copa do Brasil', "
                    "'O jogo mais importante da temporada'. Dramatiza o resultado do jogo."
                )
            ),
            OpeningVariation(
                style="hook_question",
                instructions=(
                    "Comece com uma PERGUNTA APAIXONADA: 'Como o Botafogo conseguiu isso?', "
                    "'Você viu aquele lance?', 'Qual foi o melhor momento do jogo?'. "
                    "A pergunta gera emoção compartilhada com torcedor."
                )
            ),
        ],
    }

    return rules.get(channel, rules.get("curiosidades", []))  # fallback


def get_closing_rules(channel: str) -> list[str]:
    """Retorna as variações de encerramento para cada canal."""

    rules = {
        "politica": [
            "A ficha completa de cada deputado e senador, com todos os números, está no site {site}.",
            "Para descobrir mais sobre os números que financiam a política, visite {site}.",
            "Todos esses números, com fontes e detalhes, estão disponíveis em {site}.",
            "Quer entender melhor como funciona a cota parlamentar? Tudo está em {site}.",
        ],
        "motivacional": [
            "{site} — histórias que mudam vidas.",
            "Confira mais histórias inspiradoras em {site}.",
            "Quer aprender com quem conseguiu? Visite {site}.",
            "Descubra como começar sua própria história em {site}.",
        ],
        "aprendervia": [
            "Agora você conhece a ferramenta. Teste em {site} ou procure no seu app store.",
            "Quer aprender mais? Confira tutoriais e cursos grátis em {site}.",
            "Comece a usar agora mesmo — acesse {site}.",
            "Tem outras ferramentas grátis que funcionam? Deixe nos comentários.",
        ],
        "curiosidades": [
            "Quer conhecer mais curiosidades sobre esse lugar? Siga {site}.",
            "Compartilhe esse vídeo com alguém que tem curiosidade sobre o mundo.",
            "Qual curiosidade chocou mais? Comente qual outra você gostaria de ver.",
            "Se achou interessante, curte, comenta e se inscreve para mais curiosidades.",
        ],
        "botafogo": [
            "Fala, Fogão! Que tal esse jogo?",
            "Que performance! Que você acha de como o Botafogo está jogando?",
            "Vitória fundamental! Opina nos comentários.",
            "Olha a entrega! Que tal esse começo de temporada?",
        ],
    }

    return rules.get(channel, [
        "Inscreva-se para mais vídeos.",
        "Comente sua opinião aqui.",
        "Compartilhe e se inscreva.",
        "Deixe seu like e siga nos comentários.",
    ])


def select_opening(channel: str, last_used_index: int = -1) -> OpeningVariation:
    """Seleciona próxima variação de abertura (rotativo), evitando repetição."""
    rules = get_opening_rules(channel)
    if not rules:
        return OpeningVariation("hook_number", "Comece com um número ou fato impactante.")

    # Se a última foi a última da lista, volta ao início
    next_idx = (last_used_index + 1) % len(rules)
    return rules[next_idx]


def select_closing(channel: str, site: str = "", last_used_index: int = -1) -> str:
    """Seleciona próxima variação de encerramento (rotativo)."""
    rules = get_closing_rules(channel)
    if not rules:
        return "Se gostou, curta e se inscreva."

    next_idx = (last_used_index + 1) % len(rules)
    closing = rules[next_idx]
    return closing.format(site=site) if site and "{site}" in closing else closing
