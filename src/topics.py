"""Escolha de tema sem repetição, pra qualquer canal/formato.

Mantém um registro persistente (`data/used_topics.json`) de todo tema já
usado — cron diário, roda manual pelo painel e formato longo, todos passam
por aqui. Enquanto a lista fixa do canal (`channels/<nome>.yaml`) tiver tema
não usado, sorteia dali. Quando a lista fixa se esgota, pede pro LLM um tema
novo dentro do nicho do canal, mostrando a lista completa de temas já usados
pra ele não repetir nada — assim o canal nunca fica sem tema, mesmo depois
de meses rodando todo dia.

Temas virais: se o canal tem `viral_queries` no yaml, parte dos vídeos
(`viral_share`, padrão 50%, e 100% quando a lista fixa esgota) usa um tema
tirado do que está bombando no YouTube no mês — busca os vídeos mais
vistos do nicho via yt-dlp (sem gastar cota da YouTube Data API, que fica
toda pros uploads) e pede pro LLM o ASSUNTO REAL de um deles. Se a busca ou
o LLM falhar, cai na lista fixa normalmente.

Tema que não veio da lista fixa (viral ou gerado) só vale com um assunto
real e específico (caso, pessoa, lugar, evento) que a busca na internet
confirma — antes o LLM transformava a trend num tema genérico ("7 casos
reais de crimes bizarros...") e o roteirista preenchia de memória, ou
inventava o tema do zero ("o cadáver de Tangier", "as Boas de São Miguel"
— 07/10, 8 roteiros reprovados no dia).
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path

from .providers import complete
from .script_gen import current_date_rule
from .web_search import search_topic_facts

log = logging.getLogger("topics")

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "used_topics.json"
CHANNELS_DIR = ROOT / "channels"


def _append_topic_to_yaml(channel_name: str, topic: str) -> None:
    """Grava um tema gerado na hora (LLM, lista fixa esgotada) de volta em
    channels/<nome>.yaml -> topics, pra ele aparecer na fila do painel em
    vez de ficar só invisível em data/used_topics.json."""
    path = CHANNELS_DIR / f"{channel_name}.yaml"
    if not path.exists():
        return
    text = path.read_text()
    escaped = topic.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'  - "{escaped}"'
    pattern = r"^topics:[ \t]*\n((?:  - .*\n)*)"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match:
        insert_at = match.end()
        text = text[:insert_at] + new_line + "\n" + text[insert_at:]
    else:
        sep = "" if text.endswith("\n") else "\n"
        text = text + sep + f"topics:\n{new_line}\n"
    path.write_text(text)


def _load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# Tamanho mínimo pra considerar um tema "de verdade" — qualquer coisa mais
# curta é quase certamente lixo (tag de raciocínio vazada, pontuação solta,
# resposta cortada). Visto na prática: tema virou literalmente "<think>".
_MIN_TOPIC_LEN = 12
# Sinais de que o LLM vazou raciocínio interno ou markdown em vez de
# responder só com o tema — mesmo depois do provider já tentar limpar isso.
# Também (03/10, entraram na lista do politica): "16 | 17 (o que o Brasil
# acabou de lançar)", "É DIO! Ajusta essa situação...", "TEMA sobre o
# sistema de cheques ... para fazer 2]" — barra vertical, exclamação,
# colchete e "TEMA" no começo nunca aparecem num tema de verdade.
_GARBAGE_RE = re.compile(r"<think|</think|^```|^\{|^\[|\||!|\[|\]|^tema\b", re.IGNORECASE)
# Conversa em vez de tema (visto: "Claro, estou aqui para ajudar com ideias
# de vídeos... Aqui estão alguns temas:").
_CHATTER_RE = re.compile(
    r"^(claro|certo|ok|aqui est|segue|com certeza|entendi|sure|here)\b|estou aqui|:\s*$",
    re.IGNORECASE,
)
_MAX_TOPIC_LEN = 180
# Resposta cortada no meio (visto: "...e faturar até 10 mil reais por").
_TRUNCATED_RE = re.compile(
    r"\b(por|para|pra|de|do|da|dos|das|em|no|na|com|e|ou|o|a|os|as|um|uma|que|sem|até)$",
    re.IGNORECASE,
)


def _is_valid_topic(topic: str) -> bool:
    return (
        _MIN_TOPIC_LEN <= len(topic) <= _MAX_TOPIC_LEN
        and not _GARBAGE_RE.search(topic)
        and not _CHATTER_RE.search(topic)
        and not _TRUNCATED_RE.search(topic.rstrip(" .,;-"))
    )


_ENUM_PREFIX_RE = re.compile(r"^\s*(\d+[.)]|[-*•])\s+")


def _first_line(raw: str) -> str:
    # às vezes o modelo devolve mais de uma linha mesmo pedindo pra não —
    # fica só com a primeira linha não vazia.
    cleaned = raw.strip().strip('"').strip("-").strip()
    for line in cleaned.splitlines():
        line = line.strip()
        if line:
            cleaned = line
            break
    # numeração de lista vazada ("1. O enigma da..." — dry-run de 06/10);
    # "7 fatos sobre..." (sem ponto) é tema de lista e fica
    return _ENUM_PREFIX_RE.sub("", cleaned).strip('"“” ')


# regras comuns do tema viral e do gerado: assunto ÚNICO, real e citado pelo
# nome — é o que a busca consegue confirmar e o roteiro consegue sustentar
_SUBJECT_RULES = (
    "O tema precisa ser sobre UM assunto real, específico e documentado "
    "(um caso, uma pessoa, um lugar, um evento, uma descoberta, uma "
    "ferramenta) e CITAR esse assunto pelo nome no próprio tema — nada de "
    "tema vago ('a modelo que o mundo adora', 'um mistério do Canadá') e "
    "nada de lista ('7 casos...', 'N fatos...'), que obriga a inventar "
    "itens. Se você não tem CERTEZA de que o assunto existe, escolha outro. "
    "NUNCA proponha tema que acuse ou insinue irregularidade de uma pessoa "
    "real específica — prefira tema explicativo, verificável em fonte "
    "pública.\n\n"
    "Não cite youtubers, podcasters ou o canal do vídeo de inspiração — o "
    "tema é o assunto, não quem falou dele.\n\n"
    'Responda SÓ com JSON numa linha: {"assunto": "SÓ o nome próprio do '
    'assunto como aparece na Wikipedia, no idioma ORIGINAL se for estrangeiro '
    '(ex.: "Mary Celeste", "Elizabeth Smart", "Headless Valley" — não '
    '"Vale Sem Cabeça"), sem palavra descritiva", "tema": "título do tema em '
    'português, uma linha"}'
)
_STOPWORDS = {
    "caso", "the", "and", "como", "para", "pela", "pelo", "sobre", "entre", "depois", "antes", "mais", "muito",
    "the", "and", "with", "from", "that", "dos", "das", "nos", "nas", "uma", "que", "foi",
}


def _norm(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def _subject_words(subject: str) -> list[str]:
    # número e palavra de 3 letras contam ("Voo 585", "Air") — sem eles o
    # inexistente "Voo 585 Air Canada 1985" passava só com "canada 1985"
    return [w for w in re.findall(r"\w{3,}|\d+", _norm(subject)) if w not in _STOPWORDS]


def subject_confirmed(subject: str) -> bool:
    """True se a busca real acha o assunto: um resultado que tenha TODAS as
    palavras do assunto (sem acento, ignorando palavra curta). Busca sempre
    devolve alguma coisa — contar resultados não confirma nada (o "cadáver
    de Tangier" teve 5 resultados e não existe)."""
    words = _subject_words(subject)
    if not words:
        return False
    for r in search_topic_facts(subject, max_results=5) or []:
        text = _norm(f"{r.get('titulo', '')} {r.get('trecho', '')}")
        if all(w in text for w in words):
            return True
    return False


def _parse_subject(raw: str) -> tuple[str, str] | None:
    m = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    subject = str(data.get("assunto") or "").strip()
    topic = _first_line(str(data.get("tema") or ""))
    words = _subject_words(subject)
    if not words or len(subject.split()) > 8 or not _is_valid_topic(topic):
        return None
    # o tema cita o assunto (pelo menos metade das palavras) — senão a busca
    # do roteiro ancora em outra coisa
    if sum(w in _norm(topic) for w in words) * 2 < len(words):
        return None
    if LIST_RE.match(topic):
        return None
    return subject, topic


LIST_RE = re.compile(r"^\s*\d{1,2}\s+\w+")


def _confirmed_topic(prompt: str, used: list[str], label: str, niche: str, tries: int = 3) -> str:
    """Pede assunto+tema ao LLM e só aceita o que a busca confirma; assunto
    não confirmado volta pro prompt como proibido. RuntimeError se nenhum
    passar — melhor sem tema novo do que tema inventado."""
    rejected: list[str] = []
    for attempt in range(tries):
        extra = ""
        if rejected:
            extra = "\n\nAssuntos que NÃO foram encontrados na internet (não use): " + "; ".join(rejected)
        try:
            raw = complete(
                [{"role": "user", "content": prompt + extra}], max_tokens=400,
                validate=lambda r: _parse_subject(r) is not None,
            )
        except RuntimeError as exc:
            log.warning("%s: tentativa %d/%d sem resposta válida (%s)", label, attempt + 1, tries, exc)
            continue
        subject, topic = _parse_subject(raw)  # type: ignore[misc]
        if topic in used:
            rejected.append(subject)
            continue
        # assunto claramente fora do nicho do canal (polêmica dentro do
        # nicho vale — dá view)
        problem = topic_problem(topic, niche, strict=False)
        if problem:
            log.warning("%s: %r fora do nicho (%s) — descartado", label, topic, problem)
            rejected.append(f"{subject} (fora do nicho)")
            continue
        if subject_confirmed(subject):
            log.info("%s: assunto %r confirmado na busca", label, subject)
            return topic
        log.warning("%s: assunto %r não encontrado na busca — descartado", label, subject)
        rejected.append(subject)
    raise RuntimeError(f"{label}: nenhum assunto confirmado na internet após {tries} tentativas")


def _generate_new_topic(niche: str, used: list[str]) -> str:
    used_block = "\n".join(f"- {t}" for t in used) or "(nenhum ainda)"
    prompt = (
        f"Você ajuda a planejar temas de vídeos do YouTube para um canal "
        f"sobre: {niche}.\n\n{current_date_rule()}\n\n"
        f"Temas JÁ USADOS neste canal (NÃO pode repetir nenhum destes, nem "
        f"algo muito parecido/reformulado):\n{used_block}\n\n"
        f"Proponha UM tema novo. {_SUBJECT_RULES}"
    )
    return _confirmed_topic(prompt, used, "tema novo", niche)


# Filtro da busca do YouTube: ordenar por visualizações, enviados este mês,
# só vídeos (sem canais/playlists).
_YT_VIRAL_FILTER = "CAMSBAgEEAE"
# Abaixo disso não é "viral", é só o que tinha na busca.
_VIRAL_MIN_VIEWS = 20_000


def _viral_titles(queries: list[str], per_query: int = 15, limit: int = 25) -> list[tuple[str, int]]:
    """Títulos mais vistos no YouTube no último mês pras buscas do canal,
    ordenados por visualizações. Lista vazia se o yt-dlp falhar."""
    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp não instalado — sem temas virais")
        return []
    from urllib.parse import quote

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": per_query,
        "http_headers": {"Accept-Language": "pt-BR,pt;q=0.9"},
    }
    seen: dict[str, int] = {}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for q in queries:
            url = f"https://www.youtube.com/results?search_query={quote(q)}&sp={_YT_VIRAL_FILTER}&gl=BR&hl=pt"
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as exc:  # noqa: BLE001 — rede/layout do YouTube, nunca derruba o pipeline
                log.warning("busca viral falhou (%r): %s", q, exc)
                continue
            for e in info.get("entries") or []:
                title, views = e.get("title"), e.get("view_count") or 0
                if title and views >= _VIRAL_MIN_VIEWS:
                    seen[title] = max(seen.get(title, 0), views)
    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def _generate_viral_topic(niche: str, viral: list[tuple[str, int]], used: list[str]) -> str:
    viral_block = "\n".join(f"- {t} ({v:,} views)".replace(",", ".") for t, v in viral)
    used_block = "\n".join(f"- {t}" for t in used) or "(nenhum ainda)"
    prompt = (
        f"Você ajuda a planejar temas de vídeos do YouTube para um canal "
        f"brasileiro sobre: {niche}.\n\n{current_date_rule()}\n\n"
        f"Vídeos que estão BOMBANDO no YouTube neste mês (podem estar em "
        f"outro idioma):\n{viral_block}\n\n"
        f"Temas JÁ USADOS neste canal (NÃO pode repetir nenhum destes, nem "
        f"algo muito parecido/reformulado):\n{used_block}\n\n"
        "Escolha UM dos vídeos virais acima que tenha a ver com o nicho e "
        "use o MESMO assunto real dele (o caso, a pessoa, o lugar, o evento "
        "de que ele fala) num tema com título próprio, adaptado ao público "
        "brasileiro — nunca copie o título e nunca troque o assunto por uma "
        "versão genérica. Ignore vídeos que não têm nada a ver com o nicho "
        f"ou cujo assunto você não sabe qual é. {_SUBJECT_RULES}"
    )
    return _confirmed_topic(prompt, used, "tema viral", niche, tries=2)


def _try_viral_topic(channel_name: str, niche: str, queries: list[str], used: list[str]) -> str | None:
    viral = _viral_titles(queries)
    if not viral:
        log.info("[%s] nenhum vídeo viral encontrado — usando a fila normal", channel_name)
        return None
    try:
        topic = _generate_viral_topic(niche, viral, used)
    except RuntimeError as exc:
        log.warning("[%s] LLM não gerou tema viral (%s) — usando a fila normal", channel_name, exc)
        return None
    if topic in used:
        return None
    log.info("[%s] tema viral: %s (inspirado em %d vídeos em alta)", channel_name, topic, len(viral))
    return topic


def pick_topic(
    channel_name: str,
    pool: list[str],
    niche: str,
    viral_queries: list[str] | None = None,
    viral_share: float = 0.5,
) -> str:
    """Escolhe um tema não usado ainda pra `channel_name` — fila única
    (curto e longo compartilham a mesma lista, já que só sai 1 vídeo/dia por
    canal). Marca como usado e persiste antes de devolver, pra nunca
    sortear o mesmo tema duas vezes.
    """
    state = _load()
    used = state.setdefault(channel_name, [])
    used_set = set(used)

    unused = [t for t in pool if t not in used_set]
    topic = None
    if viral_queries and (not unused or random.random() < viral_share):
        topic = _try_viral_topic(channel_name, niche, viral_queries, used)
    if topic is None and unused:
        topic = random.choice(unused)
    elif topic is None:
        log.info("[%s] lista fixa de temas esgotada — pedindo tema novo ao LLM", channel_name)
        topic = _generate_new_topic(niche, used)
    if topic not in pool:
        # tema viral ou gerado na hora: grava no yaml pra aparecer no painel
        _append_topic_to_yaml(channel_name, topic)

    used.append(topic)
    _save(state)
    return topic


# modelo pequeno (<= ~9B) aprova qualquer coisa nessa checagem
_SMALL_MODEL_RE = re.compile(r"(?<![\d.])[1-9](\.\d+)?b\b|allam|\b(mini|nano|tiny|lite)\b", re.IGNORECASE)


def _parse_verdict(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        verdict = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    return verdict if isinstance(verdict, dict) and isinstance(verdict.get("ok"), bool) else None


def topic_problem(topic: str, niche: str, strict: bool = True) -> str | None:
    """Motivo pra recusar o tema antes de gastar roteiro/render, ou None se
    serve. Pega tema embaralhado (ditado por voz: "17, pôn, não precisável
    esse tema... risos 118" virou vídeo de Salmos no politica, job 274) e
    tema fora do nicho do canal. Sem veredito da IA, `strict` recusa (tema
    digitado agora — foi assim que o embaralhado passou no teste do painel);
    sem `strict` aprova (tema da fila, já checado quando entrou nela)."""
    if not _is_valid_topic(topic):
        return "texto quebrado (lixo, conversa ou frase cortada)"
    prompt = (
        f"Nicho do canal do YouTube: {niche}\n"
        f"Tema proposto para o próximo vídeo: \"{topic}\"\n\n"
        "Responda só com JSON {\"ok\": true|false, \"motivo\": \"...\"}. "
        "ok=false se: (1) o tema não é uma frase com sentido claro — palavras "
        "embaralhadas, trechos sem nexo, instruções soltas, cara de ditado por "
        "voz mal transcrito; ou (2) o assunto claramente não pertence ao nicho "
        "do canal. Tema com sentido e dentro do nicho, mesmo com pequeno erro "
        "de digitação, é ok=true. motivo: curto, em português."
    )
    try:
        raw = complete(
            [{"role": "user", "content": prompt}], max_tokens=200,
            validate=lambda r: _parse_verdict(r) is not None,
            model_filter=lambda m: not _SMALL_MODEL_RE.search(m),
        )
    except RuntimeError as exc:
        log.warning("checagem do tema: nenhum provedor respondeu (%s)", exc)
        return "não consegui validar o tema agora (IA fora do ar) — tente de novo" if strict else None
    verdict = _parse_verdict(raw)
    return None if verdict["ok"] else (verdict.get("motivo") or "tema sem sentido ou fora do nicho")
