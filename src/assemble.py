"""Monta o vídeo final: para cada cena, imagem (efeito Ken Burns) + narração;
concatena tudo em um mp4 e mistura música de fundo em volume baixo. Sem
legenda embutida (removida a pedido — cobria demais a imagem). Só usa
ffmpeg via subprocess — sem moviepy.
"""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

# Formato padrão (Shorts/Reels, vertical). Vídeos longos passam
# width/height explícitos pra render_scene (16:9).
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"
MUSIC_VOLUME_DB = -23  # bem baixo — não pode competir com a narração


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def render_scene(
    image_path: Path,
    audio_path: Path,
    output_path: Path,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
) -> Path:
    """Renderiza uma cena: zoom lento na imagem, sincronizado com a duração
    do áudio. `width`/`height` permitem vertical (Shorts, padrão) ou
    horizontal (vídeo longo tipo documentário)."""
    duration = _ffprobe_duration(audio_path)
    fps = 30
    frames = max(int(duration * fps), 1)

    # 1.5x (não 2x) já dá supersampling suficiente pro zoom máximo de 1.3x
    # (1.5/1.3 ainda sobra folga) — só que processando 44% menos pixel que
    # 2x, o que importa nesta máquina que já roda pouca RAM sobrando com
    # outros serviços (ariaBot etc.) ligados ao mesmo tempo.
    upscale_w, upscale_h = int(width * 1.5), int(height * 1.5)
    filter_complex = (
        # crop-to-fill em vez de esticar: sem distorção mesmo se a imagem
        # gerada não vier exatamente na proporção certa.
        f"scale={upscale_w}:{upscale_h}:force_original_aspect_ratio=increase,"
        f"crop={upscale_w}:{upscale_h},"
        f"zoompan=z='min(zoom+0.0007,1.3)':d={frames}:s={width}x{height}:fps={fps}"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(image_path),
            "-i", str(audio_path),
            "-filter:v", filter_complex,
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
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


def add_background_music(video_path: Path, output_path: Path, music_dir: Path = MUSIC_DIR) -> Path:
    """Mistura uma faixa de música de fundo (grátis, CC BY — ver
    assets/music/ATTRIBUTION.md) em volume bem baixo sob a narração já
    existente no vídeo. Escolhe uma faixa aleatória e recorta pra duração
    do vídeo. Se não houver faixa nenhuma em `music_dir`, devolve o vídeo
    original sem mexer (música é bônus, não bloqueia o pipeline)."""
    tracks = sorted(music_dir.glob("*.mp3"))
    if not tracks:
        return video_path

    track = random.choice(tracks)
    duration = _ffprobe_duration(video_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-stream_loop", "-1", "-i", str(track),
            "-filter_complex",
            f"[1:a]volume={MUSIC_VOLUME_DB}dB,atrim=0:{duration}[music];"
            "[0:a][music]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return output_path
