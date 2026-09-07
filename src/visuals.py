"""Geração de imagem via Pollinations — portado de ariaBot/src/x/image.ts.

Dois níveis: com POLLINATIONS_API_KEYS (rodízio entre chaves) tenta o
gateway `gen.pollinations.ai` (modelo `flux`, melhor qualidade); se todas as
chaves falharem (ex.: 402 sem saldo) ou não houver nenhuma configurada, cai
pro endpoint anônimo `image.pollinations.ai` (mais fraco, mas sempre grátis
e sem chave).
"""
from __future__ import annotations

import logging
import random
import urllib.parse

import httpx

from .config import POLLINATIONS_API_KEYS
from .providers import _rotator_for

log = logging.getLogger("visuals")

MIN_VALID_BYTES = 1000


def _looks_like_image(data: bytes) -> bool:
    if len(data) < MIN_VALID_BYTES:
        return False
    return (
        data.startswith(b"\x89PNG")
        or data.startswith(b"RIFF")  # webp
        or data.startswith(b"\xff\xd8")  # jpeg
    )


def _try_keyed(prompt: str, width: int, height: int, seed: int) -> bytes | None:
    if not POLLINATIONS_API_KEYS:
        return None
    rotator = _rotator_for(POLLINATIONS_API_KEYS)
    encoded = urllib.parse.quote(prompt)
    for api_key in rotator.order():
        url = (
            f"https://gen.pollinations.ai/image/{encoded}"
            f"?width={width}&height={height}&nologo=true&model=flux&seed={seed}"
        )
        try:
            resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=90)
            if resp.status_code == 200 and _looks_like_image(resp.content):
                return resp.content
            if resp.status_code in (401, 403):
                rotator.ban(api_key)
            else:
                log.warning("pollinations keyed -> HTTP %s", resp.status_code)
        except Exception as exc:
            log.warning("pollinations keyed falhou: %s", exc)
    return None


def _try_anonymous(prompt: str, width: int, height: int, seed: int) -> bytes:
    encoded = urllib.parse.quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&seed={seed}"
    )
    resp = httpx.get(url, timeout=90)
    resp.raise_for_status()
    if not _looks_like_image(resp.content):
        raise RuntimeError("Pollinations (anônimo) devolveu resposta inválida")
    return resp.content


_NO_TEXT_SUFFIX = (
    ", no text, no words, no letters, no numbers, no logos, no UI, no buttons, "
    "no watermark, no signs, no signage, no plaques, no banners, no billboards"
)


def generate_image(prompt: str, width: int = 1024, height: int = 1024, seed: int | None = None) -> bytes:
    """Gera uma imagem a partir do prompt e devolve os bytes (png/jpeg/webp).

    Sempre reforça "sem texto/logo/UI" no fim do prompt — mesmo se quem
    chamou já devia ter evitado isso, todo modelo de imagem atual erra
    muito ao tentar renderizar texto legível, e imitar logos de marca (ex.
    botão do YouTube) é um risco à parte.
    """
    prompt = f"{prompt}{_NO_TEXT_SUFFIX}"
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    data = _try_keyed(prompt, width, height, seed)
    if data:
        return data
    log.info("usando endpoint anônimo do Pollinations (sem chave ou chaves falharam)")
    return _try_anonymous(prompt, width, height, seed)
