"""sender.py — camada de envio do WhatsApp V9.

Ponto unico que o orchestrator chama para enviar. NAO reimplementa o preflight/
retry: delega para `integrations.whatsapp_runtime.send_whatsapp_message` (logica
battle-tested, com refresh de status, 3 tentativas e mapeamento de erro) e
devolve um `SendResult` limpo. O pre-ack falado ("Beleza, mandando...") sai ANTES
do `await send()` — isso e responsabilidade do orchestrator, nao deste modulo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("core.whatsapp.sender")


@dataclass(slots=True)
class SendResult:
    ok: bool
    error: str = ""
    error_code: str = ""
    user_message: str = ""
    raw: dict | None = None


async def send(job) -> SendResult:
    """Envia o WhatsappJob via bridge. Retorna SendResult (nunca levanta)."""
    # import tardio: evita acoplar o import do pacote ao httpx/runtime e
    # qualquer ciclo com integrations.
    try:
        from integrations.whatsapp_runtime import send_whatsapp_message
    except Exception as exc:  # pragma: no cover - ambiente sem runtime
        logger.warning("WHATSAPP_SENDER_IMPORT_FAILED error=%s", exc)
        return SendResult(ok=False, error=f"import_failed:{exc}", error_code="runtime_unavailable",
                          user_message="O modulo de envio do WhatsApp nao esta disponivel.")

    logger.info("WHATSAPP_SENDER_DISPATCH job=%s contact=%s", job.id, job.contact)
    try:
        payload = await send_whatsapp_message(
            job.contact,
            job.message,
            source="voice_command",
            resolved_number=job.resolved_number,
            request_id=job.message_id,
        )
    except Exception as exc:
        logger.warning("WHATSAPP_SENDER_RAISED job=%s error=%s", job.id, exc)
        return SendResult(ok=False, error=str(exc), error_code="sender_exception",
                          user_message="Nao consegui falar com a ponte do WhatsApp.")

    payload = payload if isinstance(payload, dict) else {}
    ok = bool(payload.get("ok") or payload.get("success"))
    result = SendResult(
        ok=ok,
        error=str(payload.get("error") or ""),
        error_code=str(payload.get("error_code") or payload.get("stage") or ""),
        user_message=str(payload.get("user_message") or ""),
        raw=payload,
    )
    logger.info("WHATSAPP_SENDER_RESULT job=%s ok=%s error_code=%s", job.id, str(result.ok).lower(),
                result.error_code or "none")
    return result
