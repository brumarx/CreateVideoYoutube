"""Checagens determinísticas (sem IA) de texto com "cara de IA" e de título/descrição (25/11 — política
do YouTube 2026 contra conteúdo inautêntico/produzido em massa: roteiro com os chavões de sempre e
metadados fora do padrão são o que denuncia o canal-fábrica).

- `cara_de_ia(narrações)` -> (erros, avisos): chavões de texto gerado (2+ distintos = erro, 1 = aviso),
  travessão demais (aviso) e abertura genérica depois do bordão do canal (erro);
- `defeitos_de_texto(script)`: os erros no formato da lista de defeitos de redacao.defeitos;
- `titulo_e_descricao(...)`: título longo (nossos dados: título > 60 rende menos, ver performance 11/2026),
  palavra principal do título longe do começo ou nunca falada, 1ª linha da descrição longa, número de
  hashtags fora de 3-5, vírgulas demais na descrição (cara de spam);
- `limitar_hashtags`: o conserto automático do que dá para consertar (repetidas fora, no máximo 5)."""
from __future__ import annotations

import functools
import re
import unicodedata
from pathlib import Path


def _dobrar(texto: str) -> str:
    """Minúsculas sem acento: os padrões abaixo são escritos assim e valem com ou sem acento."""
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


# (rótulo, padrão sobre o texto dobrado). Ordem importa: o trecho casado sai do texto antes dos padrões
# seguintes, pra "no fascinante mundo de" não contar também como "fascinante".
CHAVOES: list[tuple[str, str]] = [
    ("não é apenas X, é Y", r"\bnao e (apenas|so)\b[^.!?;]{1,80}?(,|—|–|-)\s*(mas\s+)?(e|sim)\b"),
    ("no vasto/fascinante/incrível mundo de", r"\bno (vasto|fascinante|incrivel) mundo d"),
    ("jornada fascinante/incrível", r"\bjornada (fascinante|incrivel)"),
    ("prepare-se para", r"\bprepare-se para\b"),
    ("vamos explorar", r"\bvamos explorar\b"),
    ("em um mundo onde", r"\b(em um|num) mundo onde\b"),
    ("é crucial", r"\be crucial\b"),
    ("nos dias de hoje", r"\bnos dias de hoje\b"),
    ("além disso", r"\balem disso\b"),
    ("ademais", r"\bademais\b"),
    ("outrossim", r"\boutrossim\b"),
    ("vale ressaltar", r"\bvale ressaltar\b"),
    ("é importante destacar", r"\be importante destacar\b"),
    ("mergulhe/mergulhar/mergulhamos", r"\bmergulh(e|ar|amos)\b"),
    ("desvende/desvendar/desvendamos", r"\bdesvend(e|ar|amos)\b"),
    ("fascinante", r"\bfascinante(s)?\b"),
    ("intrigante", r"\bintrigante(s)?\b"),
    ("delve", r"\bdelv(e|es|ing)\b"),
    ("tapestry", r"\btapestry\b"),
    ("testament", r"\b(a )?testament to\b"),
]

# 1ª frase falada depois do bordão do canal
_ABERTURA_GENERICA = re.compile(
    r"^(oi|ola|hoje (eu|vamos|a gente)|neste video|nesse video|voce sabia|sabia que|bem[- ]?vind|vamos falar)\b")
_FRASES = re.compile(r"(?<=[.!?])\s+")
_TRAVESSAO = re.compile(r"—|\s–\s|\s-\s")


@functools.lru_cache(maxsize=1)
def _bordoes_dos_canais() -> tuple[str, ...]:
    """Bordões de entrada de todos os canais (channels/*.yaml): saem do começo antes de olhar a abertura."""
    try:
        import yaml

        bordoes = []
        for arq in sorted((Path(__file__).resolve().parent.parent / "channels").glob("*.yaml")):
            g = (yaml.safe_load(arq.read_text()) or {}).get("greeting")
            bordoes += [str(x) for x in (g if isinstance(g, list) else [g]) if x]
        return tuple(bordoes)
    except Exception:  # noqa: BLE001 — sem yaml legível, só o bordão passado por quem chama
        return ()


def _sem_bordao(texto: str, saudacoes: list[str] | tuple[str, ...]) -> str:
    texto = texto.strip()
    for s in sorted({*saudacoes, *_bordoes_dos_canais()}, key=len, reverse=True):
        if s and texto.startswith(s):
            return texto[len(s):].strip()
    return texto


def cara_de_ia(texto_roteiro: list[str], saudacoes: list[str] | tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """(erros, avisos) do roteiro falado (uma narração por cena). `saudacoes`: bordão(ões) do canal; os de
    todos os canais em channels/*.yaml já são reconhecidos."""
    erros: list[str] = []
    avisos: list[str] = []
    narr = [str(t or "").strip() for t in texto_roteiro if str(t or "").strip()]
    texto = " ".join(narr)
    resto = _dobrar(texto)
    achados: list[tuple[str, str]] = []
    for rotulo, padrao in CHAVOES:
        m = re.search(padrao, resto)
        if m:
            achados.append((rotulo, m.group(0)))
            resto = re.sub(padrao, " ", resto)
    if len(achados) >= 2:
        erros.append("expressões com cara de texto de IA: " + "; ".join(f"\"{a}\"" for _, a in achados[:6])
                     + " (troque por fala direta, com o fato)")
    elif achados:
        avisos.append(f"expressão com cara de texto de IA: \"{achados[0][1]}\"")

    frases = [f for f in _FRASES.split(texto) if f.strip()]
    travessoes = len(_TRAVESSAO.findall(texto))
    if travessoes >= 2 and travessoes * 3 > len(frases):
        avisos.append(f"travessão demais: {travessoes} em {len(frases)} frases (mais de 1 a cada 3 frases)")

    if narr:
        inicio = _sem_bordao(narr[0], saudacoes)
        primeira = _FRASES.split(inicio, maxsplit=1)[0] if inicio else ""
        if primeira and _ABERTURA_GENERICA.search(_dobrar(primeira).lstrip(" ,.!¿?\"'")):
            erros.append(f"abertura genérica (\"{primeira[:60]}\"): comece pelo fato mais forte, sem "
                         "'oi', 'hoje vamos', 'você sabia', 'neste vídeo'")
    return erros, avisos


def _narracoes(script: dict) -> list[str]:
    """Texto falado por cena: as frases (modo redação) ou a narração."""
    saida = []
    for sc in script.get("scenes") or []:
        frases = [str(f.get("texto") or "") for f in sc.get("frases") or [] if isinstance(f, dict)]
        saida.append(" ".join(frases) if any(frases) else str(sc.get("narration") or ""))
    return saida


def defeitos_de_texto(script: dict) -> list[str]:
    """Erros de `cara_de_ia` no formato dos defeitos de redacao.defeitos (os avisos ficam de fora: não pedem
    nova tentativa)."""
    saud = [script["_greeting"]] if script.get("_greeting") else []
    erros, _ = cara_de_ia(_narracoes(script), saud)
    return erros


# ---------- título e descrição
TITULO_MAX = 60
PALAVRA_CHAVE_ATE = 40
PRIMEIRA_LINHA_MAX = 125
HASHTAGS_MIN, HASHTAGS_MAX = 3, 5
VIRGULAS_MAX = 25

_PARADA = set(_dobrar("""a o as os um uma uns umas de da do das dos em no na nos nas por para pra pelo pela
pelos pelas com sem que e ou se ao aos à às como mais menos muito muita muitos muitas seu sua seus suas é
foi são era ser ter tem têm isso esse essa este esta esses essas estes estas aquele aquela qual quais quem
quando onde porque porquê até sobre entre depois antes ainda já também só nunca sempre todo toda todos
todas cada outro outra outros outras mesmo mesma você vocês ele ela eles elas nós eu meu minha nosso nossa
há vai vão pode podem fez faz fazer veja saiba entenda descubra""").split())


def palavra_chave(titulo: str) -> str | None:
    """A palavra principal do título: a mais longa que não é palavra vazia (número conta)."""
    palavras = [p for p in re.findall(r"[\wÀ-ÿ]+", titulo or "") if _dobrar(p) not in _PARADA and len(p) > 2]
    return max(palavras, key=len) if palavras else None


def _falada(palavra: str, narracao: str) -> bool:
    """A palavra (ou o radical dela: 'vulcão'/'vulcões') aparece na narração."""
    p, n = _dobrar(palavra), _dobrar(narracao)
    radical = p if len(p) <= 5 else p[: max(5, len(p) - 2)]
    return re.search(r"\b" + re.escape(radical), n) is not None


def _hashtags_da_descricao(descricao: str) -> list[str]:
    return re.findall(r"(?<![\w/])#[\wÀ-ÿ]+", descricao or "")


def limitar_hashtags(hashtags: list[str], maximo: int = HASHTAGS_MAX) -> list[str]:
    """Sem repetida (maiúscula não conta) e no máximo `maximo`, na ordem dada (#Shorts vem primeiro)."""
    vistos, saida = set(), []
    for h in hashtags or []:
        h = str(h).strip()
        if not h:
            continue
        h = h if h.startswith("#") else f"#{h}"
        if h.lower() not in vistos:
            vistos.add(h.lower())
            saida.append(h)
    return saida[:maximo]


def titulo_e_descricao(titulo: str, descricao: str, narracao: str, hashtags: list[str] | None = None) -> list[str]:
    """Problemas de metadados (vazio = ok). `hashtags`: as que vão na descrição; None = lidas da descrição."""
    problemas: list[str] = []
    titulo = (titulo or "").strip()
    if len(titulo) > TITULO_MAX:
        problemas.append(f"título com {len(titulo)} caracteres (máximo {TITULO_MAX}: título longo rende menos)")
    chave = palavra_chave(titulo)
    if chave:
        pos = titulo.find(chave)
        if pos + len(chave) > PALAVRA_CHAVE_ATE:
            problemas.append(f"palavra principal do título (\"{chave}\") depois dos primeiros {PALAVRA_CHAVE_ATE} "
                             "caracteres")
        if narracao and not _falada(chave, narracao):
            problemas.append(f"palavra principal do título (\"{chave}\") não é falada na narração")
    primeira = (descricao or "").strip().split("\n", 1)[0]
    if len(primeira) > PRIMEIRA_LINHA_MAX:
        problemas.append(f"1ª linha da descrição com {len(primeira)} caracteres (máximo {PRIMEIRA_LINHA_MAX}: "
                         "o resto some no '...mais')")
    tags = hashtags if hashtags is not None else _hashtags_da_descricao(descricao)
    if not HASHTAGS_MIN <= len(tags) <= HASHTAGS_MAX:
        problemas.append(f"{len(tags)} hashtags (use de {HASHTAGS_MIN} a {HASHTAGS_MAX})")
    virgulas = (descricao or "").count(",")
    if virgulas > VIRGULAS_MAX:
        problemas.append(f"descrição com {virgulas} vírgulas (mais de {VIRGULAS_MAX} parece lista de palavras-chave)")
    return problemas
