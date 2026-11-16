"""Lições permanentes: cada erro que reprova um roteiro/vídeo é guardado POR CANAL e volta
no prompt do roteirista pra sempre (não só nos últimos 8 jobs).

Duas camadas:
- regra geral por TIPO de erro (ex.: atribuicao_falsa): vira instrução fixa assim que o tipo
  aparece 2+ vezes no canal;
- erros específicos que se REPETEM (mesma frase/assunto): os que mais voltaram vêm primeiro.
Nada expira. Banco: tabela `licoes` em data/queue.db."""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .db import connect_db

log = logging.getLogger("licoes")
QUEUE_DB = Path(__file__).resolve().parent.parent / "data" / "queue.db"

# instrução geral por tipo de erro de fato (tipos de src/fact_check._TYPES) e de vídeo
REGRA_POR_TIPO = {
    "ferramenta_inexistente": "NUNCA atribua a uma ferramenta/modelo função, recurso ou preço que as fontes não citam por nome. Na dúvida, não cite a função.",
    "fato_nao_confirmado": "Só escreva fato que esteja escrito nas fontes. Sem fonte clara, corte a afirmação.",
    "atribuicao_falsa": "Não atribua a alguém/algo uma ação, frase ou capacidade que a fonte não atribui a ele. Cite só o que a fonte diz de quem.",
    "acusacao_sem_base": "Nunca acuse pessoa ou empresa sem a fonte dizer isso literalmente.",
    "data_desatualizada": "Data, preço e número têm de vir da fonte mais recente; sem data na fonte, não escreva data.",
    "titulo_enganoso": "O título só promete o que a narração entrega. Se o roteiro é teoria, o título não pode prometer passo a passo.",
    "titulo_nao_confere": "Título e miniatura só trazem fato que está na narração e nas fontes.",
    "fora_da_fonte": "Cada frase tem de estar apoiada numa fonte. Detalhe, número ou exemplo que só você 'sabe' fica de fora.",
    "caso_generico": "Evite caso genérico inventado ('uma pessoa...'). Use só caso que a fonte descreve.",
    "deboche": "Sem deboche ou ironia com pessoa, grupo ou instituição.",
    "copia_da_fonte": "Nunca copie frase da fonte: reescreva com palavras próprias, na voz do canal.",
    "frase_quebrada": "Título e frases sempre completos (nada terminando em 'com', 'de', 'e'); confira o fim de cada frase.",
    "promessa_sem_conteudo": "Se prometer 'copie este comando/prompt/passo a passo', entregue isso no roteiro; senão não prometa.",
    "unidade_errada": "Confira unidade, escala e conta de todo número (anos x décadas, mil x milhão).",
    "video": "Imagem de cada cena tem de mostrar o assunto daquela cena e variar entre as cenas; nada de imagem repetida ou fora de contexto.",
    "audio": "Texto falado sem nome estrangeiro difícil, sigla solta ou número ambíguo; escreva como se fala.",
    "historico": "Reprovações antigas (sem tipo gravado): só escreva o que as fontes confirmam, nada de detalhe inventado, e não repita os erros específicos abaixo.",
    "humano": "O DONO apagou vídeos deste canal por erro. Releia os casos abaixo e não repita nada parecido: ele não aprova vídeo com erro.",
    "par": "",
    "rejeicao": "",
}
MINIMO_REGRA_GERAL = 2   # o tipo aparece 2+ vezes no canal -> instrução fixa
MINIMO_REPETIDO = 2      # o mesmo erro específico 2+ vezes -> entra na lista de 'já repetiu'


def _chave(texto: str) -> str:
    return re.sub(r"[^a-z0-9à-ú]+", " ", texto.lower()).strip()[:90]


def _tabela(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS licoes ("
        "canal TEXT NOT NULL, tipo TEXT NOT NULL, chave TEXT NOT NULL, texto TEXT NOT NULL, "
        "vezes INTEGER NOT NULL DEFAULT 1, primeira TEXT DEFAULT CURRENT_TIMESTAMP, "
        "ultima TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (canal, tipo, chave))"
    )
    try:
        conn.execute("ALTER TABLE licoes ADD COLUMN avisado INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # coluna já existe


def registrar(canal: str, tipo: str, texto: str, db: Path = QUEUE_DB, vezes: int = 1) -> None:
    """Guarda (ou soma) um erro. Nunca derruba o pipeline."""
    texto = (texto or "").strip()
    if not texto:
        return
    try:
        with closing(connect_db(db)) as conn, conn:
            _tabela(conn)
            conn.execute(
                "INSERT INTO licoes (canal, tipo, chave, texto, vezes) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(canal, tipo, chave) DO UPDATE SET vezes = vezes + ?, "
                "texto = excluded.texto, ultima = CURRENT_TIMESTAMP",
                (canal, tipo, _chave(texto), texto[:300], vezes, vezes),
            )
    except sqlite3.Error as exc:
        log.warning("lição não guardada (%s): %s", tipo, exc)


def registrar_problemas(canal: str, problems: list[dict], db: Path = QUEUE_DB) -> None:
    """Problemas da revisão de fatos: dicts com tipo/termo/motivo."""
    for p in problems or []:
        termo, motivo = str(p.get("termo", "")).strip(), str(p.get("motivo", "")).strip()
        registrar(canal, str(p.get("tipo") or "fato_nao_confirmado"), f"{termo} ({motivo})" if motivo else termo, db)


def registrar_textos(canal: str, tipo: str, itens: list[str], db: Path = QUEUE_DB) -> None:
    for i in itens or []:
        registrar(canal, tipo, i, db)


def regras(canal: str, max_gerais: int = 8, max_repetidos: int = 8, db: Path = QUEUE_DB) -> str:
    """Bloco pro prompt: regras gerais dos tipos que já deram erro neste canal + os erros
    específicos que mais se repetiram. Vazio se nada foi aprendido ainda."""
    try:
        with closing(connect_db(db)) as conn:
            _tabela(conn)
            tipos = conn.execute(
                "SELECT tipo, SUM(vezes) v FROM licoes WHERE canal = ? GROUP BY tipo "
                "HAVING v >= ? ORDER BY v DESC LIMIT ?", (canal, MINIMO_REGRA_GERAL, max_gerais)).fetchall()
            repetidos = conn.execute(
                "SELECT texto, vezes FROM licoes WHERE canal = ? AND vezes >= ? "
                "ORDER BY vezes DESC, ultima DESC LIMIT ?", (canal, MINIMO_REPETIDO, max_repetidos)).fetchall()
    except sqlite3.Error:
        return ""
    linhas = []
    for tipo, n in tipos:
        regra = REGRA_POR_TIPO.get(tipo)
        if regra:
            linhas.append(f"- [{tipo}, já reprovou {n}x neste canal] {regra}")
    bloco = ""
    for tipo, n in _tipos_de_outros_canais(canal, [t for t, _ in tipos], db):
        regra = REGRA_POR_TIPO.get(tipo)
        if regra:
            linhas.append(f"- [{tipo}, reprovou {n}x em OUTROS canais — vale aqui também] {regra}")
    if linhas:
        bloco += ("\n\nREGRAS APRENDIDAS DOS ERROS DESTE CANAL (cada uma já reprovou vídeos de verdade; "
                  "violar qualquer uma = vídeo descartado):\n" + "\n".join(linhas))
    if repetidos:
        bloco += ("\n\nERROS QUE JÁ SE REPETIRAM NESTE CANAL (nunca mais escreva nada parecido):\n"
                  + "\n".join(f"- ({n}x) {t[:200]}" for t, n in repetidos))
    pares = ultimos_pares(canal, 3, db)
    if pares:
        bloco += "\n\nCOMO ESTES ERROS FORAM CORRIGIDOS (imite a forma da versão DEPOIS):\n" + "\n".join(f"- {x}" for x in pares)
    titulos = melhores_titulos(canal, 3, db)
    if titulos:
        bloco += "\n\nTÍTULOS QUE MAIS FUNCIONARAM NESTE CANAL (views reais; mesmo estilo, tema novo):\n" + "\n".join(
            f"- {t} ({v} views)" for t, v in titulos)
    return bloco


def importar_historico(db: Path = QUEUE_DB) -> int:
    """Uma vez: transforma os erros já gravados nos jobs (reprovações de fato/vídeo) em lições."""
    prefixos = {
        "roteiro reprovado na revisão de fatos: ": "historico",
        "vídeo reprovado na revisão antes do upload: ": "video",
        "bloqueio factual depois de uma correção: ": "historico",
        "bloqueio factual depois da troca por fato verificado: ": "historico",
    }
    n = 0
    with closing(connect_db(db)) as conn:
        _tabela(conn)
        if conn.execute("SELECT COUNT(*) FROM licoes").fetchone()[0]:
            return 0  # só uma vez: reimportar dobraria as contagens
        rows = conn.execute("SELECT channel, error FROM jobs WHERE error IS NOT NULL AND error != ''").fetchall()
    for canal, erro in rows:
        for pref, tipo in prefixos.items():
            if erro.startswith(pref):
                from .script_gen import _split_items

                for item in _split_items(erro[len(pref):]):
                    if "indisponível" in item:  # falha de infraestrutura, não erro de conteúdo
                        continue
                    m = re.match(r"([a-z_]+): (.*)", item)
                    t, txt = (m.group(1), m.group(2)) if m and m.group(1) in REGRA_POR_TIPO else (tipo, item)
                    registrar(canal, t, txt, db)
                    n += 1
    return n


# ---------------------------------------------------------------- 16/11: humano, tema e trava
PESO_HUMANO = 3                 # o dono apagar vale mais que uma reprovação automática
REPETIU_COM_REGRA = 3           # erro que volta mesmo com a regra no prompt -> precisa de trava em código
TEMA_REPROVOU_VEZES = 3         # tema que já reprovou 3+ vezes em 21 dias não é sorteado de novo
TEMA_SEMELHANCA = 0.5
_FALHAS = ("roteiro reprovado", "vídeo reprovado", "bloqueio factual", "ressalva de fato", "vídeo com ressalva",
           "revisão de fatos sem convergir", "áudio reprovado", "nome falado errado")


def registrar_humano(canal: str, tema: str, motivo: str, db: Path = QUEUE_DB) -> None:
    """O dono apagou/rejeitou um vídeo: o sinal mais forte que existe."""
    registrar(canal, "humano", f"{tema[:140]} — {motivo[:140]}", db, vezes=PESO_HUMANO)


def importar_apagados(db: Path = QUEUE_DB) -> int:
    """Uma vez: vídeos já apagados pelo dono (status deleted com motivo) viram lição."""
    n = 0
    with closing(connect_db(db)) as conn:
        _tabela(conn)
        if conn.execute("SELECT 1 FROM licoes WHERE tipo = '_marca' AND chave = 'apagados'").fetchone():
            return 0
        rows = conn.execute("SELECT channel, topic, error FROM jobs WHERE status = 'deleted' "
                            "AND error IS NOT NULL AND (error LIKE '%errad%' OR error LIKE '%ressalva%' "
                            "OR error LIKE '%ruim%' OR error LIKE '%factual%')").fetchall()
    for canal, tema, erro in rows:
        registrar_humano(canal, tema, erro, db)
        n += 1
    registrar("_global", "_marca", "apagados", db)
    return n


def _palavras(texto: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ú0-9]+", (texto or "").lower()) if len(w) > 3}


def tema_reprovado(canal: str, tema: str, db: Path = QUEUE_DB) -> str | None:
    """Tema (ou muito parecido) que já reprovou várias vezes neste canal nos últimos 21 dias.
    Devolve o motivo ou None. Evita gastar 20 min num tema que se sabe que não fecha."""
    alvo = _palavras(tema)
    if not alvo:
        return None
    try:
        with closing(connect_db(db)) as conn:
            rows = conn.execute(
                "SELECT topic, error FROM jobs WHERE channel = ? AND created_at > datetime('now', '-21 days') "
                "AND status IN ('retried', 'failed', 'failed_permanent', 'deleted') AND error IS NOT NULL",
                (canal,)).fetchall()
    except sqlite3.Error:
        return None
    falhas: dict[str, int] = {}
    for topic, erro in rows:
        if erro.lower().startswith(_FALHAS) or "apagado" in erro.lower():
            falhas[topic] = falhas.get(topic, 0) + 1
    for topic, n in falhas.items():
        a = _palavras(topic)
        if n >= TEMA_REPROVOU_VEZES and a and len(a & alvo) / len(a | alvo) >= TEMA_SEMELHANCA:
            return f"tema parecido já reprovou {n}x neste canal nos últimos 21 dias: {topic[:100]}"
    return None


def pendentes_de_trava(marcar: bool = True, db: Path = QUEUE_DB) -> list[tuple[str, str, str, int]]:
    """Erros que voltaram {REPETIU_COM_REGRA}+ vezes mesmo com a regra no prompt: o prompt não basta,
    precisam de trava em código. Cada um é avisado uma vez (canal, tipo, texto, vezes)."""
    try:
        with closing(connect_db(db)) as conn, conn:
            _tabela(conn)
            rows = conn.execute(
                "SELECT canal, tipo, texto, vezes, chave FROM licoes WHERE vezes >= ? AND avisado = 0 "
                "AND tipo NOT IN ('historico', '_marca') ORDER BY vezes DESC", (REPETIU_COM_REGRA,)).fetchall()
            if marcar and rows:
                conn.executemany("UPDATE licoes SET avisado = 1 WHERE canal = ? AND tipo = ? AND chave = ?",
                                 [(r[0], r[1], r[4]) for r in rows])
    except sqlite3.Error:
        return []
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def painel(db: Path = QUEUE_DB) -> dict:
    """Tudo que foi aprendido, por canal, pra aba 'Aprendizado' do painel."""
    out: dict = {"canais": {}, "total": 0, "trava": []}
    try:
        with closing(connect_db(db)) as conn:
            _tabela(conn)
            rows = conn.execute(
                "SELECT canal, tipo, texto, vezes, substr(ultima, 1, 10), avisado FROM licoes "
                "WHERE tipo != '_marca' ORDER BY vezes DESC, ultima DESC").fetchall()
    except sqlite3.Error:
        return out
    for canal, tipo, texto, vezes, ultima, avisado in rows:
        c = out["canais"].setdefault(canal, {"tipos": {}, "repetidos": [], "humanos": [], "total": 0})
        c["tipos"][tipo] = c["tipos"].get(tipo, 0) + vezes
        c["total"] += vezes
        out["total"] += vezes
        item = {"texto": texto, "vezes": vezes, "ultima": ultima, "tipo": tipo}
        if tipo == "humano":
            c["humanos"].append(item)
        elif vezes >= MINIMO_REPETIDO and tipo != "historico":
            c["repetidos"].append(item)
        if vezes >= REPETIU_COM_REGRA and tipo not in ("historico",):
            out["trava"].append({**item, "canal": canal})
    for canal, c in out["canais"].items():
        c["taxa"] = taxa_reprovacao(canal, db) if canal != "_global" else {}
        c["pares"] = ultimos_pares(canal, 5, db)
        c["titulos"] = melhores_titulos(canal, 3, db)
        c["tipos"] = [{"tipo": t, "vezes": v, "regra": REGRA_POR_TIPO.get(t, "")}
                      for t, v in sorted(c["tipos"].items(), key=lambda kv: -kv[1]) if t != "par"]
        c["repetidos"] = c["repetidos"][:10]
        c["humanos"] = c["humanos"][:10]
    return out


# ---------------------------------------------------------------- 16/11: pares, títulos, outros canais, medição, Studio
_TIPOS_DE_FATO = {t for t in REGRA_POR_TIPO if t not in ("video", "audio", "historico", "humano", "rejeicao")}
MINIMO_OUTROS_CANAIS = 5


def _frases(script: dict) -> list[str]:
    texto = " ".join(str(sc.get("narration", "")) for sc in (script or {}).get("scenes", []) or [])
    return [f.strip() for f in re.split(r"(?<=[.!?])\s+", texto) if f.strip()]


def registrar_pares(canal: str, antes: dict, depois: dict, problems: list[dict], db: Path = QUEUE_DB) -> int:
    """Conserto bem-sucedido: guarda a frase reprovada e como ficou (errado -> corrigido)."""
    import difflib

    novas, n = _frases(depois), 0
    for p in problems or []:
        termo = str(p.get("termo", "")).strip().lower()
        if len(termo) < 8:
            continue
        velha = next((f for f in _frases(antes) if termo[:40] in f.lower()), None)
        if not velha or not novas:
            continue
        melhor = max(novas, key=lambda f: difflib.SequenceMatcher(None, velha.lower(), f.lower()).ratio())
        if melhor != velha and difflib.SequenceMatcher(None, velha.lower(), melhor.lower()).ratio() >= 0.35:
            registrar(canal, "par", f"ANTES: {velha[:140]} | DEPOIS: {melhor[:140]}", db)
            n += 1
    return n


def ultimos_pares(canal: str, n: int = 3, db: Path = QUEUE_DB) -> list[str]:
    try:
        with closing(connect_db(db)) as conn:
            _tabela(conn)
            return [r[0] for r in conn.execute(
                "SELECT texto FROM licoes WHERE canal = ? AND tipo = 'par' ORDER BY ultima DESC LIMIT ?", (canal, n))]
    except sqlite3.Error:
        return []


def melhores_titulos(canal: str, n: int = 3, db: Path = QUEUE_DB) -> list[tuple[str, int]]:
    """Aprender com o que DEU CERTO: títulos do canal com mais views reais (mín. 20 views)."""
    try:
        with closing(connect_db(db)) as conn:
            return [(r[0], r[1]) for r in conn.execute(
                "SELECT title, views_current FROM video_performance WHERE channel = ? AND title IS NOT NULL "
                "AND title != '' AND views_current >= 20 ORDER BY views_current DESC LIMIT ?", (canal, n))]
    except sqlite3.Error:
        return []


def _tipos_de_outros_canais(canal: str, ja_tem: list[str], db: Path) -> list[tuple[str, int]]:
    """Tipo de erro de fato que reprovou muito em OUTROS canais e ainda não aparece neste."""
    try:
        with closing(connect_db(db)) as conn:
            rows = conn.execute(
                "SELECT tipo, SUM(vezes) v FROM licoes WHERE canal != ? AND canal != '_global' GROUP BY tipo "
                "HAVING v >= ? ORDER BY v DESC", (canal, MINIMO_OUTROS_CANAIS)).fetchall()
    except sqlite3.Error:
        return []
    return [(t, v) for t, v in rows if t in _TIPOS_DE_FATO and t not in ja_tem][:4]


def taxa_reprovacao(canal: str, db: Path = QUEUE_DB) -> dict:
    """Medição: % de tentativas reprovadas nos últimos 7 dias x nos 7 anteriores (está melhorando?)."""
    def _janela(ini: str, fim: str):
        with closing(connect_db(db)) as conn:
            tot, ruins = conn.execute(
                "SELECT COUNT(*), SUM(status IN ('retried','failed','failed_permanent','deleted')) FROM jobs "
                "WHERE channel = ? AND created_at >= datetime('now', ?) AND created_at < datetime('now', ?)",
                (canal, ini, fim)).fetchone()
        return int(tot or 0), int(ruins or 0)
    try:
        t1, r1 = _janela("-7 days", "+1 day")
        t0, r0 = _janela("-14 days", "-7 days")
    except sqlite3.Error:
        return {}
    return {"atual": round(100 * r1 / t1) if t1 else None, "antes": round(100 * r0 / t0) if t0 else None,
            "n_atual": t1, "n_antes": t0}


def sincronizar_apagados(db: Path = QUEUE_DB) -> int:
    """Vídeo publicado que sumiu do YouTube (você apagou no Studio): vira lição humana e o job vira 'deleted'.
    1 chamada videos.list por canal (1 unidade de cota)."""
    from googleapiclient.discovery import build

    from .config import ChannelConfig
    from .upload import _load_credentials
    from .yt_quota import spend

    achados = 0
    with closing(connect_db(db)) as conn:
        rows = conn.execute(
            "SELECT id, channel, topic, youtube_video_id FROM jobs WHERE status = 'uploaded' AND youtube_video_id IS NOT NULL "
            "AND youtube_video_id != '' AND created_at > datetime('now', '-14 days')").fetchall()
    por_canal: dict[str, list[tuple]] = {}
    for r in rows:
        por_canal.setdefault(r[1], []).append(r)
    for canal, jobs in por_canal.items():
        try:
            spend("list")
            yt = build("youtube", "v3", credentials=_load_credentials(ChannelConfig.load(canal).token_file),
                       cache_discovery=False)
            vivos = set()
            for i in range(0, len(jobs), 50):
                ids = ",".join(j[3] for j in jobs[i:i + 50])
                vivos |= {it["id"] for it in yt.videos().list(part="id", id=ids).execute().get("items", [])}
        except Exception as exc:  # noqa: BLE001 — sem token/cota: tenta amanhã, nunca derruba a rodada
            log.warning("[%s] não consegui conferir vídeos apagados: %s", canal, exc)
            continue
        for jid, _c, tema, vid in jobs:
            if vid not in vivos:
                registrar_humano(canal, tema, "apagado por você no YouTube Studio", db)
                with closing(connect_db(db)) as conn, conn:
                    conn.execute("UPDATE jobs SET status = 'deleted', error = 'apagado no YouTube Studio (detectado)' WHERE id = ?", (jid,))
                achados += 1
    return achados
