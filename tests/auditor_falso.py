"""Resposta simulada do auditor (20/11): uma decisão para CADA frase que o pedido manda julgar —
"narrativa_ok" nas narrativas, "sustentada" nas outras —, com exceções por número de frase."""
import re


def AUD(nota=8, problemas=(), especiais=None, omitir=(), enganos=None):
    """Devolve uma função pedido -> resposta. `especiais` = {n: (decisao, justificativa)};
    `omitir` = números que o auditor "esquece" (para testar a frase sem decisão). 20/11: responde também
    ao espectador (`enganos`, lista no formato do prompt) e ao editor (todo critério = `nota`)."""
    especiais = especiais or {}

    def responder(pedido: str) -> dict:
        qualidade = {"nota": nota, "problemas": list(problemas)}
        if "ESPECTADOR atento" in pedido:
            return {"conclusoes_do_publico": ["o público entende o tema", "o público aprende os fatos"],
                    "enganos": list(enganos or [])}
        if "Você é o EDITOR" in pedido:
            return {"criterios": {k: nota for k in ("abertura", "progressao", "novidade", "sem_repeticao", "narrativa",
                                                    "titulo", "conclusao", "linguagem")},
                    "problemas": [{"criterio": "geral", "cenas": [1], "descricao": str(x)} for x in problemas]}
        if "FRASES A JULGAR" not in pedido:
            return {"qualidade": qualidade}
        bloco = pedido.split("FRASES A JULGAR", 1)[1].split("Dê uma decisão", 1)[0]
        frases = []
        for m in re.finditer(r"^(\d+)\) \[(\w+);", bloco, re.M):
            n, tipo = int(m.group(1)), m.group(2)
            if n in omitir:
                continue
            decisao, just = especiais.get(n, ("narrativa_ok" if tipo == "narrativa" else "sustentada",
                                              "conferida contra as evidências citadas"))
            frases.append({"n": n, "decisao": decisao, "evidencias": [], "justificativa": just})
        return {"frases": frases, "qualidade": qualidade if "BLOCO do roteiro" not in pedido else {"nota": 0, "problemas": []}}
    return responder
