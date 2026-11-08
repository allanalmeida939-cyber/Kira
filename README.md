# Kira: assistente de voz para Windows

A Kira é uma assistente pessoal que **mora no seu computador e você controla falando**. Você conversa com ela
como com uma pessoa: ela responde com voz natural em português, mostra as coisas na tela, mexe no PC, cuida do
seu WhatsApp, toca música, aparece na TV da sala e atende você pelo celular, de qualquer lugar.

> **Em uma frase:** é como ter a Alexa, o Google Assistente e uma secretária juntos dentro do seu PC, com
> personalidade própria, memória e visual de filme de ficção científica.

---

## O que ela faz

### Conversa por voz, de verdade
- Fala e escuta em tempo real, sem apertar botão: você fala e ela responde na hora, e dá para interromper no
  meio da frase.
- Tem **personalidade ajustável**: de carinhosa a bruta e sarcástica, com controles de humor, deboche,
  paciência etc.
- **Lembra de você**: preferências, conversas passadas e o que ficou pendente. Quando você some por dias, ela
  puxa assunto ("olha quem apareceu…").
- **Aprende**: gírias que você explica e o jeito certo de fazer algo quando você corrige.
- **Reconhece a sua voz** e pode ignorar ordens de outras pessoas.
- **Modo "só quando chamada"**, estilo Alexa: ela só age quando ouve "Kira".

### Mostra as coisas na tela (painéis)
- Clima, notícias, agenda, resumo do dia, imagens, gráficos, tabelas e explicações com fotos que trocam no ritmo
  da fala dela.
- **Olho de Deus**: um globo 3D com aviões, satélites, terremotos, clima, navios e mapa até o nível da rua.
- **Estudos**: professora particular de idiomas e matérias, com simulados, flashcards, leitura de PDF e busca de
  provas antigas.
- Vários estilos de orbe animado, de fundo, de menu e de relógio, todos trocáveis por voz ("troca o orbe").

### Controla o computador
- Abre programas e sites, organiza janelas, tira **print** de qualquer janela (mesmo minimizada) e põe no
  copiar/colar.
- Lê o que você copiou, procura arquivos e cria documentos.
- **Vê a sua tela e a webcam** quando você pede ("o que é isso na minha tela?").
- **Bloqueio por senha**: cobre todos os monitores, trava o teclado e prende o mouse até digitar a senha.
- Lembretes, alarmes e avisos com hora, inclusive falados.

### Secretária do WhatsApp
- Lê e responde mensagens **por você**, no seu número, sem abrir o navegador.
- Trata **cada contato do jeito que ele trata a Kira** (doce com quem é gentil, seca com quem é grosso), ou
  com a personalidade que você escolher para cada um.
- Manda mensagem por voz ("Kira, manda pra minha mãe que eu chego às 8").

### Música
- Toca qualquer música do YouTube por voz e controla pausar, pular e volume.
- Player dentro da própria Kira, com vários visuais.

### TV (LG webOS)
- "**Kira, vai pra TV**": ela passa para a televisão inteira, com orbe, painéis, Olho de Deus, música e voz
  saindo pela TV.
- Controla a TV pela rede: liga, troca de app, volume, pausa no IPTV/Netflix e print da tela da TV.
- Abaixa o volume da TV sozinha quando você chama "Kira".

### Celular
- Abra a sua Kira no celular **de qualquer lugar**, por um link privado só seu (via Tailscale), e instale como
  app.
- Fala e escuta pelo celular, recebe notificações e manda coisas para a TV.

### Liga sozinha e economiza
- Abre junto com o Windows e volta sozinha depois de uma queda de energia.
- **Modo econômico 24h**: parada, ela passa para o servidor de voz local (grátis) e acorda quando você fala.
- Gira entre até 4 chaves do Gemini quando a cota de uma acaba.

---

## Como funciona (parte técnica)

| Parte | Tecnologia |
|---|---|
| Cérebro e voz | **Google Gemini Live** (áudio nativo, fala com fala em tempo real) |
| Transporte da voz | **LiveKit** (nuvem ou servidor local `livekit-server.exe`) + LiveKit Agents (Python) |
| Servidor e tela | **Python 3.12**: FastAPI + Uvicorn; janela desktop com pywebview + Qt WebEngine (PySide6) |
| Interface | HTML/CSS/JavaScript puro (módulos ES), canvas 2D/Three.js para orbes e globo |
| WhatsApp | Node.js 20 + **Baileys** (protocolo do WhatsApp Web, sem navegador) |
| Memória | SQLite local + busca por significado (ONNX/Qdrant), opcional **mem0** na nuvem |
| Visão e voz extra | OpenCV, MediaPipe (rostos e gestos), sherpa-onnx (reconhecer quem fala) |
| Mídia | yt-dlp + FFmpeg (música), Playwright/Chromium (navegador automático) |
| TV | API SSAP do webOS (LG) pela rede local + página própria no navegador da TV |
| Celular | Tailscale (HTTPS privado) + PWA com notificações (Web Push) |

**Fluxo:**
1. O microfone vai para a sala de voz do LiveKit.
2. O agente Python repassa o áudio ao Gemini Live.
3. O Gemini responde com voz e chama "ferramentas" (abrir painel, tocar música, mandar WhatsApp…).
4. O servidor executa e mostra o resultado na tela do PC, da TV ou do celular.

---

## Instalar

**Jeito fácil (Claude Code):** abra o VS Code com o Claude Code e peça:
> *"instala a Kira deste repositório: https://github.com/allanalmeida939-cyber/Kira"*

Ele segue o [CLAUDE.md](CLAUDE.md): instala os programas, roda o instalador, pede as suas chaves e testa.

**Jeito manual:** siga o [INSTALACAO.md](INSTALACAO.md) e rode `INSTALAR.bat`.

### O que você vai precisar
- **Windows 10/11 64 bits**, 8 GB de RAM (16 GB recomendado), microfone e internet.
- **Chave do Gemini**: grátis em https://aistudio.google.com/apikey.
- **LiveKit**: conta grátis em https://cloud.livekit.io, **ou** o modo local, que não precisa de conta.
- Opcionais: mem0, YouTube Data API, NASA e AISStream (cada uma liga um recurso a mais).

---

## Privacidade
Este repositório **não traz nenhum dado pessoal**. Memórias, conversas, contatos, chaves e contas conectadas
ficam **só no seu computador** e nunca são enviados para o git (veja o `.gitignore`). Cada pessoa usa as
próprias chaves e contas.
