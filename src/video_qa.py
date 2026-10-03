"""Revisão do vídeo PRONTO, antes do upload — a última barreira.

Motivo (03/10): 16 vídeos apagados do YouTube depois de publicados —
narração com as mesmas 3 frases repetidas 5 vezes (job 225), a 2ª metade
igual à 1ª (job 222), escudo do Barcelona e a mesma foto 20 vezes (193),
hóquei e futebol americano num vídeo do Botafogo (201). A checagem de cada
imagem na hora da escolha (src/visual_check.py) pega a maioria; esta olha o
vídeo inteiro montado, cena por cena, como quem assiste.

review_video() devolve a lista de problemas — vazia = pode subir. Nunca
levanta exceção: checagem indisponível (sem chave/cota do Gemini) só pula
a parte visual e loga.
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
from PIL import Image, ImageDraw, ImageFont

from .config import LLMKeys
from .providers import _rotator_for
from .visual_check import _MODELS, _RULES, _URL

log = logging.getLogger("video_qa")

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# mesma imagem em mais da metade das cenas (e em pelo menos 5) = vídeo
# "parado" (job 193: a mesma foto em 20 de 23 cenas)
_MAX_SAME_VISUAL_SHARE = 0.5
_MIN_SAME_VISUAL_SCENES = 5
_BATCH = 8  # cenas por chamada de visão


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def repeated_sentences(narrations: list[str]) -> list[str]:
    seen: set[str] = set()
    dup: list[str] = []
    for text in narrations:
        for sentence in _SENTENCE_SPLIT.split(text):
            key = _norm(sentence)
            if len(key.split()) < 6:
                continue
            if key in seen and sentence.strip() not in dup:
                dup.append(sentence.strip())
            seen.add(key)
    return dup


def _frame(video: Path) -> Image.Image | None:
    """Frame do meio da cena, sem a faixa da legenda (rodapé)."""
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or 0)
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{dur / 2:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", "crop=iw:ih*0.78:0:0,scale=480:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, timeout=60,
        ).stdout
        return Image.open(io.BytesIO(out)).convert("RGB") if out else None
    except Exception as exc:  # noqa: BLE001
        log.warning("frame da cena %s indisponível: %s", video.name, exc)
        return None


def _ahash(img: Image.Image) -> int:
    small = img.convert("L").resize((8, 8))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def same_visual_share(frames: list[Image.Image | None]) -> tuple[int, int]:
    """(maior grupo de cenas com a mesma imagem, total de cenas com frame)."""
    hashes = [_ahash(f) for f in frames if f is not None]
    best = 0
    for h in hashes:
        best = max(best, sum(1 for o in hashes if bin(h ^ o).count("1") <= 6))
    return best, len(hashes)


def _sheet(frames: list[tuple[int, Image.Image]]) -> bytes:
    cols = 4
    w, h = 480, max(f.height for _, f in frames)
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), "black")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(_FONT, 36)
    for k, (num, f) in enumerate(frames):
        x, y = (k % cols) * w, (k // cols) * h
        sheet.paste(f, (x, y))
        draw.rectangle([x, y, x + 70, y + 46], fill="black")
        draw.text((x + 8, y + 4), str(num), fill="yellow", font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _vision_batch(frames: list[tuple[int, Image.Image]], narrations: dict[int, str], context: str) -> list[dict] | None:
    keys = LLMKeys().gemini
    if not keys:
        return None
    falas = "\n".join(f'Cena {n}: "{narrations[n]}"' for n, _ in frames)
    prompt = (
        f"Você revisa um vídeo do YouTube antes de publicar.\n{context}\n\n"
        "A imagem tem um frame de cada cena, com o NÚMERO da cena no canto. "
        f"Narração de cada cena:\n{falas}\n\n"
        f"Para cada cena, o frame ilustra a narração sem sair do contexto?\n{_RULES}\n\n"
        'Responda SÓ JSON: {"cenas": [{"cena": numero, "ok": true ou false, "motivo": "1 frase"}]}'
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(_sheet(frames)).decode()}},
        ]}],
        "generationConfig": {"temperature": 0},
    }
    rotator = _rotator_for(keys)
    for model in _MODELS:
        for key in rotator.order():
            try:
                resp = httpx.post(_URL.format(model=model), params={"key": key}, json=body, timeout=120)
                if resp.status_code in (401, 403):
                    rotator.ban(key)
                    continue
                if resp.status_code != 200:
                    continue
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{.*\}", text, re.DOTALL)
                cenas = json.loads(m.group(0)).get("cenas") if m else None
                if isinstance(cenas, list):
                    return cenas
            except Exception as exc:  # noqa: BLE001
                log.warning("revisão visual (%s) falhou: %s", model, exc)
    return None


def review_video(narrations: list[str], scene_videos: list[Path], context: str) -> list[str]:
    problems: list[str] = []

    dup = repeated_sentences(narrations)
    if dup:
        problems.append(f"narração repete {len(dup)} frase(s): \"{dup[0][:80]}\"")

    frames = [_frame(v) for v in scene_videos]
    biggest, total = same_visual_share(frames)
    if total and biggest >= _MIN_SAME_VISUAL_SCENES and biggest / total > _MAX_SAME_VISUAL_SHARE:
        problems.append(f"a mesma imagem aparece em {biggest} de {total} cenas")

    numbered = [(i + 1, f) for i, f in enumerate(frames) if f is not None]
    by_num = {i + 1: t for i, t in enumerate(narrations)}
    for start in range(0, len(numbered), _BATCH):
        verdicts = _vision_batch(numbered[start:start + _BATCH], by_num, context)
        if verdicts is None:
            log.warning("revisão visual indisponível pras cenas %d+ — seguindo sem ela", start + 1)
            continue
        for v in verdicts:
            if isinstance(v, dict) and v.get("ok") is False:
                problems.append(f"cena {v.get('cena')} fora de contexto: {v.get('motivo')}")
    return problems
