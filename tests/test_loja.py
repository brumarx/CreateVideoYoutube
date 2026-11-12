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
