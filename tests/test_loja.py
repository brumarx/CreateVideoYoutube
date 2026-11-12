"""Loja própria na descrição (src/loja.py)."""
from src import loja

VITRINE = [
    {"id": 1, "nome": "Camisa Botafogo I 2026", "categoria": "Camisas"},
    {"id": 2, "nome": "Camisa Botafogo Retrô 1995 Túlio 7", "categoria": "Retrô"},
    {"id": 3, "nome": "Caneca Botafogo 360 ml", "categoria": "Casa"},
    {"id": 4, "nome": "Livro: A história do Botafogo", "categoria": "Livros"},
    {"id": 5, "nome": "Camisa Botafogo II 2025", "categoria": "Camisas"},
]


def test_produto_ligado_ao_assunto_vem_primeiro():
    escolha = loja.escolher(VITRINE, "Túlio Maravilha e o título de 1995", 2, 0)
    assert escolha[0]["id"] == 2


def test_palavra_de_toda_vitrine_nao_conta_como_assunto():
    # "Botafogo" está em todo produto: sem outra palavra em comum, é rodízio
    a = loja.escolher(VITRINE, "Botafogo vence no domingo", 2, 1)
    b = loja.escolher(VITRINE, "Botafogo vence no domingo", 2, 2)
    assert a != b
    assert len({p["categoria"] for p in a}) == 2  # rodízio não repete categoria


def test_vitrine_fora_do_ar_nao_derruba(monkeypatch):
    def falha(*a, **k):
        raise OSError("sem rede")
    monkeypatch.setattr(loja.requests, "get", falha)
    cfg = {"texto": "🛒 Loja", "url": "https://site/loja", "api": "https://site/api"}
    assert loja.linhas_descricao(cfg, {"title": "x", "scenes": []}, 1) == ["🛒 Loja: https://site/loja"]


def test_categoria_excluida_fica_fora(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 9, "nome": "Boné de candidato", "categoria": "Campanha"},
                    {"id": 1, "nome": "Constituição Federal", "categoria": "Livros"}]
    monkeypatch.setattr(loja.requests, "get", lambda *a, **k: R())
    itens = loja.produtos({"api": "x", "excluir_categorias": ["Campanha"]})
    assert [p["id"] for p in itens] == [1]


POLITICA = [
    {"id": 1, "nome": "Constituição Federal 2026 (Minicódigos, Edipro)", "categoria": "Livros"},
    {"id": 4, "nome": "Raízes do Brasil, Sérgio Buarque de Holanda", "categoria": "Livros"},
    {"id": 13, "nome": "Bandeira do Brasil oficial, 3 panos, 192×135 cm", "categoria": "Casa"},
    {"id": 14, "nome": "Mapa do Brasil político e rodoviário, 120×90 cm", "categoria": "Casa"},
]


def test_loja_de_outro_canal_so_com_produto_que_combina():
    assert loja.escolher(POLITICA, "Como a Constituição de 1988 mudou o Brasil", 1, 0, so_ligados=True)[0]["id"] == 1
    assert loja.escolher(POLITICA, "A bandeira do Brasil e o céu de 15 de novembro", 1, 0, so_ligados=True)[0]["id"] == 13
    # sem combinação: nada (o canal fica com a Amazon), nunca rodízio
    assert loja.escolher(POLITICA, "Como o Canal do Panamá sobe navios 26 metros", 1, 0, so_ligados=True) == []


def test_loja_de_outro_canal_sem_falso_positivo():
    # raiz "const" (construção x Constituição), autor (Holanda), palavra genérica
    for tema in ("Como Moscou construiu o metrô mais profundo do mundo",
                 "Como a Holanda vive abaixo do nível do mar",
                 "Será que os governos brasileiros caem no mundo rural?"):
        assert loja.escolher(POLITICA, tema, 1, 0, so_ligados=True) == [], tema
