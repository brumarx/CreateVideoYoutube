"""Busca fatos factuais e verificáveis no banco de transparência política
(/var/www/html/politica/data/politica.db — dados oficiais TSE, Câmara,
Senado, Portal da Transparência) para alimentar roteiros com números reais
em vez de a IA inventar. Somente leitura (conecta em modo `ro`).

Todo valor vem sempre atribuído à fonte oficial ("segundo declaração ao
TSE", "segundo dados da Câmara") — a base tem duplicatas e alguns outliers
de digitação (ex.: patrimônio na casa dos bilhões), então tratamos como
"o que foi declarado", não como fato verificado por nós.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("/var/www/html/politica/data/politica.db")

# O maior patrimônio de parlamentar verificado externamente (fora desta
# base) é ~R$239,7 milhões (Oriovisto Guimarães). Valores bem acima disso
# na base tendem a ser erro de digitação na declaração original (casa
# decimal a mais) — melhor excluir do que arriscar um dado absurdo no vídeo.
PATRIMONIO_SANITY_CAP = 260_000_000


def _last_complete_year(conn: sqlite3.Connection, table: str) -> int:
    """MAX(ano) da tabela, mas nunca o ano corrente em andamento — um total
    de "gastos do ano" com o ano ainda não terminado passaria a impressão
    errada de ranking anual completo quando é só parcial (até hoje)."""
    max_ano = conn.execute(f"SELECT MAX(ano) FROM {table}").fetchone()[0]
    if max_ano is None:
        return date.today().year - 1
    if max_ano >= date.today().year:
        return max_ano - 1
    return max_ano


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco de política não encontrado: {DB_PATH}")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def top_ceap_spenders(ano: int | None = None, limit: int = 5) -> list[dict]:
    """Deputados que mais gastaram na cota parlamentar (CEAP) no ano."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "despesas_deputados")
        rows = conn.execute(
            """
            SELECT d.nome, d.sigla_partido, d.sigla_uf, SUM(e.valor_liquido) AS total
            FROM despesas_deputados e
            JOIN deputados d ON d.id = e.deputado_id
            WHERE e.ano = ?
            GROUP BY e.deputado_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (ano, limit),
        ).fetchall()
    return [
        {"nome": r[0], "partido": r[1], "uf": r[2], "total_gasto": round(r[3], 2), "ano": ano}
        for r in rows
    ]


def top_single_expenses(ano: int | None = None, limit: int = 5) -> list[dict]:
    """Maiores despesas individuais (uma nota fiscal só) da cota parlamentar."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "despesas_deputados")
        rows = conn.execute(
            """
            SELECT DISTINCT d.nome, e.tipo_despesa, e.nome_fornecedor, e.valor_liquido, e.data_documento
            FROM despesas_deputados e
            JOIN deputados d ON d.id = e.deputado_id
            WHERE e.ano = ? AND e.valor_liquido > 0
            ORDER BY e.valor_liquido DESC
            LIMIT ?
            """,
            (ano, limit),
        ).fetchall()
    return [
        {
            "nome": r[0], "tipo_despesa": r[1], "fornecedor": r[2],
            "valor": round(r[3], 2), "data": r[4], "ano": ano,
        }
        for r in rows
    ]


def top_patrimonio(ano: int = 2022, limit: int = 5) -> list[dict]:
    """Maiores patrimônios declarados ao TSE (com filtro de sanidade contra
    erro de digitação óbvio na declaração original)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.nome, pat.valor
            FROM patrimonio pat
            JOIN politicos p ON p.id = pat.politico_id
            WHERE pat.ano = ? AND pat.valor < ?
            ORDER BY pat.valor DESC
            LIMIT ?
            """,
            (ano, PATRIMONIO_SANITY_CAP, limit),
        ).fetchall()
    return [{"nome": r[0], "patrimonio_declarado": round(r[1], 2), "ano": ano} for r in rows]


def top_score_politico(ano: int | None = None, limit: int = 5) -> list[dict]:
    """Deputados mais bem avaliados no "score político" do próprio site
    (combina presença, proposições apresentadas e uso da cota)."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "score_politico")
        rows = conn.execute(
            """
            SELECT d.nome, d.sigla_partido, d.sigla_uf, s.score_total, s.pct_presenca, s.total_proposicoes
            FROM score_politico s
            JOIN deputados d ON d.id = s.deputado_id
            WHERE s.ano = ?
            ORDER BY s.score_total DESC
            LIMIT ?
            """,
            (ano, limit),
        ).fetchall()
    return [
        {
            "nome": r[0], "partido": r[1], "uf": r[2], "score": round(r[3], 1),
            "pct_presenca": round(r[4], 1), "total_proposicoes": r[5], "ano": ano,
        }
        for r in rows
    ]


FACT_FETCHERS = [
    ("ranking de gastos da cota parlamentar (CEAP)", top_ceap_spenders),
    ("maiores despesas individuais da cota parlamentar", top_single_expenses),
    ("maiores patrimônios declarados ao TSE", top_patrimonio),
    ("ranking de score político (presença + produtividade)", top_score_politico),
]


def random_fact_set(limit: int = 5) -> dict:
    """Escolhe aleatoriamente um dos temas acima e devolve os dados +
    descrição, pronto pra virar contexto de roteiro."""
    label, fetcher = random.choice(FACT_FETCHERS)
    data = fetcher(limit=limit)
    return {"tema": label, "dados": data}
