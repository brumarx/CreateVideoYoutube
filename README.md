# youtube-ai-pipeline

Pipeline automatizado de vídeos "faceless" para YouTube: gera roteiro,
narração, imagens e monta o vídeo final, sozinho, todo dia — 100% com
ferramentas gratuitas.

## Como funciona

```
roteiro (LLM grátis) → narração (edge-tts) → imagens (Pollinations) →
montagem (ffmpeg, formato vertical 9:16) → thumbnail → upload (YouTube Data API v3)
```

- **Roteiro**: cascata de provedores LLM grátis (Groq, Gemini, Cerebras,
  OpenRouter, Mistral, xAI), com rodízio entre múltiplas chaves por provedor
  e descoberta dinâmica de modelo (`/models`) — nomes de modelo grátis mudam
  com frequência, então nunca ficam fixos no código.
- **Narração**: [edge-tts](https://github.com/rany2/edge-tts), grátis, várias
  vozes em pt-BR.
- **Imagens**: [Pollinations](https://pollinations.ai), grátis (com fallback
  anônimo se as chaves acabarem o saldo).
- **Vídeo**: só `ffmpeg` via subprocess — sem moviepy. Ken Burns (zoom lento)
  em cada cena, crop-to-fill pra nunca distorcer a imagem.
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
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # preencher com chaves grátis (nenhuma é paga)
```

1. Criar um projeto no [Google Cloud Console](https://console.cloud.google.com),
   ativar a **YouTube Data API v3**, criar uma credencial OAuth tipo
   **Desktop app**, salvar o JSON em `credentials/client_secret.json`.
2. Criar um `channels/<nome>.yaml` (ver exemplos existentes).
3. Autenticar o canal: `.venv/bin/python3 scripts/auth_youtube.py --channel <nome>`
4. Rodar um teste sem publicar: `.venv/bin/python3 scripts/run_pipeline.py --channel <nome> --topic "..." --dry-run`
5. Painel: `.venv/bin/python3 web/app.py` → `http://localhost:5088`

## Automação diária

`scripts/daily_run.py` percorre `channels/*.yaml`, gera e publica os vídeos
do dia, respeitando `uploads_per_day`. Agendado via cron (`0 10 * * *`).

## Cota da API

YouTube Data API v3: 10.000 unidades/dia por projeto Google Cloud padrão;
1 upload = 1.600 unidades → ~6 uploads/dia no total, somando todos os
canais desse mesmo projeto.
