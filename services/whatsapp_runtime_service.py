from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from threading import RLock
from uuid import uuid4
from typing import Any, Awaitable, Callable

from services.feature_runtime_registry import (
    get_feature_state,
    mark_feature_error,
    mark_feature_running,
    mark_feature_starting,
    mark_feature_stopped,
)


logger = logging.getLogger("kira.whatsapp_runtime")

StartCallback = Callable[[], bool]
ResolveCallback = Callable[[str], dict[str, Any]]
SendCallback = Callable[[str, str], Awaitable[dict[str, Any]] | dict[str, Any]]


@dataclass
class PendingWhatsAppSend:
    token: str
    contact: str
    message: str
    resolved_contact: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 120.0)


@dataclass
class WhatsAppRuntimeState:
    state: str = "off"
    last_error: str | None = None
    logged_in: bool | None = None
    last_activity_at: float | None = None
    idle_shutdown_seconds: int = 300
    pending_confirmation: PendingWhatsAppSend | None = None


_lock = RLock()
_state = WhatsAppRuntimeState()

# FASE 2C: dedupe/cooldown + status do ultimo envio.
import hashlib

SEND_COOLDOWN_SECONDS = 90.0
_recent_sends: dict[str, float] = {}  # send_key -> timestamp do ultimo envio OK
_last_send: dict[str, Any] = {}       # ultimo resultado de envio (para "deu?")


def _compute_send_key(contact: str, message: str, style: str = "") -> str:
    base = "|".join([
        str(contact or "").strip().lower(),
        str(message or "").strip().lower(),
        str(style or "").strip().lower(),
    ])
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:16]


def check_send_dedupe(contact: str, message: str, style: str = "", *, now: float | None = None) -> dict[str, Any]:
    """Retorna {allowed, reason, key, age_s}. Bloqueia reenvio identico dentro do cooldown."""
    now = time.time() if now is None else float(now)
    key = _compute_send_key(contact, message, style)
    logger.info("WHATSAPP_DEDUPE_CHECK key=%s", key)
    with _lock:
        last_ts = _recent_sends.get(key)
    if last_ts is not None and (now - last_ts) < SEND_COOLDOWN_SECONDS:
        age = now - last_ts
        logger.warning("WHATSAPP_DEDUPE_BLOCKED key=%s age_s=%.1f", key, age)
        logger.info("WHATSAPP_SEND_COOLDOWN_ACTIVE key=%s remaining_s=%.1f", key, SEND_COOLDOWN_SECONDS - age)
        return {"allowed": False, "reason": "duplicate_within_cooldown", "key": key, "age_s": age}
    logger.info("WHATSAPP_DEDUPE_OK key=%s", key)
    return {"allowed": True, "reason": "ok", "key": key, "age_s": None}


def record_send_result(contact: str, message: str, style: str, ok: bool, *, error: str = "", now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    key = _compute_send_key(contact, message, style)
    with _lock:
        if ok:
            _recent_sends[key] = now
        _last_send.clear()
        _last_send.update({
            "contact": contact, "style": style, "ok": bool(ok),
            "error": error or "", "key": key, "at": now,
            "status": "sent" if ok else "failed",
        })


def get_last_send_status() -> dict[str, Any]:
    with _lock:
        snap = dict(_last_send)
        pending = _state.pending_confirmation
        pending_active = pending is not None and pending.expires_at >= time.time()
    snap["pending_active"] = bool(pending_active)
    if pending_active and not snap:
        snap["status"] = "awaiting_confirmation"
    return snap


def reset_for_tests() -> None:
    with _lock:
        _state.state = "off"
        _state.last_error = None
        _state.logged_in = None
        _state.last_activity_at = None
        _state.pending_confirmation = None
        _recent_sends.clear()
        _last_send.clear()


def _snapshot() -> dict[str, Any]:
    feature = get_feature_state("whatsapp")
    with _lock:
        pending = _state.pending_confirmation
        return {
            "enabled": bool(feature.enabled),
            "state": _state.state,
            "runtime_state": _state.state,
            "requiresStart": _state.state != "running",
            "process_running": _state.state == "running",
            "bridge_available": False,
            "connected": bool(_state.logged_in) if _state.logged_in is not None else False,
            "logged_in": _state.logged_in,
            "last_error": _state.last_error,
            "idle_shutdown_seconds": _state.idle_shutdown_seconds,
            "pending_confirmation": asdict(pending) if pending else None,
            "updated_at": time.time(),
        }


def get_whatsapp_status() -> dict[str, Any]:
    logger.info("WHATSAPP_STATUS_REQUESTED light=true")
    logger.info("WHATSAPP_STATUS_NO_WAKEUP")
    return _snapshot()


def touch_idle() -> None:
    with _lock:
        _state.last_activity_at = time.time()
    logger.info("WHATSAPP_IDLE_TIMER_TOUCH")


def ensure_whatsapp_started(*, reason: str = "user_requested", starter: StartCallback | None = None) -> dict[str, Any]:
    with _lock:
        if _state.state == "running":
            touch_idle()
            return _snapshot()
        _state.state = "starting"
        _state.last_error = None
    mark_feature_starting("whatsapp", reason=reason)
    try:
        ok = True if starter is None else bool(starter())
    except Exception as exc:
        ok = False
        err = str(exc)
    else:
        err = "start_failed"
    if not ok:
        with _lock:
            _state.state = "error"
            _state.last_error = err
        mark_feature_error("whatsapp", reason=err)
        return _snapshot()
    with _lock:
        _state.state = "running"
        _state.last_error = None
    mark_feature_running("whatsapp", reason=reason)
    touch_idle()
    return _snapshot()


def prepare_message(
    contact: str,
    message: str,
    *,
    resolver: ResolveCallback | None = None,
    starter: StartCallback | None = None,
    raw_text: str = "",
    style_inferer: Callable[[str, str], Any] | None = None,
    question_suggester: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    contact = str(contact or "").strip()
    message = str(message or "").strip()
    raw_text = str(raw_text or "").strip()
    logger.info("WHATSAPP_TASK_ROUTE lazy=true")
    logger.info("WHATSAPP_MESSAGE_PREPARE_BEGIN")
    if not contact:
        return {"ok": False, "error": "missing_contact", "requires_confirmation": False}
    if not message:
        return {"ok": False, "error": "missing_message", "requires_confirmation": False}
    runtime = ensure_whatsapp_started(reason="user_requested", starter=starter)
    if runtime.get("state") == "error":
        return {"ok": False, "error": runtime.get("last_error") or "start_failed", "requires_confirmation": False}
    logger.info("WHATSAPP_CONTACT_SEARCH_BEGIN")
    resolved_name = contact
    found = True
    if resolver is not None:
        resolved = resolver(contact)
        item = resolved.get("contact") if isinstance(resolved.get("contact"), dict) else {}
        # nome salvo no celular primeiro ("Vanessa" = "Sunshine"); fora da lista da Kira, o WhatsApp procura na agenda
        # do celular, como pela voz (antes "Breno" dava contact_not_found só por não estar na lista)
        resolved_name = str(item.get("whatsapp_name") or item.get("name") or contact).strip() or contact
        found = bool(resolved.get("found")) or bool(contact.strip())
    if found:
        logger.info("WHATSAPP_CONTACT_FOUND contact=%s", resolved_name)
    else:
        logger.info("WHATSAPP_CONTACT_NOT_FOUND contact=%s", contact)
        question = question_suggester("contact") if question_suggester else ""
        return {
            "ok": False,
            "error": "contact_not_found",
            "requires_confirmation": False,
            "requires_clarification": True,
            "clarification_topic": "contact",
            "clarification_question": question,
            "contact": contact,
        }

    # FASE 2B/C6: inferencia de estilo (sem envio). Se ambiguo/incompleto, NAO cria
    # confirmacao de envio — devolve uma pergunta de esclarecimento.
    style = "user_speaking"
    style_reason = "default"
    needs_clarification = False
    clarification_topic = ""
    if style_inferer is not None:
        try:
            inference = style_inferer(raw_text or message, message)
            style = str(getattr(inference, "style", style) or style)
            style_reason = str(getattr(inference, "reason", style_reason) or style_reason)
            needs_clarification = bool(getattr(inference, "needs_user_clarification", False))
            clarification_topic = str(getattr(inference, "clarification_topic", "") or "")
        except Exception as exc:
            logger.info("WHATSAPP_STYLE_INFERENCE_FAILED_NON_FATAL reason=%s", exc)

    logger.info("WHATSAPP_STYLE_INFERRED style=%s reason=%s", style, style_reason)

    if needs_clarification:
        question = question_suggester(clarification_topic) if question_suggester else ""
        logger.info("WHATSAPP_PREPARE_NEEDS_CLARIFICATION topic=%s", clarification_topic)
        touch_idle()
        return {
            "ok": True,
            "requires_confirmation": False,
            "requires_clarification": True,
            "clarification_topic": clarification_topic,
            "clarification_question": question,
            "style": style,
            "style_reason": style_reason,
            "contact": resolved_name,
            "message": message,
            "not_sent": True,
        }

    token = f"wa-confirm-{uuid4().hex[:12]}"
    pending = PendingWhatsAppSend(token=token, contact=contact, resolved_contact=resolved_name, message=message)
    with _lock:
        _state.pending_confirmation = pending
    logger.info("WHATSAPP_CONFIRMATION_CREATED token=%s", token)
    logger.info("WHATSAPP_MESSAGE_PREPARED")
    logger.info("WHATSAPP_SEND_WAITING_CONFIRMATION")
    touch_idle()
    return {
        "ok": True,
        "requires_confirmation": True,
        "confirm_token": token,
        "contact": resolved_name,
        "message": message,
        "style": style,
        "style_reason": style_reason,
        "not_sent": True,
    }


async def confirm_send(token: str, *, sender: SendCallback, style: str = "") -> dict[str, Any]:
    with _lock:
        pending = _state.pending_confirmation
        if pending is None or pending.token != token:
            return {"ok": False, "error": "confirmation_not_found"}
        if pending.expires_at < time.time():
            _state.pending_confirmation = None
            logger.info("WHATSAPP_CONFIRMATION_EXPIRED token=%s", token)
            return {"ok": False, "error": "confirmation_expired"}
        _state.pending_confirmation = None
    contact_final = pending.resolved_contact or pending.contact
    # FASE 2C: dedupe/cooldown ANTES do envio real — evita reenvio identico.
    dedupe = check_send_dedupe(contact_final, pending.message, style)
    if not dedupe.get("allowed"):
        logger.warning("WHATSAPP_REAL_SEND_BLOCKED reason=dedupe contact=%s", contact_final)
        return {
            "ok": False,
            "error": "duplicate_within_cooldown",
            "dedupe_blocked": True,
            "contact": contact_final,
            "message": pending.message,
        }
    logger.info("WHATSAPP_CONFIRMATION_ACCEPTED token=%s", token)
    logger.info("WHATSAPP_SEND_CONFIRMED")
    logger.info("WHATSAPP_REAL_SEND_BEGIN contact=%s", contact_final)
    try:
        result = sender(contact_final, pending.message)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        ok = bool(result.get("ok", result.get("success"))) if isinstance(result, dict) else bool(result)
    except Exception as exc:
        logger.info("WHATSAPP_REAL_SEND_FAILED reason=%s", exc)
        record_send_result(contact_final, pending.message, style, False, error=str(exc))
        return {"ok": False, "error": str(exc)}
    record_send_result(contact_final, pending.message, style, ok, error="" if ok else "sender_failed")
    if ok:
        logger.info("WHATSAPP_REAL_SEND_OK contact=%s", contact_final)
        logger.info("WHATSAPP_MESSAGE_SENT_OK")
        touch_idle()
        return {"ok": True, "contact": contact_final, "message": pending.message}
    logger.info("WHATSAPP_REAL_SEND_FAILED reason=sender_failed")
    return {"ok": False, "error": "sender_failed"}


def cancel_confirmation(token: str) -> dict[str, Any]:
    with _lock:
        pending = _state.pending_confirmation
        if pending is None or pending.token != token:
            return {"ok": False, "error": "confirmation_not_found"}
        _state.pending_confirmation = None
    logger.info("WHATSAPP_CONFIRMATION_CANCELLED token=%s", token)
    touch_idle()
    return {"ok": True}


def run_idle_shutdown_check(*, now: float | None = None, secretary_mode: bool = False) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    with _lock:
        if _state.pending_confirmation is not None:
            logger.info("WHATSAPP_IDLE_SHUTDOWN_SKIPPED reason=pending_confirmation")
            return _snapshot()
        if secretary_mode:
            logger.info("WHATSAPP_IDLE_SHUTDOWN_SKIPPED reason=secretary_mode")
            return _snapshot()
        last = _state.last_activity_at or now
        if _state.state != "running" or now - last < _state.idle_shutdown_seconds:
            return _snapshot()
        _state.state = "off"
    logger.info("WHATSAPP_IDLE_SHUTDOWN reason=no_activity")
    mark_feature_stopped("whatsapp", reason="idle_timeout")
    return _snapshot()
