# Voice Validation Checklist

## 0) Preparacao

1. Inicie a Kira normalmente.
2. Confirme que `logs/voice_runtime.log` esta sendo atualizado.

## 1) Marcar inicio de cada cenario

Antes de cada teste, execute:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:3000/api/debug/voice-marker" -ContentType "application/json" -Body '{"label":"TESTE_1_FALA_SIMPLES"}'
```

Troque apenas o label para:
- `TESTE_1_FALA_SIMPLES`
- `TESTE_2_COMANDO_WHATSAPP`
- `TESTE_3_CONFIRMACAO`
- `TESTE_4_DUAS_INTERACOES`

## 2) Frases exatas sugeridas

### TESTE 1 - Fala simples
Fale:
"Kira, qual e a capital do Brasil?"

### TESTE 2 - Comando WhatsApp
Fale:
"Kira, mande mensagem para Joao: chego em 10 minutos."

### TESTE 3 - Confirmacao
Primeiro gere uma acao que exija confirmacao e em seguida fale:
"sim"
Repita depois com:
"nao"

### TESTE 4 - Duas interacoes seguidas
Fale em sequencia curta:
1. "Kira, que horas sao?"
2. "Kira, mande mensagem para Joao: podemos falar agora?"

## 3) Gerar relatorio

```powershell
py tools/voice_validation_report.py
```

