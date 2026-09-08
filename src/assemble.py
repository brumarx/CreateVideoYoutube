"""Monta o vídeo final: para cada cena, imagem (efeito Ken Burns) + narração;
concatena tudo em um mp4 e mistura música de fundo em volume baixo. Sem
legenda embutida (removida a pedido — cobria demais a imagem). Só usa
ffmpeg via subprocess — sem moviepy.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

TRANSITION_DURATION = 0.5  # segundos de crossfade entre cenas

# Formato padrão (Shorts/Reels, vertical). Vídeos longos passam
# width/height explícitos pra render_scene (16:9).
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"
MUSIC_VOLUME_DB = -23  # bem baixo — não pode competir com a narração

WATERMARK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _escape_drawtext(text: str) -> str:
    # o parser do drawtext trata ':', '\' e apóstrofo como especiais mesmo
    # dentro de aspas simples.
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _list_number_filter(number: int, height: int, accent: str) -> str:
    """"Selo" de contagem regressiva (vídeo de lista, ex.: "10 fatos..."):
    número grande no canto, cartão translúcido escuro com borda na cor de
    destaque do canal — não é só um texto solto, pra ficar com cara de
    coisa desenhada de propósito, não gambiarra."""
    font_size = height // 7
    margin = font_size // 3
    box_size = int(font_size * 1.6)
    hexcolor = accent.lstrip("#")
    return (
        f",drawbox=x={margin}:y={margin}:w={box_size}:h={box_size}:"
        f"color=black@0.55:t=fill"
        f",drawbox=x={margin}:y={margin}:w={box_size}:h={box_size}:"
        f"color=0x{hexcolor}@0.9:t=4"
        f",drawtext=fontfile='{WATERMARK_FONT}':text='{number}':"
        f"fontsize={font_size}:fontcolor=0x{hexcolor}:"
        f"borderw=4:bordercolor=black@0.7:"
        f"x={margin}+({box_size}-text_w)/2:y={margin}+({box_size}-text_h)/2"
    )


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
    watermark: str | None = None,
    list_number: int | None = None,
    accent: str = "#ffffff",
) -> Path:
    """Renderiza uma cena: zoom lento na imagem, sincronizado com a duração
    do áudio. `width`/`height` permitem vertical (Shorts, padrão) ou
    horizontal (vídeo longo tipo documentário). `watermark` (ex.:
    "@FractalCurioso") grava o @ do canal no canto — não impede repostagem,
    mas prova de onde saiu o vídeo original e desestimula quem rouba
    conteúdo sem dar trabalho nenhum a mais pra quem assiste. `list_number`
    (vídeo de lista, ex.: "10 fatos...") grava um selo de contagem
    regressiva no canto oposto ao watermark."""
    duration = _ffprobe_duration(audio_path)
    # 24 (não 30) fps — 20% menos frames pra codificar em CPU fraca (Pi 5,
    # sem encoder de vídeo por hardware nesse modelo) sem ficar perceptível
    # pra conteúdo narrado/foto (sem movimento rápido de verdade).
    fps = 24
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
    if list_number is not None:
        filter_complex += _list_number_filter(list_number, height, accent)
    if watermark:
        font_size = max(width, height) // 45
        margin = font_size
        filter_complex += (
            f",drawtext=fontfile='{WATERMARK_FONT}':text='{_escape_drawtext(watermark)}':"
            f"fontsize={font_size}:fontcolor=white@0.55:"
            f"borderw=2:bordercolor=black@0.4:"
            f"x=w-text_w-{margin}:y=h-text_h-{margin}"
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


def _concat_fast(scene_paths: list[Path], output_path: Path) -> Path:
    """Concat demuxer + `-c copy` — só remuxa o bitstream, não reencoda
    nada. Instantâneo mesmo num Pi, mas sem transição (corte seco)."""
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


def concat_scenes(scene_paths: list[Path], output_path: Path, crossfade: bool = True) -> Path:
    """Concatena os mp4 de cada cena. `crossfade=True` usa xfade (vídeo) +
    acrossfade (áudio) encadeados pra transição suave — mas isso obriga
    reencodar o vídeo inteiro do zero, o que é caro numa CPU fraca sem
    encoder de hardware (Raspberry Pi 5 não tem bloco de encode H.264 —
    testado, `h264_v4l2m2m` não acha dispositivo). Por isso run_pipeline.py
    só liga crossfade no formato curto (poucos minutos); formato longo
    (15-20min) usa `crossfade=False` (corte seco, mas instantâneo — via
    `_concat_fast`, sem reencode nenhum)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(scene_paths) == 1:
        shutil.copy(scene_paths[0], output_path)
        return output_path

    if not crossfade:
        return _concat_fast(scene_paths, output_path)

    durations = [_ffprobe_duration(p) for p in scene_paths]
    # crossfade não pode passar da cena mais curta (offset ficaria negativo)
    transition = min(TRANSITION_DURATION, min(durations) * 0.4)

    inputs = []
    for p in scene_paths:
        inputs += ["-i", str(p)]

    filter_parts = []
    v_label, a_label = "0:v", "0:a"
    cumulative = durations[0]
    for i in range(1, len(scene_paths)):
        next_v, next_a = f"v{i}", f"a{i}"
        offset = cumulative - transition
        filter_parts.append(
            f"[{v_label}][{i}:v]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[{next_v}]"
        )
        filter_parts.append(f"[{a_label}][{i}:a]acrossfade=d={transition:.3f}[{next_a}]")
        v_label, a_label = next_v, next_a
        cumulative += durations[i] - transition

    subprocess.run(
        [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{v_label}]", "-map", f"[{a_label}]",
            # esse reencode só roda pro formato curto (poucos minutos, 1 vez),
            # então vale gastar os 4 núcleos do Pi + preset mais rápido —
            # diferente do render_scene (roda muitas vezes por vídeo, aí sim
            # precisa deixar núcleo sobrando pro resto da máquina).
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "4", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
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
