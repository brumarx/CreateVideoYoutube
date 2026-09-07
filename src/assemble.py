"""Monta o vídeo final: para cada cena, imagem (efeito Ken Burns) + narração;
concatena tudo em um mp4. Sem legenda embutida (removida a pedido — cobria
demais a imagem). Só usa ffmpeg via subprocess — sem moviepy.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920  # formato vertical (Shorts/Reels-friendly); mude se quiser 16:9


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def render_scene(image_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Renderiza uma cena: zoom lento na imagem, sincronizado com a duração
    do áudio."""
    duration = _ffprobe_duration(audio_path)
    fps = 30
    frames = max(int(duration * fps), 1)

    upscale_w, upscale_h = VIDEO_WIDTH * 2, VIDEO_HEIGHT * 2
    filter_complex = (
        # crop-to-fill em vez de esticar: sem distorção mesmo se a imagem
        # gerada não vier exatamente 9:16.
        f"scale={upscale_w}:{upscale_h}:force_original_aspect_ratio=increase,"
        f"crop={upscale_w}:{upscale_h},"
        f"zoompan=z='min(zoom+0.0007,1.3)':d={frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={fps}"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(image_path),
            "-i", str(audio_path),
            "-filter:v", filter_complex,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            "-shortest",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return output_path


def concat_scenes(scene_paths: list[Path], output_path: Path) -> Path:
    """Concatena os mp4 de cada cena num vídeo final único."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.with_suffix(".txt")
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in scene_paths))

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
    list_file.unlink()
    return output_path
