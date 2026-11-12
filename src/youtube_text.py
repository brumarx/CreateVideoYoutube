"""Texto que vai pro YouTube, sem dependência de API (testável no CI)."""

YOUTUBE_DESCRIPTION_LIMIT = 5000  # o YouTube recusa descrição maior (400 invalidDescription)
_SOURCES_HEAD = "\n\nFontes consultadas:\n"


def _tamanho(texto: str) -> int:
    """A API do YouTube mede a descrição em bytes UTF-8 (título de fonte em japonês conta 3 por letra)."""
    return len(texto.encode("utf-8"))


def fit_description(description: str, limit: int = YOUTUBE_DESCRIPTION_LIMIT) -> str:
    """Cabe no limite do YouTube sem perder o início nem o fim (créditos das fotos, que a licença
    exige, vídeos relacionados e hashtags). Primeiro corta só as linhas da lista de fontes (a parte
    mais longa e menos importante), do fim para o começo; se ainda não couber, corta no fim de uma linha."""
    if _tamanho(description) <= limit:
        return description
    head, sep, resto = description.partition(_SOURCES_HEAD)
    if sep:
        fontes, sep2, cauda = resto.partition("\n\n")  # a lista de fontes termina na primeira linha em branco
        linhas = fontes.split("\n")
        while linhas and _tamanho(head + sep + "\n".join(linhas) + sep2 + cauda) > limit:
            linhas.pop()
        if linhas:
            description = head + sep + "\n".join(linhas) + sep2 + cauda
        else:
            description = head + ("\n\n" + cauda if cauda else "")
    if _tamanho(description) > limit:
        cut = description.encode("utf-8")[:limit].decode("utf-8", "ignore")
        description = cut[:cut.rfind("\n")] if "\n" in cut else cut
    return description
