"""Dados reais de partidas do Botafogo via API pública da ESPN (sem chave),
mesmo endpoint já usado no ariaBot (`ariaBot/src/commands/actions/football.ts`)
só pra placar/tabela — aqui vamos no endpoint de RESUMO de partida, que
devolve formação, posse de bola, estatísticas de time, escalação, gols
cronológicos e destaques por jogador.

Filosofia igual a `src/politica_data.py`: nunca inventa nada — se a ESPN não
tiver um dado, ele simplesmente não entra no dict de `facts`, e o
`prompt_base` do canal instrui a IA a não falar sobre o que não veio.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger("botafogo_data")

BOTAFOGO_ESPN_ID = "6086"

# Mesmas competições listadas em ariaBot/src/commands/actions/football.ts
# (LEAGUES) — o Botafogo pode estar ou não inscrito em cada uma dependendo
# da época do ano; ligas sem retorno são só ignoradas.
LEAGUES: dict[str, str] = {
    "brasileirao": "bra.1",
    "copa_brasil": "bra.copa_do_brazil",
    "carioca": "bra.camp.carioca",
    "libertadores": "conmebol.libertadores",
    "sulamericana": "conmebol.sudamericana",
    "mundial_clubes": "fifa.cwc",
}

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "botafogo_covered_matches.json"

# Só cobre jogo futuro dentro desse horizonte — evita prévia de um jogo
# marcado pra daqui a 2 meses (sem informação nenhuma de contexto ainda).
PREVIEW_HORIZON_DAYS = 5

_TIMEOUT = 20


def _espn_get(url: str) -> dict | None:
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("ESPN falhou (%s): %s", url, exc)
        return None


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"previewed": [], "recapped": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _all_events() -> list[dict]:
    """Junta o calendário do Botafogo em todas as competições conhecidas.
    Cada evento sai marcado com a liga de origem (slug ESPN + nome
    amigável) — precisamos disso de novo pra buscar o resumo depois."""
    events: list[dict] = []
    for liga_nome, liga_slug in LEAGUES.items():
        data = _espn_get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_slug}/teams/{BOTAFOGO_ESPN_ID}/schedule"
        )
        if not data or not data.get("events"):
            continue
        for ev in data["events"]:
            comp = (ev.get("competitions") or [{}])[0]
            status = comp.get("status", {}).get("type", {})
            events.append({
                "id": ev.get("id"),
                "date": ev.get("date"),
                "name": ev.get("name"),
                "completed": bool(status.get("completed")),
                "liga_slug": liga_slug,
                "liga_nome": liga_nome,
            })
    return events


def _fetch_summary(liga_slug: str, event_id: str) -> dict | None:
    return _espn_get(
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_slug}/summary?event={event_id}"
    )


def _standings_line(liga_slug: str = "bra.1") -> str | None:
    """Situação do Botafogo na tabela — só faz sentido pra liga de pontos
    corridos (Brasileirão); competições de mata-mata não têm tabela."""
    data = _espn_get(f"https://site.api.espn.com/apis/v2/sports/soccer/{liga_slug}/standings")
    entries = (data or {}).get("children", [{}])[0].get("standings", {}).get("entries", [])
    for idx, entry in enumerate(entries, start=1):
        team = entry.get("team", {})
        if team.get("id") == BOTAFOGO_ESPN_ID or "botafogo" in (team.get("displayName") or "").lower():
            pts = next((s.get("value") for s in entry.get("stats", []) if s.get("name") == "points"), None)
            if pts is not None:
                return f"{idx}º lugar, {int(pts)} pontos"
            return f"{idx}º lugar"
    return None


def _team_side(summary: dict, team_id: str = BOTAFOGO_ESPN_ID) -> tuple[dict | None, dict | None]:
    """Devolve (stats_do_time, stats_do_adversario) a partir de
    boxscore.teams — cada item tem `team.id` + `statistics`."""
    teams = (summary.get("boxscore") or {}).get("teams") or []
    bota = next((t for t in teams if t.get("team", {}).get("id") == team_id), None)
    rival = next((t for t in teams if t.get("team", {}).get("id") != team_id), None)
    return bota, rival


def _stat_map(team_block: dict | None) -> dict[str, str]:
    if not team_block:
        return {}
    return {s["name"]: s.get("displayValue") for s in team_block.get("statistics", []) if s.get("name")}


def _roster_for(summary: dict, team_id: str = BOTAFOGO_ESPN_ID) -> dict | None:
    for r in summary.get("rosters") or []:
        if r.get("team", {}).get("id") == team_id:
            return r
    return None


def build_recap_facts(event: dict, summary: dict) -> dict:
    facts: dict = {"tipo": "pos-jogo", "competicao": LEAGUES_NOME.get(event["liga_nome"], event["liga_nome"])}

    header = (summary.get("header") or {}).get("competitions", [{}])[0]
    competitors = header.get("competitors", [])
    bota_c = next((c for c in competitors if c.get("team", {}).get("id") == BOTAFOGO_ESPN_ID), None)
    rival_c = next((c for c in competitors if c.get("team", {}).get("id") != BOTAFOGO_ESPN_ID), None)
    if bota_c and rival_c:
        bota_gols, rival_gols = bota_c.get("score"), rival_c.get("score")
        rival_nome = rival_c.get("team", {}).get("displayName")
        facts["placar"] = f"Botafogo {bota_gols} x {rival_gols} {rival_nome}"
        if bota_gols is not None and rival_gols is not None:
            try:
                bg, rg = int(bota_gols), int(rival_gols)
                facts["resultado_botafogo"] = "vitória" if bg > rg else ("derrota" if bg < rg else "empate")
            except ValueError:
                pass

    game_info = summary.get("gameInfo") or {}
    venue = game_info.get("venue", {}).get("fullName")
    if venue:
        facts["estadio"] = venue
    if game_info.get("attendance"):
        facts["publico"] = game_info["attendance"]
    officials = game_info.get("officials") or []
    if officials:
        facts["arbitro"] = officials[0].get("displayName")

    bota_roster = _roster_for(summary)
    rival_roster = next(
        (r for r in summary.get("rosters") or [] if r.get("team", {}).get("id") != BOTAFOGO_ESPN_ID), None
    )
    if bota_roster and bota_roster.get("formation"):
        facts["formacao_botafogo"] = bota_roster["formation"]
        titulares = [
            p["athlete"]["displayName"]
            for p in bota_roster.get("roster", [])
            if p.get("starter") and p.get("athlete", {}).get("displayName")
        ]
        if titulares:
            facts["escalacao_titular_botafogo"] = titulares
    if rival_roster and rival_roster.get("formation"):
        facts["formacao_adversario"] = rival_roster["formation"]

    bota_stats, rival_stats = _team_side(summary)
    bota_map, rival_map = _stat_map(bota_stats), _stat_map(rival_stats)
    if bota_map.get("possessionPct") and rival_map.get("possessionPct"):
        facts["posse_de_bola"] = {"botafogo": bota_map["possessionPct"], "adversario": rival_map["possessionPct"]}
    if bota_map.get("totalShots"):
        facts["finalizacoes"] = {
            "botafogo": {"total": bota_map.get("totalShots"), "no_alvo": bota_map.get("shotsOnTarget")},
            "adversario": {"total": rival_map.get("totalShots"), "no_alvo": rival_map.get("shotsOnTarget")},
        }
    if bota_map.get("totalPasses"):
        facts["passes"] = {
            "botafogo": {"total": bota_map.get("totalPasses"), "certos": bota_map.get("accuratePasses")},
            "adversario": {"total": rival_map.get("totalPasses"), "certos": rival_map.get("accuratePasses")},
        }
    if bota_map.get("wonCorners"):
        facts["escanteios"] = {"botafogo": bota_map.get("wonCorners"), "adversario": rival_map.get("wonCorners")}
    if bota_map.get("yellowCards") is not None:
        facts["cartoes"] = {
            "botafogo": {"amarelos": bota_map.get("yellowCards"), "vermelhos": bota_map.get("redCards")},
            "adversario": {"amarelos": rival_map.get("yellowCards"), "vermelhos": rival_map.get("redCards")},
        }

    gols = [
        {"minuto": ev.get("clock", {}).get("displayValue"), "descricao": ev.get("text")}
        for ev in summary.get("keyEvents") or []
        if (ev.get("type", {}).get("text") or "").lower().startswith(("goal", "penalty"))
    ]
    if gols:
        facts["gols"] = gols

    for leader_block in summary.get("leaders") or []:
        if leader_block.get("team", {}).get("id") == BOTAFOGO_ESPN_ID:
            destaques = []
            for categoria in leader_block.get("leaders", [])[:5]:
                top = (categoria.get("leaders") or [None])[0]
                if top:
                    destaques.append({
                        "jogador": top.get("athlete", {}).get("displayName"),
                        "categoria": categoria.get("name"),
                        "valor": top.get("displayValue"),
                    })
            if destaques:
                facts["destaques_botafogo"] = destaques

    return facts


def build_preview_facts(event: dict, summary: dict | None) -> dict:
    facts: dict = {"tipo": "pre-jogo", "competicao": LEAGUES_NOME.get(event["liga_nome"], event["liga_nome"])}

    header = ((summary or {}).get("header") or {}).get("competitions", [{}])[0]
    competitors = header.get("competitors") or []
    rival_c = next((c for c in competitors if c.get("team", {}).get("id") != BOTAFOGO_ESPN_ID), None)
    facts["adversario"] = (rival_c or {}).get("team", {}).get("displayName") or event.get("name")
    facts["data_hora"] = event.get("date")

    game_info = (summary or {}).get("gameInfo") or {}
    venue = game_info.get("venue", {}).get("fullName")
    if venue:
        facts["local"] = venue

    if event["liga_slug"] == "bra.1":
        situacao = _standings_line()
        if situacao:
            facts["situacao_botafogo_tabela"] = situacao

    # Seções opcionais da ESPN pra pré-jogo — nem sempre vêm preenchidas
    # antes do apito inicial; só entram no facts quando existem de verdade.
    ultimos = (summary or {}).get("lastFiveGames")
    if ultimos:
        facts["ultimos_jogos_disponiveis_na_fonte"] = True

    return facts


LEAGUES_NOME = {
    "brasileirao": "Brasileirão Série A",
    "copa_brasil": "Copa do Brasil",
    "carioca": "Campeonato Carioca",
    "libertadores": "Copa Libertadores",
    "sulamericana": "Copa Sul-Americana",
    "mundial_clubes": "Mundial de Clubes",
}


def next_pending_task() -> dict | None:
    """Escolhe a próxima tarefa pendente: prioriza recapear o jogo
    concluído mais recente ainda não coberto; se não houver, prevê o
    próximo jogo agendado (dentro do horizonte) ainda não coberto. `None`
    quando não há nada pendente hoje — o chamador deve simplesmente não
    gerar vídeo nenhum nesse dia."""
    events = _all_events()
    if not events:
        return None

    state = _load_state()
    recapped = set(state.get("recapped", []))
    previewed = set(state.get("previewed", []))

    concluidos = sorted(
        (e for e in events if e["completed"] and e["id"] not in recapped),
        key=lambda e: e["date"] or "",
        reverse=True,
    )
    if concluidos:
        escolhido = concluidos[0]
        summary = _fetch_summary(escolhido["liga_slug"], escolhido["id"])
        if summary is None:
            log.warning("ESPN não devolveu resumo pra evento %s — pulando hoje", escolhido["id"])
            return None
        # marca este e qualquer concluído mais antigo como coberto, pra não
        # acumular fila de jogos passados se o canal ficar um tempo parado.
        for e in concluidos:
            recapped.add(e["id"])
        state["recapped"] = sorted(recapped)
        _save_state(state)

        facts = build_recap_facts(escolhido, summary)
        placar = facts.get("placar", escolhido["name"])
        return {"tipo": "pos-jogo", "titulo": f"{placar}: pós-jogo", "facts": facts}

    from datetime import datetime, timedelta, timezone

    agora = datetime.now(timezone.utc)
    limite = agora + timedelta(days=PREVIEW_HORIZON_DAYS)
    futuros = sorted(
        (
            e for e in events
            if not e["completed"] and e["id"] not in previewed and e["date"]
            and agora <= datetime.fromisoformat(e["date"].replace("Z", "+00:00")) <= limite
        ),
        key=lambda e: e["date"],
    )
    if not futuros:
        return None

    escolhido = futuros[0]
    summary = _fetch_summary(escolhido["liga_slug"], escolhido["id"])
    previewed.add(escolhido["id"])
    state["previewed"] = sorted(previewed)
    _save_state(state)

    facts = build_preview_facts(escolhido, summary)
    return {"tipo": "pre-jogo", "titulo": f"Botafogo x {facts['adversario']}: prévia", "facts": facts}
