"""Roteiros dos vídeos publicados, guardados depois do upload (a pasta do
job é apagada) — matéria-prima do longo semanal de cada canal: uma
compilação dos Shorts da semana, com fatos que JÁ passaram na revisão
(menos reprovação, nada de pesquisa nova). Ver channels/<nome>.yaml ->
longo_semanal e scripts/run_pipeline.py --compilation."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("script_archive")

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "data" / "published_scripts"
# mínimo de Shorts na semana pra valer uma compilação; menos que isso, o
# longo da semana sai de um tema da fila
MIN_VIDEOS = 3
# narração de cada Short no prompt do longo (limite por minuto do Groq)
NARRATION_CHARS = 900

WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def weekday_index(name: str | None) -> int | None:
    """'sábado' -> 5 (segunda = 0, como date.weekday())."""
    name = (name or "").strip().lower().replace("-feira", "")
    plain = name.replace("ç", "c").replace("á", "a")
    for i, day in enumerate(WEEKDAYS):
        if name == day or plain == day.replace("ç", "c").replace("á", "a"):
            return i
    return None


def archive(channel: str, job_id: int, work_dir: Path, video_id: str, long_form: bool = False) -> None:
    """Guarda o roteiro do vídeo que acabou de subir. Nunca levanta exceção:
    é só pro longo semanal, o upload já deu certo."""
    try:
        script = json.loads((work_dir / "script.json").read_text())
    except (OSError, ValueError):
        return
    entry = {
        "job_id": job_id, "video_id": video_id, "date": date.today().isoformat(), "long": long_form,
        "title": script.get("title", ""),
        "narration": " ".join(sc.get("narration", "") for sc in script.get("scenes", [])),
    }
    try:
        folder = ARCHIVE_DIR / channel
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{job_id}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2))
    except OSError as exc:
        log.warning("não consegui guardar o roteiro do job %s: %s", job_id, exc)


def compilation_facts(channel: str, days: int = 7) -> dict | None:
    """Os Shorts publicados nos últimos `days` dias, no formato de DADOS REAIS
    do roteiro ({tema, dados}), ou None se forem menos de MIN_VIDEOS."""
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    items = []
    for path in sorted((ARCHIVE_DIR / channel).glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if entry.get("date", "") >= since and not entry.get("long") and entry.get("narration"):
            items.append({"titulo": entry["title"], "narracao": entry["narration"][:NARRATION_CHARS]})
    if len(items) < MIN_VIDEOS:
        return None
    return {
        "tipo": "compilacao",
        "tema": f"Compilação da semana: {len(items)} histórias do canal",
        "dados": items,
        "aviso": (
            "Vídeo LONGO que junta os Shorts da semana do canal num vídeo só: uma parte por história, na "
            "ordem dos dados, com transição entre elas. Use só os fatos das narrações (já conferidos). "
            "Título: o fio comum das histórias com o número delas (ex.: \"5 países que resolveram o "
            "impossível\"), não \"compilação\"."
        ),
    }
