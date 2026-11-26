"""26/11: siglas — tabela central de como a voz fala cada uma, para todos os canais, que aprende.

Antes: 23 leituras fixas em src/tts.py (ACRONYM_SPOKEN) e uma regra de tamanho (até 4 letras a voz decide; 5+
vira palavra). Errava nos dois sentidos: ONU, ENEM, FIFA, NASA se leem como palavra; uma sigla longa que se
soletra virava palavra. O corpus de todos os vídeos tem 163 siglas (scripts/minerar_corpus.py).

- `data/siglas_aprendidas.json`: sigla → "soletra" | "palavra" | "nao_sigla" (palavra em maiúsculas por ênfase:
  "TORCEDOR", "PARA"). Quem decide é o modelo, uma vez por sigla; a grafia da soletração sai do CÓDIGO
  (LETRAS), nunca do modelo.
- `para_fala(texto)`: troca cada sigla conhecida pela fala ("TCU" → "tê cê u"; "ONU" → "Onu"); plural
  ("PECs", "ONGs") acompanha.
- `preparar(script)`: aprende numa chamada só as siglas do roteiro que a tabela não conhece.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("siglas")
ARQUIVO = Path(__file__).resolve().parent.parent / "data" / "siglas_aprendidas.json"
_trava = threading.Lock()

LETRAS = {"A": "á", "B": "bê", "C": "cê", "D": "dê", "E": "é", "F": "éfe", "G": "gê", "H": "agá", "I": "i",
          "J": "jota", "K": "cá", "L": "éle", "M": "ême", "N": "êne", "O": "ó", "P": "pê", "Q": "quê", "R": "érre",
          "S": "ésse", "T": "tê", "U": "u", "V": "vê", "W": "dáblio", "X": "xis", "Y": "ípsilon", "Z": "zê"}
MODOS = ("soletra", "palavra", "nao_sigla")
_SIGLA = re.compile(r"\b([A-Z]{2,8})(s?)\b")
# já tratadas em outro lugar: IA (regra própria "IÁ"), siglas de estado em "PT-BA" (tts), números romanos
_FORA = {"IA", "IAS", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "XV", "XX", "XXI", "XIX"}

_PEDIDO = """Uma voz brasileira vai ler um texto em português com estas palavras em MAIÚSCULAS. Para cada uma diga:
- "soletra": sigla que no Brasil se fala letra por letra (STF, TCU, CBF, PIB, CPI);
- "palavra": sigla que se fala como palavra (ONU, ENEM, FIFA, NASA, UNESCO, PIX, SUS, ICARDA);
- "nao_sigla": palavra comum escrita em maiúsculas por ênfase ou título (PARA, TORCEDOR, FIM, BOTAFOGO).
Palavras:
{siglas}
Responda SÓ JSON: {{"siglas": {{"SIGLA": "soletra" | "palavra" | "nao_sigla"}}}}"""


def _carregar() -> dict:
    try:
        return json.loads(ARQUIVO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar(dados: dict) -> None:
    try:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def siglas_do_texto(texto: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _SIGLA.finditer(texto or "") if m.group(1) not in _FORA))


def soletrada(sigla: str) -> str:
    return " ".join(LETRAS.get(c, c) for c in sigla.upper())


def classificar(siglas: list[str]) -> dict[str, str]:
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    tabela = _carregar()
    novas = [s for s in dict.fromkeys(siglas) if s not in tabela and s not in _FORA]
    if novas:
        try:
            from .redacao import _obj
            raw = complete([{"role": "user", "content": _PEDIDO.format(siglas="\n".join(f"- {s}" for s in novas))}],
                           max_tokens=1500, temperature=0.0, model_filter=is_strong_model,
                           validate=lambda r: isinstance(_obj(r).get("siglas"), dict))
            respostas = _obj(raw).get("siglas") or {}
        except ProvedoresIndisponiveis:
            raise
        except RuntimeError as exc:
            log.warning("siglas: classificação indisponível (%s) — a voz decide sozinha", exc)
            respostas = {}
        aprendidas = {s: str(respostas.get(s)).strip().lower() for s in novas
                      if str(respostas.get(s) or "").strip().lower() in MODOS}
        if aprendidas:
            with _trava:
                dados = _carregar()
                dados.update(aprendidas)
                _salvar(dados)
            log.info("siglas aprendidas: %s", aprendidas)
        tabela.update(aprendidas)
    return {s: tabela[s] for s in siglas if s in tabela}


def preparar(script: dict) -> dict[str, str]:
    texto = " ".join(sc.get("narration", "") for sc in script.get("scenes") or [])
    return classificar(siglas_do_texto(texto))


def para_fala(texto: str, fixas: dict[str, str] | None = None) -> str:
    """Troca as siglas conhecidas pela fala. `fixas` (src/tts.py ACRONYM_SPOKEN) vencem a tabela."""
    tabela = _carregar()
    fixas = fixas or {}

    def troca(m: re.Match) -> str:
        sigla, plural = m.group(1), m.group(2)
        if sigla in fixas or sigla in _FORA:
            return m.group(0)
        modo = tabela.get(sigla)
        if modo == "soletra":
            return soletrada(sigla) + plural
        if modo == "palavra":
            return sigla[0] + sigla[1:].lower() + plural
        return m.group(0)
    return _SIGLA.sub(troca, texto or "")


def falas_soletradas() -> dict[str, str]:
    """{sigla: fala} das que se soletram — a voz fala assim e a legenda junta de volta na sigla."""
    return {s: soletrada(s) for s, m in _carregar().items() if m == "soletra"}


def lidas_como_palavra() -> set[str]:
    """Siglas que se leem como palavra e palavras em maiúsculas por ênfase: a voz recebe "Onu", a legenda mostra
    a grafia original."""
    return {s for s, m in _carregar().items() if m in ("palavra", "nao_sigla")}
