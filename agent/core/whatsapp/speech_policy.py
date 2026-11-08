"""speech_policy.py — TODO o texto de fala/previa do WhatsApp em um lugar so.

build_*_text(): geram o texto DETERMINISTICO da previa/ack. Quem FALA e o chamador
(o Gemini fala o retorno da tool; em FALHA o gate fala via speak_whatsapp_ack_exact).
SUCESSO nao tem fala deterministica nem pre-ack: a unica confirmacao e a fala NATURAL
do Gemini, disparada pela nota de envio do orchestrator. Falhas usam o
user_message que o send_whatsapp_message ja monta por tipo de erro.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("core.whatsapp.speech_policy")


def _name(job) -> str:
    return (job.resolved_contact or job.contact or "essa pessoa").strip() or "essa pessoa"


def _clip(message: str, limit: int = 220) -> str:
    msg = " ".join(str(message or "").split())
    return msg if len(msg) <= limit else msg[: limit - 1].rstrip() + "…"


def build_preview_text(job, spoken_message: str) -> str:
    """Texto da previa (o Gemini fala como retorno da tool)."""
    name = _name(job)
    msg = _clip(spoken_message)
    if job.preview_mode == "skip":
        return f"Tenho uma mensagem pronta pra {name}. Posso mandar?"
    if job.confirmation_mode == "required":
        return f'Vou mandar pra {name}: "{msg}". Posso mandar?'
    return f'Mandando pra {name}: "{msg}".'


def build_success_text(job) -> str:
    return f"Pronto, chefe. Mandei pra {_name(job)}."


def build_failure_text(job, reason: str = "") -> str:
    name = _name(job)
    reason = str(reason or "").strip()
    return reason or f"Não consegui mandar pra {name} agora, chefe. A ponte do WhatsApp não respondeu."
