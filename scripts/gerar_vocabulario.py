"""Gera src/dados/vocabulario.json: meses e lugares (países, regiões) em pt/en/es a partir do CLDR (Babel),
mais estados do Brasil e dos EUA, oceanos e regiões polares escritos aqui, e cidades do GeoNames.
Uso: scripts/gerar_vocabulario.py [caminho/cities15000.txt] Rodar de novo só para atualizar;
o sistema lê o JSON (Babel não é dependência em tempo de execução)."""
import json
import re
from pathlib import Path

from babel import Locale

IDIOMAS = ("pt", "en", "es")
SAIDA = Path(__file__).resolve().parent.parent / "src" / "dados" / "vocabulario.json"

ESTADOS_BR = ["Acre", "Alagoas", "Amapá", "Amazonas", "Bahia", "Ceará", "Distrito Federal", "Espírito Santo", "Goiás",
              "Maranhão", "Mato Grosso", "Mato Grosso do Sul", "Minas Gerais", "Pará", "Paraíba", "Paraná",
              "Pernambuco", "Piauí", "Rio de Janeiro", "Rio Grande do Norte", "Rio Grande do Sul", "Rondônia",
              "Roraima", "Santa Catarina", "São Paulo", "Sergipe", "Tocantins"]
# (inglês, português quando diferente)
ESTADOS_EUA = [("Alabama",), ("Alaska", "Alasca"), ("Arizona",), ("Arkansas",), ("California", "Califórnia"),
               ("North Carolina", "Carolina do Norte"), ("South Carolina", "Carolina do Sul"), ("Colorado",),
               ("Connecticut",), ("North Dakota", "Dakota do Norte"), ("South Dakota", "Dakota do Sul"),
               ("Delaware",), ("Florida", "Flórida"), ("Georgia", "Geórgia"), ("Hawaii", "Havaí"), ("Idaho",),
               ("Illinois",), ("Indiana",), ("Iowa",), ("Kansas",), ("Kentucky",), ("Louisiana", "Luisiana"),
               ("Maine",), ("Maryland",), ("Massachusetts",), ("Michigan",), ("Minnesota",), ("Mississippi",),
               ("Missouri",), ("Montana",), ("Nebraska",), ("Nevada",), ("New Hampshire", "Nova Hampshire"),
               ("New Jersey", "Nova Jersey"), ("New York", "Nova York", "Nova Iorque"), ("New Mexico", "Novo México"),
               ("Ohio",), ("Oklahoma",), ("Oregon",), ("Pennsylvania", "Pensilvânia"), ("Rhode Island",),
               ("Tennessee",), ("Texas",), ("Utah",), ("Vermont",), ("Virginia", "Virgínia"),
               ("West Virginia", "Virgínia Ocidental"), ("Washington",), ("Wisconsin",), ("Wyoming",)]
GEOGRAFIA = [("Arctic", "Ártico", "Ártico"), ("Antarctic", "Antártico", "Antártico"),
             ("Antarctica", "Antártida", "Antártica", "Antártida"),
             ("Arctic Circle", "Círculo Polar Ártico", "Círculo Polar Ártico"),
             ("Antarctic Circle", "Círculo Polar Antártico"), ("North Pole", "Polo Norte"), ("South Pole", "Polo Sul"),
             ("Atlantic Ocean", "Oceano Atlântico", "Océano Atlántico", "Atlantic", "Atlântico", "Atlántico"),
             ("Pacific Ocean", "Oceano Pacífico", "Océano Pacífico", "Pacific", "Pacífico"),
             ("Indian Ocean", "Oceano Índico", "Océano Índico"),
             ("Arctic Ocean", "Oceano Ártico", "Océano Ártico"),
             ("Southern Ocean", "Oceano Antártico", "Oceano Austral", "Océano Austral"),
             ("Mediterranean Sea", "Mar Mediterrâneo", "Mediterráneo", "Mediterranean", "Mediterrâneo"),
             ("Caribbean Sea", "Mar do Caribe", "Mar das Caraíbas", "Mar Caribe"), ("Red Sea", "Mar Vermelho", "Mar Rojo"),
             ("Black Sea", "Mar Negro"), ("North Sea", "Mar do Norte", "Mar del Norte"),
             ("Baltic Sea", "Mar Báltico"), ("Barents Sea", "Mar de Barents"),
             ("Equator", "Equador (linha)", "Linha do Equador", "Ecuador (línea)"),
             ("Scandinavia", "Escandinávia", "Escandinavia"), ("Siberia", "Sibéria"),
             ("Middle East", "Oriente Médio", "Médio Oriente", "Oriente Medio"),
             ("Amazon", "Amazônia", "Amazonia", "Amazonía"), ("Sahara", "Saara", "Sáhara"),
             ("Himalayas", "Himalaia", "Himalaya"), ("Andes",), ("Alps", "Alpes")]


_FORMA_LATINA = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\- ]{2,39}$")


def cidades(arquivo: Path | None) -> list[dict]:
    """GeoNames cities15000 (CC-BY 4.0, download.geonames.org/export/dump): cidades com 100 mil habitantes ou mais,
    capitais nacionais (PPLC) e estaduais (PPLA, 50 mil ou mais); formas em alfabeto latino com maiúscula (os
    nomes alternativos trazem romanizações de outros alfabetos em minúscula, que ficam de fora)."""
    if not arquivo or not arquivo.exists():
        return []
    out = []
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        c = linha.split("\t")
        if len(c) < 15:
            continue
        pop, feature = int(c[14] or 0), c[7]
        if not (pop >= 100_000 or feature == "PPLC" or (feature == "PPLA" and pop >= 50_000)):
            continue
        formas = {c[1], c[2]} | set(c[3].split(","))
        # código de aeroporto ("TYO", "NAT", "LYR") não é nome: colidiria com sigla
        formas = sorted(f.strip() for f in formas if _FORMA_LATINA.match(f.strip()) and not f.strip().isupper())
        if formas:
            out.append({"id": "C-" + c[0], "tipo": "cidade", "pais": c[8], "formas": formas})
    return out


def main():
    import sys
    nomes = {l: Locale.parse(l).territories for l in IDIOMAS}
    lugares = []
    for codigo in nomes["en"]:
        formas = sorted({nomes[l][codigo] for l in IDIOMAS if codigo in nomes[l]})
        if codigo in ("ZZ", "UN", "EU", "EZ", "QO", "XA", "XB"):
            continue
        lugares.append({"id": codigo, "tipo": "regiao" if codigo.isdigit() else "pais", "formas": formas})
    for e in ESTADOS_BR:
        lugares.append({"id": "BR-" + e, "tipo": "estado", "formas": [e]})
    for t in ESTADOS_EUA:
        lugares.append({"id": "US-" + t[0], "tipo": "estado", "formas": sorted(set(t))})
    for t in GEOGRAFIA:
        lugares.append({"id": "G-" + t[0], "tipo": "geografia", "formas": sorted(set(t))})
    lugares += cidades(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    meses = {}
    for l in IDIOMAS:
        loc = Locale.parse(l)
        for largura in ("wide", "abbreviated"):
            for n, nome in loc.months["format"][largura].items():
                meses[nome.lower().rstrip(".")] = n
    meses.update({"sept": 9, "set": 9, "dic": 12, "ene": 1})
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({"meses": dict(sorted(meses.items())), "lugares": lugares}, ensure_ascii=False,
                                indent=0), encoding="utf-8")
    print(f"{len(meses)} formas de mês, {len(lugares)} lugares -> {SAIDA}")


if __name__ == "__main__":
    main()
