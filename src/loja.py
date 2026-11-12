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
_STOP = {"de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "no", "na", "com", "para", "pra", "um", "uma",
         # genéricas: "história"/"livro"/"oficial" no título não fazem um livro combinar
         "livro", "livros", "guia", "historia", "história", "oficial", "grande", "como", "uma", "sobre", "que"}


def _raiz(palavra: str) -> str:
    p = unicodedata.normalize("NFKD", palavra.lower()).encode("ascii", "ignore").decode()
    return p[:5]


def _palavras(texto: str, inteiras: bool = False) -> set[str]:
    """Raízes de 5 letras; `inteiras`: palavra inteira sem acento e sem plural,
    sem números (raiz confundia "construção" com "Constituição")."""
    palavras = [w for w in re.findall(r"[\wÀ-ÿ]{3,}", texto or "") if w.lower() not in _STOP]
    if not inteiras:
        return {_raiz(w) for w in palavras}
    out = set()
    for w in palavras:
        if any(ch.isdigit() for ch in w) or len(w) < 4:
            continue
        w = unicodedata.normalize("NFKD", w.lower()).encode("ascii", "ignore").decode()
        w = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if w not in _GENERICAS:
            out.add(w)
    return out


# palavra solta que aparece em tema de qualquer canal ("governos brasileiros")
_GENERICAS = {"brasileiro", "brasileira", "brasil", "mundo", "federal", "nacional", "pai", "paise"}
# "Raízes do Brasil, Sérgio Buarque de Holanda" -> "Raízes do Brasil": autor,
# editora e medida não contam (vídeo da Holanda não é do Sérgio Buarque)
_SO_TITULO = re.compile(r"\s*(,|—|–|\(|\s-\s).*$")


def _titulo_produto(nome: str) -> str:
    return _SO_TITULO.sub("", nome) or nome


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


def escolher(itens: list[dict], texto: str, n: int, semente: int, so_ligados: bool = False) -> list[dict]:
    """Os `n` produtos que mais combinam com o assunto do vídeo; sem nada em
    comum, rodízio pela semente (job), sem repetir categoria quando dá.
    `so_ligados`: loja de outro canal (livros da Política no Fractal) — só
    produto que combina, nunca rodízio."""
    if not itens or n <= 0:
        return []
    # palavra que está em boa parte da vitrine ("Botafogo" em todo produto,
    # "camisa") não diz nada sobre o vídeo: só conta a que distingue
    nome = (lambda p: _titulo_produto(p["nome"])) if so_ligados else (lambda p: p["nome"])
    contagem: dict[str, int] = {}
    for p in itens:
        for w in _palavras(nome(p), so_ligados):
            contagem[w] = contagem.get(w, 0) + 1
    comum = set() if so_ligados else {w for w, c in contagem.items() if c > max(2, len(itens) // 5)}
    assunto = _palavras(texto, so_ligados) - comum
    pontos = [(len(_palavras(nome(p), so_ligados) & assunto), i, p) for i, p in enumerate(itens)]
    ligados = [p for s, _, p in sorted(pontos, key=lambda t: (-t[0], t[1])) if s > 0]
    if so_ligados:
        return ligados[:n]
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
    """Linha da loja + produtos pro topo da descrição. `so_se_combinar` (loja
    de outro canal): só título/tema contam, e sem produto que combine não sai
    nada — o canal fica com a busca da Amazon."""
    if not cfg or not cfg.get("url"):
        return []
    so = bool(cfg.get("so_se_combinar"))
    partes = [script.get("title") or "", script.get("topic") or ""]
    if not so:
        partes += [sc.get("narration", "") for sc in script.get("scenes", [])]
    escolha = escolher(produtos(cfg), " ".join(partes), int(cfg.get("produtos", 2)), semente, so_ligados=so)
    if so and not escolha:
        return []
    linhas = [f"{cfg.get('texto') or '🛒 Loja'}: {cfg['url']}"]
    for p in escolha:
        linhas.append(f"▸ {p['nome']}: {cfg.get('compra', cfg['url'].rsplit('/loja', 1)[0] + '/compra')}/{p['id']}")
    return linhas


def linha_comentario(cfg: dict) -> str:
    if not cfg or not cfg.get("url"):
        return ""
    return f"{cfg.get('texto') or '🛒 Loja'}: {cfg['url']}"
