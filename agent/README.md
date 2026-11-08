# Kira Desktop

Esta versao da Kira sobe a interface LiveKit em uma janela desktop PySide6 e inicia os processos auxiliares automaticamente.

## Estrutura

```text
agent/
|-- agent.py
|-- kira_tools/
|   |-- gerenciamento_arquivos/
|   |-- controladores/
|   |-- produtividade/
|   |-- monitoramento/
|   |-- agenda/
|   `-- memoria_perfil_formularios/
|-- desktop_app.py
|-- token_server.py
|-- prompts.py
|-- requirements.txt
`-- web/
    `-- voice-ai-agent.html
```

## O que o `desktop_app.py` faz

- abre a janela nativa da Kira
- inicia `token_server.py` em background
- inicia `agent.py start` em background
- carrega a interface LiveKit dentro do app
- minimiza para a bandeja do sistema

## Dependencias

No `cmd`, dentro da pasta do projeto:

```bat
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Se voce ainda nao tiver `venv`, crie antes:

```bat
py -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Executar tudo

Com o ambiente pronto, rode:

```bat
venv\Scripts\python.exe desktop_app.py
```

Se voce ativou o ambiente virtual antes, tambem funciona:

```bat
python desktop_app.py
```

## Arquivo `.env`

A Kira desktop usa o mesmo `.env` do projeto. O minimo necessario e:

```env
LIVEKIT_URL=wss://seu-projeto.livekit.cloud
LIVEKIT_API_KEY=sua_chave
LIVEKIT_API_SECRET=seu_segredo
LIVEKIT_ROOM=kira-room
TOKEN_SERVER_PORT=8765
```

Opcional, mas recomendado quando voce quiser dispatch explicito do worker:

```env
LIVEKIT_AGENT_NAME=kira
```

Se `LIVEKIT_AGENT_NAME` estiver definido, o `token_server.py` tenta despachar a Kira para a mesma sala automaticamente.

## Logs

Ao rodar o app desktop, os logs ficam em:

```text
logs/agent.log
logs/token_server.log
```
