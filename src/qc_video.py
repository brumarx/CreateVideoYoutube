"""Controle técnico do MP4 montado, antes do upload (25/11 — política do YouTube 2026 contra conteúdo
inautêntico/produzido em massa: vídeo com tela preta, silêncio, imagem travada ou volume fora do padrão é
sinal de "feito por máquina sem revisão").

Só ffmpeg/ffprobe (grátis, local). Uma passada só de decodificação com todos os filtros juntos
(blackdetect + freezedetect no vídeo reduzido a 6 qps e 270 px de largura; silencedetect + ebur128 no
áudio): num Pi 5 leva ~13 s num Short de 2 min e ~27 s num vídeo de 3 min (medido em 25/11 nos jobs 993
e 948, que passaram sem nenhum alerta). Mais uma chamada mínima que decodifica só o 1º quadro (a capa do
Short quando não há miniatura própria).

`revisar_tecnica` devolve a lista de problemas (vazia = aprovado); quem chama trata como a revisão do
vídeo (src/video_qa.py): problema reprova o vídeo antes de subir."""
from __future__ import annotations

import io
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("qc_video")

# limites (25/11)
PRETO_MIN_S = 0.4          # tela preta mais longa que isso é defeito (transição normal é < 0,4 s)
SILENCIO_MIN_S = 1.0       # silêncio no meio mais longo que isso (a música de fundo não deixa zerar)
SILENCIO_DB = -50          # abaixo disso é silêncio (a música fica em torno de -37 LUFS sob a voz)
FIM_IGNORADO_S = 1.5       # o fim do vídeo pode ter silêncio (fade da música/corte)
CONGELADO_MIN_S = 2.5      # imagem parada com a narração rodando (a legenda karaokê muda a cada palavra)
LUFS_MIN, LUFS_MAX = -20.0, -10.0  # a mixagem normaliza em -14 (assemble.LOUDNORM); fora disso algo quebrou
CAPA_CLARA_MIN = 0.01      # 1º quadro com menos de 1% de pixels claros = capa preta no feed de Shorts
LUMA_CLARA = 50            # luma (0-255) a partir da qual o pixel conta como "claro"


@dataclass
class Medicoes:
    duracao: float
    tem_audio: bool = True
    pretos: list[tuple[float, float]] = field(default_factory=list)
    silencios: list[tuple[float, float]] = field(default_factory=list)
    congelados: list[tuple[float, float]] = field(default_factory=list)
    lufs: float | None = None
    capa_clara: float | None = None  # fração de pixels claros no 1º quadro


def _probe(video: Path) -> tuple[float, bool]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(video)],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    ).stdout
    import json

    data = json.loads(out or "{}")
    dur = float((data.get("format") or {}).get("duration") or 0)
    tem_audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])
    return dur, tem_audio


_NUM = r"(-?[\d.]+)"


def _intervalos(log_txt: str, ini_re: str, fim_re: str, duracao: float) -> list[tuple[float, float]]:
    """Pares início/fim na ordem em que o ffmpeg escreve; intervalo sem fim (vai até o final do arquivo)
    termina na duração."""
    eventos = [(m.start(), "i", float(m.group(1))) for m in re.finditer(ini_re + _NUM, log_txt)]
    eventos += [(m.start(), "f", float(m.group(1))) for m in re.finditer(fim_re + _NUM, log_txt)]
    eventos.sort()
    saida, aberto = [], None
    for _, tipo, t in eventos:
        if tipo == "i":
            aberto = t
        elif aberto is not None:
            saida.append((aberto, t))
            aberto = None
    if aberto is not None:
        saida.append((aberto, duracao))
    return saida


def analisar_log(log_txt: str, duracao: float, tem_audio: bool = True) -> Medicoes:
    """Lê a saída (stderr) da passada única do ffmpeg."""
    pretos = [(float(a), float(b)) for a, b in re.findall(r"black_start:" + _NUM + r"\s+black_end:" + _NUM, log_txt)]
    silencios = _intervalos(log_txt, r"silence_start:\s*", r"silence_end:\s*", duracao)
    congelados = _intervalos(log_txt, r"freezedetect\.freeze_start:\s*", r"freezedetect\.freeze_end:\s*", duracao)
    lufs = None
    if m := re.search(r"Integrated loudness:\s*\n\s*I:\s*" + _NUM + r"\s*LUFS", log_txt):
        lufs = float(m.group(1))
    return Medicoes(duracao=duracao, tem_audio=tem_audio, pretos=pretos, silencios=silencios,
                    congelados=congelados, lufs=lufs)


def _comando(video: Path, tem_audio: bool) -> list[str]:
    filtro_v = (f"[0:v]fps=6,scale=270:-2,blackdetect=d={PRETO_MIN_S}:pix_th=0.10,"
                f"freezedetect=n=-60dB:d={CONGELADO_MIN_S}[v]")
    mapas = ["-map", "[v]"]
    if tem_audio:
        filtro_v += f";[0:a]silencedetect=n={SILENCIO_DB}dB:d={SILENCIO_MIN_S},ebur128=framelog=quiet[a]"
        mapas += ["-map", "[a]"]
    return ["ffmpeg", "-hide_banner", "-nostats", "-threads", "2", "-i", str(video),
            "-filter_complex", filtro_v, *mapas, "-f", "null", "-"]


def _capa_clara(video: Path) -> float | None:
    """Fração de pixels claros no 1º quadro (decodifica só ele, reduzido)."""
    try:
        png = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", "scale=108:-2,format=gray",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, check=True, stdin=subprocess.DEVNULL, timeout=60,
        ).stdout
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("L")
    except Exception as exc:  # noqa: BLE001
        log.warning("1º quadro não lido (%s): %s", video, exc)
        return None
    hist = img.histogram()
    total = sum(hist) or 1
    return sum(hist[LUMA_CLARA:]) / total


def medir(video: Path) -> Medicoes:
    duracao, tem_audio = _probe(video)
    proc = subprocess.run(_comando(video, tem_audio), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou na análise técnica: {proc.stderr[-300:]}")
    m = analisar_log(proc.stderr, duracao, tem_audio)
    m.capa_clara = _capa_clara(video)
    return m


def _sobreposicao(a: tuple[float, float], intervalos: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a[1], b[1]) - max(a[0], b[0])) for b in intervalos)


def problemas(m: Medicoes, max_segundos: float | None = None) -> list[str]:
    """Regras sobre as medições (função pura, testável sem ffmpeg)."""
    saida: list[str] = []
    for ini, fim in m.pretos:
        if fim - ini > PRETO_MIN_S:
            saida.append(f"tela preta de {fim - ini:.1f} s em {ini:.1f} s")
    if not m.tem_audio:
        saida.append("vídeo sem faixa de áudio")
    limite_fim = m.duracao - FIM_IGNORADO_S
    for ini, fim in m.silencios:
        # o trecho final (fade/corte da música) não conta: só o silêncio que começa antes dele
        dur = min(fim, limite_fim) - ini
        if ini < limite_fim and dur > SILENCIO_MIN_S:
            saida.append(f"silêncio de {dur:.1f} s em {ini:.1f} s")
    for ini, fim in m.congelados:
        # parado enquanto ninguém fala (pausa longa já é reprovada acima) não é o defeito aqui
        falando = (fim - ini) - _sobreposicao((ini, fim), m.silencios)
        if falando > CONGELADO_MIN_S:
            saida.append(f"imagem congelada por {fim - ini:.1f} s em {ini:.1f} s com a narração rodando")
    if m.tem_audio:
        if m.lufs is None:
            saida.append("volume integrado não medido (ebur128 sem resultado)")
        elif not LUFS_MIN <= m.lufs <= LUFS_MAX:
            saida.append(f"volume integrado {m.lufs:.1f} LUFS fora de {LUFS_MIN:g}..{LUFS_MAX:g}")
    if m.capa_clara is not None and m.capa_clara < CAPA_CLARA_MIN:
        saida.append(f"1º quadro quase preto ({m.capa_clara:.1%} de pixels claros): capa ruim no feed de Shorts")
    if max_segundos and m.duracao > max_segundos:
        saida.append(f"duração {m.duracao:.0f} s acima do máximo do canal ({max_segundos:.0f} s)")
    return saida


def revisar_tecnica(video: Path, max_segundos: float | None = None) -> list[str]:
    """Problemas técnicos do MP4 final (vazio = aprovado). Falha do próprio ffmpeg vira problema: vídeo que
    nem o ffmpeg lê não sobe."""
    try:
        m = medir(Path(video))
    except Exception as exc:  # noqa: BLE001
        return [f"controle técnico não rodou: {exc}"[:300]]
    log.info("controle técnico de %s: %.0f s, %s LUFS, capa %.1f%% clara, %d preto(s), %d silêncio(s), "
             "%d congelamento(s)", video, m.duracao, m.lufs, (m.capa_clara or 0) * 100, len(m.pretos),
             len(m.silencios), len(m.congelados))
    return problemas(m, max_segundos)
