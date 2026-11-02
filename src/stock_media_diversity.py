"""Melhor diversidade de stock footage entre vídeos — evita repetição.

Mantém histórico cruzado de 7 dias, não só de 1 vídeo. Também ordena por:
1. Relevância (já faz)
2. Recência (clips novos do Pexels/Pixabay)
3. Distância de cor (evita vídeo monocromático)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("stock_media_diversity")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RECENT_FILE_7DAYS = DATA_DIR / "stock_media_recent_7days.json"


def _hue_from_hex(hex_color: str) -> float:
    """Converte cor hex (#RRGGBB) para matiz (0-360 graus)."""
    try:
        hex_color = hex_color.lstrip("#")
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)

        r, g, b = r / 255.0, g / 255.0, b / 255.0
        max_val = max(r, g, b)
        min_val = min(r, g, b)
        delta = max_val - min_val

        if delta == 0:
            hue = 0
        elif max_val == r:
            hue = 60 * (((g - b) / delta) % 6)
        elif max_val == g:
            hue = 60 * (((b - r) / delta) + 2)
        else:
            hue = 60 * (((r - g) / delta) + 4)

        return hue % 360
    except Exception:
        return 0


def _color_distance(hue1: float, hue2: float) -> float:
    """Distância entre dois matizes no círculo cromático (0-180)."""
    diff = abs(hue1 - hue2)
    return min(diff, 360 - diff)


def load_recent_media() -> dict[str, Any]:
    """Carrega histórico de 7 dias de stock media usado."""
    if not RECENT_FILE_7DAYS.exists():
        return {"clips": {}, "photos": {}, "updated_at": None}

    with open(RECENT_FILE_7DAYS) as f:
        data = json.load(f)

    # Limpar entradas com mais de 7 dias
    if data.get("updated_at"):
        cutoff = datetime.fromisoformat(data["updated_at"]) - timedelta(days=7)
        for key in ["clips", "photos"]:
            for media_id in list(data[key].keys()):
                if datetime.fromisoformat(data[key][media_id].get("used_at", "2026-01-01")) < cutoff:
                    del data[key][media_id]

    return data


def save_recent_media(data: dict[str, Any]) -> None:
    """Salva histórico de stock media em uso."""
    RECENT_FILE_7DAYS.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    with open(RECENT_FILE_7DAYS, "w") as f:
        json.dump(data, f, indent=2)


def mark_used(media_id: str, media_type: str, dominant_color: str = "#808080") -> None:
    """Marca um clip/foto como usado (para evitar repetição)."""
    data = load_recent_media()
    key_type = "clips" if media_type == "video" else "photos"
    data[key_type][media_id] = {
        "used_at": datetime.now().isoformat(),
        "color_hue": _hue_from_hex(dominant_color),
    }
    save_recent_media(data)


def score_candidates(
    candidates: list[dict],
    relevance_scores: list[float],
    dominant_colors: list[str],
    media_type: str = "video",
) -> list[tuple[int, float]]:
    """
    Ordena candidatos por: relevância (30%) + recência (30%) + distância de cor (40%).

    Retorna lista de (índice, score) ordenada descrescente.
    """
    recent = load_recent_media()
    key_type = "clips" if media_type == "video" else "photos"
    recent_media = recent.get(key_type, {})

    # Calcular recência (0-1, onde 1 é novo)
    recency_scores = []
    for candidate in candidates:
        media_id = candidate.get("id", "unknown")
        if media_id in recent_media:
            # Já foi usado — penalizar
            used_at = datetime.fromisoformat(recent_media[media_id]["used_at"])
            age_days = (datetime.now() - used_at).days
            recency = max(0, 1 - (age_days / 7))  # 7 dias = 0
        else:
            recency = 1.0  # Novo, máximo score
        recency_scores.append(recency)

    # Calcular distância de cor (quanto mais diferente do último usado, melhor)
    color_distance_scores = []
    if recent_media:
        last_used = max(recent_media.values(), key=lambda x: x["used_at"])
        last_hue = last_used.get("color_hue", 0)

        for dominant_color in dominant_colors:
            current_hue = _hue_from_hex(dominant_color)
            dist = _color_distance(last_hue, current_hue)
            # Normalizar 0-180 para 0-1
            color_score = dist / 180.0
            color_distance_scores.append(color_score)
    else:
        color_distance_scores = [0.5] * len(candidates)  # Sem histórico, neutro

    # Combinar scores (relevância 30%, recência 30%, cor 40%)
    combined_scores = []
    for i in range(len(candidates)):
        relevance = (relevance_scores[i] if i < len(relevance_scores) else 0.5) / 10.0  # normalizar
        recency = recency_scores[i]
        color = color_distance_scores[i]

        combined = (0.3 * relevance) + (0.3 * recency) + (0.4 * color)
        combined_scores.append((i, combined))

    # Retornar ordenado por score (descrescente)
    return sorted(combined_scores, key=lambda x: x[1], reverse=True)


def filter_used_candidates(
    candidates: list[dict],
    media_type: str = "video",
    allow_same_video: bool = False,
) -> list[dict]:
    """Remove candidatos que foram usados nos últimos 7 dias (se allow_same_video=False)."""
    if allow_same_video:
        return candidates

    recent = load_recent_media()
    key_type = "clips" if media_type == "video" else "photos"
    recent_media = recent.get(key_type, {})

    filtered = []
    for candidate in candidates:
        media_id = candidate.get("id", "unknown")
        if media_id not in recent_media:
            filtered.append(candidate)

    return filtered or candidates  # Se tudo foi usado, retornar tudo (fallback)
