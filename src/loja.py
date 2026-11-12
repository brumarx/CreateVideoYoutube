"""Loja própria do canal na descrição e no comentário (botafogo.win/loja,
politica-br.org/loja).

Os produtos vêm ao vivo da vitrine do site (`loja.api`), nunca de lista
escrita aqui: produto desativado no site some do vídeo seguinte. O link de
cada produto é o `/compra/<id>` do próprio site, que conta o clique e segue
pro link de afiliado (Mercado Livre/Amazon). Canal liga em
channels/<nome>.yaml -> loja.
"""
from __future__ import annotations

import logging
import re
import unicodedata

import requests

log = logging.getLogger("loja")

DISCLOSURE = "Links da loja são de afiliado (Mercado Livre e Amazon): recebemos comissão por compras, sem custo extra pra você."
_STOP = {"de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "no", "na", "com", "para", "pra", "um", "uma"}


def _raiz(palavra: str) -> str:
    p = unicodedata.normalize("NFKD", palavra.lower()).encode("ascii", "ignore").decode()
    return p[:5]


def _palavras(texto: str) -> set[str]:
    return {_raiz(w) for w in re.findall(r"[\wÀ-ÿ]{3,}", texto or "") if w.lower() not in _STOP}


def produtos(cfg: dict) -> list[dict]:
    """[{id, nome, categoria}] da vitrine; [] se o site não responder (o vídeo sai sem produto)."""
    try:
        # vitrine da Política recusa o agente padrão do requests (403)
        resp = requests.get(cfg["api"], timeout=10, headers={"User-Agent": "Mozilla/5.0 (canal-youtube)"})
        resp.raise_for_status()
        itens = resp.json()
    except Exception as exc:  # noqa: BLE001 — loja é bônus, nunca derruba o vídeo
        log.warning("vitrine da loja indisponível (%s): %s", cfg.get("api"), exc)
        return []
    fora = {c.lower() for c in cfg.get("excluir_categorias") or []}
    out = []
    for p in itens if isinstance(itens, list) else []:
        nome = (p.get("nome") or p.get("name") or "").strip()
        cat = (p.get("categoria") or p.get("category") or "").strip()
        if p.get("id") is None or not nome or cat.lower() in fora:
            continue
        out.append({"id": p["id"], "nome": nome, "categoria": cat})
    return out


def escolher(itens: list[dict], texto: str, n: int, semente: int) -> list[dict]:
    """Os `n` produtos que mais combinam com o assunto do vídeo; sem nada em
    comum, rodízio pela semente (job), sem repetir categoria quando dá."""
    if not itens or n <= 0:
        return []
    # palavra que está em boa parte da vitrine ("Botafogo" em todo produto,
    # "camisa") não diz nada sobre o vídeo: só conta a que distingue
    contagem: dict[str, int] = {}
    for p in itens:
        for w in _palavras(p["nome"]):
            contagem[w] = contagem.get(w, 0) + 1
    comum = {w for w, c in contagem.items() if c > max(2, len(itens) // 5)}
    assunto = _palavras(texto) - comum
    pontos = [(len(_palavras(p["nome"]) & assunto), i, p) for i, p in enumerate(itens)]
    ligados = [p for s, _, p in sorted(pontos, key=lambda t: (-t[0], t[1])) if s > 0]
    k = semente % len(itens)
    girados = itens[k:] + itens[:k]
    escolha: list[dict] = []
    for p in ligados + girados:
        if p in escolha:
            continue
        if any(e["categoria"] == p["categoria"] for e in escolha) and p not in ligados:
            continue
        escolha.append(p)
        if len(escolha) == n:
            break
    for p in girados:  # poucas categorias: completa mesmo repetindo
        if len(escolha) == n:
            break
        if p not in escolha:
            escolha.append(p)
    return escolha


def linhas_descricao(cfg: dict, script: dict, semente: int) -> list[str]:
    """Linha da loja + produtos pro topo da descrição."""
    if not cfg or not cfg.get("url"):
        return []
    linhas = [f"{cfg.get('texto') or '🛒 Loja'}: {cfg['url']}"]
    texto = " ".join([script.get("title") or "", script.get("topic") or ""]
                     + [sc.get("narration", "") for sc in script.get("scenes", [])])
    for p in escolher(produtos(cfg), texto, int(cfg.get("produtos", 2)), semente):
        linhas.append(f"▸ {p['nome']}: {cfg.get('compra', cfg['url'].rsplit('/loja', 1)[0] + '/compra')}/{p['id']}")
    return linhas


def linha_comentario(cfg: dict) -> str:
    if not cfg or not cfg.get("url"):
        return ""
    return f"{cfg.get('texto') or '🛒 Loja'}: {cfg['url']}"
