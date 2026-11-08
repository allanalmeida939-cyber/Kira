"""Pacote WhatsApp V9 da Kira.

Arquitetura tool-pura: o Gemini chama `enviar_whatsapp` -> orchestrator cria um
WhatsappJob no job_store -> (preview/confirmacao) -> sender faz POST no bridge ->
speech_policy fala o ack. Substitui a logica WhatsApp que vivia inchada no
assistant.py (propose_whatsapp_reply / process_whatsapp_confirmation /
_send_confirmed_whatsapp_message / _classify_whatsapp_confirmation_reply).

Importar como `from core.whatsapp.<modulo> import ...` (o worker tem agent/ no
sys.path; NAO usar prefixo `agent.` nem o nome top-level `whatsapp`, que colide
com os stubs da raiz).
"""
