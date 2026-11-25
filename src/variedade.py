"""Variedade entre vídeos do mesmo canal (25/11 — política do YouTube 2026 contra conteúdo "produzido em
massa"): canal em que todo vídeo tem a mesma música, os mesmos clipes de banco e o mesmo subtema na mesma
semana tem cara de fábrica.

- música: rodízio pela que foi usada há MAIS tempo no canal (antes era sorteio: a mesma faixa saía em
  vídeos seguidos);
- clipe/foto de banco (Pexels, Pixabay, Coverr, Smithsonian, Met): o mesmo item não volta nos últimos
  JANELA_MIDIA vídeos do canal. O "recente" de src/stock_media.py só PREFERE o inédito (e é global, sem
  canal); aqui é bloqueio;
- crédito de cada item de banco usado no vídeo, para a descrição (autor, banco, licença, link);
- subtema: helper `subtema_repetido` (mesmo assunto em 14 dias), para quem escolhe o tema.

Estado em data/variedade.json, por canal: [{video, em, musica, midias: [chave]}], os mais novos no fim.
O vídeo em montagem é marcado por `iniciar_video(canal, video)` (contextvar): as buscas de banco não
precisam receber canal/vídeo por parâmetro. Sem vídeo iniciado (testes, ferramentas avulsas), nada é
bloqueado nem gravado."""
from __future__ import annotations

import contextvars
import fcntl
import json
import logging
import os
import random
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("variedade")

HISTORICO = Path(__file__).resolve().parent.parent / "data" / "variedade.json"
JANELA_MIDIA = 30      # vídeos do canal em que o mesmo clipe/foto de banco não volta
MAX_VIDEOS = 60        # vídeos guardados por canal (o dobro da janela)
DIAS_SUBTEMA = 14

_atual: contextvars.ContextVar[dict | None] = contextvars.ContextVar("variedade_video", default=None)


# ---------- estado
@contextmanager
def _travado():
    """Canais rodam em processos separados (painel, daily_run): trava o arquivo durante ler+gravar."""
    HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORICO.with_suffix(".lock"), "w") as trava:
        fcntl.flock(trava, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(trava, fcntl.LOCK_UN)


def _ler() -> dict[str, list[dict]]:
    try:
        dados = json.loads(HISTORICO.read_text())
        return dados if isinstance(dados, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _gravar(dados: dict) -> None:
    tmp = HISTORICO.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1))
    os.replace(tmp, HISTORICO)


def _entrada(dados: dict, canal: str, video: str) -> dict:
    videos = dados.setdefault(canal, [])
    for e in videos:
        if e.get("video") == video:
            return e
    e = {"video": video, "em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "musica": None, "midias": []}
    videos.append(e)
    del videos[:-MAX_VIDEOS]
    return e


def _outros(canal: str, video: str) -> list[dict]:
    """Histórico do canal sem o próprio vídeo (refazer o mesmo job pode repetir o que ele já usou)."""
    return [e for e in _ler().get(canal, []) if e.get("video") != video]


# ---------- vídeo em montagem
def iniciar_video(canal: str, video: str | int) -> None:
    """Marca o vídeo em montagem (chamado pelo pipeline antes de buscar imagens)."""
    _atual.set({"canal": canal, "video": str(video), "creditos": []})


def encerrar_video() -> None:
    _atual.set(None)


def midia_bloqueada(chave: str) -> bool:
    """O item de banco saiu num dos últimos JANELA_MIDIA vídeos do canal (sem vídeo iniciado: nunca)."""
    atual = _atual.get()
    if not atual:
        return False
    recentes = _outros(atual["canal"], atual["video"])[-JANELA_MIDIA:]
    return any(chave in (e.get("midias") or []) for e in recentes)


def registrar_midia(chave: str, credito: str | None = None) -> None:
    """Item de banco escolhido para o vídeo em montagem: guarda no histórico e o crédito para a descrição."""
    atual = _atual.get()
    if not atual:
        return
    if credito and credito not in atual["creditos"]:
        atual["creditos"].append(credito)
    try:
        with _travado():
            dados = _ler()
            e = _entrada(dados, atual["canal"], atual["video"])
            if chave not in e["midias"]:
                e["midias"].append(chave)
            _gravar(dados)
    except OSError as exc:  # variedade é proteção extra: nunca derruba o vídeo
        log.warning("histórico de variedade não gravado: %s", exc)


def creditos_do_video() -> list[str]:
    atual = _atual.get()
    return list(atual["creditos"]) if atual else []


def credito(autor: str | None, banco: str, licenca: str, link: str | None) -> str:
    """Linha de crédito sem vírgula (a descrição tem limite de vírgulas, ver qualidade_texto)."""
    autor = re.sub(r"\s+", " ", str(autor or "").replace(",", " ")).strip()
    base = f"{autor} — {banco}" if autor else banco
    return f"{base} ({licenca})" + (f": {link}" if link else "")


# ---------- música
def escolher_musica(canal: str, video: str | int, faixas: list[Path], rng: random.Random | None = None) -> Path:
    """Rodízio: a faixa usada há mais tempo no canal (nunca usada vem antes; empate sorteado). Grava a
    escolha no vídeo: a remontagem do mesmo vídeo (troca de imagem) mantém a mesma faixa."""
    rng = rng or random
    video = str(video)
    if not faixas:
        raise ValueError("sem faixas")
    try:
        with _travado():
            dados = _ler()
            ja = next((e.get("musica") for e in dados.get(canal, []) if e.get("video") == video), None)
            escolhida = next((f for f in faixas if f.name == ja), None)
            if escolhida is None:
                ultimo_uso = {e.get("musica"): n for n, e in enumerate(dados.get(canal, [])) if e.get("musica")}
                menor = min(ultimo_uso.get(f.name, -1) for f in faixas)
                escolhida = rng.choice([f for f in faixas if ultimo_uso.get(f.name, -1) == menor])
                _entrada(dados, canal, video)["musica"] = escolhida.name
                _gravar(dados)
            return escolhida
    except OSError as exc:
        log.warning("rodízio de música indisponível (%s): sorteio", exc)
        return rng.choice(faixas)


# ---------- subtema
_PARADA = set("""a o as os um uma uns umas de da do das dos em no na nos nas por para pra pelo pela pelos pelas com
sem que e ou se ao aos à às como mais menos muito muita seu sua seus suas é foi ser são era quem qual quais
quando onde porque por que isso esse essa este esta the of and in to""".split())


def _palavras(texto: str) -> set[str]:
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {p[:6] for p in re.findall(r"[a-z0-9]+", t) if p not in _PARADA and len(p) > 2}


def subtema_repetido(topico: str, recentes: list[tuple[str, str]], dias: int = DIAS_SUBTEMA,
                     agora: datetime | None = None, limiar: float = 0.5) -> str | None:
    """O tema recente (dentro de `dias`) com o mesmo subtema, ou None. `recentes` = [(tema, data ISO)].
    Mesmo subtema = metade ou mais das palavras de conteúdo do menor dos dois em comum (radical de 6
    letras: 'vulcão'/'vulcões' contam juntos)."""
    agora = agora or datetime.now(timezone.utc)
    p = _palavras(topico)
    if not p:
        return None
    for tema, quando in recentes:
        try:
            dt = datetime.fromisoformat(str(quando))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if agora - dt > timedelta(days=dias):
            continue
        q = _palavras(tema)
        if q and len(p & q) / min(len(p), len(q)) >= limiar:
            return tema
    return None


def subtema_repetido_no_canal(canal: str, topico: str, dias: int = DIAS_SUBTEMA) -> str | None:
    """`subtema_repetido` contra os vídeos publicados do canal (fila em data/queue.db)."""
    from .orchestrator import published_jobs

    recentes = [(j.topic, j.updated_at or j.created_at or "") for j in published_jobs() if j.channel == canal]
    return subtema_repetido(topico, recentes, dias)
