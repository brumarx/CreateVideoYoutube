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
import re
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

# Dia sem jogo: vídeo de notícias recentes do clube (feed pt-BR da ESPN já
# filtrado pelo time) — só notícia publicada DEPOIS do último vídeo de
# notícias (e nunca mais velha que NEWS_MAX_AGE_HOURS), no máximo
# NEWS_PER_VIDEO por vídeo, cada uma usada uma única vez. Pedido do dono do
# canal (15/10): ninguém quer saber de coisa velha.
NEWS_FEED_URL = f"https://now.core.api.espn.com/v1/sports/news?lang=pt&region=br&limit=50&teams={BOTAFOGO_ESPN_ID}"
NEWS_MAX_AGE_HOURS = 24
NEWS_PER_VIDEO = 3
NEWS_STORY_CHARS = 1800
# foto de matéria só entra se foi publicada até N dias antes da matéria — a
# ESPN reaproveita foto de arquivo (visto de verdade: foto de fev/2025 numa
# matéria de set/2026), e foto velha num vídeo "de hoje" engana o torcedor.
NEWS_PHOTO_MAX_AGE_DAYS = 2

# Reserva quando a ESPN não tem notícia nova (visto em Data FIFA: 3+ dias sem
# nada e o canal parado): o Portal Botafogo (/var/www/html/botafogo, API
# local) agrega manchetes de várias fontes (FogãoNET, ge, O Globo…) a cada
# 15 min, já sem duplicata exata e sem homônimo (Botafogo-PB/SP). Manchete
# agregada só traz título + resumo curto, por isso entram mais por vídeo.
PORTAL_API = "http://127.0.0.1:8130"
PORTAL_SITE = "https://botafogo.win"
PORTAL_NEWS_PER_VIDEO = 5
# mesma notícia em 3 veículos com títulos diferentes ("Juventus anuncia
# Neto" x "Neto é anunciado pela Juventus") — acima disso de palavras em
# comum, conta como a mesma notícia.
PORTAL_DUP_OVERLAP = 0.4


def _espn_get(url: str) -> dict | None:
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("ESPN falhou (%s): %s", url, exc)
        return None


def elenco() -> list[str]:
    """Nomes do elenco atual (ESPN) — quem a cena pode citar e ganhar foto
    (ver src/people_photos.py). Lista vazia se a ESPN cair."""
    data = _espn_get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/bra.1/teams/{BOTAFOGO_ESPN_ID}/roster")
    return [a["displayName"] for a in (data or {}).get("athletes", []) if a.get("displayName")]


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"previewed": [], "recapped": [], "news": []}


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
            lados = {c.get("homeAway"): (c.get("team") or {}).get("displayName") for c in comp.get("competitors") or []}
            events.append({
                "id": ev.get("id"),
                "date": ev.get("date"),
                "name": ev.get("name"),
                "home": lados.get("home"),
                "away": lados.get("away"),
                "completed": bool(status.get("completed")),
                "liga_slug": liga_slug,
                "liga_nome": liga_nome,
            })
    return events


def _mando(home: str | None, away: str | None, venue: str | None = None) -> str | None:
    """Frase pronta de quem joga em casa — só "Coritiba x Botafogo" + "Couto
    Pereira" virou "o Coritiba visita o Botafogo no Couto Pereira" (job 178,
    barrado na revisão): o LLM não sabe que o primeiro nome é o mandante."""
    if not home or not away:
        return None
    onde = f" ({venue})" if venue else ""
    if "botafogo" in home.lower():
        return f"Botafogo é o MANDANTE: joga em casa{onde}; o {away} é o visitante"
    if "botafogo" in away.lower():
        return f"Botafogo é o VISITANTE: joga fora, na casa do {home}{onde}"
    return None


def _agenda() -> dict:
    """Contexto de calendário pro vídeo de notícias: data de hoje e os
    próximos jogos com data COMPLETA (horário de Brasília). Sem isso, uma
    matéria que diz "clássico no dia 7" virou "o clássico já passou" no
    roteiro (job 164, barrado na revisão) — o LLM não sabe o mês. Fonte:
    calendário do Portal (/matches, mais completo); ESPN de reserva."""
    from datetime import datetime, timedelta, timezone

    brt = timezone(timedelta(hours=-3))
    agora = datetime.now(timezone.utc)
    proximos = []
    try:
        resp = httpx.get(f"{PORTAL_API}/matches", timeout=_TIMEOUT)
        resp.raise_for_status()
        for m in resp.json().get("upcoming") or []:
            quando = datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00"))
            if quando >= agora:
                proximos.append({
                    "jogo": f"{m['home']['name']} x {m['away']['name']}",
                    "mando": _mando(m["home"]["name"], m["away"]["name"], m.get("venue")),
                    "competicao": m.get("competition"),
                    "data": quando.astimezone(brt).strftime("%d/%m/%Y às %Hh%M"),
                    "local": m.get("venue"),
                })
    except Exception as exc:  # noqa: BLE001 — cai pra ESPN
        log.warning("calendário do Portal indisponível (%s) — usando ESPN", exc)
    if not proximos:
        for e in sorted((e for e in _all_events() if not e["completed"] and e["date"]), key=lambda e: e["date"]):
            quando = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if quando >= agora:
                jogo = f"{e['home']} x {e['away']}" if e.get("home") and e.get("away") else e["name"]
                proximos.append({
                    "jogo": jogo,
                    "mando": _mando(e.get("home"), e.get("away")),
                    "competicao": LEAGUES_NOME.get(e["liga_nome"], e["liga_nome"]),
                    "data": quando.astimezone(brt).strftime("%d/%m/%Y às %Hh%M"),
                })
    return {"hoje": agora.astimezone(brt).strftime("%d/%m/%Y"), "proximos_jogos": proximos[:3]}


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


# nome da estatística dos destaques vem em inglês da ESPN — o roteiro
# copiava ("Gabriel Batista fez 4 saves", vídeo r1BmNW1nzKQ)
_CATEGORIA_PT = {
    "saves": "defesas",
    "defensiveInterventions": "intervenções defensivas",
    "totalShots": "finalizações",
    "shotsOnTarget": "finalizações no alvo",
    "accuratePasses": "passes certos",
    "totalPasses": "passes",
    "goals": "gols",
    "goalAssists": "assistências",
    "totalTackles": "desarmes",
    "interceptions": "interceptações",
    "totalClearance": "cortes",
    "foulsCommitted": "faltas cometidas",
    "wonCorners": "escanteios",
}


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
        parse_goal(ev.get("text") or "", ev.get("clock", {}).get("displayValue"), (ev.get("type") or {}).get("text") or "")
        for ev in summary.get("keyEvents") or []
        if re.match(r"(own )?goal|penalty - scored", (ev.get("type", {}).get("text") or "").lower())
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
                        "categoria": _CATEGORIA_PT.get(categoria.get("name"), categoria.get("name")),
                        "valor": top.get("displayValue"),
                    })
            if destaques:
                facts["destaques_botafogo"] = destaques

    return facts


# lance do gol como a ESPN descreve ("left footed shot from the centre of
# the box to the bottom left corner") -> português, pro roteiro e pro
# cartão do gol (src/goal_card.py). Ordem importa: o mais específico antes.
_GOL_ORIGEM = [
    ("very close range", "na pequena área"),
    ("six yard box", "na pequena área"),
    ("penalty", "de pênalti"),
    ("outside the box", "de fora da área"),
    ("left side of the box", "do lado esquerdo da área"),
    ("right side of the box", "do lado direito da área"),
    ("centre of the box", "do meio da área"),
    ("difficult angle on the left", "sem ângulo, pela esquerda"),
    ("difficult angle on the right", "sem ângulo, pela direita"),
    ("long range", "de muito longe"),
]
_GOL_DESTINO = [
    ("top left corner", "no ângulo esquerdo"),
    ("top right corner", "no ângulo direito"),
    ("bottom left corner", "no canto esquerdo, rasteiro"),
    ("bottom right corner", "no canto direito, rasteiro"),
    ("high centre of the goal", "no alto, no meio do gol"),
    ("centre of the goal", "no meio do gol"),
]
_GOL_JEITO = [
    ("left footed", "chute de pé esquerdo"),
    ("right footed", "chute de pé direito"),
    ("header", "de cabeça"),
]


def parse_goal(text: str, minuto: str | None, tipo: str = "") -> dict:
    """Gol do keyEvents da ESPN em campos — "Goal! Coritiba 1, Botafogo 1.
    Danilo Santos (Botafogo) left footed shot from the centre of the box to
    the centre of the goal. Assisted by Cristian Medina." Campo que a ESPN
    não traz simplesmente não entra (o roteiro nunca descreve lance que a
    fonte não descreve)."""
    low = text.lower()
    gol: dict = {"minuto": minuto}
    m = re.search(r"([A-Za-zÀ-ÿ '-]+?) (\d+), ([A-Za-zÀ-ÿ '-]+?) (\d+)\.", text)
    if m:
        gol["placar_depois"] = f"{m[1].strip()} {m[2]} x {m[4]} {m[3].strip()}"
    if "own goal" in low or tipo.lower().startswith("own goal"):
        m = re.search(r"own goal by ([^,]+), ([^.]+)\.", text, re.IGNORECASE)
        gol["gol_contra"] = True
        if m:
            gol["autor"], gol["time_do_autor"] = m[1].strip(), m[2].strip()
        return gol
    m = re.search(r"\.\s*([^.()]+?) \(([^)]+)\) (?:converts|left footed|right footed|header|shot)", text)
    if m:
        gol["autor"], gol["time"] = m[1].strip(), m[2].strip()
    if "penalty" in low or tipo.lower().startswith("penalty"):
        gol["penalti"] = True
    for chave, lista in (("jeito", _GOL_JEITO), ("de_onde", _GOL_ORIGEM), ("onde_entrou", _GOL_DESTINO)):
        trecho = low.split(" to the ")[0] if chave != "onde_entrou" else low.split(" to the ")[-1]
        hit = next((pt for en, pt in lista if en in trecho), None)
        if hit:
            gol[chave] = hit
    m = re.search(r"assisted by ([^.]+?)(?: with| following|\.|$)", text, re.IGNORECASE)
    if m:
        gol["assistencia"] = m.group(1).strip()
    return gol


def _norm(text: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


MATCH_PHOTOS_MAX = 8
# foto que não é do lance: arte de estatística da ESPN, logo de portal,
# print de app/ao vivo, thumb de vídeo (vem com tarja de emissora)
_FOTO_NAO_JOGO = re.compile(r"stitcher|logo|share-30-anos|photobooth|video\.glbimg|placeholder", re.IGNORECASE)


def match_photos(rival: str | None, kickoff: str | None) -> tuple[list[dict], list[dict]]:
    """Fotos REAIS da partida pro pós-jogo: capas das matérias do Portal
    publicadas depois do apito final que citam o adversário no título. Sem
    isso o pós-jogo caía no banco de vídeos, que pra futebol traz clube
    estrangeiro e até futebol feminino (13/10). Devolve (fotos, fontes)."""
    from datetime import datetime, timedelta

    if not rival or not kickoff:
        return [], []
    genericas = {"clube", "club", "sport", "esporte", "atletico", "america", "futebol", "regatas", "sociedade"}
    chaves = {w for w in re.findall(r"[a-z]+", _norm(rival)) if len(w) >= 4 and w not in genericas}
    if not chaves:
        return [], []
    inicio = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    janela = (inicio + timedelta(minutes=100), inicio + timedelta(hours=36))
    items: list[dict] = []
    for page in (1, 2, 3):  # a API devolve no máximo 50 por página
        try:
            resp = httpx.get(f"{PORTAL_API}/articles", params={"per_page": 50, "page": page}, timeout=_TIMEOUT)
            resp.raise_for_status()
            items += resp.json().get("items") or []
        except Exception as exc:  # noqa: BLE001 — portal fora: pós-jogo segue sem foto
            log.warning("Portal Botafogo falhou (fotos do jogo): %s", exc)
            break
    fotos, fontes = [], []
    for art in items:
        img, published = art.get("cover_image_url"), art.get("published_at")
        if not img or not published or _FOTO_NAO_JOGO.search(img) or _outro_botafogo(art):
            continue
        quando = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if not janela[0] <= quando <= janela[1] or not chaves & set(re.findall(r"[a-z]+", _norm(art.get("title") or ""))):
            continue
        if not _portal_photo_ok(img, quando) or img in {f["url"] for f in fotos}:
            continue
        veiculo = _nome_veiculo(art.get("source_name"))
        fotos.append({"url": img, "credito": veiculo, "jogo": True})
        fontes.append({"titulo": f"{art['title']} ({veiculo})", "url": f"{PORTAL_SITE}/noticias/{art['slug']}"})
        if len(fotos) == MATCH_PHOTOS_MAX:
            break
    log.info("pós-jogo: %d foto(s) do jogo no portal", len(fotos))
    return fotos, fontes


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
    mando = _mando(event.get("home"), event.get("away"), venue)
    if mando:
        facts["mando"] = mando

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


def daily_portal_task(per_video: int = PORTAL_NEWS_PER_VIDEO) -> dict | None:
    """Vídeo diário com temas do botafogo.win (independente de ter jogo):
    manchetes do Portal; ESPN só de reserva se o Portal estiver fora do ar
    ou sem nada novo."""
    return portal_news_task(per_video) or news_task()


def next_pending_task(news_fallback: bool = True) -> dict | None:
    """Escolhe a próxima tarefa pendente: prioriza recapear o jogo
    concluído mais recente ainda não coberto; se não houver, prevê o
    próximo jogo agendado (dentro do horizonte) ainda não coberto; sem jogo
    nenhum pra cobrir, vídeo de notícias recentes (news_task). `None` quando
    não há nem isso — o chamador simplesmente não gera vídeo nesse dia."""
    events = _all_events()
    if not events:
        return (news_task() or portal_news_task()) if news_fallback else None

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
        rival = next((t for t in (escolhido.get("home"), escolhido.get("away")) if t and "botafogo" not in t.lower()), None)
        fotos, fontes = match_photos(rival, escolhido.get("date"))
        return {"tipo": "pos-jogo", "titulo": f"{placar}: pós-jogo", "facts": facts, "fotos": fotos, "fontes": fontes}

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
        # sem jogo pra cobrir hoje: vídeo com as notícias recentes do clube
        # (ESPN primeiro — matéria completa; senão manchetes do Portal)
        return (news_task() or portal_news_task()) if news_fallback else None

    escolhido = futuros[0]
    summary = _fetch_summary(escolhido["liga_slug"], escolhido["id"])
    previewed.add(escolhido["id"])
    state["previewed"] = sorted(previewed)
    _save_state(state)

    facts = build_preview_facts(escolhido, summary)
    return {"tipo": "pre-jogo", "titulo": f"Botafogo x {facts['adversario']}: prévia", "facts": facts}


def _story_text(html: str) -> str:
    """Texto corrido da matéria — tira tags (<video1>, <alsosee>, links) e
    corta no limite, sem quebrar palavra."""
    import html as html_lib

    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", html_lib.unescape(text)).strip()
    if len(text) > NEWS_STORY_CHARS:
        text = text[:NEWS_STORY_CHARS].rsplit(" ", 1)[0] + "…"
    return text


# aviso de rodapé/chamada do site que o extrator deixa passar e a narração
# lia em voz alta (job 229: "os comentários não representam a opinião do
# jornal e são de responsabilidade do autor")
_RODAPE = re.compile(
    r"[^.!?]*(coment[aá]rios n[aã]o representam|responsabilidade (exclusiva )?d[oe]s? autor|leia (tamb[eé]m|mais)"
    r"|siga o .{0,40} n[oa]s? (redes|instagram|x\b)|clique aqui|inscreva-se|assine (j[aá]|o)|receba as not[ií]cias"
    r"|todos os direitos reservados)[^.!?]*[.!?]?",
    re.IGNORECASE,
)


def _texto_da_fonte(url: str | None) -> str:
    """Texto da matéria original (sem menu/anúncio/rodapé, via trafilatura)
    pra notícia agregada que chega do portal só com o título. "" se não deu
    — quem chama pula a notícia em vez de mandar só o título pro roteiro."""
    if not url:
        return ""
    try:
        import trafilatura

        resp = httpx.get(url, timeout=12, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
        })
        resp.raise_for_status()
        texto = re.sub(r"\s+", " ", trafilatura.extract(resp.text) or "").strip()
        texto = _RODAPE.sub("", texto).strip()
    except Exception as exc:  # noqa: BLE001 — site fora/bloqueando: pula a notícia
        log.warning("texto da fonte indisponível (%s): %s", url, exc)
        return ""
    if len(texto) < 300:
        return ""
    if len(texto) > NEWS_STORY_CHARS:
        texto = texto[:NEWS_STORY_CHARS].rsplit(" ", 1)[0] + "…"
    return texto


def _is_about_botafogo(article: dict) -> bool:
    """O feed filtrado pelo time também traz matéria que só CITA o Botafogo
    de passagem (ranking com 20 clubes, lista da Seleção) — só vale a que tem
    o clube (ou a SAF/Textor) no TÍTULO (no resumo não basta: o ranking do
    Palmeiras passava por citar o Botafogo lá)."""
    text = (article.get("headline") or "").lower()
    return any(k in text for k in ("botafogo", "fogão", "textor", "alvinegr"))


def _photo_is_fresh(url: str, published) -> bool:
    """Data da foto vem do caminho da URL na CDN da ESPN
    (`/photo/2026/0925/...`, `/common/2026/0919/...`). Sem data legível,
    descarta — melhor cair pra outra imagem do que arriscar foto antiga."""
    from datetime import datetime, timezone

    m = re.search(r"/(20\d\d)/(\d\d)(\d\d)/", url)
    if not m:
        return False
    try:
        photo_day = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    except ValueError:
        return False
    return 0 <= (published - photo_day).days <= NEWS_PHOTO_MAX_AGE_DAYS


def news_task() -> dict | None:
    """Tarefa de vídeo de NOTÍCIAS pro dia sem jogo: até NEWS_PER_VIDEO
    matérias recentes ainda não usadas, com as fotos que vieram nelas.
    `None` quando não tem notícia nova — nesse caso não sai vídeo."""
    from datetime import datetime, timedelta, timezone

    data = _espn_get(NEWS_FEED_URL)
    if not data:
        return None
    state = _load_state()
    used = set(state.get("news", []))
    recentes = _recent_titles(state)
    limite = _news_since(state)

    escolhidas = []
    for art in data.get("headlines") or []:
        art_id = str(art.get("id") or "")
        published = art.get("published")
        if not art_id or art_id in used or not published or not _is_about_botafogo(art):
            continue
        if datetime.fromisoformat(published.replace("Z", "+00:00")) < limite:
            continue
        headline = art.get("headline") or ""
        if _is_duplicate(headline, escolhidas) or _is_duplicate(headline, recentes):
            log.info("notícia repetida (mesma história): %s", headline)
            continue
        escolhidas.append(art)
        if len(escolhidas) == NEWS_PER_VIDEO:
            break
    if not escolhidas:
        return None

    state["news"] = sorted(used | {str(a["id"]) for a in escolhidas})
    _remember_titles(state, [a.get("headline") or "" for a in escolhidas])
    state["last_news_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    noticias, fotos, fontes = [], [], []
    for art in escolhidas:
        noticias.append({
            "titulo": art.get("headline"),
            "resumo": art.get("description"),
            "texto": _story_text(art.get("story") or ""),
            "publicado_em": art.get("published"),
        })
        link = ((art.get("links") or {}).get("web") or {}).get("href")
        if link:
            fontes.append({"titulo": art.get("headline"), "url": link})
        published = datetime.fromisoformat(art["published"].replace("Z", "+00:00"))
        for img in art.get("images") or []:
            if not img.get("url") or not _photo_is_fresh(img["url"], published):
                log.info("foto descartada (antiga ou sem data): %s", img.get("url"))
                continue
            if img["url"] not in {f["url"] for f in fotos}:
                fotos.append({"url": img["url"], "credito": img.get("credit"), "noticia": len(noticias) - 1})

    facts = {"tipo": "noticias", "noticias": noticias, **_agenda()}
    return {
        "tipo": "noticias",
        "titulo": _news_topic([a.get("headline") or "" for a in escolhidas]),
        "facts": facts,
        "fotos": fotos,
        "fontes": fontes,
    }


# palavra que aparece em quase toda manchete do clube não diz que é a mesma
# história ("Botafogo" contava e ainda assim "Danilo elogia Tite" x "Danilo
# projeta final contra a Chape" — a mesma entrevista — passavam, 15/10)
_GENERIC_TITLE_WORDS = {
    "botafogo", "fogão", "fogao", "glorioso", "alvinegro", "alvinegra", "estrela", "solitária",
    "para", "pelo", "pela", "contra", "sobre", "após", "apos", "mais", "como", "diz", "afirma",
}
# manchetes já usadas ficam lembradas uns dias: a mesma história volta no dia
# seguinte com outro título (e outro id), em outro veículo
RECENT_TITLES_DAYS = 3


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"\w+", title.lower()) if len(w) > 3 and w not in _GENERIC_TITLE_WORDS}


def _subject(title: str) -> str | None:
    """Protagonista da manchete: o nome próprio que a abre ("Danilo projeta…",
    "Ex-Athletico e Botafogo, Eduardo Freeland…" não tem)."""
    m = re.match(r"\s*([A-ZÀ-Ý][\wÀ-ÿ]+(?: [A-ZÀ-Ý][\wÀ-ÿ]+)?)\s+[a-zà-ÿ]", title)
    if not m or m[1].lower() in _GENERIC_TITLE_WORDS:
        return None
    return m[1].lower()


def _is_duplicate(title: str, chosen: list[dict] | list[str]) -> bool:
    """Mesma história já escolhida (ou usada nos últimos dias): muitas
    palavras próprias em comum, 2+ nomes em comum, ou o mesmo protagonista
    abrindo a manchete."""
    words = _title_words(title)
    subject = _subject(title)
    for other in chosen:
        other_title = other if isinstance(other, str) else other.get("title") or other.get("headline") or ""
        o = _title_words(other_title)
        if not words or not o:
            continue
        common = words & o
        if len(common) / min(len(words), len(o)) > PORTAL_DUP_OVERLAP or len(common) >= 3:
            return True
        if subject and subject == _subject(other_title):
            return True
        names = {w for w in common if re.search(rf"\b{re.escape(w)}\b", other_title.lower())
                 and re.search(rf"\b{w[0].upper()}{re.escape(w[1:])}\b", title)}
        if len(names) >= 2:
            return True
    return False


def _news_since(state: dict):
    """Só entra notícia publicada depois do último vídeo de notícias (ESPN ou
    portal) — e nunca mais velha que NEWS_MAX_AGE_HOURS, mesmo depois de dias
    sem vídeo."""
    from datetime import datetime, timedelta, timezone

    limite = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
    try:
        return max(limite, datetime.fromisoformat(state["last_news_at"]))
    except (KeyError, ValueError, TypeError):
        return limite


def _recent_titles(state: dict) -> list[str]:
    from datetime import date, timedelta

    desde = (date.today() - timedelta(days=RECENT_TITLES_DAYS)).isoformat()
    return [t["title"] for t in state.get("recent_titles", []) if t.get("date", "") >= desde]


def _remember_titles(state: dict, titles: list[str]) -> None:
    from datetime import date, timedelta

    desde = (date.today() - timedelta(days=RECENT_TITLES_DAYS)).isoformat()
    hoje = date.today().isoformat()
    state["recent_titles"] = [t for t in state.get("recent_titles", []) if t.get("date", "") >= desde] + [
        {"title": t, "date": hoje} for t in titles if t
    ]


def _portal_photo_ok(url: str, published) -> bool:
    """Mesma regra da ESPN (foto de arquivo engana o torcedor) pra quando a
    URL TEM data: `/uploads/2026/09/...` vale no mesmo mês da matéria,
    `20261002-153711.jpg` (netvasco) vale até 7 dias antes. Sem data
    nenhuma na URL (ge, O Globo, Terra mudam o caminho) a capa é da própria
    matéria e entra — antes era descartada, e o vídeo caía em clipe de
    banco com estádio e camisa de clube estrangeiro. Thumb do YouTube
    (live/vídeo de canal, cheia de texto) nunca entra."""
    from datetime import datetime, timezone

    # thumb do YouTube e print de tela (programa de TV com logo/tarja de
    # outra emissora — job 239 saiu com um print do SBT) nunca entram
    if "ytimg.com" in url or re.search(r"captura|screenshot|screen-shot|print-?de-?tela", url, re.IGNORECASE):
        return False
    if _photo_is_fresh(url, published):
        return True
    m = re.search(r"/(20\d\d)/(\d\d)/", url)
    if m:
        return (int(m[1]), int(m[2])) == (published.year, published.month)
    m = re.search(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)", url)
    if m:
        try:
            photo_day = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return False
        return 0 <= (published - photo_day).days <= 7
    if re.search(r"/(20\d\d)/(\d{4})/", url):
        return False  # padrão da ESPN com data velha (já reprovado acima)
    return True


# outros "Botafogo" que o agregador do portal pega por palavra-chave: o
# Botafogo-SP (Ribeirão Preto, "Pantera", cobertura do Futebol Interior),
# o Botafogo-PB e o córrego Botafogo de Goiânia (O Popular) — já saíram no
# vídeo do Fogão como se fossem notícia do clube.
_OUTRO_BOTAFOGO = re.compile(
    r"botafogo ?(-|\()(sp|pb)\b|ribeir[aã]o preto|pantera|c[oó]rrego|goi[aâ]nia|jo[aã]o pessoa|belo jardim"
    # o BAIRRO Botafogo (job 229: chuva, árvore caída na Rua São Clemente)
    r"|\brua\b|bairro|chuva|alagamento|bols[oõ]es|[aá]rvore|tr[aâ]nsito|tiroteio|assalto|zona sul|praia de botafogo|enseada",
    re.IGNORECASE,
)
_OUTRO_BOTAFOGO_FONTES = ("futebolinterior.com.br", "opopular.com.br")


_VEICULO_DOMINIO = {
    "odia.ig.com.br": "O Dia", "lance.com.br": "Lance", "itatiaia.com.br": "Rádio Itatiaia",
    "flamengo.com.br": "site do Flamengo", "jornalcruzeiro.com.br": "Jornal Cruzeiro", "fluminense.com.br": "site do Fluminense",
}


def _nome_veiculo(nome: str | None) -> str:
    """Nome falável do veículo: o agregador do portal manda "LANCE! (via
    Google Notícias)", "ge — Botafogo", "odia.ig.com.br", "John Textor
    (@Textor44) no X" — lido em voz alta isso vira "via Google Notícias",
    endereço soletrado e arroba no meio da narração."""
    nome = re.sub(r"\s*\(via Google Not[ií]cias\)\s*$", "", (nome or "").strip())
    nome = re.sub(r"\s+[—–-]\s+.*$", "", nome)
    nome = re.sub(r"^(.+?)\s*\(@\w+\)\s+no X$", r"\1, no X", nome)
    if re.fullmatch(r"[\w-]+(\.[\w-]+)*\.(com|org|net)(\.br)?", nome, re.IGNORECASE):
        nome = _VEICULO_DOMINIO.get(nome.lower(), nome.split(".")[0].capitalize())
    return nome or "Portal Botafogo"


def _outro_botafogo(art: dict) -> bool:
    texto = art.get("title") or ""  # resumo cita "Botafogo-SP" de passagem (adversário)
    fonte = f"{art.get('source_url') or ''} {art.get('source_name') or ''}".lower()
    return bool(_OUTRO_BOTAFOGO.search(texto)) or any(f in fonte or f.split(".")[0] in fonte.replace(" ", "")
                                                      for f in _OUTRO_BOTAFOGO_FONTES)


PORTAL_MIN_EXCERPT = 400  # resumo menor que isso é chamada, não a notícia
_PROMO_RE = re.compile(
    r"chave pix|\bcupom\b|seja membro|whatsapp\.com/channel|inscreva-se no canal|ative o sininho|"
    r"compre na|link na bio|\bjoin\b",
    re.IGNORECASE,
)


def _is_video_or_promo(art: dict) -> bool:
    url = (art.get("source_url") or "").lower()
    if "youtube.com" in url or "youtu.be" in url:
        return True
    return bool(_PROMO_RE.search(art.get("excerpt") or ""))


def portal_news_task(per_video: int = PORTAL_NEWS_PER_VIDEO) -> dict | None:
    """Tarefa de notícias a partir do Portal Botafogo (reserva da ESPN): até
    PORTAL_NEWS_PER_VIDEO manchetes recentes, de assuntos diferentes, ainda
    não usadas. Matéria própria do portal (authored) vem com o texto
    inteiro; agregada, só com o resumo do veículo."""
    from datetime import datetime, timedelta, timezone

    try:
        resp = httpx.get(f"{PORTAL_API}/articles", params={"per_page": 50}, timeout=_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("items") or []
    except Exception as exc:  # noqa: BLE001 — portal fora do ar: sem vídeo hoje
        log.warning("Portal Botafogo falhou: %s", exc)
        return None

    state = _load_state()
    used = set(state.get("portal_news", []))
    recentes = _recent_titles(state)
    limite = _news_since(state)

    # matéria própria do portal primeiro: é a única com texto inteiro (as
    # agregadas só têm resumo) — rende o roteiro mais rico
    items.sort(key=lambda a: a.get("source_type") != "authored")
    escolhidas: list[dict] = []
    for art in items:
        published = art.get("published_at")
        title = (art.get("title") or "").strip()
        # post de live/vídeo de canal (ex.: "LIVE DO SETOR") não é notícia
        if not published or str(art["id"]) in used or len(title) < 25 or "live" in title.lower().split():
            continue
        if _outro_botafogo(art):
            log.info("portal: pulando notícia de outro Botafogo: %s", title)
            continue
        if datetime.fromisoformat(published.replace("Z", "+00:00")) < limite:
            continue
        if _is_duplicate(title, escolhidas) or _is_duplicate(title, recentes):
            log.info("portal: notícia repetida (mesma história): %s", title)
            continue
        if art.get("source_type") != "authored" and _is_video_or_promo(art):
            # descrição de vídeo do YouTube ("CUPOM GOSETOR", "Chave PIX",
            # "SEJA MEMBRO") não é notícia — job 305
            log.info("portal: pulando vídeo/propaganda: %s", title)
            continue
        if art.get("source_type") != "authored" and len((art.get("excerpt") or "").strip()) < PORTAL_MIN_EXCERPT:
            # agregada sem resumo ou só com chamada ("Seis nomes terão o
            # vínculo encerrado... Saiba mais detalhes", sem os nomes — job
            # 305): só isso fazia o roteiro "encher linguiça" sem o fato
            art["_texto"] = _texto_da_fonte(art.get("source_url"))
            if not art["_texto"]:
                log.info("portal: pulando notícia sem texto: %s", title)
                continue
        escolhidas.append(art)
        if len(escolhidas) == per_video:
            break
    if not escolhidas:
        return None

    state["portal_news"] = sorted(used | {str(a["id"]) for a in escolhidas})
    _remember_titles(state, [a["title"] for a in escolhidas])
    state["last_news_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    return _portal_task(escolhidas)


def portal_task_from_slugs(slugs: list[str]) -> dict | None:
    """Refaz a tarefa de um vídeo já publicado a partir das matérias que ele
    usou (links botafogo.win/noticias/<slug> da descrição) — pra regravar
    um vídeo com erro sem depender das matérias ainda estarem no topo."""
    escolhidas = []
    for slug in slugs:
        try:
            resp = httpx.get(f"{PORTAL_API}/articles/{slug}", timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("portal: matéria %s indisponível: %s", slug, exc)
            continue
        art = resp.json()
        if art.get("source_type") != "authored" and len((art.get("excerpt") or "").strip()) < PORTAL_MIN_EXCERPT:
            art["_texto"] = _texto_da_fonte(art.get("source_url"))
        escolhidas.append(art)
    return _portal_task(escolhidas) if escolhidas else None


# notícia de outra modalidade do clube (job 458: clássico do NBB "pode ser
# na Tijuca" virou jogo de futebol na narração) — vai marcada pro roteiro
_MODALIDADES = [
    ("basquete", re.compile(r"\b(nbb|basquete|basketball|fiba|liga de las am[eé]ricas|champions league das am[eé]ricas)\b", re.I)),
    ("vôlei", re.compile(r"\b(v[oô]lei|volleyball|superliga)\b", re.I)),
    ("futsal", re.compile(r"\bfutsal\b", re.I)),
    ("futebol de areia", re.compile(r"\bbeach soccer|futebol de areia\b", re.I)),
    ("polo aquático", re.compile(r"\bpolo aqu[aá]tico\b", re.I)),
    ("e-sports", re.compile(r"\be-?sports?\b", re.I)),
]


def _modalidade(art: dict) -> str | None:
    texto = f"{art.get('title') or ''} {art.get('excerpt') or ''}"
    for nome, pattern in _MODALIDADES:
        if pattern.search(texto):
            return nome
    return None


def _news_topic(titulos: list[str]) -> str:
    """Tema do vídeo de notícias: TODAS as manchetes, não só a primeira —
    com só a 1ª ("Zorro Alvinegro...", 15/10) o roteiro e o título viravam
    a história dela e as outras entravam como desvio de assunto."""
    titulos = [t for t in titulos if t]
    if len(titulos) == 1:
        return f"Notícias do Botafogo: {titulos[0]}"
    return f"As {len(titulos)} notícias do Botafogo hoje: " + " | ".join(titulos)


def _portal_task(escolhidas: list[dict]) -> dict:
    from datetime import datetime

    noticias, fotos, fontes = [], [], []
    for art in escolhidas:
        texto = art.get("_texto") or ""
        if art.get("source_type") == "authored":
            try:
                detail = httpx.get(f"{PORTAL_API}/articles/{art['slug']}", timeout=_TIMEOUT).json()
                texto = _story_text(detail.get("body_html") or "")
            except Exception as exc:  # noqa: BLE001 — fica só com o resumo
                log.warning("texto da matéria %s indisponível: %s", art["slug"], exc)
        veiculo = _nome_veiculo(art.get("source_name"))
        noticia = {
            "titulo": art["title"],
            "resumo": art.get("excerpt"),
            "texto": texto,
            "veiculo": veiculo,
            "publicado_em": art["published_at"],
        }
        if modalidade := _modalidade(art):
            noticia["modalidade"] = modalidade
        noticias.append(noticia)
        # link sempre pro portal (a página da matéria lá já credita e linka o
        # veículo original) — leva a audiência do canal pro botafogo.win
        fontes.append({"titulo": f"{art['title']} ({veiculo})", "url": f"{PORTAL_SITE}/noticias/{art['slug']}"})
        published = datetime.fromisoformat(art["published_at"].replace("Z", "+00:00"))
        img = art.get("cover_image_url")
        if img and _portal_photo_ok(img, published) and img not in {f["url"] for f in fotos}:
            fotos.append({"url": img, "credito": veiculo, "noticia": len(noticias) - 1})

    return {
        "tipo": "noticias",
        "titulo": _news_topic([a["title"] for a in escolhidas]),
        "facts": {"tipo": "noticias", "noticias": noticias, **_agenda()},
        "fotos": fotos,
        "fontes": fontes,
    }
