# Instalar a Kira (instruções para o Claude Code)

Quando o usuário pedir para instalar a Kira, siga estes passos em ordem, no Windows, a partir da raiz deste
repositório. Fale com ele em português simples. Explique cada passo antes de rodar e peça confirmação antes de
instalar programas no sistema.

## 1. Conferir os programas

Rode os comandos abaixo e veja o que já está instalado:

```bat
py -3.12 --version
node --version
git --version
```

Instale o que faltar com o `winget` (peça a confirmação dele antes):

| Programa | Comando |
|---|---|
| Python 3.12 | `winget install -e --id Python.Python.3.12` |
| Node.js 20 LTS ou mais novo | `winget install -e --id OpenJS.NodeJS.LTS` |
| Visual C++ Redistributable (sempre instale) | `winget install -e --id Microsoft.VCRedist.2015+.x64` |
| Git | `winget install -e --id Git.Git` |
| Tailscale (só se ele quiser usar a Kira pelo celular) | `winget install -e --id Tailscale.Tailscale` |

Depois de instalar, abra um terminal novo para o PATH valer.

## 2. Rodar o instalador

```bat
INSTALAR.bat
```

O instalador:

- cria `agent\venv`;
- instala `requirements-lock.txt`, ou `requirements.txt` se a lista exata falhar;
- instala o Chromium do Playwright;
- roda o `npm install` da ponte do WhatsApp;
- baixa o FFmpeg (`bin\ffmpeg\ffmpeg.exe`) e o `livekit\livekit-server.exe`;
- cria o `.env`.

Ele leva de 10 a 30 minutos. Rode em segundo plano e acompanhe.

Se um pacote falhar, leia o erro e instale aquele pacote sozinho no venv
(`agent\venv\Scripts\python.exe -m pip install <pacote>`). Não troque a versão do Python: tem que ser a 3.12.

## 3. Chaves no `.env` (o usuário fornece; nunca invente nem envie a lugar nenhum)

Peça a ele as chaves e preencha:

- **Obrigatório: Gemini.** A chave é criada em https://aistudio.google.com/apikey. Coloque a mesma chave em
  `GEMINI_API_KEY` e em `GEMINI_1_API_KEY`. Se ele tiver até 3 chaves extras, vão em
  `GEMINI_2_API_KEY`..`GEMINI_4_API_KEY`.
- **Obrigatório: LiveKit**, com uma de duas opções:
  - **Nuvem:** conta em https://cloud.livekit.io, depois *Settings > Keys*. Preencha
    `LIVEKIT_CLOUD_URL_1` (`wss://...`), `LIVEKIT_CLOUD_API_KEY_1` e `LIVEKIT_CLOUD_API_SECRET_1`, com
    `LIVEKIT_PROVIDER=auto`.
  - **Sem conta:** `LIVEKIT_PROVIDER=local`. As chaves `LIVEKIT_LOCAL_*` já vêm prontas.
- **Opcionais:** `MEM0_API_KEY`, `YOUTUBE_API_KEY`, `NASA_API_KEY` e `AISSTREAM_KEY`. Pergunte se ele quer
  alguma; sem elas a Kira funciona.

Nunca faça commit do `.env`.

## 4. Primeiro teste

Abra `Kira.bat`. Espere a tela com a raposa e peça a ele para clicar em **INICIAR SESSÃO** e falar.

Se der erro, veja os registros:

- `logs\Backend\backend_*.log` (servidor);
- `logs\Kira\kira_session_*.log` (voz).

| Erro | O que significa |
|---|---|
| "erro de websocket" ou 429 no LiveKit | Os minutos do LiveKit Cloud acabaram. Use `LIVEKIT_PROVIDER=local` |
| `GEMINI` com 400/403 | A chave Gemini está errada |
| `GEMINI_INPUT_DIGITAL_SILENCE` | O microfone está errado. Escolha em Configurações > Áudio |

## 5. Depois de funcionar, avise o usuário

- Em Configurações > Personalidade, ele põe o nome dele em "como chamar". O padrão é "Chefe".
- WhatsApp: na aba WhatsApp, leia o QR Code em Aparelhos conectados.
- Celular: com o Tailscale no PC e no celular, vá em Configurações > Celular.
- TV LG: com a TV na mesma rede, diga "Kira, procura a TV".
- Detalhes para pessoas: `INSTALACAO.md`.

## Regras do projeto

- O Python é sempre `agent\venv\Scripts\python.exe` (nunca o global).
- Não rode testes que mexem em arquivos reais sem necessidade.
- Não suba dados pessoais (`memory/`, `data/`, `logs/`, `configs/*.json`, `.env`) para o git.
