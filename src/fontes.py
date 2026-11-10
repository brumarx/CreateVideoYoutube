"""Bloco de fontes ÚNICO: lido igual pela extração de fatos, pelo revisor, pelo juiz e pelo
conserto (mesma ordem, mesmo separador, mesmo limite). Antes cada um montava o seu e um mesmo
número podia ser aceito numa fatia e reprovado noutra (rodada 828)."""
import json

LIMITE_FONTES = 9000


def bloco_fontes(facts: dict | None, web_facts: list[dict] | None) -> str:
    partes: list[str] = []
    if facts:
        partes.append(json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    partes += [f"- {f.get('titulo', '')}: {f.get('trecho', '')}" for f in web_facts or []]
    return "\n".join(partes)[:LIMITE_FONTES]
