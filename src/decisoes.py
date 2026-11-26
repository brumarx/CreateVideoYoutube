"""26/11: leitura das decisões dos juízes (auditor, conferência, revisão, sustentação) — e aprendizado.

Svalbard 1025–1026: o juiz escreveu "factual: sustentada" em vez de "sustentada"; o código não reconheceu,
perdeu 4 pedidos e parou o job. Ninguém sabe de antemão tudo o que o modelo pode escrever, então:

1. `normalizar`: o que é só forma (maiúscula, acento, aspas, rótulo antes de ":", espaço no lugar de "_").
2. `ler`: decisão permitida, ou a que já foi APRENDIDA para aquela palavra (data/decisoes_aprendidas.json).
3. `aprender`: palavra desconhecida é perguntada uma vez ao próprio modelo ("qual destas você quis dizer?"),
   e a resposta fica guardada — da próxima vez sai sem chamada. Trava: palavra com sentido de reprovação
   ("parcialmente", "não", "excede"…) nunca vira decisão de aprovação, diga o modelo o que disser.
   Toda palavra desconhecida fica registrada (com a contagem) mesmo quando não se aprende nada.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from pathlib import Path
from typing import Iterable

log = logging.getLogger("decisoes")
ARQUIVO = Path(__file__).resolve().parent.parent / "data" / "decisoes_aprendidas.json"
_trava = threading.Lock()
# raízes de reprovação/ressalva: palavra que tem uma delas nunca é lida como aprovação
_NEGATIVA = re.compile(r"(^|_)(nao|parcial|excede|exager|sem|contradi|insufic|falt|incorret|errad|oculta|descart|"
                       r"corrig|diverg|irrelev|engan|duvid|incert|reprov|rejeit|fals|incomplet)")


def normalizar(valor) -> str:
    d = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    d = d.strip().strip("\"'`*.").lower()
    d = d.rsplit(":", 1)[-1].strip().strip("\"'`*.")
    return re.sub(r"[\s\-]+", "_", d)


def _carregar() -> dict:
    try:
        return json.loads(ARQUIVO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"aprendidas": {}, "desconhecidas": {}}


def _salvar(dados: dict) -> None:
    try:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def ler(valor, permitidas: Iterable[str], contexto: str) -> str | None:
    """A decisão permitida que `valor` quer dizer, ou None (desconhecida — fica registrada)."""
    permitidas = set(permitidas)
    d = normalizar(valor)
    if d in permitidas:
        return d
    with _trava:
        dados = _carregar()
        aprendida = dados.get("aprendidas", {}).get(contexto, {}).get(d)
        if aprendida in permitidas:
            return aprendida
        if d:
            cont = dados.setdefault("desconhecidas", {}).setdefault(contexto, {})
            cont[d] = cont.get(d, 0) + 1
            _salvar(dados)
    return None


def aprender(contexto: str, permitidas: Iterable[str], aprovadoras: Iterable[str], palavras: Iterable[str]) -> dict:
    """Pergunta ao modelo forte o que cada palavra desconhecida quis dizer, guarda o que passar na trava e
    devolve {palavra: decisão}. Sem modelo, {}."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    permitidas, aprovadoras = sorted(set(permitidas)), set(aprovadoras)
    palavras = sorted({normalizar(p) for p in palavras if normalizar(p)} - set(permitidas))
    if not palavras:
        return {}
    pedido = ("Um avaliador devia responder com UMA destas decisões: " + ", ".join(permitidas) + ".\n"
              "Ele escreveu as palavras abaixo. Para cada uma, diga qual decisão da lista ele quis dizer, ou "
              "\"nenhuma\" se não dá para saber.\n" + "\n".join(f"- {p}" for p in palavras)
              + '\nResponda SÓ JSON: {"mapa": {"palavra": "decisão"}}')
    try:
        from .redacao import _obj
        raw = complete([{"role": "user", "content": pedido}], max_tokens=600, temperature=0.0,
                       validate=lambda r: isinstance(_obj(r).get("mapa"), dict), model_filter=is_strong_model)
        mapa = _obj(raw).get("mapa") or {}
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("aprendizado de decisões indisponível: %s", exc)
        return {}
    novas = {}
    for p in palavras:
        alvo = normalizar(mapa.get(p, ""))
        if alvo not in permitidas:
            continue
        if alvo in aprovadoras and _NEGATIVA.search(p):
            log.warning("decisão %r (%s): o modelo leu como %r, mas a palavra tem sentido de reprovação — não "
                        "aprendida", p, contexto, alvo)
            continue
        novas[p] = alvo
    if novas:
        with _trava:
            dados = _carregar()
            dados.setdefault("aprendidas", {}).setdefault(contexto, {}).update(novas)
            _salvar(dados)
        log.info("decisões aprendidas (%s): %s", contexto, novas)
    return novas


def ler_lote(valores: list, permitidas: Iterable[str], aprovadoras: Iterable[str], contexto: str) -> list[str | None]:
    """Lê várias decisões de uma resposta; as desconhecidas são aprendidas numa chamada só e lidas de novo."""
    permitidas = set(permitidas)
    lidas = [ler(v, permitidas, contexto) for v in valores]
    faltam = [v for v, d in zip(valores, lidas) if d is None and normalizar(v)]
    if faltam:
        mapa = aprender(contexto, permitidas, aprovadoras, faltam)
        lidas = [d if d is not None else mapa.get(normalizar(v)) for v, d in zip(valores, lidas)]
    return lidas
