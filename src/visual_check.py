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
import time
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
- tem texto, logo ou marca em destaque que não tem a ver com a cena;
- mostra um jogador/atleta em close, de uniforme com escudo (real ou
  imitação) — num vídeo de notícia parece um jogador de verdade do clube
  que não existe (imagem de IA de "jogador do Botafogo" é sempre NÃO);
- mostra dinheiro de outro país identificável (nota de dólar, euro) numa
  cena sobre dinheiro brasileiro, reais ou gasto público do Brasil;
- mostra o ROSTO de uma pessoa em destaque numa cena que cita uma pessoa
  real pelo nome (político, jogador, técnico) — quem assiste acha que é
  ela, e não é. Silhueta, mãos ou pessoa de costas estão OK.
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


# modelo/chave sem cota (429) ou travado (timeout) fica de fora por um tempo
# no processo: sem isso CADA imagem esperava 3 timeouts de 2 min do
# gemini-flash-latest + 429 de todas as chaves (07/10: ~6 min por imagem,
# render de 27 cenas parado a manhã inteira)
_TIMEOUT = 45
_COOLDOWN_429 = 15 * 60
_COOLDOWN_TIMEOUT = 30 * 60
_cooldown: dict[str, float] = {}


def _resting(*names: str) -> bool:
    now = time.monotonic()
    return any(_cooldown.get(n, 0) > now for n in names)


def _rest(name: str, seconds: int) -> None:
    _cooldown[name] = time.monotonic() + seconds


def _gemini(prompt: str, jpeg: bytes) -> str | None:
    keys = LLMKeys().gemini
    if not keys:
        return None
    body = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"temperature": 0},
    }
    rotator = _rotator_for(keys)
    for model in _MODELS:
        for key in rotator.order():
            slot = f"gemini:{model}:{key[-6:]}"
            if _resting(f"gemini:{model}", slot):
                continue
            try:
                resp = httpx.post(_URL.format(model=model), headers={"x-goog-api-key": key}, json=body, timeout=_TIMEOUT)
                if resp.status_code in (401, 403):
                    rotator.ban(key)
                    continue
                if resp.status_code == 429:
                    _rest(slot, _COOLDOWN_429)
                    continue
                if resp.status_code == 200:
                    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            except httpx.TimeoutException:
                log.warning("visão gemini (%s) sem resposta em %ds — fora por %d min", model, _TIMEOUT, _COOLDOWN_TIMEOUT // 60)
                _rest(f"gemini:{model}", _COOLDOWN_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                log.warning("visão gemini (%s) falhou: %s", model, exc)
    return None


# reserva quando a cota grátis do Gemini acaba (aconteceu em 03/10 à tarde,
# as 3 chaves com 429 — a checagem visual ficou desligada e passou vídeo
# com notícia do BAIRRO Botafogo e a mesma imagem em 9 de 16 cenas)
_OPENAI_COMPAT_VISION = [
    ("mistral", "https://api.mistral.ai/v1/chat/completions", ["mistral-small-latest"]),
    # Groq grátis: o qwen3.8 aceita imagem (testado 06/10, leu o texto da
    # thumbnail) — divide a cota diária com o texto
    ("groq", "https://api.groq.com/openai/v1/chat/completions", ["qwen/qwen3.8-27b"]),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", ["google/gemma-4-31b-it:free", "qwen/qwen3.8-27b:free"]),
]


def _openai_compat(prompt: str, jpeg: bytes) -> str | None:
    llm = LLMKeys()
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    for provider, url, models in _OPENAI_COMPAT_VISION:
        keys = getattr(llm, provider)
        if not keys:
            continue
        rotator = _rotator_for(keys)
        for model in models:
            for key in rotator.order():
                slot = f"{provider}:{model}:{key[-6:]}"
                if _resting(f"{provider}:{model}", slot):
                    continue
                try:
                    resp = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=_TIMEOUT, json={
                        "model": model, "temperature": 0,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ]}],
                    })
                    if resp.status_code in (401, 403):
                        rotator.ban(key)
                        continue
                    if resp.status_code == 429:
                        _rest(slot, _COOLDOWN_429)
                        continue
                    if resp.status_code == 200:
                        content = resp.json()["choices"][0]["message"]["content"]
                        if content:
                            return content
                except httpx.TimeoutException:
                    log.warning("visão %s (%s) sem resposta em %ds — fora por %d min", provider, model, _TIMEOUT, _COOLDOWN_TIMEOUT // 60)
                    _rest(f"{provider}:{model}", _COOLDOWN_TIMEOUT)
                except Exception as exc:  # noqa: BLE001
                    log.warning("visão %s (%s) falhou: %s", provider, model, exc)
    return None


def ask_vision(prompt: str, jpeg: bytes) -> str | None:
    """Resposta de texto de um modelo com visão: Gemini, Mistral, Groq,
    OpenRouter grátis e Cloudflare. None se nenhum respondeu."""
    return _gemini(prompt, jpeg) or _openai_compat(prompt, jpeg) or _cloudflare_vision(prompt, jpeg)


def _cloudflare_vision(prompt: str, jpeg: bytes) -> str | None:
    from .providers import CLOUDFLARE_CHAT_URL

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    for entry in LLMKeys().cloudflare:
        account, _, token = entry.partition(":")
        if not token:
            continue
        try:
            resp = httpx.post(
                CLOUDFLARE_CHAT_URL.format(account=account), headers={"Authorization": f"Bearer {token}"}, timeout=120,
                json={"model": "@cf/mistralai/mistral-small-3.1-24b-instruct", "temperature": 0, "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}},
                    ]},
                ]},
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                if content:
                    return content
        except Exception as exc:  # noqa: BLE001
            log.warning("visão cloudflare falhou: %s", exc)
    return None


def matches_scene(image: bytes, narration: str, context: str) -> bool | None:
    if not image:
        return None
    try:
        jpeg = _jpeg(Image.open(io.BytesIO(image)))
    except Exception:  # noqa: BLE001 — imagem ilegível: não usa
        return False
    prompt = (
        f"Você confere as imagens de um vídeo do YouTube antes de publicar.\n{context}\n\n"
        f'Narração DESTA cena: "{narration}"\n\n'
        "A imagem (pode ter 2 frames lado a lado do mesmo clipe) pode ilustrar esta cena "
        f"sem sair do contexto?\n{_RULES}\n\n"
        'Responda SÓ JSON: {"ok": true ou false, "motivo": "1 frase"}'
    )
    text = ask_vision(prompt, jpeg)
    if text is None:
        log.warning("checagem visual indisponível (nenhum provedor de visão respondeu)")
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        verdict = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(verdict.get("ok"), bool):
        return None
    if not verdict["ok"]:
        log.info("imagem fora de contexto: %s", verdict.get("motivo"))
    return verdict["ok"]
