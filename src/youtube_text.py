"""Texto que vai pro YouTube, sem dependência de API (testável no CI)."""

YOUTUBE_DESCRIPTION_LIMIT = 5000  # o YouTube recusa descrição maior (400 invalidDescription)
_SOURCES_HEAD = "\n\nFontes consultadas:\n"


def fit_description(description: str, limit: int = YOUTUBE_DESCRIPTION_LIMIT) -> str:
    """Cabe no limite do YouTube sem perder o início. Primeiro corta a lista de
    fontes (a parte mais longa e menos importante), linha a linha; se ainda não
    couber, corta no fim de uma linha."""
    if len(description) <= limit:
        return description
    head, sep, sources = description.partition(_SOURCES_HEAD)
    if sep:
        kept = []
        for line in sources.split("\n"):
            candidate = head + sep + "\n".join(kept + [line])
            if len(candidate) > limit:
                break
            kept.append(line)
        description = head + (sep + "\n".join(kept) if kept else "")
    if len(description) > limit:
        cut = description[:limit]
        description = cut[:cut.rfind("\n")] if "\n" in cut else cut
    return description
