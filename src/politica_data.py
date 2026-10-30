"""Busca fatos factuais e verificáveis no banco de transparência política
(/var/www/html/politica/data/politica.db — dados públicos oficiais: TSE,
Câmara, Senado, Portal da Transparência, CGU, TCU) para alimentar roteiros
com números reais em vez de a IA inventar. Somente leitura (conecta em modo
`ro` — este projeto NUNCA escreve nesse banco; um outro sistema depende
dele).

Todo valor vem sempre atribuído à fonte oficial ("segundo declaração ao
TSE", "segundo dados da Câmara") — a base tem duplicatas e alguns outliers
de digitação (ex.: patrimônio na casa dos bilhões), então tratamos como
"o que foi declarado", não como fato verificado por nós.

Fetchers de PADRÃO ESTATÍSTICO (`top_suspeitas_*`): o banco tem tabelas
`suspeitas_*` com sinalizações automáticas (ex.: "gasto = 10x o patrimônio
declarado", "valor estatisticamente atípico"). Usar isso é seguro DESDE QUE
a narração nunca afirme irregularidade — só descreva o padrão numérico e
deixe claro que é uma sinalização estatística, não uma acusação ou decisão
judicial. Por isso esses fetchers sempre vêm com um campo "aviso" que entra
no prompt do roteiro. Já as tabelas `ceis_cnep`/`tcu_condenados`/
`ficha_limpa` são registros OFICIAIS FINAIS (sanção/condenação já decidida
por órgão competente) — não precisam desse aviso, só a atribuição de fonte
já usada em todo o resto.
"""
from __future__ import annotations

import json
import random
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# o site (politica-br.org) migrou pro Postgres (schema `dados`, sincronizado
# pelo ETL); o SQLite antigo sai de uso. DSN em ~/.politica.env (POL_PG_DSN)
PG_ENV_FILE = Path.home() / ".politica.env"
STATE_FILE = ROOT / "data" / "used_political_facts.json"
# site dos dados (migrou de brmx.org/politica em 26/10): link da descrição
SITE_URL = "https://politica-br.org"

# O maior patrimônio de parlamentar verificado externamente (fora desta
# base) é ~R$239,7 milhões (Oriovisto Guimarães). Valores bem acima disso
# na base tendem a ser erro de digitação na declaração original (casa
# decimal a mais) — melhor excluir do que arriscar um dado absurdo no vídeo.
PATRIMONIO_SANITY_CAP = 260_000_000

AVISO_SUSPEITA = (
    "ATENÇÃO: isso é uma sinalização ESTATÍSTICA automática (padrão fora do "
    "comum), NÃO uma acusação, decisão judicial ou prova de irregularidade. "
    "Narre só o padrão numérico e deixe claro que pode ter explicação "
    "legítima (ex.: erro de preenchimento, herança, venda de bem) — nunca "
    "diga que a pessoa 'cometeu' algo ou é culpada de algo."
)

AVISO_CRESCIMENTO = (
    "ATENÇÃO: isso é a comparação de patrimônio DECLARADO à Justiça "
    "Eleitoral entre duas candidaturas da mesma pessoa (por CPF) — não é "
    "prova de enriquecimento ilícito. Crescimento de patrimônio tem "
    "inúmeras explicações legítimas (herança, casamento, venda de bem, "
    "sucesso em negócio próprio antes de entrar pra política). Narre só o "
    "fato numérico (\"o patrimônio declarado passou de X para Y entre a "
    "candidatura de ANO1 e ANO2\"), nunca insinue que o cargo público foi a "
    "causa do crescimento. Além disso, é autodeclarado pelo próprio "
    "candidato à Justiça Eleitoral — pode conter erro de digitação (ex.: "
    "casa decimal a mais); sempre diga \"segundo a declaração à Justiça "
    "Eleitoral\", nunca apresente o valor como fato 100% verificado. "
    "Cargo é o DISPUTADO em cada candidatura (a pessoa pode não ter sido "
    "eleita) e muda de uma pra outra — no título, diga \"candidatos\" ou "
    "\"políticos\", nunca um cargo só (\"vereadores\") que não vale pra todos."
)


def _last_complete_year(conn: _PgConn, table: str, column: str = "ano") -> int:
    """MAX(ano) da tabela, mas nunca o ano corrente em andamento — um total
    de "gastos do ano" com o ano ainda não terminado passaria a impressão
    errada de ranking anual completo quando é só parcial (até hoje)."""
    max_ano = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
    if max_ano is None:
        return date.today().year - 1
    if max_ano >= date.today().year:
        return max_ano - 1
    return max_ano


class _PgConn:
    """Conexão Postgres com a cara da do sqlite3 que este módulo usava
    (`conn.execute(sql, params).fetchone()`, `with conn:`): placeholder
    `?` vira `%s` e LIKE vira ILIKE (o LIKE do sqlite ignorava caixa).
    Somente leitura."""

    def __init__(self, dsn: str):
        import psycopg

        from psycopg.types.numeric import FloatLoader

        self._conn = psycopg.connect(dsn, options="-c search_path=dados -c default_transaction_read_only=on")
        # numeric vem como Decimal, que o json.dumps dos dados não aceita
        self._conn.adapters.register_loader("numeric", FloatLoader)

    def execute(self, sql: str, params: tuple = ()):
        sql = re.sub(r"\bLIKE\b", "ILIKE", sql.replace("%", "%%").replace("?", "%s"))
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def __enter__(self) -> "_PgConn":
        return self

    def __exit__(self, *exc) -> None:
        self._conn.close()


def _pg_dsn() -> str:
    for linha in PG_ENV_FILE.read_text().splitlines():
        if linha.startswith("POL_PG_DSN="):
            return linha.split("=", 1)[1].strip()
    raise RuntimeError(f"POL_PG_DSN não encontrado em {PG_ENV_FILE}")


def _connect() -> _PgConn:
    return _PgConn(_pg_dsn())


def _last_complete_month(conn: _PgConn) -> str:
    """Último ano_mes (YYYY-MM) de remuneracao_magistrados com dado
    realmente importado pras 3 cortes (STF/STJ/TCU) — cada corte importa
    com atraso diferente (visto na prática: um mês só com TCU, sem STF/STJ
    ainda), e o TCU não preenche `rendimento_liquido` diretamente (só
    `total_creditos`/`total_debitos`), então nem sempre o MAX(ano_mes) bruto
    tem dado utilizável pras 3."""
    current = date.today().strftime("%Y-%m")
    rows = conn.execute(
        "SELECT DISTINCT ano_mes FROM remuneracao_magistrados WHERE ano_mes < ? ORDER BY ano_mes DESC LIMIT 8",
        (current,),
    ).fetchall()
    for (ano_mes,) in rows:
        n_tribunais = conn.execute(
            """
            SELECT COUNT(DISTINCT m.tribunal)
            FROM remuneracao_magistrados r JOIN magistrados m ON m.id = r.magistrado_id
            WHERE r.ano_mes = ?
              AND COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos) > 0
            """,
            (ano_mes,),
        ).fetchone()[0]
        if n_tribunais >= 2:
            return ano_mes
    return rows[0][0] if rows else current


def top_ceap_spenders(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Deputados que mais gastaram na cota parlamentar (CEAP) no ano."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "despesas_deputados")
        rows = conn.execute(
            """
            SELECT MAX(d.nome), MAX(d.sigla_partido), MAX(d.sigla_uf), SUM(e.valor_liquido) AS total, MAX(d.url_foto)
            FROM despesas_deputados e
            JOIN deputados d ON d.id = e.deputado_id
            WHERE e.ano = ?
            GROUP BY e.deputado_id
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "partido": r[1], "uf": r[2], "total_gasto": round(r[3], 2), "ano": ano, "foto_url": r[4]}
        for r in rows
    ]


def top_single_expenses(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores despesas individuais (uma nota fiscal só) da cota parlamentar."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "despesas_deputados")
        rows = conn.execute(
            """
            SELECT DISTINCT d.nome, e.tipo_despesa, e.nome_fornecedor, e.valor_liquido, e.data_documento, d.url_foto
            FROM despesas_deputados e
            JOIN deputados d ON d.id = e.deputado_id
            WHERE e.ano = ? AND e.valor_liquido > 0
            ORDER BY e.valor_liquido DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {
            "nome": r[0], "tipo_despesa": r[1], "fornecedor": r[2],
            "valor": round(r[3], 2), "data": r[4], "ano": ano, "foto_url": r[5],
        }
        for r in rows
    ]


def top_patrimonio(ano: int = 2022, limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores patrimônios declarados ao TSE (com filtro de sanidade contra
    erro de digitação óbvio na declaração original). Soma os bens de cada
    candidatura em `bens_candidatos` — a tabela `patrimonio` NÃO serve: não
    bate com o TSE nem em reais nem em centavos (Marcelo Rangel declarou
    R$1,46 mi em 2022 e ela dizia R$259,9 mi; job 174 barrado na revisão)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT MAX(c.nm_candidato), MAX(c.ds_cargo), MAX(c.sg_partido), MAX(c.sg_uf), SUM(b.vr_bem) AS total
            FROM bens_candidatos b
            JOIN candidatos c ON c.sq_candidato = b.sq_candidato
            WHERE b.ano_eleicao = ?
            GROUP BY b.sq_candidato
            HAVING SUM(b.vr_bem) < ?
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (str(ano), PATRIMONIO_SANITY_CAP, limit, offset),
        ).fetchall()
    return [
        {
            "posicao_no_ranking": offset + i + 1, "nome": r[0], "cargo_disputado": r[1],
            "partido": r[2], "uf": r[3], "patrimonio_declarado": round(r[4], 2), "ano": ano,
        }
        for i, r in enumerate(rows)
    ]


def top_score_politico(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Deputados mais bem avaliados no "score político" do próprio site
    (combina presença, proposições apresentadas e uso da cota)."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "score_politico")
        rows = conn.execute(
            """
            SELECT d.nome, d.sigla_partido, d.sigla_uf, s.score_total, s.pct_presenca, s.total_proposicoes, d.url_foto,
                   s.rank_geral
            FROM score_politico s
            JOIN deputados d ON d.id = s.deputado_id
            WHERE s.ano = ? AND s.rank_geral IS NOT NULL
            ORDER BY s.rank_geral, lower(public.unaccent(d.nome)), d.id
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {
            # o site publica 2 casas e empates dividem a posição (etl/regras.py); presença sem dado fica None, não 0
            "posicao_no_ranking": r[7], "nome": r[0], "partido": r[1], "uf": r[2], "score": round(r[3], 1),
            "pct_presenca": round(r[4], 1) if r[4] is not None else None, "total_proposicoes": r[5], "ano": ano,
            "foto_url": r[6],
        }
        for r in rows
    ]


def top_cartao_corporativo(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores gastos em cartão corporativo (presidência/ministérios) por
    portador. Exclui portador "SIGILOSO" — nome classificado não é uma
    pessoa identificável, citar isso como "quem mais gastou" seria enganoso."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "cartao_corporativo")
        rows = conn.execute(
            """
            SELECT nome_portador, politico_tipo, MAX(orgao), SUM(valor) AS total
            FROM cartao_corporativo
            WHERE ano = ? AND nome_portador NOT IN ('SIGILOSO', '') AND nome_portador IS NOT NULL
            GROUP BY nome_portador, politico_tipo
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "portador": _PORTADOR_CARTAO.get(r[1], r[1]), "orgao": r[2], "total_gasto": round(r[3], 2), "ano": ano}
        for r in rows
    ]


# `politico_tipo` do cartão é código do ETL — o roteiro dizia "cargo orgao_federal"
_PORTADOR_CARTAO = {"presidencia": "servidor da Presidência da República", "orgao_federal": "servidor de órgão federal"}


def top_emendas(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Deputados que mais destinaram em emendas parlamentares pagas no ano.
    Filtra fora bancadas/comissões (autor_partido vazio) — só parlamentar
    individual identificado."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "emendas_parlamentares")
        rows = conn.execute(
            """
            SELECT MAX(em.autor_nome), MAX(em.autor_partido), MAX(em.autor_uf), SUM(em.valor_pago) AS total, MAX(d.url_foto)
            FROM emendas_parlamentares em
            LEFT JOIN deputados d ON d.id = em.deputado_id
            WHERE em.ano = ? AND em.deputado_id IS NOT NULL AND em.autor_partido != ''
            GROUP BY em.deputado_id
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "partido": r[1], "uf": r[2], "total_pago": round(r[3], 2), "ano": ano, "foto_url": r[4]}
        for r in rows
    ]


def top_contratos_publicos(limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores contratos públicos federais por valor final (registro
    individual — cada linha é um contrato real, não agregado)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT nome_empresa, nome_orgao, objeto, valor_final
            FROM contratos_publicos
            WHERE nome_empresa != '' AND valor_final > 0
            ORDER BY valor_final DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"empresa": r[0], "orgao": r[1], "objeto": (r[2] or "")[:200], "valor": round(r[3], 2)}
        for r in rows
    ]


def top_doacoes_campanha(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores doadores individuais de campanha (candidato a deputado/
    senador) num ano eleitoral."""
    with _connect() as conn:
        if ano is None:
            ano = 2022  # só há 2018/2022 na base — pega sempre o mais recente
        rows = conn.execute(
            """
            SELECT nome_doador, SUM(valor_receita) AS total
            FROM campanha_receitas
            WHERE ano_eleicao = ? AND nome_doador NOT IN ('', 'NAO INFORMADO') AND nome_doador IS NOT NULL
            GROUP BY nome_doador
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [{"doador": r[0], "total_doado": round(r[1], 2), "ano_eleicao": ano} for r in rows]


def top_fornecedores_campanha(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Fornecedores que mais receberam de campanhas eleitorais num ano."""
    with _connect() as conn:
        if ano is None:
            ano = 2022
        rows = conn.execute(
            """
            SELECT nome_forn, MAX(desc_cnae), SUM(total_pago) AS total
            FROM campanha_despesas
            WHERE ano_eleicao = ? AND nome_forn NOT IN ('', 'NAO INFORMADO') AND nome_forn IS NOT NULL
            GROUP BY nome_forn
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"fornecedor": r[0], "ramo": r[1], "total_recebido": round(r[2], 2), "ano_eleicao": ano}
        for r in rows
    ]


def top_pib_estados(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Estados com maior PIB total (a coluna de população/per capita vem
    sempre vazia nesta base — usa só o total mesmo, do IBGE)."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "ibge_pib_estado")
        rows = conn.execute(
            """
            SELECT nome_uf, pib_mil_reais
            FROM ibge_pib_estado
            WHERE ano = ? AND pib_mil_reais > 0
            ORDER BY pib_mil_reais DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [{"estado": r[0], "pib_mil_reais": round(r[1], 2), "ano": ano} for r in rows]


def sancoes_recentes(limit: int = 5, offset: int = 0) -> list[dict]:
    """Sanções federais mais recentes contra empresas/pessoas (CEIS/CNEP —
    cadastro OFICIAL de empresas inidôneas/suspensas, decisão já tomada
    pelo órgão sancionador; não é suspeita, é registro final)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT nome_sancionado, tipo_sancao, orgao_sancionador, data_inicio, fonte
            FROM ceis_cnep
            WHERE nome_sancionado != '' AND data_inicio != ''
            ORDER BY substr(data_inicio, 7, 4) || substr(data_inicio, 4, 2) || substr(data_inicio, 1, 2) DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "sancao": r[1], "orgao": r[2], "data": r[3], "fonte": r[4]}
        for r in rows
    ]


def condenacoes_tcu(limit: int = 5, offset: int = 0) -> list[dict]:
    """Empresas declaradas inidôneas/impedidas pelo TCU — decisão final do
    órgão, não suspeita. A tabela virou isso no ETL de out/26: processo
    vazio e prazo 0 em todas; a data de fim fica em `situacao` e o nome de
    MEI vem com o CNPJ na frente ("31.982.299 EDUARDO ...")."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT regexp_replace(nome, '^[0-9./-]+\\s+', ''), tipo_sancao, data_transito, situacao
            FROM tcu_condenados
            WHERE nome != ''
            ORDER BY to_date(data_transito, 'DD/MM/YYYY') DESC NULLS LAST, id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"empresa": r[0], "sancao": r[1], "decisao_em": r[2], "vale_ate": r[3]}
        for r in rows
    ]


def suspeitas_patrimonio(limit: int = 5, offset: int = 0) -> list[dict]:
    """Padrões estatísticos fora do comum entre patrimônio declarado e
    gasto/crescimento (severidade ALTA) — ver AVISO_SUSPEITA no prompt."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(d.nome, s.nome), COALESCE(d.sigla_partido, s.sigla_partido),
                   sp.descricao, sp.flag
            FROM suspeitas_patrimonio sp
            LEFT JOIN deputados d ON d.id = sp.deputado_id
            LEFT JOIN senadores s ON s.id = sp.senador_id
            WHERE sp.severidade = 'ALTA' AND COALESCE(d.nome, s.nome) IS NOT NULL
              AND sp.flag NOT IN ('CEAP_SUPERA_PAT', 'PAT_ZERO_CEAP', 'FORNECEDOR_SANCIONADO')
            ORDER BY sp.id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "partido": r[1], "padrao_detectado": r[2], "tipo_flag": r[3]}
        for r in rows
    ]


def custo_anual_deputado(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Quebra oficial do que custa manter 1 deputado federal por ano — soma
    de valores já publicados (subsídio, auxílio-moradia, verba de gabinete
    pra equipe) + a média real de uso da cota parlamentar (CEAP) no último
    ano completo. Nenhum valor estimado por nós: tudo vem de colunas já
    publicadas em `verbas_gabinete` (Ato da Mesa/Lei) ou calculado por soma/
    média direta de `despesas_deputados`."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "despesas_deputados")
        row = conn.execute(
            "SELECT salario_dep, auxilio_moradia, valor_anual FROM verbas_gabinete WHERE ano = ?",
            (ano,),
        ).fetchone()
        if row is None:
            return []
        salario_dep, auxilio_moradia, verba_gabinete_anual = row

        media_ceap = conn.execute(
            """
            SELECT AVG(total) FROM (
                SELECT SUM(valor_liquido) AS total
                FROM despesas_deputados
                WHERE ano = ?
                GROUP BY deputado_id
            )
            """,
            (ano,),
        ).fetchone()[0] or 0.0

    subsidio_anual = round(salario_dep * 12, 2)
    auxilio_moradia_anual = round(auxilio_moradia * 12, 2)
    verba_gabinete_anual = round(verba_gabinete_anual, 2)
    media_ceap = round(media_ceap, 2)
    total = round(subsidio_anual + auxilio_moradia_anual + verba_gabinete_anual + media_ceap, 2)

    items = [
        {"item": "Subsídio (salário oficial)", "valor_anual": subsidio_anual, "ano": ano},
        {"item": "Auxílio-moradia", "valor_anual": auxilio_moradia_anual, "ano": ano},
        {"item": "Verba de gabinete (equipe de até 25 assessores)", "valor_anual": verba_gabinete_anual, "ano": ano},
        {"item": "Cota parlamentar (CEAP) — média real de uso por deputado", "valor_anual": media_ceap, "ano": ano},
        {"item": "TOTAL somado (por deputado, por ano)", "valor_anual": total, "ano": ano},
    ]
    return items[offset : offset + limit]


# flags da cota que contam história; desde out/26 o score 100 vai também pra
# "despesa em fim de semana", "valor redondo" e "LATAM em 6 meses" (milhares,
# triviais). Combustível vem com km e voltas ao mundo, refeição com Big Macs.
_FLAGS_CEAP = ("COMBUSTIVEL_KM_OUTLIER", "ALIMENTACAO_TICKET_MEDIO_ATIPICO", "DUPLICATA", "OUTLIER_VALOR")


def _numero_br(texto: str) -> str:
    """O ETL escreve número ora à inglesa ("R$ 94,963", "2.7x"), ora à
    brasileira — o roteiro leu R$ 94,963 como 94 reais. Padroniza: milhar
    com ponto, decimal com vírgula, "x" vira "vezes"."""
    texto = re.sub(r"(\d),(\d{3})(?!\d)", r"\1.\2", texto)
    texto = re.sub(r"(\d)\.(\d)\s?[x×]", r"\1,\2 vezes", texto)
    texto = re.sub(r"(\d)\s?[x×](?=\s)", r"\1 vezes", texto)
    texto = re.sub(r"(?<![\d.])(\d+)\.(\d)(?!\d)", r"\1,\2", texto)
    texto = re.sub(r"(R\$\s?\d+)\.(\d{2})(?!\d)", r"\1,\2", texto)  # "R$ 23.90"
    texto = re.sub(r"\s*\(z=[^)]*\)", "", texto)  # desvio-padrão: jargão que a voz leria "sigma"
    return texto.replace("~", "cerca de ")


def suspeitas_ceap(limit: int = 5, offset: int = 0) -> list[dict]:
    """Padrões estatísticos fora do comum em despesas da cota parlamentar
    (deputados e senadores), um por parlamentar e tipo — ver AVISO_SUSPEITA
    no prompt."""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *, row_number() OVER (PARTITION BY flag ORDER BY score_risco DESC, nome) AS rn FROM (
                SELECT DISTINCT ON (COALESCE(d.nome, s.nome), sc.flag)
                       COALESCE(d.nome, s.nome) AS nome, COALESCE(d.sigla_partido, s.sigla_partido), sc.casa,
                       sc.descricao, sc.valor, sc.nome_fornecedor, sc.ano, sc.score_risco, sc.flag
                FROM suspeitas_ceap sc
                LEFT JOIN deputados d ON d.id = sc.deputado_id
                LEFT JOIN senadores s ON s.id = sc.senador_id
                WHERE sc.flag IN ({",".join("?" * len(_FLAGS_CEAP))})
                  AND (sc.severidade = 'ALTA' OR sc.flag LIKE '%_OUTLIER' OR sc.flag LIKE '%_ATIPICO')
                  AND COALESCE(d.nome, s.nome) IS NOT NULL
                ORDER BY COALESCE(d.nome, s.nome), sc.flag, sc.score_risco DESC, sc.ano DESC, sc.id
            ) t
            ORDER BY rn, array_position(ARRAY[{",".join("?" * len(_FLAGS_CEAP))}]::text[], flag)
            LIMIT ? OFFSET ?
            """,
            (*_FLAGS_CEAP, *_FLAGS_CEAP, limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        fato = {
            "nome": r[0], "partido": r[1], "cargo": "senador" if r[2] == "senado" else "deputado federal",
            "padrao_detectado": _numero_br(r[3]), "ano": r[6],
        }
        if r[8] in ("DUPLICATA", "OUTLIER_VALOR"):
            # padrão de UMA nota; os outros são total do ano — a nota de R$ 50
            # que vinha junto parecia o gasto todo
            fato |= {"fornecedor": r[5], "valor_da_despesa": f"R$ {r[4]:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")}
        out.append(fato)
    return out


def maior_crescimento_patrimonio(limit: int = 5, offset: int = 0) -> list[dict]:
    """Compara o patrimônio total declarado pela MESMA pessoa (via CPF) em
    duas candidaturas diferentes (2018/2020/2022/2024) — só `bens_candidatos`
    tem múltiplos anos; a tabela `patrimonio` isolada só cobre 2022. Filtra
    fora CPF vazio e limita a RAZÃO de crescimento (não só o valor final) —
    visto na prática que um "vereador" pulando de R$107 mil pra R$132
    milhões passa pelo teto de valor absoluto (calibrado pra senador) mas é
    claramente erro de digitação, não crescimento real; crescimento
    legítimo raramente multiplica por mais de ~20x em poucos anos."""
    GROWTH_RATIO_CAP = 20
    with _connect() as conn:
        rows = conn.execute(
            """
            WITH pat AS (
                SELECT MAX(c.nr_cpf_cand) AS cpf, MAX(c.nm_candidato) AS nome, MAX(c.ds_cargo) AS cargo,
                       MAX(b.ano_eleicao) AS ano, SUM(b.vr_bem) AS total
                FROM bens_candidatos b
                JOIN candidatos c ON c.sq_candidato = b.sq_candidato
                WHERE c.nr_cpf_cand != '' AND c.nr_cpf_cand IS NOT NULL
                GROUP BY b.sq_candidato
            )
            SELECT p1.nome, p1.cargo, p1.ano, p1.total, p2.ano, p2.total, p2.cargo
            FROM pat p1
            JOIN pat p2 ON p2.cpf = p1.cpf AND p2.ano > p1.ano
            WHERE p2.total > p1.total AND p2.total < ? AND p1.total > 0
              AND p2.total <= p1.total * ?
            ORDER BY (p2.total - p1.total) DESC
            LIMIT ? OFFSET ?
            """,
            (PATRIMONIO_SANITY_CAP, GROWTH_RATIO_CAP, limit, offset),
        ).fetchall()
    return [
        {
            # 1 "cargo" só (o da candidatura antiga) virou título errado:
            # "Patrimônio de vereadores triplicou" num vídeo sobre deputado
            # federal (F7kCsdCbAzA). É cargo DISPUTADO, não necessariamente
            # eleito.
            "nome": r[0],
            "ano_anterior": r[2], "cargo_disputado_anterior": r[1], "patrimonio_anterior": round(r[3], 2),
            "ano_recente": r[4], "cargo_disputado_recente": r[6], "patrimonio_recente": round(r[5], 2),
            "crescimento": round(r[5] - r[3], 2),
        }
        for r in rows
    ]


# quem indicou cada ministro do STF (fato público, conferido) — vai junto
# nos dados pro roteiro poder dizer "indicado por Lula" sem a revisão de
# fatos reprovar; nome fora da lista fica sem o campo (não se chuta)
_INDICACAO_STF = {
    **{n: "governo do PT (Lula)" for n in (
        "cezar peluso", "ayres britto", "joaquim barbosa", "eros grau", "ricardo lewandowski",
        "carmen lucia", "menezes direito", "dias toffoli", "cristiano zanin", "flavio dino",
    )},
    **{n: "governo do PT (Dilma Rousseff)" for n in (
        "luiz fux", "rosa weber", "teori zavascki", "barroso", "edson fachin",
    )},
    "alexandre de moraes": "Michel Temer",
    "andre mendonca": "Jair Bolsonaro",
    "nunes marques": "Jair Bolsonaro",
    "gilmar mendes": "Fernando Henrique Cardoso",
    "celso de mello": "José Sarney",
    "marco aurelio": "Fernando Collor",
    "ellen gracie": "Fernando Henrique Cardoso",
}


def _indicado_por(nome: str, tribunal: str, nomeado_por: str | None = None) -> str | None:
    if tribunal != "STF":
        return None
    if nomeado_por:  # o banco já sabe (Wikidata)
        return f"governo do PT ({nomeado_por})" if nomeado_por in ("Lula", "Dilma Rousseff") else nomeado_por
    plain = unicodedata.normalize("NFKD", nome.lower()).encode("ascii", "ignore").decode()
    plain = re.sub(r"\s+", " ", plain)
    for chave, quem in _INDICACAO_STF.items():
        partes = chave.split()
        if all(re.search(rf"\b{re.escape(w)}\b", plain) for w in partes):
            return quem
    return None


def maiores_salarios_magistrados(limit: int = 5, offset: int = 0) -> list[dict]:
    """Maiores salários líquidos individuais entre STF, STJ e TCU no último
    mês com dado completo — TCU não preenche `rendimento_liquido` direto
    (usa total_creditos - total_debitos como equivalente)."""
    with _connect() as conn:
        ano_mes = _last_complete_month(conn)
        rows = conn.execute(
            """
            SELECT m.nome, m.tribunal,
                   COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos) AS liquido,
                   m.url_foto, m.nomeado_por, r.situacao
            FROM remuneracao_magistrados r
            JOIN magistrados m ON m.id = r.magistrado_id
            WHERE r.ano_mes = ?
              AND COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos) > 0
            ORDER BY liquido DESC
            LIMIT ? OFFSET ?
            """,
            (ano_mes, limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "tribunal": r[1], "salario_liquido_mensal": round(r[2], 2), "mes": ano_mes, "foto_url": r[3],
         # aposentado recebe aposentadoria, não salário da ativa: sem isso o
         # revisor de fatos reprovava "Peluso recebe salário em 2026" (15/10)
         "situacao": "aposentado (o valor é a aposentadoria paga pelo tribunal)"
         if "APOSENTAD" in (r[5] or "").upper() else "na ativa",
         **({"indicado_por": quem} if (quem := _indicado_por(r[0], r[1], r[4])) else {})}
        for r in rows
    ]


def total_arrecadacao_por_partido(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Total arrecadado em campanha por partido (soma de todos os
    candidatos a deputado federal do partido). Não existe uma categoria
    isolada de "Fundo Partidário/FEFC" nos dados de receita — só 4
    categorias genéricas (recursos próprios, pessoas físicas, internet,
    comercialização de bens) — então isso é o total arrecadado, não só a
    fatia pública."""
    with _connect() as conn:
        if ano is None:
            ano = 2022
        rows = conn.execute(
            """
            SELECT d.sigla_partido, COUNT(DISTINCT cr.deputado_id) AS n_candidatos, SUM(cr.valor_receita) AS total
            FROM campanha_receitas cr
            JOIN deputados d ON d.id = cr.deputado_id
            WHERE cr.ano_eleicao = ? AND d.sigla_partido != ''
            GROUP BY d.sigla_partido
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"partido": r[0], "n_candidatos": r[1], "total_arrecadado": round(r[2], 2), "ano_eleicao": ano}
        for r in rows
    ]


def total_gasto_eleicoes(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Total gasto por TODOS os candidatos numa eleição (soma de
    campanha_despesas) — é o gasto total de campanha dos candidatos
    (mistura doação privada + fundo público/FEFC), NÃO o custo do TSE para
    organizar a eleição em si (esse dado não está nesta base). `limit`/
    `offset` só existem pra manter a assinatura padrão — sempre devolve 1
    item (o total do ano)."""
    with _connect() as conn:
        if ano is None:
            ano = 2022
        row = conn.execute(
            "SELECT COUNT(*), SUM(total_pago) FROM campanha_despesas WHERE ano_eleicao = ?",
            (ano,),
        ).fetchone()
        if not row or row[1] is None:
            return []
        n_despesas, total = row
    return [{
        "ano_eleicao": ano,
        "total_gasto_por_candidatos": round(total, 2),
        "n_registros_de_despesa": n_despesas,
    }][offset : offset + limit]


def comparacao_judiciario_legislativo(limit: int = 5, offset: int = 0) -> list[dict]:
    """Compara o que cada magistrado (STF/STJ/TCU) recebe líquido por ano,
    em média, contra o que um deputado federal recebe PESSOALMENTE por ano
    (subsídio + auxílio-moradia — sem contar a verba de gabinete/equipe,
    pra comparar maçã com maçã: só o que cada um embolsa, não orçamento de
    terceiros)."""
    with _connect() as conn:
        ano_mes = _last_complete_month(conn)
        magistrados_rows = conn.execute(
            """
            SELECT m.tribunal,
                   ROUND((AVG(COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos)) * 12)::numeric, 2)
            FROM remuneracao_magistrados r JOIN magistrados m ON m.id = r.magistrado_id
            WHERE r.ano_mes = ?
              AND COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos) > 0
            GROUP BY m.tribunal
            """,
            (ano_mes,),
        ).fetchall()

        ano_dep = _last_complete_year(conn, "despesas_deputados")
        row = conn.execute(
            "SELECT salario_dep, auxilio_moradia FROM verbas_gabinete WHERE ano = ?",
            (ano_dep,),
        ).fetchone()

    items = [
        {"cargo": f"Ministro do {tribunal}", "recebimento_liquido_anual": total, "referencia": ano_mes}
        for tribunal, total in magistrados_rows
    ]
    if row:
        salario_dep, auxilio_moradia = row
        items.append({
            "cargo": "Deputado Federal (só subsídio + auxílio-moradia, sem verba de gabinete)",
            "recebimento_liquido_anual": round(salario_dep * 12 + auxilio_moradia * 12, 2),
            "referencia": str(ano_dep),
        })
    items.sort(key=lambda i: i["recebimento_liquido_anual"], reverse=True)
    return items[offset : offset + limit]


AVISO_MUNDO = (
    "Comparação do site com parlamentos de outros países (valores aproximados em dólar, compilados pelo "
    "politica-br.org): aqui os OUTROS PAÍSES ESTÃO NOS DADOS e podem ser comparados — só com os campos "
    "da lista, sem conta nova. salario_x_salario_medio = quantas vezes o salário do parlamentar cabe no "
    "salário médio do país. O Brasil é o protagonista; diga sempre de qual país é cada número. Os "
    "valores estão em DÓLAR: escreva sempre \"US$\", nunca \"R$\", e não converta pra real."
)


def brasil_vs_mundo(limit: int = 5, offset: int = 0) -> list[dict]:
    """O parlamentar brasileiro (salário em dólar, em salários médios e
    mínimos do país, orçamento do parlamento) ao lado de outros países,
    dos que pagam proporcionalmente menos pros que pagam mais — o gancho de
    país da série do Fractal com dado real (tabela parlamentar_mundial)."""
    cols = (
        "nome_pais, salario_mensal_usd, salario_x_salario_medio, salario_x_min_wage,"
        " orcamento_parlamento_usd_milhoes, n_total, mandato_anos"
    )
    with _connect() as conn:
        br = conn.execute(f"SELECT {cols} FROM parlamentar_mundial WHERE iso2 = 'BR'").fetchone()
        outros = conn.execute(
            f"SELECT {cols} FROM parlamentar_mundial WHERE iso2 != 'BR' AND salario_x_salario_medio > 0"
            " ORDER BY salario_x_salario_medio, nome_pais LIMIT ? OFFSET ?",
            (limit - 1, offset),
        ).fetchall()
    if not br or not outros:
        return []
    # valor já com "US$" escrito: com o número solto, o roteiro de teste
    # (27/10) disse "R$ 12 mil" pro salário de US$ 12 mil
    def usd(v: float) -> str:
        return f"US$ {v:,.0f}".replace(",", ".")

    return [
        {
            "pais": r[0], "salario_parlamentar_por_mes": usd(r[1]),
            "salario_x_salario_medio": f"{r[2]:g} vezes o salário médio de lá".replace(".", ","),
            "salario_x_salario_minimo": f"{r[3]:g} vezes o salário mínimo de lá".replace(".", ","),
            "orcamento_do_parlamento_por_ano": (
                f"US$ {r[4] / 1000:.1f} bilhões" if r[4] >= 1000 else f"US$ {r[4]:g} milhões"
            ).replace(".", ","),
            "parlamentares": r[5], "mandato_anos": r[6],
        }
        for r in [br, *outros]
    ]


# conflitos_interesse do site: só os tipos SEM casamento por nome entram no
# canal — sócio confirmado pelo CPF na Receita, e o mesmo fornecedor
# (CNPJ/CPF) pago pela campanha e depois pela cota. Os por nome/sobrenome
# (SOCIO_NOME_*, FAMILIAR_PROVAVEL, SOCIO_DIRETO, DOADOR_SOCIO) e
# parentesco_assessores ficam fora: homônimo viraria acusação falsa.
_CARGO_CASA = {"camara": "deputado federal", "senado": "senador", "governador": "governador",
               "ministro": "ministro do governo federal", "magistrado": "ministro de tribunal superior"}

AVISO_SOCIOS = (
    "Sociedade confirmada no registro de sócios da Receita Federal pelo CPF do político (e nome completo "
    "igual). SER SÓCIO DE EMPRESA NÃO É ILEGAL nem irregular: diga isso no vídeo. Nada de insinuar "
    "favorecimento, conflito, uso do cargo ou que a empresa recebeu dinheiro público — os dados não dizem. "
    "O assunto é o tamanho e o tipo do negócio de quem está no poder. As empresas são PRIVADAS: o dinheiro "
    "delas não é do contribuinte — neste vídeo NÃO use a linha do pagador de impostos (nada de \"seu "
    "imposto\", \"quem paga é você\"), e não feche com \"é justo pagar com seu imposto\". Feche com "
    "pergunta sobre o fato (ex.: \"Quem faz as leis deveria ter tantos negócios?\", \"Você sabia que "
    "seu deputado era empresário?\"). Fora nome, cargo, partido e empresas, não diga nada sobre a pessoa "
    "(eleição, coligação, trajetória). Sócio NÃO é administrador: nunca diga que \"administra\", "
    "\"gerencia\" ou \"comanda\" as empresas. Cada empresa/ramo é SÓ da pessoa em cuja linha ela está "
    "nos dados — não misture os negócios de um com os de outro."
)
AVISO_CEAP_CAMPANHA = (
    "Mesmo fornecedor (mesmo CNPJ/CPF) pago primeiro com dinheiro da campanha e, depois da eleição, com a "
    "cota parlamentar (dinheiro público). NÃO É ILEGAL usar o mesmo fornecedor: diga isso no vídeo; o "
    "ponto é que a relação comercial da campanha continuou paga pelo contribuinte. Sem insinuar desvio, "
    "favorecimento ou irregularidade. Fornecedor pessoa física aparece sem nome — não tente nomear."
)


def politicos_socios_empresas(limit: int = 5, offset: int = 0) -> list[dict]:
    """Políticos (deputado, senador, governador, ministro, magistrado) com
    mais empresas em que constam como sócios na Receita, casados pelo CPF
    (SOCIO_CPF_CONFIRMADO) com nome 100% igual — ex.: Dal Barreto, sócio de
    114 empresas, quase todas postos de combustível."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT nome_politico, MAX(sigla_partido), MAX(sigla_uf), casa, COUNT(DISTINCT cnpj) AS n,"
            " MAX(politico_id) FROM conflitos_interesse"
            " WHERE tipo_conflito = 'SOCIO_CPF_CONFIRMADO' AND descricao LIKE '%Sobreposição de nome: 100%'"
            " GROUP BY nome_politico, casa ORDER BY n DESC, nome_politico LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        itens = []
        for nome, partido, uf, casa, n, pid in rows:
            empresas = [
                e for (e,) in conn.execute(
                    "SELECT DISTINCT nome_empresa FROM conflitos_interesse WHERE tipo_conflito = 'SOCIO_CPF_CONFIRMADO'"
                    " AND nome_politico = ? AND casa = ? ORDER BY nome_empresa LIMIT 6",
                    (nome, casa),
                ).fetchall()
            ]
            foto = None
            if casa in ("camara", "senado"):
                tabela = "deputados" if casa == "camara" else "senadores"
                row = conn.execute(f"SELECT url_foto FROM {tabela} WHERE id = ? LIMIT 1", (pid,)).fetchone()
                foto = row[0] if row else None
            itens.append({
                "nome": nome, "cargo": _CARGO_CASA.get(casa, casa), "partido": partido, "uf": uf or None,
                "empresas_como_socio": n, "exemplos_de_empresas": empresas, "foto_url": foto,
            })
    return itens


def fornecedor_campanha_e_cota(limit: int = 5, offset: int = 0) -> list[dict]:
    """Parlamentar que pagou o mesmo fornecedor com dinheiro da campanha e,
    eleito, com a cota (CEAP_CAMPANHA), pelo valor pago com a cota."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.nome_politico, c.sigla_partido, c.sigla_uf, c.casa, c.nome_empresa, c.valor_secundario,"
            " c.total_pago, c.n_despesas, d.url_foto FROM conflitos_interesse c"
            " LEFT JOIN (SELECT id, MAX(url_foto) AS url_foto FROM deputados GROUP BY id) d"
            " ON d.id = c.politico_id AND c.casa = 'camara'"
            " WHERE c.tipo_conflito = 'CEAP_CAMPANHA' AND c.total_pago > 0"
            " ORDER BY c.total_pago DESC, c.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [
        {
            "nome": r[0], "cargo": _CARGO_CASA.get(r[3], r[3]), "partido": r[1], "uf": r[2],
            # pessoa física vem com CPF mascarado: gente comum não vira nome em vídeo
            "fornecedor": "um fornecedor pessoa física" if "***" in (r[4] or "") else r[4],
            "pago_com_dinheiro_da_campanha": round(r[5] or 0, 2),
            "pago_depois_com_a_cota_parlamentar": round(r[6], 2),
            "despesas_na_cota": r[7], "foto_url": r[8],
        }
        for r in rows
    ]


# linhas por vídeo: o 1º é o protagonista, os outros só contraste — com 5,
# o roteiro virava lista lida ("Fulano R$ X, Beltrano R$ Y...", 13-83 views)
FATOS_POR_VIDEO = 3

# (label, fetcher, kwargs_extra, anos_possiveis|None, max_paginas, aviso|None)
# anos_possiveis=None -> fetcher decide o ano sozinho (não aceita/precisa do
# parâmetro, ou usa o "último ano completo" automaticamente).
FACT_FETCHERS = [
    ("ranking de gastos da cota parlamentar (CEAP)", top_ceap_spenders, [2023, 2024, 2025], 4, None),
    ("maiores despesas individuais da cota parlamentar", top_single_expenses, [2023, 2024, 2025], 4, None),
    ("maiores patrimônios declarados ao TSE", top_patrimonio, [2022], 6, None),
    ("ranking de score político (presença + produtividade)", top_score_politico, [2023, 2024, 2025], 4, None),
    ("maiores gastos em cartão corporativo", top_cartao_corporativo, [2024, 2025], 6, None),
    ("deputados que mais destinaram em emendas parlamentares", top_emendas, [2023, 2024, 2025], 4, None),
    ("maiores contratos públicos federais", top_contratos_publicos, [None], 6, None),
    ("maiores doadores de campanha", top_doacoes_campanha, [2018, 2022], 6, None),
    ("fornecedores que mais receberam de campanhas eleitorais", top_fornecedores_campanha, [2018, 2022], 6, None),
    ("estados com maior PIB (total)", top_pib_estados, [2021, 2022, 2023], 3, None),
    ("sanções federais mais recentes contra empresas (CEIS/CNEP)", sancoes_recentes, [None], 8, None),
    ("empresas declaradas inidôneas pelo TCU (proibidas de contratar com o governo)", condenacoes_tcu, [None], 6, None),
    ("padrões estatísticos incomuns entre patrimônio e gastos", suspeitas_patrimonio, [None], 6, AVISO_SUSPEITA),
    ("padrões estatísticos incomuns na cota parlamentar", suspeitas_ceap, [None], 6, AVISO_SUSPEITA),
    ("quanto custa manter um deputado federal por ano (soma oficial)", custo_anual_deputado, [None], 1, None),
    ("maior crescimento de patrimônio declarado entre candidaturas", maior_crescimento_patrimonio, [None], 6, AVISO_CRESCIMENTO),
    ("maiores salários entre STF, STJ e TCU", maiores_salarios_magistrados, [None], 3, None),
    ("total arrecadado em campanha por partido", total_arrecadacao_por_partido, [2018, 2022], 3, None),
    ("total gasto por candidatos numa eleição", total_gasto_eleicoes, [2018, 2022], 1, None),
    ("comparação de quanto recebe um deputado vs um ministro de tribunal superior", comparacao_judiciario_legislativo, [None], 1, None),
    ("parlamentar brasileiro comparado com o de outros países", brasil_vs_mundo, [None], 6, AVISO_MUNDO),
    ("políticos sócios de mais empresas (confirmado pelo CPF na Receita)", politicos_socios_empresas, [None], 8, AVISO_SOCIOS),
    ("fornecedor pago pela campanha e depois pela cota parlamentar", fornecedor_campanha_e_cota, [None], 8, AVISO_CEAP_CAMPANHA),
]


# peso no sorteio do fato do dia, pelas views dos curtos (out/26): salários
# do Judiciário 185/61/61, patrimônio 167, doadores 122, salto de
# patrimônio 96, contratos (telefonia) 89; custo de um deputado 14 e
# "padrões estranhos" 22-43. Fora daqui vale 1.
FACT_WEIGHTS = {
    "políticos sócios de mais empresas (confirmado pelo CPF na Receita)": 3,
    "fornecedor pago pela campanha e depois pela cota parlamentar": 3,
    "parlamentar brasileiro comparado com o de outros países": 4,
    "maiores salários entre STF, STJ e TCU": 4,
    "comparação de quanto recebe um deputado vs um ministro de tribunal superior": 3,
    "maiores patrimônios declarados ao TSE": 3,
    "maiores doadores de campanha": 3,
    "maior crescimento de patrimônio declarado entre candidaturas": 3,
    "maiores contratos públicos federais": 2,
    "maiores gastos em cartão corporativo": 2,
    "ranking de gastos da cota parlamentar (CEAP)": 2,
    "maiores despesas individuais da cota parlamentar": 2,
    "estados com maior PIB (total)": 0.2,
    "quanto custa manter um deputado federal por ano (soma oficial)": 0.3,
}
# fetcher que já usou todas as páginas só volta se nada mais tiver página
# nova — senão o de peso alto repetia o mesmo ranking em 2 semanas
_EXHAUSTED_WEIGHT = 0.05


def _exhausted(label: str, anos: list, max_paginas: int, limit: int) -> bool:
    used = set(_load_used().get(label, []))
    return all(f"{ano}:{p * limit}" in used for ano in anos for p in range(max_paginas))


def _weighted_order(limit: int) -> list:
    """FACT_FETCHERS em ordem de sorteio ponderado, sem reposição."""
    pool = list(FACT_FETCHERS)
    weights = [
        FACT_WEIGHTS.get(f[0], 1) * (_EXHAUSTED_WEIGHT if _exhausted(f[0], f[2], f[3], limit) else 1)
        for f in pool
    ]
    order = []
    while pool:
        i = random.choices(range(len(pool)), weights=weights)[0]
        order.append(pool.pop(i))
        weights.pop(i)
    return order


def _load_used() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_used(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _pick_page(label: str, anos: list[int | None], max_paginas: int, limit: int) -> tuple[int | None, int]:
    """Escolhe (ano, offset) ainda não usado pra esse fetcher — nunca repete
    a mesma "página" de ranking. Quando todas as combinações já saíram (o
    canal rodou tempo suficiente), reinicia o controle só desse fetcher —
    aceitável porque o site-fonte também vai ter atualizado os dados."""
    state = _load_used()
    used = set(state.get(label, []))
    candidates = [(ano, p * limit) for ano in anos for p in range(max_paginas)]
    unused = [c for c in candidates if f"{c[0]}:{c[1]}" not in used]
    if not unused:
        used = set()
        unused = candidates
    ano, offset = random.choice(unused)
    used.add(f"{ano}:{offset}")
    state[label] = sorted(used)
    _save_used(state)
    return ano, offset


def pick_fact_set(label: str, limit: int = FATOS_POR_VIDEO) -> dict:
    """Como random_fact_set(), mas força um fetcher específico pelo label em
    vez de sortear — usado quando o painel (ou um teste manual) quer um tema
    específico de política em vez de deixar aleatório."""
    for entry_label, fetcher, anos, max_paginas, aviso in FACT_FETCHERS:
        if entry_label == label:
            ano, offset = _pick_page(entry_label, anos, max_paginas, limit)
            kwargs = {"limit": limit, "offset": offset}
            if ano is not None:
                kwargs["ano"] = ano
            data = fetcher(**kwargs)
            result = {"tema": entry_label, "dados": data}
            if aviso:
                result["aviso"] = aviso
            return result
    raise ValueError(f"Nenhum fetcher de política com o label {label!r}")


def random_fact_set(limit: int = FATOS_POR_VIDEO) -> dict:
    """Escolhe aleatoriamente um dos temas acima, numa página ainda não
    usada, e devolve os dados prontos pra virar contexto de roteiro. Tenta
    até 3 fetchers diferentes se o primeiro vier vazio (página sem dado
    suficiente pra essa combinação de ano/offset). Na maior parte dos dias
    (PERFIL_EM_ALTA_SHARE) sai o perfil de quem está nas manchetes."""
    if random.random() < PERFIL_EM_ALTA_SHARE and (perfil := perfil_em_alta()):
        return perfil
    candidates = _weighted_order(limit)
    for label, fetcher, anos, max_paginas, aviso in candidates[:3]:
        ano, offset = _pick_page(label, anos, max_paginas, limit)
        kwargs = {"limit": limit, "offset": offset}
        if ano is not None:
            kwargs["ano"] = ano
        data = fetcher(**kwargs)
        if data:
            result = {"tema": label, "dados": data}
            if aviso:
                result["aviso"] = aviso
            return result
    # todos os 3 sorteados vieram vazios (raro) — cai pro mais confiável
    return {"tema": FACT_FETCHERS[0][0], "dados": FACT_FETCHERS[0][1](limit=limit)}


# partidos de direita: perfil deles é positivo (linha do canal); dos demais,
# segue o tom fiscalizador de sempre
PARTIDOS_DIREITA = {"PL", "NOVO", "PP", "REPUBLICANOS", "UNIÃO", "PRTB", "MISSÃO"}
_ENFOQUE_DIREITA = (
    "Vídeo de PERFIL POSITIVO: o parlamentar é alinhado à linha do canal. Destaque o que ele conquistou "
    "e faz: votação, presença, proposições e pautas (das manchetes e propostas). Cota parlamentar e "
    "gastos não entram. Feche com uma pergunta a favor dele (ex.: \"Você votaria nele de novo?\"), nunca "
    "com \"você acha justo pagar isso com o seu imposto\". Urna é eletrônica: nada de \"cédula\". "
    "Conte como HISTÓRIA, não relatório: nada de percorrer ano a ano (\"Em 2023... Em 2024... Em 2025...\"); "
    "escolha os 2 ou 3 números mais fortes e a manchete mais recente. "
    "Título: o feito mais forte dele com o número "
    "(ex.: \"Nikolas: 3 milhões de votos, recorde do Brasil\"), sem R$ — a regra de título com valor "
    "em reais não vale pra perfil."
)
_ENFOQUE_FISCAL = (
    "Vídeo de PERFIL: apresente os números do parlamentar com o tom fiscalizador do canal. Conte como "
    "HISTÓRIA (quem é, por que está nas manchetes, o número que mais chama atenção), não relatório ano a "
    "ano. Título com o número mais forte e quem."
)


def perfil_parlamentar(texto: str, deputado_id: int | None = None) -> dict | None:
    """Dados do banco sobre o deputado citado em `texto` (tema digitado,
    ex.: "Nikolas Ferreira: ..."), no mesmo formato de random_fact_set:
    presença, proposições, votações, ranking do score e gasto da cota por
    ano, mais as proposições de maior peso. None se ninguém do banco bater.
    `deputado_id` pula a busca pelo nome (perfil_em_alta: nome de uma
    palavra só, "Zucco", não casa com _PROPER_NAME_RE).
    O banco não é atualizado todo dia: o pipeline junta busca na internet
    pro que é recente (o "aviso" diz até quando vão os dados)."""
    candidatos = _PROPER_NAME_RE.findall(texto)
    if not candidatos and deputado_id is None:
        return None
    _COLS = "SELECT id, nome, sigla_partido, sigla_uf, data_nascimento, municipio_nasc, url_foto FROM deputados"
    with _connect() as conn:
        dep = None
        if deputado_id is not None:
            dep = conn.execute(f"{_COLS} WHERE id = ? ORDER BY id_legislatura DESC LIMIT 1", (deputado_id,)).fetchone()
        for nome in sorted(candidatos, key=len, reverse=True):
            if dep:
                break
            dep = conn.execute(
                f"{_COLS} WHERE nome LIKE ? ORDER BY id_legislatura DESC LIMIT 1",
                (f"%{nome}%",),
            ).fetchone()
        if not dep:
            return None
        dep_id, nome, partido, uf, nascimento, municipio, foto = dep
        anos = []
        for ano, pct, sessoes, presentes, props, ceap, rank in conn.execute(
            "SELECT s.ano, p.pct_presenca, p.total_sessoes, p.sessoes_presentes, s.total_proposicoes,"
            " s.total_ceap, s.rank_geral FROM score_politico s"
            " LEFT JOIN presenca_deputados p ON p.deputado_id = s.deputado_id AND p.ano = s.ano"
            " WHERE s.deputado_id = ? AND (p.sessoes_presentes > 0 OR s.total_proposicoes > 0) ORDER BY s.ano",
            (dep_id,),
        ):
            total = conn.execute("SELECT COUNT(rank_geral) FROM score_politico WHERE ano = ?", (ano,)).fetchone()[0]
            votos = conn.execute(
                "SELECT n_total, n_sim, n_nao FROM resumo_votos WHERE deputado_id = ? AND ano = ?", (dep_id, ano),
            ).fetchone()
            anos.append({
                "ano": ano,
                "presenca_pct": round(pct, 1) if pct is not None else None,
                "sessoes_presente": f"{presentes} de {sessoes}" if sessoes else None,
                "proposicoes_apresentadas": props,
                "votacoes": votos[0] if votos else None,
                "ranking_geral": f"{rank}º de {total} parlamentares" if rank else None,
                "cota_parlamentar_gasta": f"R$ {ceap:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if ceap else None,
            })
        principais, vistas = [], set()
        for tipo, ano, ementa in conn.execute(
            "SELECT tipo, ano, ementa FROM proposicoes_deputados WHERE deputado_id = ? ORDER BY peso DESC, ano DESC LIMIT 10",
            (dep_id,),
        ):
            chave = (ementa or "")[:60]
            if chave in vistas or len(principais) == 4:
                continue
            vistas.add(chave)
            principais.append({"tipo": tipo, "ano": ano, "ementa": (ementa or "")[:150]})
        # manchetes que o site junta da internet (Google Notícias) por
        # político: é o que o banco tem de atual
        manchetes = [
            {"titulo": titulo, "data": (publicado or "")[:10]}
            for titulo, publicado in conn.execute(
                "SELECT titulo, publicado_em FROM noticias_politicos WHERE politico_id = ? AND casa = 'camara'"
                " ORDER BY publicado_em DESC, id DESC LIMIT 6",
                (dep_id,),
            )
        ]
    if not anos:
        return None
    positivo = (partido or "").upper() in PARTIDOS_DIREITA
    if positivo:
        # perfil positivo sem gasto da cota: com o dado na mão, o molde do
        # canal ("sai do seu imposto") virava crítica no vídeo de elogio
        for ano in anos:
            ano.pop("cota_parlamentar_gasta", None)
    return {
        "tipo": "perfil_parlamentar",
        "tema": texto,
        "dados": [{
            "nome": nome, "partido": partido, "uf": uf, "nascimento": nascimento,
            "cidade_natal": municipio, "por_ano": anos, "principais_proposicoes": principais,
            "manchetes_recentes": manchetes, "foto_url": foto or None,
        }],
        "pagina": f"{SITE_URL}/deputado/{dep_id}",
        "enfoque": _ENFOQUE_DIREITA if positivo else _ENFOQUE_FISCAL,
        "aviso": (
            f"Dados do banco da Câmara (até {anos[-1]['ano']}, ano em curso parcial). manchetes_recentes "
            "são só o título da matéria: use o fato do título, sem detalhe que ele não diz. Fato recente que "
            "não está aqui só entra se estiver na PESQUISA REAL NA INTERNET."
        ),
    }


# perfil de quem está nas manchetes: o do Nikolas (22/10) fez 1.104 views
# contra 13-245 dos rankings de 5 nomes desconhecidos — gente que o público
# já procura, contada como história (a fórmula da série de países do Fractal)
PERFIL_EM_ALTA_SHARE = 0.6
_PERFIL_EM_ALTA_LABEL = "perfil em alta"
_PERFIL_REPETE_DIAS = 60


def perfil_senador(senador_id: int) -> dict | None:
    """Como perfil_parlamentar, pro Senado: presença, autorias, ranking do
    score e cota por ano (score_senadores), matérias de que é autor
    principal e manchetes recentes. Direita sem o gasto da cota (perfil
    positivo), demais com ele."""
    with _connect() as conn:
        sen = conn.execute(
            "SELECT nome, sigla_partido, sigla_uf, data_nascimento, estado_nascimento, formacao, url_foto"
            " FROM senadores WHERE id = ?",
            (senador_id,),
        ).fetchone()
        if not sen:
            return None
        nome, partido, uf, nascimento, estado_nasc, formacao, foto = sen
        anos = []
        for ano, pct, autorias, ceap, rank in conn.execute(
            "SELECT ano, pct_presenca, total_autorias, total_ceap, rank_geral FROM score_senadores"
            " WHERE senador_id = ? ORDER BY ano",
            (senador_id,),
        ).fetchall():
            total = conn.execute("SELECT COUNT(rank_geral) FROM score_senadores WHERE ano = ?", (ano,)).fetchone()[0]
            anos.append({
                "ano": ano,
                "presenca_pct": round(pct, 1) if pct is not None else None,
                "materias_de_autoria": autorias,
                "ranking_geral": f"{rank}º de {total} senadores" if rank else None,
                "cota_parlamentar_gasta": f"R$ {ceap:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if ceap else None,
            })
        autor_principal = [
            {"tipo": sig, "quantidade": n}
            for sig, n in conn.execute(
                "SELECT materia_sig, COUNT(*) FROM autorias_senadores WHERE senador_id = ? AND is_principal = 1"
                " GROUP BY materia_sig ORDER BY COUNT(*) DESC LIMIT 4",
                (senador_id,),
            ).fetchall()
        ]
        manchetes = [
            {"titulo": titulo, "data": (publicado or "")[:10]}
            for titulo, publicado in conn.execute(
                "SELECT titulo, publicado_em FROM noticias_politicos WHERE politico_id = ? AND casa = 'senado'"
                " ORDER BY publicado_em DESC, id DESC LIMIT 6",
                (senador_id,),
            ).fetchall()
        ]
    if not anos and not manchetes:
        return None
    positivo = (partido or "").upper() in PARTIDOS_DIREITA
    if positivo:
        for ano in anos:
            ano.pop("cota_parlamentar_gasta", None)
    return {
        "tipo": "perfil_parlamentar",
        "tema": nome,
        "dados": [{
            "nome": nome, "cargo": "senador", "partido": partido, "uf": uf, "nascimento": nascimento,
            "estado_natal": estado_nasc, "formacao": formacao, "por_ano": anos,
            "materias_como_autor_principal": autor_principal, "manchetes_recentes": manchetes,
            "foto_url": foto or None,
        }],
        "pagina": f"{SITE_URL}/senador/{senador_id}",
        "enfoque": _ENFOQUE_DIREITA if positivo else _ENFOQUE_FISCAL,
        "aviso": (
            f"Dados do banco do Senado (até {anos[-1]['ano'] if anos else 'hoje'}, ano em curso parcial). "
            "manchetes_recentes são só o título da matéria: use o fato do título, sem detalhe que ele não diz. "
            "Fato recente que não está aqui só entra se estiver na PESQUISA REAL NA INTERNET."
        ),
    }


def perfil_em_alta() -> dict | None:
    """Perfil do deputado ou senador em exercício com mais manchetes nos
    últimos 30 dias (tabela noticias_politicos, que o site junta do Google
    Notícias), pulando quem já saiu nos últimos 60 dias. None se ninguém
    servir. Estado: "<id>:<data>" deputado, "s<id>:<data>" senador."""
    state = _load_used()
    hoje = date.today()
    recentes = {
        e.split(":")[0] for e in state.get(_PERFIL_EM_ALTA_LABEL, [])
        if (hoje - date.fromisoformat(e.split(":")[1])).days < _PERFIL_REPETE_DIAS
    }
    desde = (hoje - timedelta(days=30)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT 'camara', d.id, d.nome, d.sigla_partido, d.sigla_uf, COUNT(*) AS n FROM noticias_politicos n"
            " JOIN deputados d ON d.id = n.politico_id"
            " AND d.id_legislatura = (SELECT MAX(id_legislatura) FROM deputados)"
            " WHERE n.casa = 'camara' AND n.publicado_em >= ?"
            " GROUP BY d.id, d.nome, d.sigla_partido, d.sigla_uf"
            " UNION ALL"
            " SELECT 'senado', s.id, s.nome, s.sigla_partido, s.sigla_uf, COUNT(*) AS n FROM noticias_politicos n"
            " JOIN senadores s ON s.id = n.politico_id AND s.em_exercicio = 1"
            " WHERE n.casa = 'senado' AND n.publicado_em >= ?"
            " GROUP BY s.id, s.nome, s.sigla_partido, s.sigla_uf"
            " ORDER BY n DESC LIMIT 20",
            (desde, desde),
        ).fetchall()
    for casa, pid, nome, partido, uf, _n in rows:
        chave = f"s{pid}" if casa == "senado" else str(pid)
        if chave in recentes:
            continue
        perfil = perfil_senador(pid) if casa == "senado" else perfil_parlamentar(nome, deputado_id=pid)
        if not perfil:
            continue
        cargo = "o senador" if casa == "senado" else "o deputado federal"
        perfil["tema"] = f"{nome} ({partido}-{uf}): {cargo} que está nas manchetes"
        state.setdefault(_PERFIL_EM_ALTA_LABEL, []).append(f"{chave}:{hoje.isoformat()}")
        _save_used(state)
        return perfil
    return None


_PROPER_NAME_RE = re.compile(
    r"\b[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:de|da|do|dos|das|e)\s+[A-ZÀ-Ý][a-zà-ÿ]+|\s+[A-ZÀ-Ý][a-zà-ÿ]+)+\b"
)

# tabelas que já têm foto oficial cadastrada (mesmo padrão usado pelos
# fetchers acima) — cobre deputado, senador e magistrado (STF/STJ/TCU).
_TABELAS_COM_FOTO = ("deputados", "senadores", "magistrados")


def foto_pessoa_conhecida(texto: str) -> str | None:
    """Procura, dentro de `texto` (ex.: um tema digitado à mão no painel),
    o nome de alguém já cadastrado no banco com foto oficial (deputado,
    senador, ministro do STF/STJ/TCU) e devolve a URL da foto — ou None se
    ninguém bater.

    Motivação: quando o tema é sobre uma pessoa real nomeada e não vem do
    banco (ver web_search.py), a thumbnail cairia pra imagem gerada por
    IA, que a REGRA CRÍTICA em script_gen.py já proíbe de mostrar o rosto
    dela — mas se a pessoa JÁ tem foto oficial cadastrada aqui (achado de
    verdade: Alexandre de Moraes já está na tabela `magistrados`, com foto
    do Wikimedia Commons), usar a foto real é seguro e muito melhor que
    não ter foto nenhuma."""
    candidatos = _PROPER_NAME_RE.findall(texto)
    if not candidatos:
        return None
    with _connect() as conn:
        for nome in candidatos:
            for tabela in _TABELAS_COM_FOTO:
                row = conn.execute(
                    f"SELECT url_foto FROM {tabela} WHERE nome LIKE ? AND url_foto IS NOT NULL AND url_foto != '' LIMIT 1",
                    (f"%{nome}%",),
                ).fetchone()
                if row and row[0]:
                    return row[0]
    return None
