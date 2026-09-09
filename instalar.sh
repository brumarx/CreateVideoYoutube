#!/usr/bin/env bash
# Instala o pipeline: dependências de sistema, venv Python, diretórios
# necessários (gitignorados, não vêm no clone) e o .env de exemplo.
#
# Automatiza só o que dá pra automatizar. As partes que exigem uma conta
# sua (Google Cloud, provedores de LLM/imagem grátis, canal do YouTube)
# ficam documentadas no final — não tem como automatizar isso com
# segurança nem sentido (são credenciais SUAS).
#
# Uso:
#   ./instalar.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== 1/5: dependências de sistema (ffmpeg, python3-venv) =="
if command -v apt-get >/dev/null 2>&1; then
    MISSING=()
    command -v ffmpeg >/dev/null 2>&1 || MISSING+=(ffmpeg)
    python3 -c "import venv" >/dev/null 2>&1 || MISSING+=(python3-venv)
    python3 -c "import pip" >/dev/null 2>&1 || MISSING+=(python3-pip)
    if [ "${#MISSING[@]}" -gt 0 ]; then
        echo "instalando: ${MISSING[*]} (sudo apt-get)"
        sudo apt-get update -qq
        sudo apt-get install -y "${MISSING[@]}"
    else
        echo "já instalado, ok"
    fi
else
    echo "apt-get não encontrado (não é Debian/Ubuntu) — garanta manualmente:"
    echo "  ffmpeg, python3 (>=3.11) com módulos venv e pip"
fi

echo
echo "== 2/5: ambiente virtual Python =="
if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "criado em .venv/"
else
    echo ".venv/ já existe, reaproveitando"
fi

echo
echo "== 3/5: dependências Python =="
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "instaladas"

echo
echo "== 4/5: diretórios locais (gitignorados, não vêm no clone) =="
mkdir -p data output logs credentials
echo "data/ output/ logs/ credentials/ ok"

echo
echo "== 5/5: arquivo .env =="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "criado a partir de .env.example — falta preencher as chaves (veja abaixo)"
else
    echo ".env já existe, não mexi (edite manualmente se precisar de chave nova)"
fi

cat <<'EOF'

========================================================================
 Instalação automática concluída. Faltam só as partes que dependem de
 CONTA SUA — não dá (nem faz sentido) automatizar isso:
========================================================================

1) Chaves grátis de LLM/imagem/vídeo — edite o .env e preencha o que
   quiser usar (todas têm free tier, nenhuma pede cartão):
     - Groq, Gemini, Cerebras, OpenRouter, Mistral, xAI (roteiro)
     - Pollinations (imagem)                     — funciona até sem chave
     - Pexels (vídeo/foto real, ver src/stock_media.py)

2) YouTube Data API v3 (upload):
     a. Criar projeto em https://console.cloud.google.com
     b. Ativar "YouTube Data API v3"
     c. Criar credencial OAuth tipo "Desktop app"
     d. Salvar o JSON em credentials/client_secret.json

3) Por canal (repita pra cada channels/<nome>.yaml que quiser usar):
     .venv/bin/python3 scripts/auth_youtube.py --channel <nome>

4) Testar sem publicar:
     .venv/bin/python3 scripts/run_pipeline.py --channel <nome> --topic "..." --dry-run

5) Painel de administração:
     .venv/bin/python3 web/app.py
     -> http://localhost:5088

6) Automação diária (cron, ~10h da manhã):
     crontab -e
     0 10 * * * cd $(pwd) && .venv/bin/python3 scripts/daily_run.py >> logs/daily_run.log 2>&1

Detalhes de cada canal, cota de API e como o pipeline funciona: README.md
========================================================================
EOF
