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
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path("/var/www/html/politica/data/politica.db")
STATE_FILE = ROOT / "data" / "used_political_facts.json"

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
    "Eleitoral\", nunca apresente o valor como fato 100% verificado."
)


def _last_complete_year(conn: sqlite3.Connection, table: str, column: str = "ano") -> int:
    """MAX(ano) da tabela, mas nunca o ano corrente em andamento — um total
    de "gastos do ano" com o ano ainda não terminado passaria a impressão
    errada de ranking anual completo quando é só parcial (até hoje)."""
    max_ano = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
    if max_ano is None:
        return date.today().year - 1
    if max_ano >= date.today().year:
        return max_ano - 1
    return max_ano


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco de política não encontrado: {DB_PATH}")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _last_complete_month(conn: sqlite3.Connection) -> str:
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
            SELECT d.nome, d.sigla_partido, d.sigla_uf, SUM(e.valor_liquido) AS total, d.url_foto
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
    erro de digitação óbvio na declaração original)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.nome, pat.valor
            FROM patrimonio pat
            JOIN politicos p ON p.id = pat.politico_id
            WHERE pat.ano = ? AND pat.valor < ?
            ORDER BY pat.valor DESC
            LIMIT ? OFFSET ?
            """,
            (ano, PATRIMONIO_SANITY_CAP, limit, offset),
        ).fetchall()
    return [{"nome": r[0], "patrimonio_declarado": round(r[1], 2), "ano": ano} for r in rows]


def top_score_politico(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Deputados mais bem avaliados no "score político" do próprio site
    (combina presença, proposições apresentadas e uso da cota)."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "score_politico")
        rows = conn.execute(
            """
            SELECT d.nome, d.sigla_partido, d.sigla_uf, s.score_total, s.pct_presenca, s.total_proposicoes, d.url_foto
            FROM score_politico s
            JOIN deputados d ON d.id = s.deputado_id
            WHERE s.ano = ?
            ORDER BY s.score_total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {
            "nome": r[0], "partido": r[1], "uf": r[2], "score": round(r[3], 1),
            "pct_presenca": round(r[4], 1), "total_proposicoes": r[5], "ano": ano, "foto_url": r[6],
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
            SELECT nome_portador, politico_tipo, orgao, SUM(valor) AS total
            FROM cartao_corporativo
            WHERE ano = ? AND nome_portador NOT IN ('SIGILOSO', '') AND nome_portador IS NOT NULL
            GROUP BY nome_portador, politico_tipo
            ORDER BY total DESC
            LIMIT ? OFFSET ?
            """,
            (ano, limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "cargo": r[1], "orgao": r[2], "total_gasto": round(r[3], 2), "ano": ano}
        for r in rows
    ]


def top_emendas(ano: int | None = None, limit: int = 5, offset: int = 0) -> list[dict]:
    """Deputados que mais destinaram em emendas parlamentares pagas no ano.
    Filtra fora bancadas/comissões (autor_partido vazio) — só parlamentar
    individual identificado."""
    with _connect() as conn:
        if ano is None:
            ano = _last_complete_year(conn, "emendas_parlamentares")
        rows = conn.execute(
            """
            SELECT em.autor_nome, em.autor_partido, em.autor_uf, SUM(em.valor_pago) AS total, d.url_foto
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
            SELECT nome_forn, desc_cnae, SUM(total_pago) AS total
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
    """Condenações do TCU (Tribunal de Contas da União) — decisão final do
    órgão, não suspeita."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT nome, tipo_sancao, processo, prazo_inab_anos
            FROM tcu_condenados
            WHERE nome != ''
            ORDER BY nome
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "sancao": r[1], "processo": r[2], "anos_inabilitado": r[3]}
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


def suspeitas_ceap(limit: int = 5, offset: int = 0) -> list[dict]:
    """Padrões estatísticos fora do comum em despesas da cota parlamentar
    (score de risco mais alto) — ver AVISO_SUSPEITA no prompt."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.nome, d.sigla_partido, sc.descricao, sc.score_risco, sc.ano
            FROM suspeitas_ceap sc
            JOIN deputados d ON d.id = sc.deputado_id
            WHERE sc.score_risco >= 20
            ORDER BY sc.score_risco DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {"nome": r[0], "partido": r[1], "padrao_detectado": r[2], "score_risco": r[3], "ano": r[4]}
        for r in rows
    ]


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
                SELECT c.nr_cpf_cand AS cpf, c.nm_candidato AS nome, c.ds_cargo AS cargo,
                       b.ano_eleicao AS ano, SUM(b.vr_bem) AS total
                FROM bens_candidatos b
                JOIN candidatos c ON c.sq_candidato = b.sq_candidato
                WHERE c.nr_cpf_cand != '' AND c.nr_cpf_cand IS NOT NULL
                GROUP BY b.sq_candidato
            )
            SELECT p1.nome, p1.cargo, p1.ano, p1.total, p2.ano, p2.total
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
            "nome": r[0], "cargo": r[1],
            "ano_anterior": r[2], "patrimonio_anterior": round(r[3], 2),
            "ano_recente": r[4], "patrimonio_recente": round(r[5], 2),
            "crescimento": round(r[5] - r[3], 2),
        }
        for r in rows
    ]


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
                   m.url_foto
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
        {"nome": r[0], "tribunal": r[1], "salario_liquido_mensal": round(r[2], 2), "mes": ano_mes, "foto_url": r[3]}
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
                   ROUND(AVG(COALESCE(NULLIF(r.rendimento_liquido, 0), r.total_creditos - r.total_debitos)) * 12, 2)
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
    ("condenações do TCU", condenacoes_tcu, [None], 6, None),
    ("padrões estatísticos incomuns entre patrimônio e gastos", suspeitas_patrimonio, [None], 6, AVISO_SUSPEITA),
    ("padrões estatísticos incomuns na cota parlamentar", suspeitas_ceap, [None], 6, AVISO_SUSPEITA),
    ("quanto custa manter um deputado federal por ano (soma oficial)", custo_anual_deputado, [None], 1, None),
    ("maior crescimento de patrimônio declarado entre candidaturas", maior_crescimento_patrimonio, [None], 6, AVISO_CRESCIMENTO),
    ("maiores salários entre STF, STJ e TCU", maiores_salarios_magistrados, [None], 3, None),
    ("total arrecadado em campanha por partido", total_arrecadacao_por_partido, [2018, 2022], 3, None),
    ("total gasto por candidatos numa eleição", total_gasto_eleicoes, [2018, 2022], 1, None),
    ("comparação de quanto recebe um deputado vs um ministro de tribunal superior", comparacao_judiciario_legislativo, [None], 1, None),
]


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


def pick_fact_set(label: str, limit: int = 5) -> dict:
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


def random_fact_set(limit: int = 5) -> dict:
    """Escolhe aleatoriamente um dos temas acima, numa página ainda não
    usada, e devolve os dados prontos pra virar contexto de roteiro. Tenta
    até 3 fetchers diferentes se o primeiro vier vazio (página sem dado
    suficiente pra essa combinação de ano/offset)."""
    candidates = list(FACT_FETCHERS)
    random.shuffle(candidates)
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
