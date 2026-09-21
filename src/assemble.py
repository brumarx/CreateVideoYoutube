"""Monta o vídeo final: para cada cena, imagem (efeito Ken Burns) + narração;
concatena tudo em um mp4 e mistura música de fundo em volume baixo. Legenda
embutida estilo TikTok (2-3 palavras por vez, faixa escura só atrás do
texto) — uma tentativa anterior foi removida por cobrir demais a imagem;
esta é bem menor e fica numa faixa central-baixa que não encosta no selo de
lista nem na marca d'água. Só usa ffmpeg via subprocess — sem moviepy.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import unicodedata
from pathlib import Path

from PIL import ImageFont

TRANSITION_DURATION = 0.5  # segundos de crossfade entre cenas

# Formato padrão (Shorts/Reels, vertical). Vídeos longos passam
# width/height explícitos pra render_scene (16:9).
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"
MUSIC_VOLUME_DB = -23  # bem baixo — não pode competir com a narração
SFX_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"

WATERMARK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Nome da família pro fontconfig (usado pelo libass no filtro `ass`) — é
# diferente do caminho do arquivo usado pelo `drawtext` (fontfile=), que
# não passa pelo fontconfig. Conferido com `fc-list`: o arquivo
# DejaVuSans-Bold.ttf está registrado como família "DejaVu Sans", estilo
# Bold (não como uma família "DejaVu Sans Bold" separada).
ASS_FONT_FAMILY = "DejaVu Sans"


def _escape_drawtext(text: str) -> str:
    # o parser do drawtext trata ':' e '\' como especiais mesmo dentro de
    # aspas simples. Apóstrofo é pior ainda: escapar com "\'" quebra o
    # parser do filtergraph quando tem vírgula logo depois (ex.: "O'Donnell,
    # interveio..." vira "No such filter" porque o parser fecha a aspa no
    # lugar errado) — mais simples e robusto trocar por aspa tipográfica
    # (’), que não é caractere especial pra ffmpeg nenhum.
    text = text.replace("'", "’")
    text = text.replace("\\", "\\\\").replace(":", "\\:")
    # bug confirmado do próprio ffmpeg: qualquer texto com acento (Ó, Ã, Á,
    # Ú etc.) sai com o ÚLTIMO caractere cortado (visto na prática: a marca
    # d'água "@NúmerosdaRepública" saiu "@NúmerosdaRepúbli" no vídeo
    # publicado — a legenda já tinha sido corrigida, mas a marca d'água
    # usava esta mesma função sem passar por _strip_accents). Centralizado
    # aqui pra proteger todo uso de drawtext, não só a legenda.
    return _strip_accents(text)


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
        f",drawtext=fontfile='{WATERMARK_FONT}':text='{number}':expansion=none:"
        f"fontsize={font_size}:fontcolor=0x{hexcolor}:"
        f"borderw=4:bordercolor=black@0.7:"
        f"x={margin}+({box_size}-text_w)/2:y={margin}+({box_size}-text_h)/2"
    )


def _caption_chunks(
    narration: str, duration: float, font: ImageFont.FreeTypeFont, max_width: float
) -> list[tuple[str, float, float]]:
    """Divide a narração (já em maiúsculas) em pedacinhos curtos por LARGURA
    REAL renderizada (medida com a própria fonte via PIL, não estimada) —
    uma estimativa de "largura média de caractere" saiu errada na prática e
    deixou legenda estourando a borda do vídeo e sendo cortada pelo ffmpeg
    (o corte no meio de uma letra acentuada é o que parecia "acento
    quebrado"). Tempo de exibição de cada pedaço é proporcional à duração
    real do áudio (sem alinhamento por palavra do TTS, mas o edge-tts narra
    num ritmo bem constante, então fica sincronizado o bastante
    visualmente)."""
    words = narration.split()
    if not words:
        return []

    chunks: list[list[str]] = []
    current: list[str] = []
    for w in words:
        candidate = " ".join([*current, w])
        if current and font.getlength(candidate) > max_width:
            chunks.append(current)
            current = [w]
        else:
            current.append(w)
    if current:
        chunks.append(current)

    total_words = len(words)
    out = []
    t = 0.0
    for chunk in chunks:
        chunk_dur = duration * (len(chunk) / total_words)
        out.append((" ".join(chunk), t, t + chunk_dur))
        t += chunk_dur
    return out


def _strip_accents(text: str) -> str:
    # bug confirmado do próprio ffmpeg (não é escaping nosso): o filtro
    # drawtext desta build derruba o ÚLTIMO caractere da string sempre que
    # ela contém um acento (Ó, Ã, Á etc.) — reproduzido isolado, sem box,
    # sem borderw, com text= OU textfile=, então não tem workaround de
    # sintaxe. Solução: tirar o acento só da legenda (a narração falada
    # continua 100% correta, só o texto na tela perde o acento visual).
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _caption_filter(narration: str, duration: float, width: int, height: int) -> str:
    font_size = min(width, height) // 16
    # calculado a partir do RODAPÉ (não do topo) — uma fração fixa da
    # altura total (72%) deixava a legenda "no meio" no formato longo
    # (16:9, mais baixo/achatado que o vertical): a mesma % de cima pra
    # baixo sobra bem menos espaço embaixo numa tela mais baixa. Margem
    # fixa de baixo funciona igual nos dois formatos.
    margin_bottom = int(height * 0.08)
    y = height - margin_bottom - font_size
    font = ImageFont.truetype(WATERMARK_FONT, font_size)
    # margem de 9% de cada lado — sobra segura medida na largura REAL do
    # texto renderizado (ver _caption_chunks), não numa estimativa.
    max_width = width * 0.82
    parts = []
    for text, start, end in _caption_chunks(_strip_accents(narration.upper()), duration, font, max_width):
        esc = _escape_drawtext(text)
        parts.append(
            f",drawtext=fontfile='{WATERMARK_FONT}':text='{esc}':expansion=none:"
            f"fontsize={font_size}:fontcolor=white:"
            f"borderw=3:bordercolor=black:"
            f"box=1:boxcolor=black@0.55:boxborderw=16:"
            f"x=(w-text_w)/2:y={y}:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    return "".join(parts)


def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass_text(text: str) -> str:
    # chaves são sintaxe de tag ASS ({\k...}) — se sobrar uma na palavra em
    # si (nunca deveria, mas texto de narração é imprevisível), vira tag
    # quebrada e derruba a legenda inteira daquele evento.
    return text.replace("{", "(").replace("}", ")").replace("\\", "")


def _group_words(
    word_boundaries: list[dict], font: ImageFont.FreeTypeFont, max_width: float, max_words: int = 6,
) -> list[list[dict]]:
    """Agrupa palavras (com tempo REAL cada uma) em blocos curtos pra
    legenda — por largura renderizada real (igual _caption_chunks) E um
    teto de palavras por bloco, o que vier primeiro."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_text = ""
    for w in word_boundaries:
        candidate = f"{current_text} {w['text']}".strip()
        if current and (font.getlength(candidate) > max_width or len(current) >= max_words):
            groups.append(current)
            current, current_text = [w], w["text"]
        else:
            current.append(w)
            current_text = candidate
    if current:
        groups.append(current)
    return groups


def _build_ass_captions(
    word_boundaries: list[dict], width: int, height: int, accent: str, output_path: Path,
) -> None:
    """Legenda karaokê de verdade: cada palavra "acende" na cor de destaque
    do canal no instante exato em que é falada (tempo real do edge-tts, ver
    src/tts.py), não uma estimativa por proporção. `ass` (libass) renderiza
    isso numa só passada de filtro — mais barato em CPU que a cadeia de
    vários `drawtext` encadeados do esquema antigo (_caption_filter), que
    fica só como fallback pra quando não vier tempo real nenhum."""
    font_size = min(width, height) // 16
    margin_v = int(height * 0.08)
    font = ImageFont.truetype(WATERMARK_FONT, font_size)
    max_width = width * 0.82
    accent_hex = accent.lstrip("#")
    # ASS é &HAABBGGRR (alpha invertido: 00 = opaco, FF = transparente) —
    # troca a ordem RRGGBB -> BBGGRR do hex do canal.
    accent_bgr = accent_hex[4:6] + accent_hex[2:4] + accent_hex[0:2]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{ASS_FONT_FAMILY},{font_size},&H00{accent_bgr},&H00FFFFFF,&H00000000,&H73000000,-1,0,0,0,100,100,0,0,3,2,0,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for group in _group_words(word_boundaries, font, max_width):
        start, end = group[0]["start"], group[-1]["end"]
        text = ""
        for idx, w in enumerate(group):
            dur = (group[idx + 1]["start"] - w["start"]) if idx + 1 < len(group) else (w["end"] - w["start"])
            centis = max(round(dur * 100), 1)
            text += f"{{\\k{centis}}}{_escape_ass_text(_strip_accents(w['text'].upper()))} "
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Karaoke,,0,0,0,,{text.strip()}\n")

    output_path.write_text("".join(lines), encoding="utf-8")


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
    image_path: Path | None,
    audio_path: Path,
    output_path: Path,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    watermark: str | None = None,
    list_number: int | None = None,
    accent: str = "#ffffff",
    caption: str | None = None,
    word_boundaries: list[dict] | None = None,
    stat_overlay: dict | None = None,
    impact_beat: bool = False,
    video_path: Path | None = None,
) -> Path:
    """Renderiza uma cena: zoom lento na imagem, sincronizado com a duração
    do áudio. `width`/`height` permitem vertical (Shorts, padrão) ou
    horizontal (vídeo longo tipo documentário). `watermark` (ex.:
    "@FractalCurioso") grava o @ do canal no canto — não impede repostagem,
    mas prova de onde saiu o vídeo original e desestimula quem rouba
    conteúdo sem dar trabalho nenhum a mais pra quem assiste. `list_number`
    (vídeo de lista, ex.: "10 fatos...") grava um selo de contagem
    regressiva no canto oposto ao watermark. `word_boundaries` (ver
    src/tts.py -> narrate()) grava legenda karaokê com tempo real de
    palavra; `caption` (texto cru da narração) é só o fallback por
    proporção pra quando não vier tempo real nenhum. `stat_overlay`
    (opcional, ver src/script_gen.py) grava uma barra comparativa animada
    quando a cena citar 2 valores reais comparáveis (ex.: posse de bola).

    `video_path` (opcional, ver src/stock_media.py): filmagem REAL de banco
    de vídeo em vez de imagem estática — fica muito mais viva na tela que
    qualquer imagem gerada por IA com zoom simulado. Quando informado,
    `image_path` é ignorado; o clipe é cortado/loopado pra bater com a
    duração do áudio (looping cobre clipe mais curto que a narração)."""
    if video_path is not None:
        return _render_scene_from_video(
            video_path, audio_path, output_path, width, height, watermark, list_number, accent,
            caption, word_boundaries, stat_overlay, impact_beat,
        )
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

    # cenas longas (>8s de narração) ficam muitos segundos com zoom
    # contínuo e nada mudando na tela — atenção cai depois de ~5-7s sem
    # nenhuma mudança visual. Um "pulso" breve de zoom mais rápido no meio
    # da cena quebra a monotonia sem cortar cena nem gastar reencode extra
    # (mesmo filtro zoompan, só muda a taxa por uma janela curta de frames).
    zoom_expr = "min(zoom+0.0007,1.3)"
    if duration > 8.0:
        mid = frames // 2
        window = max(int(fps * 0.35), 3)
        zoom_expr = f"min(zoom+if(between(on,{mid - window},{mid + window}),0.006,0.0007),1.3)"

    filter_complex = (
        # crop-to-fill em vez de esticar: sem distorção mesmo se a imagem
        # gerada não vier exatamente na proporção certa.
        f"scale={upscale_w}:{upscale_h}:force_original_aspect_ratio=increase,"
        f"crop={upscale_w}:{upscale_h},"
        f"zoompan=z='{zoom_expr}':d={frames}:s={width}x{height}:fps={fps}"
    )
    filter_complex += _overlay_filter_suffix(
        duration, width, height, watermark, list_number, accent, caption,
        word_boundaries, output_path.with_suffix(".ass"), stat_overlay,
    )

    impact = _impact_sfx_args(filter_complex, impact_beat)
    base_inputs = ["-loop", "1", "-i", str(image_path), "-i", str(audio_path)]
    if impact is None:
        media_args = [*base_inputs, "-filter:v", filter_complex]
    else:
        impact_input, impact_filter = impact
        media_args = [*base_inputs, *impact_input, *impact_filter]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            *media_args,
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            "-shortest",
            str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return output_path


def _stat_bar_filter(overlay: dict, width: int, height: int, accent: str) -> str:
    """Barra comparativa animada (ex.: posse de bola, 2 valores 0-100) —
    cresce da esquerda pra direita no primeiro segundo da cena. Só ativa
    quando o roteiro citou 2 valores comparáveis vindos de DADOS REAIS (ver
    script_gen.py -> "stat_overlay"), nunca inventado ou estimado."""
    bar_w = int(width * 0.6)
    bar_h = max(height // 45, 14)
    x0 = (width - bar_w) // 2
    label_font = max(height // 38, 18)
    label_gap = label_font // 4  # espaço entre o texto do rótulo e a barra dele
    row_gap = bar_h // 2  # espaço entre o fim de uma barra e o rótulo da próxima
    row_height = label_font + label_gap + bar_h + row_gap
    y0 = int(height * 0.12)

    def bar_and_label(label_y: int, label: str, value: float, color_hex: str) -> str:
        bar_y = label_y + label_font + label_gap
        target_w = max(int(bar_w * (value / 100)), 1)
        text = _escape_drawtext(f"{label} {value:.0f}%")
        # expansion=none é obrigatório aqui — o rótulo sempre tem um '%'
        # (percentual), e o parser de expansão do drawtext (%{...}) trata
        # um '%' sozinho como início de sintaxe quebrada ("Stray % near")
        # e SOME o texto inteiro sem erro nenhum no processo (achado
        # testando esta função: "Botafogo 50%" nunca aparecia no vídeo).
        return (
            f",drawtext=fontfile='{WATERMARK_FONT}':text='{text}':expansion=none:"
            f"fontsize={label_font}:fontcolor=white:borderw=2:bordercolor=black:"
            f"x={x0}:y={label_y}"
            f",drawbox=x={x0}:y={bar_y}:w={bar_w}:h={bar_h}:color=white@0.25:t=fill"
            f",drawbox=x={x0}:y={bar_y}:w='min(t/0.8,1)*{target_w}':h={bar_h}:color=0x{color_hex}@0.9:t=fill"
        )

    return (
        bar_and_label(y0, overlay["a_label"], overlay["a_value"], accent.lstrip("#"))
        + bar_and_label(y0 + row_height, overlay["b_label"], overlay["b_value"], "999999")
    )


def _impact_sfx_args(video_filter: str, impact_beat: bool) -> tuple[list[str], list[str]] | None:
    """Se `impact_beat` e existir assets/sfx/impact.mp3 (ver SFX_DIR),
    devolve (args extras de input, args de filtro/mapeamento) pra mixar um
    impacto curto logo no início do áudio da cena — `None` quando não tem
    arquivo ou a cena não é o destaque (vira no-op, mesma filosofia de
    add_background_music sem faixa). Assume a ordem fixa de input já usada
    nas duas cenas (render_scene/_render_scene_from_video): mídia = 0,
    áudio da narração = 1 — o impacto, se usado, sempre entra depois,
    como input 2."""
    impact_path = SFX_DIR / "impact.mp3"
    if not (impact_beat and impact_path.exists()):
        return None
    filter_complex = (
        f"[0:v]{video_filter}[vout];"
        "[2:a]adelay=150|150,volume=-6dB[imp];"
        "[1:a][imp]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    return ["-i", str(impact_path)], ["-filter_complex", filter_complex, "-map", "[vout]", "-map", "[aout]"]


# Correção de cor + vinheta leve, sempre ativa — filtros nativos do ffmpeg
# (sem LUT, sem passe extra), custo de render desprezível, mas já tira a
# cara de "filmagem de banco crua" (achado na pesquisa de mercado: um dos
# jeitos mais baratos de parecer mais produzido). Fica ANTES de qualquer
# texto/selo desenhado por cima, pra não escurecer a legenda também.
COLOR_GRADE_SUFFIX = ",eq=contrast=1.05:saturation=1.12,vignette=PI/5"


def _overlay_filter_suffix(
    duration: float, width: int, height: int, watermark: str | None,
    list_number: int | None, accent: str, caption: str | None,
    word_boundaries: list[dict] | None = None, ass_path: Path | None = None,
    stat_overlay: dict | None = None,
) -> str:
    """Filtros compartilhados entre cena de imagem (zoompan) e cena de
    vídeo real (src/stock_media.py) — legenda, selo de lista e marca
    d'água não dependem de como o vídeo de fundo foi gerado."""
    suffix = COLOR_GRADE_SUFFIX
    if list_number is not None:
        suffix += _list_number_filter(list_number, height, accent)
    if stat_overlay:
        suffix += _stat_bar_filter(stat_overlay, width, height, accent)
    if word_boundaries and ass_path is not None:
        # legenda karaokê com tempo real (ver _build_ass_captions) — filtro
        # `ass` do libass, uma passada só, mais barato em CPU que a cadeia
        # de drawtext do fallback abaixo.
        _build_ass_captions(word_boundaries, width, height, accent, ass_path)
        suffix += f",ass='{ass_path}'"
    elif caption:
        # fallback: sem tempo real de palavra (edge-tts não mandou
        # WordBoundary por algum motivo) — legenda por proporção, pior
        # sincronia mas nunca fica sem legenda nenhuma por causa disso.
        suffix += _caption_filter(caption, duration, width, height)
    if watermark:
        font_size = max(width, height) // 45
        margin = font_size
        suffix += (
            f",drawtext=fontfile='{WATERMARK_FONT}':text='{_escape_drawtext(watermark)}':expansion=none:"
            f"fontsize={font_size}:fontcolor=white@0.55:"
            f"borderw=2:bordercolor=black@0.4:"
            f"x=w-text_w-{margin}:y=h-text_h-{margin}"
        )
    return suffix


def _render_scene_from_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    width: int,
    height: int,
    watermark: str | None,
    list_number: int | None,
    accent: str,
    caption: str | None,
    word_boundaries: list[dict] | None = None,
    stat_overlay: dict | None = None,
    impact_beat: bool = False,
) -> Path:
    """Filmagem REAL de banco de vídeo (Pexels) em vez de imagem estática
    com zoom — ver src/stock_media.py. `-stream_loop -1` cobre o caso do
    clipe ser mais curto que a narração (comum: clipe de banco costuma ter
    5-20s); `-t duration` corta no tamanho certo tanto se loopou quanto se
    o clipe já era mais longo."""
    duration = _ffprobe_duration(audio_path)
    fps = 24
    filter_v = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}"
    filter_v += _overlay_filter_suffix(
        duration, width, height, watermark, list_number, accent, caption,
        word_boundaries, output_path.with_suffix(".ass"), stat_overlay,
    )

    impact = _impact_sfx_args(filter_v, impact_beat)
    base_inputs = ["-stream_loop", "-1", "-i", str(video_path), "-i", str(audio_path)]
    if impact is None:
        media_args = [*base_inputs, "-map", "0:v", "-map", "1:a", "-filter:v", filter_v]
    else:
        impact_input, impact_filter = impact
        media_args = [*base_inputs, *impact_input, *impact_filter]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            *media_args,
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


def concat_scenes(
    scene_paths: list[Path], output_path: Path, crossfade: bool = True, sfx_path: Path | None = None,
) -> Path:
    """Concatena os mp4 de cada cena. `crossfade=True` usa xfade (vídeo) +
    acrossfade (áudio) encadeados pra transição suave — mas isso obriga
    reencodar o vídeo inteiro do zero, o que é caro numa CPU fraca sem
    encoder de hardware (Raspberry Pi 5 não tem bloco de encode H.264 —
    testado, `h264_v4l2m2m` não acha dispositivo). Por isso run_pipeline.py
    só liga crossfade no formato curto (poucos minutos); formato longo
    (15-20min) usa `crossfade=False` (corte seco, mas instantâneo — via
    `_concat_fast`, sem reencode nenhum).

    `sfx_path` (ex.: assets/sfx/whoosh.mp3, ver SFX_DIR — opcional, some
    sem erro se o arquivo não existir) mixa um efeito sonoro curto em cada
    ponto de transição, só no caminho com crossfade (corte seco não tem
    "ponto" de transição pra ancorar o som)."""
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
    transition_offsets = []
    for i in range(1, len(scene_paths)):
        next_v, next_a = f"v{i}", f"a{i}"
        offset = cumulative - transition
        transition_offsets.append(offset)
        filter_parts.append(
            f"[{v_label}][{i}:v]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[{next_v}]"
        )
        filter_parts.append(f"[{a_label}][{i}:a]acrossfade=d={transition:.3f}[{next_a}]")
        v_label, a_label = next_v, next_a
        cumulative += durations[i] - transition

    has_sfx = sfx_path is not None and sfx_path.exists()
    if has_sfx:
        sfx_input_idx = len(scene_paths)
        inputs += ["-i", str(sfx_path)]
        mix_labels = [f"[{a_label}]"]
        for i, off in enumerate(transition_offsets):
            delay_ms = max(int(off * 1000), 0)
            wh_label = f"wh{i}"
            filter_parts.append(
                f"[{sfx_input_idx}:a]adelay={delay_ms}|{delay_ms},volume=-9dB[{wh_label}]"
            )
            mix_labels.append(f"[{wh_label}]")
        mixed_label = "sfxmix"
        # normalize=0 é essencial — o padrão do amix divide o volume de
        # TODOS os inputs pela contagem deles, o que abaixaria a narração
        # cada vez que mais um whoosh entrasse na mistura (queremos o
        # whoosh baixo por causa do volume= dele, não por rateio do amix).
        filter_parts.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[{mixed_label}]"
        )
        a_label = mixed_label

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


# As faixas do Kevin MacLeod (CC BY 4.0) EXIGEM essa linha na descrição de
# todo vídeo que usa a faixa; as do Pixabay Content License não exigem
# (atribuição opcional), mas incluídas do mesmo jeito por boa prática — ver
# assets/music/ATTRIBUTION.md pros detalhes de licença de cada uma. Mapa
# fixo em vez de parsear o markdown, pra nunca publicar sem atribuição por
# causa de um parse errado.
TRACK_ATTRIBUTION = {
    "morning.mp3": '"Morning" Kevin MacLeod (incompetech.com) — Licensed under Creative Commons: By Attribution 4.0 License',
    "evening.mp3": '"Evening" Kevin MacLeod (incompetech.com) — Licensed under Creative Commons: By Attribution 4.0 License',
    "deep_relaxation.mp3": '"Deep Relaxation" Kevin MacLeod (incompetech.com) — Licensed under Creative Commons: By Attribution 4.0 License',
    "study_and_relax.mp3": '"Study And Relax" Kevin MacLeod (incompetech.com) — Licensed under Creative Commons: By Attribution 4.0 License',
    "tense_suspense.mp3": '"Tense Suspense" by leberch (Pixabay)',
    "animado_motivational.mp3": '"Inspirational Cinematic Motivational Music" by SigmaMusicArt (Pixabay)',
}

# Clima mais próximo de cada faixa — usado por add_background_music(mood=...)
# pra não sortear música de "relaxar estudando" pra narrar uma derrota ou
# uma denúncia grave (feedback direto: descompasso feio entre tom do vídeo
# e trilha). "tense_suspense.mp3"/"animado_motivational.mp3" (Pixabay,
# baixadas 2026-09-21) cobrem os climas que faltavam.
TRACK_MOOD = {
    "morning.mp3": "neutro",
    "evening.mp3": "melancolico",
    "deep_relaxation.mp3": "calmo",
    "study_and_relax.mp3": "neutro",
    "tense_suspense.mp3": "tenso",
    "animado_motivational.mp3": "animado",
}


def add_background_music(
    video_path: Path, output_path: Path, mood: str | None = None, music_dir: Path = MUSIC_DIR,
) -> tuple[Path, str | None]:
    """Mistura uma faixa de música de fundo (grátis, CC BY — ver
    assets/music/ATTRIBUTION.md) em volume bem baixo sob a narração já
    existente no vídeo. Escolhe uma faixa entre as do clima pedido
    (`mood`, ver TRACK_MOOD e script_gen.py -> "mood") e recorta pra
    duração do vídeo. Sem faixa nenhuma pro clima pedido (catálogo ainda
    curto) ou sem `mood` nenhum, sorteia entre TODAS — nunca bloqueia o
    pipeline por falta de faixa de um clima específico. Se não houver
    faixa nenhuma em `music_dir`, devolve o vídeo original sem mexer
    (música é bônus, não bloqueia o pipeline).

    Devolve também a linha de atribuição da faixa escolhida (None se não
    usou música nenhuma), pra quem chamar colocar na descrição do vídeo —
    a licença exige isso."""
    tracks = sorted(music_dir.glob("*.mp3"))
    if not tracks:
        return video_path, None

    candidates = [t for t in tracks if TRACK_MOOD.get(t.name) == mood] if mood else []
    track = random.choice(candidates or tracks)
    attribution = TRACK_ATTRIBUTION.get(track.name)
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
    return output_path, attribution
