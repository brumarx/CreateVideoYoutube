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
    if linhas:
        bloco += ("\n\nREGRAS APRENDIDAS DOS ERROS DESTE CANAL (cada uma já reprovou vídeos de verdade; "
                  "violar qualquer uma = vídeo descartado):\n" + "\n".join(linhas))
    if repetidos:
        bloco += ("\n\nERROS QUE JÁ SE REPETIRAM NESTE CANAL (nunca mais escreva nada parecido):\n"
                  + "\n".join(f"- ({n}x) {t[:200]}" for t, n in repetidos))
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
