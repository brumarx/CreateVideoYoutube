"""26/11: pronúncia central — uma tabela para todos os canais, que aprende.

Antes cada acerto de pronúncia ficava num lugar: PRONUNCIATIONS fixo em src/tts.py ("Tite", "SAF"), regra
avulsa ("Atlético-MG" → "Atlético Mineiro"), o campo "pronuncia"/"idioma" que o roteirista do modo antigo
preenchia (o da redação — Fractal — não preenche nada: Svalbard, Longyearbyen, NordGen iam para a voz sem
orientação) e a grafia que a revisão de áudio descobria quando a voz errava um nome — usada naquele job e
perdida. Agora:

- `data/pronuncias_aprendidas.json` guarda, por nome: "idioma" (en/es/fr/de/it/nl → voz nativa), "fala"
  (grafia à portuguesa com a tônica, para nome de outro alfabeto/idioma) ou "pt" (a voz brasileira lê certo).
- `preparar(script)`: nomes próprios da narração que a tabela não conhece são classificados numa chamada só
  ao modelo forte e guardados; devolve (pronuncia, idioma) dos nomes do roteiro.
- `confirmar_fala(nome, grafia)`: a grafia que a revisão de áudio CONFIRMOU (ouviu certo depois da troca)
  entra na tabela — o mesmo nome não sai errado de novo em nenhum canal.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("pronuncia")
ARQUIVO = Path(__file__).resolve().parent.parent / "data" / "pronuncias_aprendidas.json"
IDIOMAS_NATIVOS = ("en", "es", "fr", "de", "it", "nl")
_trava = threading.Lock()

# sequência de palavras com maiúscula (com conectivos de nome: "Rio de Janeiro", "Ludwig van Beethoven")
_NOME = re.compile(r"\b[A-ZÀ-Ý][\w'’-]*(?:\s+(?:(?:de|da|do|dos|das|van|von|der|den|la|le|del|di|du)\s+)?[A-ZÀ-Ý][\w'’-]*)*")
# palavra com maiúscula que é só começo de frase/português comum
_COMUNS = {"o", "a", "os", "as", "um", "uma", "e", "em", "no", "na", "nos", "nas", "de", "do", "da", "lá", "aí", "é",
           "foi", "são", "hoje", "mas", "se", "ele", "ela", "eles", "elas", "isso", "esse", "essa", "este", "esta",
           "para", "por", "com", "como", "quando", "onde", "porque", "cerca", "após", "depois", "antes", "ainda",
           "mais", "menos", "cada", "todo", "toda", "nem", "já", "só", "comenta", "pra", "você", "nós", "sem"}

_PEDIDO = """Uma voz brasileira (pt-BR) vai ler em voz alta os nomes abaixo, que aparecem num vídeo em português.
Para CADA nome, diga como ela deve falar:
- "pt": a leitura brasileira normal já está certa (nome português/brasileiro, nome com forma em português
  como Noruega, Londres, Nova York, ou nome que o Brasil fala do jeito escrito);
- "en", "es", "fr", "de", "it" ou "nl": nome desse idioma, que deve ser falado com a pronúncia nativa;
- uma GRAFIA À PORTUGUESA com acento na sílaba tônica, para nome de outro idioma/alfabeto (norueguês, russo,
  japonês, árabe…) que a voz leria errado — ex.: "Longyearbyen": "Lóngüierbüen", "Dyatlov": "Diátlov".
Sigla que se soletra (ICARDA, ONU) é "pt".
Nomes:
{nomes}
Responda SÓ JSON: {{"nomes": {{"Nome": "pt" | "en" | ... | "Grafia"}}}}"""


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


def nomes_da_narracao(texto: str, comuns: set[str] | None = None) -> list[str]:
    """Nomes próprios candidatos (sem palavra comum de começo de frase), na ordem em que aparecem. Palavra
    que aparece em minúscula no próprio texto (ou em `comuns`) é palavra comum ("Imagine", "Primeira")."""
    minusculas = set(re.findall(r"\b[a-zà-ÿ][\w-]*", texto or "")) | (comuns or set())
    out = []
    for m in _NOME.finditer(texto or ""):
        nome = m.group(0).strip("'’- ")
        palavras = nome.split()
        while palavras and palavras[0].lower() in _COMUNS:
            palavras = palavras[1:]
        nome = " ".join(palavras)
        if len(palavras) == 1 and nome.lower() in minusculas:
            continue
        if len(nome) >= 3 and not nome.isdigit() and nome not in out:
            out.append(nome)
    return out


def _valida(classe) -> str | None:
    c = str(classe or "").strip().strip("\"'")
    if not c or len(c) > 40 or "\n" in c:
        return None
    return c.lower() if c.lower() in ("pt",) + IDIOMAS_NATIVOS else c


def classificar(nomes: list[str]) -> dict[str, str]:
    """Pergunta ao modelo forte os nomes que a tabela não conhece; guarda e devolve {nome: classe}."""
    from .providers import ProvedoresIndisponiveis, complete, is_strong_model
    tabela = _carregar()
    novos = [n for n in dict.fromkeys(nomes) if n not in tabela]
    if not novos:
        return {n: tabela[n] for n in nomes if n in tabela}
    try:
        from .redacao import _obj
        raw = complete([{"role": "user", "content": _PEDIDO.format(nomes="\n".join(f"- {n}" for n in novos))}],
                       max_tokens=1500, temperature=0.0, model_filter=is_strong_model,
                       validate=lambda r: isinstance(_obj(r).get("nomes"), dict))
        respostas = _obj(raw).get("nomes") or {}
    except ProvedoresIndisponiveis:
        raise
    except RuntimeError as exc:
        log.warning("pronúncia: classificação indisponível (%s) — nomes vão sem orientação", exc)
        respostas = {}
    aprendidas = {n: c for n in novos if (c := _valida(respostas.get(n)))}
    if aprendidas:
        with _trava:
            dados = _carregar()
            dados.update(aprendidas)
            _salvar(dados)
        log.info("pronúncia aprendida: %s", aprendidas)
    tabela.update(aprendidas)
    return {n: tabela[n] for n in nomes if n in tabela}


def preparar(script: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(pronuncia, idioma) dos nomes do roteiro, pela tabela central (aprende os que faltam)."""
    texto = " ".join(sc.get("narration", "") for sc in script.get("scenes") or [])
    classes = classificar(nomes_da_narracao(texto))
    pronuncia = {n: c for n, c in classes.items() if c != "pt" and c not in IDIOMAS_NATIVOS}
    idioma = {n: c for n, c in classes.items() if c in IDIOMAS_NATIVOS}
    return pronuncia, idioma


def confirmar_fala(nome: str, grafia: str) -> None:
    """A grafia que a revisão de áudio confirmou entra na tabela (vence a classificação anterior)."""
    if not nome or not grafia or nome.lower() == grafia.lower():
        return
    with _trava:
        dados = _carregar()
        dados[nome] = grafia
        _salvar(dados)
    log.info("pronúncia confirmada pela revisão de áudio: %s -> %s", nome, grafia)
