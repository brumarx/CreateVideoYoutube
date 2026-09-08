"""Geração de imagem via Pollinations — portado de ariaBot/src/x/image.ts.

Dois níveis: com POLLINATIONS_API_KEYS (rodízio entre chaves) tenta o
gateway `gen.pollinations.ai` (modelo `flux`, melhor qualidade); se todas as
chaves falharem (ex.: 402 sem saldo) ou não houver nenhuma configurada, cai
pro endpoint anônimo `image.pollinations.ai` (mais fraco, mas sempre grátis
e sem chave).
"""
from __future__ import annotations

import base64
import logging
import random
import urllib.parse

import httpx

from .config import POLLINATIONS_API_KEYS
from .providers import LLMKeys, _fetch_gemini_models, _rotator_for

log = logging.getLogger("visuals")

MIN_VALID_BYTES = 1000
MAX_IMAGE_ATTEMPTS = 3

# Testei detectar imagem "quase em branco" (aconteceu de verdade: uma cena
# virou um gradiente azul liso) por estatística de pixel (densidade de
# bordas, contraste) — não deu certo: uma foto de céu estrelado escuro (boa,
# de propósito) e a imagem quebrada tinham assinaturas quase idênticas.
# Em vez disso, pergunta pro Gemini (grátis, com visão) se a imagem tem
# conteúdo reconhecível — é mais lento mas confiável de verdade.
_BLANK_CHECK_PROMPT = (
    "Responda só SIM ou NÃO. Esta imagem é quase em branco / um gradiente "
    "liso sem nenhum objeto, cena ou textura reconhecível (ex.: só um "
    "degradê de cor, sem forma nenhuma)?"
)


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


def _try_anonymous(prompt: str, width: int, height: int, seed: int) -> bytes | None:
    encoded = urllib.parse.quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&seed={seed}"
    )
    try:
        resp = httpx.get(url, timeout=90)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("pollinations anônimo falhou: %s", exc)
        return None
    if not _looks_like_image(resp.content):
        log.warning("pollinations anônimo devolveu resposta inválida")
        return None
    return resp.content


_NO_TEXT_SUFFIX = (
    ", no text, no words, no letters, no numbers, no logos, no UI, no buttons, "
    "no watermark, no signs, no signage, no plaques, no banners, no billboards"
)


def _mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF"):
        return "image/webp"
    return "image/jpeg"


def _is_blank(data: bytes) -> bool:
    """Pergunta pro Gemini se a imagem parece quase em branco. Se não
    houver chave configurada ou a chamada falhar, deixa passar (não bloqueia
    o pipeline por causa de uma checagem opcional)."""
    keys = LLMKeys().gemini
    if not keys:
        return False

    rotator = _rotator_for(keys)
    image_b64 = base64.b64encode(data).decode()
    for api_key in rotator.order():
        models = _fetch_gemini_models(api_key)
        for model in models:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            )
            body = {
                "contents": [{
                    "parts": [
                        {"text": _BLANK_CHECK_PROMPT},
                        {"inline_data": {"mime_type": _mime_type(data), "data": image_b64}},
                    ]
                }]
            }
            try:
                resp = httpx.post(url, json=body, timeout=30)
                if resp.status_code != 200:
                    log.warning("checagem de imagem (gemini/%s) -> HTTP %s", model, resp.status_code)
                    continue
                answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
                return answer.startswith("SIM")
            except Exception as exc:
                log.warning("checagem de imagem (gemini/%s) falhou: %s", model, exc)
                continue
    return False


def _fetch_once(prompt: str, width: int, height: int, seed: int) -> bytes | None:
    data = _try_keyed(prompt, width, height, seed)
    if data:
        return data
    log.info("usando endpoint anônimo do Pollinations (sem chave ou chaves falharam)")
    return _try_anonymous(prompt, width, height, seed)


def generate_image(prompt: str, width: int = 1024, height: int = 1024, seed: int | None = None) -> bytes:
    """Gera uma imagem a partir do prompt e devolve os bytes (png/jpeg/webp).

    Sempre reforça "sem texto/logo/UI" no fim do prompt — mesmo se quem
    chamou já devia ter evitado isso, todo modelo de imagem atual erra
    muito ao tentar renderizar texto legível, e imitar logos de marca (ex.
    botão do YouTube) é um risco à parte.

    Se a imagem sair "quase em branco", tenta de novo com outra seed antes
    de desistir — já aconteceu em produção.
    """
    prompt = f"{prompt}{_NO_TEXT_SUFFIX}"
    base_seed = seed if seed is not None else random.randint(0, 2**31 - 1)

    best: bytes | None = None
    for attempt in range(MAX_IMAGE_ATTEMPTS):
        attempt_seed = base_seed if attempt == 0 else random.randint(0, 2**31 - 1)
        data = _fetch_once(prompt, width, height, attempt_seed)
        if data is None:
            log.warning("falha ao buscar imagem (tentativa %d/%d) — tentando de novo", attempt + 1, MAX_IMAGE_ATTEMPTS)
            continue
        if not _is_blank(data):
            return data
        log.warning("imagem quase em branco (tentativa %d/%d) — tentando de novo", attempt + 1, MAX_IMAGE_ATTEMPTS)
        best = best or data

    if best is None:
        raise RuntimeError("não foi possível gerar imagem após todas as tentativas (Pollinations indisponível)")
    log.warning("todas as tentativas saíram quase em branco — usando a última mesmo assim")
    return best
