"""26/11: léxico de afirmação — UMA tabela das palavras que mudam a escala do que se afirma.

Antes eram listas soltas (_ALEGA, _ABSOLUTO, _PRECISAO, _EXTREMO em redacao.py; outras em evidencias, ordem,
script_gen) que não batiam entre si: "protege" estava na conferência do fechamento e fora do pedido (3 recusas
iguais); "primeira e única retirada" passou pela conferência em código porque "única" não tinha categoria e virou
leitura enganosa no fim do Svalbard 1029. Contagem real no corpus de todos os vídeos (scripts/minerar_corpus.py):
primeira 56, já 49, primeiro 42, total 29, única 12, sempre 12, todos 12, nunca 10, garante 10, porque 9.

Cada categoria: `frase` (como aparece no roteiro, pt) e `fonte` (o que a evidência precisa trazer para sustentar,
pt/en/es). Para acrescentar uma palavra: uma entrada aqui — o pedido ao escritor, a conferência em código da
edição e do fechamento passam a usá-la juntos.
"""
from __future__ import annotations

import re

LEXICO: dict[str, dict[str, str]] = {
    "quantificador": {
        "frase": r"tod[oa]s (os|as)|nenhum[a]?|ninguém|a maioria|a maior parte",
        # "ninguém mais mexe" = "only the depositor has access": "só X" sustenta "ninguém além de X"
        "fonte": r"all|every|none|no one|nobody|most|majority|tod[oa]s|nenhum|maioria|maior parte|ningun[oa]?|la mayoría|"
                 r"only|solely|exclusive\w*|apenas|somente|únic[oa]s?|sólo|solamente",
    },
    "frequencia": {
        "frase": r"sempre|nunca|jamais|frequentemente|constantemente|regularmente|recorrentes?|repetidamente|toda vez|"
                 r"todo ano|todos os anos|ano após ano|anualmente|com frequência|muitas vezes|várias vezes|"
                 r"seguidamente|crônic[oa]s?|permanentemente|permanentes?",
        "fonte": r"always|never|frequent\w*|constant\w*|regular\w*|recurr\w*|repeated\w*|every (year|time|spring|summer|"
                 r"winter)|each year|annual\w*|sempre|nunca|frequente\w*|recorrent\w*|todo ano|anual\w*|chronic\w*|"
                 r"permanent\w*|crônic\w*|siempre|jamás",
    },
    "certeza": {
        "frase": r"comprovadamente|certamente|com certeza|sem dúvida|garantidamente|definitivamente|está provado|provou que",
        "fonte": r"proven|proved|confirmed|certain\w*|guarantee\w*|definitely|comprovad\w*|confirmad\w*|garant\w*",
    },
    # 23/11 (job 1006): "parcialmente danificado" virou "destruído"
    "extensao": {
        # "do mundo inteiro" é abrangência, não extensão (categoria "mundial")
        "frase": r"(?<!mundo )inteir[oa]s?|totalmente|completamente|por completo|integralmente|destru[íi]d[oa]s?|destruiu|destruíram|"
                 r"destruição|arrasad[oa]s?|aniquilad[oa]s?",
        "fonte": r"entire\w*|whole|complete\w*|total\w*|fully|wholly|inteir\w*|complet\w*|integral\w*|destr\w*|wiped out|"
                 r"razed|annihilat\w*",
    },
    "natureza": {"frase": r"naturalmente", "fonte": r"natural\w*"},
    # 26/11 (corpus: "bancos de genes do mundo inteiro" × "genebanks around the world")
    "mundial": {
        "frase": r"(d?o|pelo|no) mundo inteiro|todo o (mundo|planeta)|(d?o|pelo) planeta inteiro",
        "fonte": r"world\w*|global\w*|international\w*|mundial\w*|internacional\w*|mundo|planeta|planet",
    },
    "causa": {
        "frase": r"causou|causaram|provocou|provocaram|por causa d[eoa]s?|graças a|fez com que|levou a|resultou",
        "fonte": r"caus\w*|because|due to|led to|lead to|result\w*|owing to|provoc\w*|por causa|levou|graças|devido|"
                 r"em razão|por conta|em consequência|consequência",
    },
    # 26/11 (Svalbard 1029: "Foi a primeira e única retirada" — a fonte dizia a primeira; "única" é outra afirmação)
    "unicidade": {
        "frase": r"únic[oa]s?|unicamente|exclusivamente|só uma vez|uma única vez|a primeira e única|o primeiro e único",
        "fonte": r"only|sole|solely|unique\w*|single|exclusive\w*|únic[oa]\w*|unic[oa]\w*|exclusiv\w*|solamente|sólo|solo",
    },
}

# garantia e efeito: o que o roteiro AFIRMA que algo faz/fez (o fechamento e a narrativa não podem dizer)
GARANTIA = (r"funciona|funcionou|garante|garantiu|assegura|assegurou|prova|provou|total|totalmente|sempre|já|"
            r"porque|por isso|graças|salvou|protege|resolve|resolveu")


def frase(categoria: str) -> re.Pattern:
    return re.compile(r"\b(" + LEXICO[categoria]["frase"] + r")\b", re.I)


def fonte(categoria: str) -> re.Pattern:
    return re.compile(r"\b(" + LEXICO[categoria]["fonte"] + r")\b", re.I)


def precisao() -> dict[str, tuple[re.Pattern, re.Pattern]]:
    """escala → (marcadores na frase, sustentação na evidência)."""
    return {c: (frase(c), fonte(c)) for c in LEXICO}


def garantia() -> re.Pattern:
    return re.compile(r"\b(" + GARANTIA + r")\b", re.I)


def palavras_de_garantia() -> list[str]:  # noqa
    return [str(p) for p in GARANTIA.split("|")]
