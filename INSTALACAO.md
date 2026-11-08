# Kira: manual de instalação

A Kira é uma assistente de voz para Windows: você fala, ela responde com voz, abre painéis, toca música,
controla o PC, responde o WhatsApp por você e pode aparecer na TV e no celular.

Este pacote vem "zerado": sem memórias, sem contatos, sem chaves e sem conta conectada. Tudo o que for seu
você coloca no seu computador.

---

## 1. O que o computador precisa

| Item | Para quê | Obrigatório? |
|---|---|---|
| Windows 10 ou 11, 64 bits | A Kira só roda no Windows | Sim |
| 8 GB de RAM (16 GB recomendado) e ~10 GB livres no disco | Os pacotes de voz e visão são grandes | Sim |
| Microfone e fone ou caixa de som | Falar com ela | Sim |
| Internet | O "cérebro" (Gemini) e a voz (LiveKit) ficam na nuvem | Sim |

## 2. Programas para instalar (nesta ordem)

1. **Python 3.12** (testado no 3.12.9): https://www.python.org/downloads/release/python-3129/
   - Baixe o "Windows installer (64-bit)".
   - Na primeira tela, **marque "Add python.exe to PATH"**.
2. **Node.js 20 ou mais novo** (versão LTS): https://nodejs.org. É usado pela ponte do WhatsApp.
3. **Microsoft Visual C++ Redistributable 2015-2022 (x64)**: https://aka.ms/vs/17/release/vc_redist.x64.exe.
   Os pacotes de áudio, visão e IA precisam dele.
4. **Git**, para baixar e atualizar a Kira: https://git-scm.com/download/win
5. **Tailscale**, só se quiser usar a Kira pelo celular: https://tailscale.com/download.
   Instale no PC **e** no celular, com a mesma conta.

O **DirectX não é necessário**; o que já vem no Windows basta.

## 3. Baixar a Kira

```bat
cd %USERPROFILE%\Desktop
git clone <endereço-do-repositório> Kira
cd Kira
```

## 4. Instalar tudo de uma vez

Dê dois cliques em **`INSTALAR.bat`**. Ele:

- cria o ambiente Python em `agent\venv`;
- instala todos os pacotes (demora de 10 a 30 minutos na primeira vez);
- instala o navegador interno da Kira (Chromium do Playwright);
- instala a ponte do WhatsApp (`npm install`);
- baixa o **FFmpeg** (áudio/vídeo) para `bin\ffmpeg\ffmpeg.exe`;
- baixa o **servidor de voz local do LiveKit** para `livekit\livekit-server.exe`;
- cria o seu `.env` a partir do `.env.example`.

Se algum passo falhar, a mensagem diz qual. Dá para rodar o `INSTALAR.bat` de novo sem problema.

## 5. As chaves (arquivo `.env`)

Abra o arquivo **`.env`** com o Bloco de Notas e preencha.

### Obrigatórias

**Gemini (o cérebro e a voz da Kira)**, de graça:

1. Entre em https://aistudio.google.com/apikey com uma conta Google.
2. Clique em "Create API key".
3. Cole a chave em `GEMINI_API_KEY=` e também em `GEMINI_1_API_KEY=`.
4. Opcional: crie chaves em até 4 contas Google diferentes (`GEMINI_2_API_KEY`, `GEMINI_3_API_KEY`,
   `GEMINI_4_API_KEY`). A Kira troca sozinha quando a cota de uma acaba.

**LiveKit (o "telefone" que leva sua voz até a Kira)**. Escolha uma das opções:

- **Nuvem (recomendado)**, de graça com limite de minutos por mês:
  1. Crie a conta em https://cloud.livekit.io.
  2. Crie um projeto e vá em *Settings > Keys*.
  3. Preencha `LIVEKIT_CLOUD_URL_1` (começa com `wss://`), `LIVEKIT_CLOUD_API_KEY_1` e `LIVEKIT_CLOUD_API_SECRET_1`.
  4. Deixe `LIVEKIT_PROVIDER=auto`.
- **Só local**, sem conta e sem limite, rodando no próprio PC: deixe `LIVEKIT_PROVIDER=local`. As chaves
  `LIVEKIT_LOCAL_*` já vêm prontas.

### Opcionais (cada uma liga um recurso a mais)

| Chave | Onde pegar | O que liga |
|---|---|---|
| `MEM0_API_KEY` | https://app.mem0.ai | Memória longa na nuvem (sem ela, a memória fica só no PC) |
| `YOUTUBE_API_KEY` | Google Cloud Console > YouTube Data API v3 | Vídeos nos painéis |
| `NASA_API_KEY` | https://api.nasa.gov | Dados da NASA no Olho de Deus |
| `AISSTREAM_KEY` | https://aisstream.io | Navios no Olho de Deus |
| `GEMINI_BROWSER_API_KEY_1..4` | Igual ao Gemini | Chaves separadas para o navegador automático |

Nunca envie o `.env` para ninguém nem para o GitHub. O `.gitignore` já impede isso.

## 6. Abrir a Kira

Dê dois cliques em **`Kira.bat`**. Na primeira vez demora mais.

Quando aparecer a tela com a raposa, clique em **INICIAR SESSÃO** e fale.

Depois, ajuste tudo em **Configurações** (engrenagem):

- **Personalidade**: em "como chamar você", ponha o seu nome. O padrão é "Chefe".
- **Áudio**: escolha o microfone e o fone.
- **Aparência**: orbe, fundo, menu e animações (com "Sem animações", o PC fica mais leve).
- **Modo econômico**: quanto tempo parada até trocar para o LiveKit local, e as palavras de ativação.

## 7. Conectar as contas dele

- **WhatsApp**:
  1. Abra a aba WhatsApp da Kira e aparece um QR Code.
  2. No celular: WhatsApp > Aparelhos conectados > Conectar aparelho.
  3. Leia o QR Code.
  4. Os contatos e a personalidade de cada um ficam nessa mesma aba.
- **Música/YouTube**: na aba Música, entre com a sua conta quando ela pedir. Fica salvo só no seu PC.
- **TV LG (webOS)**: com a TV e o PC na mesma rede Wi-Fi, peça "Kira, procura a TV". Na primeira vez a TV pede
  para aceitar a Kira; aceite no controle.

## 8. Usar pelo celular (Tailscale)

1. Instale o Tailscale no PC e no celular, com a mesma conta, e deixe os dois ligados.
2. Na Kira: Configurações > **Celular**. Siga os passos; ela mostra o seu link próprio (`https://<seu-pc>.<sua-rede>.ts.net`).
3. Abra o link no celular. No Chrome, use "Adicionar à tela inicial" para virar um app.

O link só funciona para aparelhos da **sua** rede Tailscale. Ninguém de fora consegue abrir.

## 9. Problemas comuns

| Sintoma | O que fazer |
|---|---|
| "Erro de websocket" ao iniciar a sessão | Os minutos da conta LiveKit Cloud acabaram: use `LIVEKIT_PROVIDER=local` ou crie outra conta |
| Ela não escuta | Configurações > Áudio: escolha o microfone certo; veja se outro programa não está usando o microfone |
| Ela fica muda | Confira a chave Gemini no `.env` (sem espaços); veja `logs\Kira\` |
| Tela piscando ou fechando sozinha em placa de vídeo antiga | Aparência > Animações > "Sem animações" |
| WhatsApp não conecta | Rode `INSTALAR.bat` de novo (precisa do Node.js 20+) e leia o QR Code de novo |
| Travou no bloqueio da Kira | Ctrl+Alt+Del > Gerenciador de Tarefas > encerrar "python"; ou crie um arquivo vazio chamado `runtime\destravar_kira` |

Os registros de tudo ficam em `logs\` (apagados sozinhos depois de 2 dias).
