"""Link de afiliado da Amazon na descrição: uma busca na amazon.com.br com
o ID de associado, ligada ao assunto de cada vídeo (livro sobre o país do
Fractal, curso de inglês no Aprender vIA, camisa no Botafogo). Busca, e não
produto fixo: nunca aponta pra item esgotado ou que saiu de linha.
Canal liga em channels/<nome>.yaml -> afiliado_amazon."""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

from .config import ChannelConfig
from .providers import complete

log = logging.getLogger("affiliate")

AMAZON_TAG = "botafogowin-20"
# exigido pelo Acordo Operacional do Programa de Associados
AMAZON_DISCLOSURE = "Como associado da Amazon, recebo por compras qualificadas (sem custo extra pra você)."

_TERM_RE = re.compile(r"^[\wÀ-ÿ'\- ]{3,50}$")


def amazon_search_url(term: str) -> str:
    return f"https://www.amazon.com.br/s?k={quote_plus(term)}&tag={AMAZON_TAG}"


def _product_term(script: dict, default: str) -> str:
    narration = " ".join(sc.get("narration", "") for sc in script.get("scenes", []))[:1500]
    prompt = (
        "Sugira UM termo de busca da Amazon Brasil (2 a 5 palavras, em português) de um produto que quem "
        "assistiu este vídeo compraria. Prefira um livro sobre o assunto PRINCIPAL do vídeo — o país, a "
        "pessoa, o time, o evento (ex.: \"livro Copa do Mundo\", \"biografia Rebeca Andrade\") — ou o "
        "produto que o vídeo ensina a usar (ex.: \"curso de inglês\"). Nada de remédio, "
        "aposta ou arma. Responda só o termo, sem aspas.\n\n"
        f"TÍTULO: {script.get('title', '')}\nNARRAÇÃO: {narration}"
    )
    try:
        term = complete(
            [{"role": "user", "content": prompt}], max_tokens=40, temperature=0.3,
            validate=lambda r: bool(_TERM_RE.match(r.strip().strip('"'))) and 2 <= len(r.split()) <= 5,
        ).strip().strip('"')
        return term
    except RuntimeError as exc:
        log.warning("termo do afiliado: nenhum provedor respondeu (%s) — usando o do canal", exc)
        return default


def amazon_line(script: dict, channel: ChannelConfig) -> str | None:
    """Linha "🛒 texto: link" pro topo da descrição, ou None se o canal não usa."""
    cfg = channel.amazon_affiliate
    if not cfg:
        return None
    default = str(cfg.get("busca") or "").strip()
    term = default if cfg.get("fixo") else _product_term(script, default)
    if not term:
        return None
    text = str(cfg.get("texto") or "🛒 Na Amazon").strip()
    log.info("afiliado Amazon: busca %r", term)
    return f"{text} ({term}): {amazon_search_url(term)}"
