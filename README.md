# youtube-ai-pipeline

Pipeline automatizado de vídeos "faceless" para YouTube: gera roteiro,
narração, imagens e monta o vídeo final, sozinho, todo dia — 100% com
ferramentas gratuitas.

## Como funciona

```
roteiro (LLM grátis) → narração (edge-tts) → vídeo/imagem (Pexels real + Pollinations) →
montagem (ffmpeg, 9:16 curto ou 16:9 longo) → thumbnail → upload (YouTube Data API v3)
```

- **Roteiro**: cascata de provedores LLM grátis (Groq, Gemini, Cerebras,
  OpenRouter, Mistral, xAI), com rodízio entre múltiplas chaves por provedor
  e descoberta dinâmica de modelo (`/models`) — nomes de modelo grátis mudam
  com frequência, então nunca ficam fixos no código.
- **Narração**: [edge-tts](https://github.com/rany2/edge-tts), grátis, várias
  vozes em pt-BR.
- **Imagens**: [Pollinations](https://pollinations.ai), grátis (com fallback
  anônimo se as chaves acabarem o saldo).
- **Vídeo**: cascata de conteúdo real antes de recorrer à IA — filmagem
  e foto real do [Pexels](https://www.pexels.com) quando existe algo que
  bate com a cena (`src/stock_media.py`); só cai pra imagem gerada por IA
  com Ken Burns (zoom lento) quando não existe nada real pro assunto.
  Montagem só com `ffmpeg` via subprocess — sem moviepy, crop-to-fill pra
  nunca distorcer.
- **Upload**: YouTube Data API v3, com thumbnail customizada automática
  (requer verificação de telefone do canal — feature do próprio YouTube).
- **Canal "política"**: puxa dados reais de um banco de transparência
  política (TSE/Câmara/Senado) em vez de deixar a IA inventar números —
  ver `src/politica_data.py`.

## Estrutura

```
channels/*.yaml       # 1 arquivo por canal: nicho, voz, tópicos, privacidade
credentials/           # gitignored — client_secret.json, token_<canal>.json
src/
  providers.py          # cascata de LLM com rodízio de chaves
  visuals.py             # geração de imagem (Pollinations)
  stock_media.py          # vídeo/foto real (Pexels) — prioridade sobre imagem de IA
  tts.py                  # narração (edge-tts)
  script_gen.py            # roteiro estruturado em JSON
  assemble.py               # montagem do vídeo (ffmpeg)
  thumbnail.py               # thumbnail (Pollinations + Pillow)
  upload.py                   # upload YouTube Data API v3
  orchestrator.py               # fila de jobs (SQLite)
  politica_data.py                # dados reais pro canal de política
scripts/
  auth_youtube.py         # gera o token OAuth de um canal
  run_pipeline.py           # roda 1 vídeo ponta-a-ponta
  daily_run.py                # roda a fila diária de todos os canais (cron)
web/app.py               # painel de administração (Flask, rede local)
```

## Setup

```bash
git clone https://github.com/brumarx/CreateVideoYoutube.git
cd CreateVideoYoutube
./instalar.sh
```

`instalar.sh` cuida da parte automatizável: dependência de sistema
(`ffmpeg`), ambiente virtual Python, `pip install`, diretórios locais
(`data/`, `output/`, `logs/`, `credentials/` — gitignorados, não vêm no
clone) e cria o `.env` a partir do `.env.example`. No final ele imprime
o que falta, que depende de conta sua e não dá pra automatizar:

1. Preencher o `.env` com chaves grátis (LLM, imagem, vídeo/foto real —
   nenhuma pede cartão).
2. Criar um projeto no [Google Cloud Console](https://console.cloud.google.com),
   ativar a **YouTube Data API v3**, criar uma credencial OAuth tipo
   **Desktop app**, salvar o JSON em `credentials/client_secret.json`.
3. Criar um `channels/<nome>.yaml` (ver exemplos existentes).
4. Autenticar o canal: `.venv/bin/python3 scripts/auth_youtube.py --channel <nome>`
5. Rodar um teste sem publicar: `.venv/bin/python3 scripts/run_pipeline.py --channel <nome> --topic "..." --dry-run`
6. Painel: `.venv/bin/python3 web/app.py` → `http://localhost:5088`

Sem shell Bash (Windows sem WSL, etc.), siga os mesmos passos manualmente
— `instalar.sh` só automatiza, não é obrigatório.

## Automação diária

`scripts/daily_run.py` percorre `channels/*.yaml`, gera e publica os vídeos
do dia, respeitando `uploads_per_day`. Agendado via cron (`0 10 * * *`).

## Cota da API

YouTube Data API v3: 10.000 unidades/dia por projeto Google Cloud padrão;
1 upload = 1.600 unidades → ~6 uploads/dia no total, somando todos os
canais desse mesmo projeto.
