"""Confere, com um modelo de visão (Gemini), se a imagem/clipe escolhido
combina com o que a cena NARRA antes de entrar no vídeo.

Motivo (vídeos de 03/10): a escolha do banco (Pexels) era cega — só contava
palavras da busca no nome do arquivo — e saíram estádio do Wolfsburg, camisa
do Beşiktaş, escudo do Barcelona, futebol americano e um papel em branco na
parede ilustrando "o escudo" num vídeo do Botafogo.

Devolve True (combina), False (fora de contexto) ou None (checagem
indisponível: sem chave, cota, rede) — quem chama decide o que fazer com
None; nunca levanta exceção.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import subprocess
from pathlib import Path

import httpx
from PIL import Image

from .config import LLMKeys
from .providers import _rotator_for

log = logging.getLogger("visual_check")

# lite primeiro (cota diária maior no free tier), flash se ele falhar
_MODELS = ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-flash-latest")
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_RULES = """Responda NÃO (ok=false) se a imagem:
- mostra OUTRO ESPORTE do que o narrado. "Futebol" é futebol de campo
  (soccer, bola redonda no pé): futebol americano (capacete, bola oval),
  rugby, basquete etc. é SEMPRE fora de contexto num vídeo de futebol;
- mostra clube, time, escudo, camisa ou torcida IDENTIFICÁVEL diferente do
  que a cena cita (ex.: camisa do Barcelona num vídeo do Botafogo);
- mostra pessoa/lugar/objeto específico que contradiz a narração, ou um
  assunto sem relação nenhuma com ela (ex.: papel em branco pra "escudo");
- tem texto, logo ou marca em destaque que não tem a ver com a cena.
Imagem genérica mas coerente com o assunto e o clima da cena (estádio sem
clube identificável, bola, torcida genérica, pessoa num escritório numa
cena sobre trabalho) está OK."""


def _jpeg(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def frames_from_clip(path: Path) -> bytes | None:
    """2 frames do clipe (começo e meio) lado a lado — clipe de banco muda
    de plano, um frame só deixava passar a metade errada."""
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or 0)
        frames = []
        for t in (min(1.0, dur * 0.1), dur * 0.55):
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                 "-vf", "scale=640:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                capture_output=True, timeout=60,
            ).stdout
            if out:
                frames.append(Image.open(io.BytesIO(out)))
        if not frames:
            return None
        sheet = Image.new("RGB", (sum(f.width for f in frames) + 10 * (len(frames) - 1), max(f.height for f in frames)))
        x = 0
        for f in frames:
            sheet.paste(f, (x, 0))
            x += f.width + 10
        return _jpeg(sheet)
    except Exception as exc:  # noqa: BLE001
        log.warning("não deu pra extrair frame do clipe: %s", exc)
        return None


def matches_scene(image: bytes, narration: str, context: str) -> bool | None:
    keys = LLMKeys().gemini
    if not keys or not image:
        return None
    try:
        data = base64.b64encode(_jpeg(Image.open(io.BytesIO(image)))).decode()
    except Exception:  # noqa: BLE001 — imagem ilegível: não usa
        return False
    prompt = (
        f"Você confere as imagens de um vídeo do YouTube antes de publicar.\n{context}\n\n"
        f'Narração DESTA cena: "{narration}"\n\n'
        "A imagem (pode ter 2 frames lado a lado do mesmo clipe) pode ilustrar esta cena "
        f"sem sair do contexto?\n{_RULES}\n\n"
        'Responda SÓ JSON: {"ok": true ou false, "motivo": "1 frase"}'
    )
    body = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": data}}]}],
        "generationConfig": {"temperature": 0},
    }
    rotator = _rotator_for(keys)
    for model in _MODELS:
        for key in rotator.order():
            try:
                resp = httpx.post(_URL.format(model=model), params={"key": key}, json=body, timeout=60)
                if resp.status_code in (401, 403):
                    rotator.ban(key)
                    continue
                if resp.status_code != 200:
                    continue  # 429/503: próxima chave/modelo
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{.*\}", text, re.DOTALL)
                verdict = json.loads(m.group(0)) if m else {}
            except Exception as exc:  # noqa: BLE001
                log.warning("checagem visual (%s) falhou: %s", model, exc)
                continue
            if isinstance(verdict.get("ok"), bool):
                if not verdict["ok"]:
                    log.info("imagem fora de contexto: %s", verdict.get("motivo"))
                return verdict["ok"]
    log.warning("checagem visual indisponível (todas as chaves/modelos falharam)")
    return None
