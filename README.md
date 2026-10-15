# youtube-ai-pipeline

Pipeline automatizado de vídeos "faceless" para YouTube: escolhe o tema,
escreve o roteiro, confere os fatos, narra, monta o vídeo com legenda,
faz a thumbnail e publica — sozinho, todo dia, **só com free tier**
(nenhuma chave pede cartão; os limites grátis têm teto no próprio código).

Roda num Raspberry Pi 5 (Linux arm64), mas qualquer Linux/macOS/WSL com
Python 3.11+ e ffmpeg serve.

## Como funciona

```
tema (fila do canal, tendências, dados reais) → roteiro (LLM grátis)
  → revisão de fatos e português → narração (TTS) → visual de cada cena
  → montagem (ffmpeg, Short 9:16 ou 16:9) → revisão do vídeo e do áudio
  → thumbnail → upload (YouTube Data API v3) + comentário
```

- **Roteiro**: cascata de LLMs grátis (Groq, Gemini, Cerebras, OpenRouter,
  Mistral, xAI, Cloudflare Workers AI) com rodízio de várias chaves por
  provedor. Os modelos são descobertos na hora (`/models`) — nome de modelo
  grátis muda toda hora, então nunca fica fixo no código.
- **Fatos**: canal com dados reais (política, futebol, notícias) só pode
  afirmar o que veio dos dados; tema digitado à mão é pesquisado na
  internet (Tavily, com DuckDuckGo de reserva). Um revisor (`src/fact_check.py`)
  reprova o roteiro que inventa nome, número ou consequência.
- **Narração**: edge-tts (grátis); Azure Speech, Google Cloud TTS e
  ElevenLabs entram como reserva, cada um dentro da cota grátis.
- **Visual**, do mais real pro último recurso: foto da própria notícia →
  filmagem/foto de banco (Pexels, Pixabay) → imagem de IA (Cloudflare FLUX,
  Pollinations). Toda imagem passa por um modelo de visão que confere se
  combina com o que a cena narra.
- **Montagem**: só `ffmpeg` (sem moviepy) — legenda karaokê, música de
  fundo por clima, efeitos sonoros, zoom lento em foto.
- **Upload**: YouTube Data API v3, com thumbnail, playlist e comentário.

## Canais incluídos (`channels/*.yaml`)

| canal | o que faz |
|---|---|
| `botafogo` | pré e pós-jogo do Botafogo (dados da ESPN, fotos reais do jogo, cartão de cada gol) e 1 vídeo de notícias por dia a partir do portal botafogo.win — cada notícia no seu bloco, com a manchete na tela |
| `politica` | dados de transparência (TSE/Câmara/Senado), ver `src/politica_data.py` |
| `curiosidades`, `motivacional`, `aprendervia` | fila de temas do canal + temas em alta no YouTube |

Cada `.yaml` define nicho, vozes, tamanho do vídeo, privacidade, bordão de
abertura, hashtags, site e o `prompt_base` do roteiro. Para um canal novo,
copie um deles e ajuste.

## Instalação

### 1. Clonar e rodar o instalador

```bash
git clone https://github.com/brumarx/CreateVideoYoutube.git
cd CreateVideoYoutube
./instalar.sh
```

O `instalar.sh` instala `ffmpeg` e `python3-venv` (via `apt-get`, pede
sudo), cria o `.venv/`, instala o `requirements.txt`, cria as pastas locais
(`data/ output/ logs/ credentials/`, que não vêm no clone) e copia o
`.env.example` para `.env`. Fora do Debian/Ubuntu, instale `ffmpeg` e
Python 3.11+ na mão e rode os mesmos passos do script.

### 2. Chaves grátis (`.env`)

Abra o `.env` e preencha o que for usar. Várias chaves do mesmo provedor
vão separadas por vírgula e entram em rodízio. Se existir
`~/.secrets/keys.env` (ou o caminho em `SECRETS_FILE`), as chaves vêm de lá
primeiro — útil pra manter todas num lugar só, fora do repositório.

| variável | pra quê | onde pegar |
|---|---|---|
| `GROQ_API_KEYS` | roteiro, revisão, transcrição do áudio | https://console.groq.com/keys |
| `GEMINI_API_KEYS` | roteiro e visão | https://aistudio.google.com/apikey |
| `CEREBRAS_API_KEYS`, `OPENROUTER_API_KEYS`, `MISTRAL_API_KEYS`, `XAI_API_KEYS` | mais LLMs na cascata (opcionais) | sites de cada provedor |
| `CLOUDFLARE_AI_KEYS` | imagem (FLUX), texto e visão — `ACCOUNT_ID:TOKEN` | dash.cloudflare.com → Workers AI → "Use REST API" |
| `PEXELS_API_KEYS` | vídeo/foto real | https://www.pexels.com/api/ |
| `PIXABAY_API_KEYS` | 2º banco de vídeo/foto | https://pixabay.com/api/docs/ |
| `POLLINATIONS_API_KEYS` | imagem de IA de reserva (opcional) | https://pollinations.ai |
| `TAVILY_API_KEYS` | pesquisa pra tema digitado (opcional; sem ela usa DuckDuckGo) | https://tavily.com |
| `AZURE_SPEECH_KEYS`, `AZURE_SPEECH_REGION` | vozes extras (opcional) | portal.azure.com → Speech |
| `GCP_TTS_KEY_FILE` | voz de reserva Google Cloud TTS (JSON de conta de serviço) | console.cloud.google.com |
| `ELEVENLABS_API_KEYS` | voz de reserva (opcional) | https://elevenlabs.io |

O mínimo pra funcionar: **uma chave de LLM** (Groq ou Gemini) e **uma de
banco de imagem** (Pexels). O resto melhora a qualidade e evita ficar sem
cota.

### 3. Acesso ao YouTube (uma vez)

1. Em https://console.cloud.google.com crie um projeto e ative a
   **YouTube Data API v3**.
2. Em "Tela de consentimento OAuth", adicione seu e-mail como usuário de
   teste.
3. Em "Credenciais", crie um **ID do cliente OAuth** do tipo **App para
   computador** e salve o JSON em `credentials/client_secret.json`.
4. Autorize cada canal (abre o navegador; escolha a conta/canal certo):

   ```bash
   .venv/bin/python3 scripts/auth_youtube.py --channel <nome>
   ```

> Com o app OAuth em modo **Teste**, o Google invalida o token a cada ~7
> dias (`invalid_grant`). É só rodar o `auth_youtube.py` de novo; vídeos que
> ficaram prontos sem subir saem com `scripts/retry_uploads.py`.
>
> Thumbnail personalizada exige o canal verificado por telefone
> (youtube.com/verify). Sem isso o upload usa um quadro do vídeo.

### 4. Testar

```bash
# só o roteiro (rápido, pra ler antes de gastar tempo renderizando)
.venv/bin/python3 scripts/run_pipeline.py --channel curiosidades --script-only

# vídeo completo sem publicar
.venv/bin/python3 scripts/run_pipeline.py --channel curiosidades --no-upload

# vídeo de um tema específico
.venv/bin/python3 scripts/run_pipeline.py --channel curiosidades --topic "..." --no-upload
```

O vídeo sai em `output/job_<n>/final.mp4`.

### 5. Painel

```bash
.venv/bin/python3 web/app.py      # http://localhost:5088
```

Mostra a fila, aprova roteiros/vídeos, troca formato e vozes de cada canal.

### 6. Automação diária (cron)

```bash
crontab -e
```

```cron
0 10 * * * cd /caminho/CreateVideoYoutube && .venv/bin/python3 scripts/daily_run.py >> logs/daily_run.log 2>&1
@reboot sleep 20 && cd /caminho/CreateVideoYoutube && .venv/bin/python3 web/app.py >> logs/dashboard.log 2>&1
```

`daily_run.py` passa por todos os `channels/*.yaml`, respeita
`uploads_per_day` e tenta de novo (até 6 vezes) quando um vídeo é reprovado
nas revisões.

## Estrutura

```
channels/*.yaml        1 arquivo por canal
credentials/           client_secret.json e token_<canal>.json (gitignorado)
src/
  providers.py         cascata de LLM com rodízio de chaves
  topics.py            fila de temas + temas em alta
  script_gen.py        roteiro em JSON
  fact_check.py        revisão de fatos do roteiro
  spellcheck.py        revisão de português (LanguageTool)
  tts.py               narração e pronúncia
  stock_media.py       vídeo/foto real (Pexels, Pixabay)
  visuals.py           imagem de IA
  visual_check.py      modelo de visão: a imagem combina com a cena?
  assemble.py          montagem (ffmpeg), legenda, música
  thumbnail.py         thumbnail
  news_card.py         manchete na tela nos vídeos de notícias
  goal_card.py         cartão do gol (pós-jogo)
  video_qa.py          revisão final do vídeo e do áudio
  upload.py            YouTube Data API v3
  orchestrator.py      fila de jobs (SQLite)
  botafogo_data.py     ESPN + portal botafogo.win
  politica_data.py     dados de transparência política
scripts/
  auth_youtube.py      token OAuth de um canal
  run_pipeline.py      1 vídeo ponta a ponta
  daily_run.py         rodada diária de todos os canais
  retry_uploads.py     sobe vídeos prontos que falharam no upload
web/app.py             painel (Flask, rede local)
```

## Cota da API do YouTube

10.000 unidades/dia por projeto do Google Cloud; 1 upload custa 1.600 →
uns 6 vídeos por dia somando todos os canais do mesmo projeto.

## Problemas comuns

- **`invalid_grant` no upload**: token expirou — rode `auth_youtube.py` de
  novo (ver passo 3).
- **"nada a publicar"** no canal botafogo: sem jogo pendente; normal.
- **Todas as LLMs com 429/413**: cota grátis do dia acabou; adicione mais
  chaves ou espere o dia seguinte.
- **Vídeo reprovado na revisão**: veja `logs/daily_run.log` — o motivo
  (frase inventada, imagem fora de contexto, pronúncia) vem na linha.
