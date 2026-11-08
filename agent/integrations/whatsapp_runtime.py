from __future__ import annotations

import asyncio
import asyncio as _asyncio
import inspect
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.whatsapp_context import (
    build_whatsapp_batch_notification_speech,
    build_whatsapp_direct_notification_speech,
)
from whatsapp.whatsapp_logger import get_whatsapp_logger
from whatsapp.whatsapp_diagnostics import get_active_diagnostics
from whatsapp.whatsapp_group_guard import WhatsAppGroupGuard


logger = logging.getLogger(__name__)

WHATSAPP_BRIDGE_URL = "http://127.0.0.1:5050"


def _bridge_headers() -> dict[str, str]:
    """O bridge (whatsapp_bridge/bridge.mjs) só aceita pedidos com o token interno da Kira."""
    return {"X-Kira-Internal": os.getenv("KIRA_INTERNAL_TOKEN", "")}
_BATCH_WINDOW_SECONDS = 4.0

# FASE 2D.3: cooldown de VOZ para avisos (assistant/secretary notify) — evita floodar a fala.
# A UI continua recebendo TUDO; só a voz é limitada.
_ASSISTANT_VOICE_PER_CONTACT_COOLDOWN_S = 90.0
_ASSISTANT_VOICE_GLOBAL_COOLDOWN_S = 30.0
_assistant_voice_last_by_contact: dict[str, float] = {}
_assistant_voice_last_global: float = 0.0


def _assistant_voice_cooldown_check(contact: str) -> tuple[bool, str]:
    now = time.monotonic()
    if now - _assistant_voice_last_global < _ASSISTANT_VOICE_GLOBAL_COOLDOWN_S:
        return False, "global_cooldown"
    last_c = _assistant_voice_last_by_contact.get(str(contact or ""), 0.0)
    if now - last_c < _ASSISTANT_VOICE_PER_CONTACT_COOLDOWN_S:
        return False, "contact_cooldown"
    return True, "ok"


def _assistant_voice_mark_spoken(contact: str) -> None:
    global _assistant_voice_last_global
    now = time.monotonic()
    _assistant_voice_last_global = now
    _assistant_voice_last_by_contact[str(contact or "")] = now


def reset_assistant_voice_cooldown_for_tests() -> None:
    global _assistant_voice_last_global
    _assistant_voice_last_global = 0.0
    _assistant_voice_last_by_contact.clear()

# buffer: contact -> list[_BufferedIncomingWhatsAppEvent]
_pending_events: dict[str, list["_BufferedIncomingWhatsAppEvent"]] = defaultdict(list)
# timers: contact -> asyncio.Task
_pending_tasks: dict[str, object] = {}
_group_guard = WhatsAppGroupGuard()

_POTENTIAL_DELEGATION_RE = re.compile(
    r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:avisa|avisar|fala|falar|diz|dizer|manda|mandar|responde|responder|envia|enviar)\s+"
    r"(?:(?:pra|pro|para|ao|a|o)\s+\S+|mensagem\b)",
    re.IGNORECASE,
)
_ASSISTANT_ADDRESSED_RE = re.compile(
    r"^(?:(?:oi|ei)\s+)?kira[\s,:;-]+",
    re.IGNORECASE,
)


def _resolve_whatsapp_runtime_config_path() -> Path:
    override = str(os.getenv("KIRA_WHATSAPP_RUNTIME_CONFIG", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # agent/integrations -> agent -> project root
    base_dir = Path(__file__).resolve().parents[2]
    return base_dir / "configs" / "whatsapp_runtime.json"


def load_whatsapp_runtime_config() -> dict[str, object]:
    """
    Le a mesma fonte de verdade do launcher (configs/whatsapp_runtime.json).
    """
    path = _resolve_whatsapp_runtime_config_path()
    default_cfg: dict[str, object] = {"enabled": False, "mode": "assistant"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_cfg
    if not isinstance(raw, dict):
        return default_cfg
    cfg = dict(default_cfg)
    cfg["enabled"] = bool(raw.get("enabled", default_cfg["enabled"]))
    cfg["mode"] = str(raw.get("mode", default_cfg["mode"]) or "assistant").strip().lower() or "assistant"
    return cfg


def is_whatsapp_runtime_enabled() -> bool:
    return bool(load_whatsapp_runtime_config().get("enabled", False))


@dataclass(slots=True)
class IncomingWhatsAppEvent:
    contact: str
    text: str
    timestamp: str | None = None
    message_id: str | None = None
    source: str = "whatsapp"
    raw_payload: dict[str, object] | None = None
    transport: str = "bridge_http_poll"
    is_group: bool | None = None
    original_text: str | None = None
    received_at_monotonic: float | None = None
    received_at_epoch: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class _BufferedIncomingWhatsAppEvent:
    event: IncomingWhatsAppEvent
    remembered: object = None


@dataclass(slots=True)
class _AgentHandlerOutcome:
    handled: bool
    speech: str | None = None


def _safe(text: str) -> str:
    """Sanitiza texto para saida segura em terminais Windows."""
    if not isinstance(text, str):
        text = str(text)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = _normalize_text(value).lower()
    if normalized in {"1", "true", "yes", "sim"}:
        return True
    if normalized in {"0", "false", "no", "nao", "nÃ£o"}:
        return False
    return None


def _truncate_preview(text: str, *, limit: int = 90) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def _build_event(
    contact: str,
    raw_text: str,
    metadata: dict[str, object] | None = None,
) -> IncomingWhatsAppEvent:
    payload = dict(metadata or {})
    raw_payload = payload.get("raw_payload")
    raw_payload_dict = dict(raw_payload) if isinstance(raw_payload, dict) else None
    original_text = str(
        payload.get("original_text")
        or (raw_payload_dict or {}).get("text")
        or raw_text
    )
    event = IncomingWhatsAppEvent(
        contact=_normalize_text(contact) or "Desconhecido",
        text=_normalize_text(raw_text),
        timestamp=_normalize_text(payload.get("timestamp")) or None,
        message_id=_normalize_text(payload.get("message_id")) or None,
        source=_normalize_text(payload.get("source")) or "whatsapp",
        raw_payload=raw_payload_dict,
        transport=_normalize_text(payload.get("transport")) or "bridge_http_poll",
        is_group=_coerce_bool(payload.get("is_group")),
        original_text=original_text,
        received_at_monotonic=_coerce_float(payload.get("received_at_monotonic")),
        received_at_epoch=_coerce_float(payload.get("received_at_epoch")),
        metadata=payload,
    )
    return event


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _event_flag(event: IncomingWhatsAppEvent, key: str, default: bool = False) -> bool:
    if isinstance(event.metadata, dict):
        value = _coerce_bool(event.metadata.get(key))
        if value is not None:
            return value
    return default


async def notify_whatsapp_message(agent, speech_coordinator, event: IncomingWhatsAppEvent, speech_text: str) -> bool:
    mode = _normalize_text(event.metadata.get("mode") if isinstance(event.metadata, dict) else "") or "silent"
    should_speak = _event_flag(event, "speak", default=False)
    logger.info(
        "[WPP_RUNTIME] Notificacao recebida - contact=%s mode=%s speak=%s",
        _safe(event.contact),
        mode,
        should_speak,
    )
    if not should_speak:
        logger.info("[WPP_RUNTIME] Modo %s - NAO falar (speak=False)", mode)
        return False

    # Secretária: quem responde é o backend (services/whatsapp_secretary.py), por escrito e sem avisar por voz.
    if mode == "secretary":
        logger.info("SECRETARY_MAIN_VOICE_SUPPRESSED contact=%s reason=secretary_replies_in_backend", _safe(event.contact))
        return False

    # FASE 2D.3: cooldown de voz (por contato 90s + global 30s). A UI ja recebeu a mensagem;
    # aqui so limitamos a FALA para nao floodar. Texto/UI continuam completos.
    if mode in ("assistant", "secretary"):
        allowed, cd_reason = _assistant_voice_cooldown_check(event.contact)
        if not allowed:
            logger.info("WHATSAPP_ASSISTANT_NOTICE_COOLDOWN_ACTIVE contact=%s reason=%s", _safe(event.contact), cd_reason)
            logger.info("WHATSAPP_ASSISTANT_VOICE_SKIPPED reason=cooldown contact=%s", _safe(event.contact))
            logger.info("WHATSAPP_ASSISTANT_NOTICE_TEXT_ONLY contact=%s", _safe(event.contact))
            return False

    if mode == "assistant":
        logger.info(
            "[WPP_RUNTIME] Modo ASSISTENTE - falando notificacao de %s",
            _safe(event.contact),
        )
    elif mode == "secretary":
        logger.info(
            "[WPP_RUNTIME] Modo SECRETARIA - falando notificacao de %s",
            _safe(event.contact),
        )
    else:
        logger.info("[WPP_RUNTIME] Falando notificacao - contact=%s", _safe(event.contact))
    logger.info("WHATSAPP_ASSISTANT_VOICE_REQUESTED contact=%s mode=%s", _safe(event.contact), mode)
    delivered = await _speak_with_fallback(agent, speech_coordinator, speech_text)
    if delivered and mode in ("assistant", "secretary"):
        _assistant_voice_mark_spoken(event.contact)
        logger.info("WHATSAPP_ASSISTANT_NOTICE_SPOKEN contact=%s", _safe(event.contact))
        logger.info("WHATSAPP_ASSISTANT_VOICE_DONE contact=%s", _safe(event.contact))
    return delivered


def _event_requires_individual_delivery(entry: _BufferedIncomingWhatsAppEvent) -> bool:
    if _event_flag(entry.event, "send_to_llm", default=False) or _event_flag(
        entry.event, "auto_reply", default=False
    ):
        return True

    kind = _normalize_text(getattr(entry.remembered, "kind", ""))
    if kind and kind != "direct_message":
        return True

    text = entry.event.text
    if _POTENTIAL_DELEGATION_RE.match(text):
        return True
    if _ASSISTANT_ADDRESSED_RE.match(text):
        lowered = text.lower()
        return any(
            token in lowered
            for token in (
                " avisa ",
                " avisar ",
                " fala ",
                " falar ",
                " diz ",
                " dizer ",
                " manda ",
                " mandar ",
                " responde ",
                " responder ",
                " envia ",
                " enviar ",
            )
        )
    return False


def _extract_handler_outcome(result: object) -> _AgentHandlerOutcome:
    if result is False:
        return _AgentHandlerOutcome(handled=False)
    if isinstance(result, str):
        speech = _normalize_text(result)
        return _AgentHandlerOutcome(handled=True, speech=speech or None)
    if isinstance(result, dict):
        handled = result.get("handled")
        speech = _normalize_text(result.get("speech") or result.get("message"))
        if handled is False:
            return _AgentHandlerOutcome(handled=False, speech=speech or None)
        return _AgentHandlerOutcome(handled=True, speech=speech or None)
    speech = _normalize_text(getattr(result, "speech", "") or getattr(result, "message", ""))
    if speech:
        return _AgentHandlerOutcome(handled=True, speech=speech)
    return _AgentHandlerOutcome(handled=True)


async def _await_if_needed(value) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_agent_callable(func, *args):
    try:
        signature = inspect.signature(func)
    except Exception:
        return await _await_if_needed(func(*args))

    accepts_varargs = any(
        param.kind == inspect.Parameter.VAR_POSITIONAL
        for param in signature.parameters.values()
    )
    if accepts_varargs:
        return await _await_if_needed(func(*args))

    positional_capacity = sum(
        1
        for param in signature.parameters.values()
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )
    return await _await_if_needed(func(*args[:positional_capacity]))


async def _remember_event(agent, event: IncomingWhatsAppEvent) -> object:
    remember_event = getattr(agent, "remember_whatsapp_message_event", None)
    if callable(remember_event):
        try:
            remembered = await _call_agent_callable(remember_event, event)
            logger.debug(
                "[WHATSAPP] Evento estruturado registrado via remember_whatsapp_message_event id=%s contact=%s",
                event.message_id or "?",
                _safe(event.contact),
            )
            return remembered
        except Exception as exc:
            logger.warning(
                "[WHATSAPP] Falha em remember_whatsapp_message_event: %s",
                _safe(str(exc)),
            )

    remember_legacy = getattr(agent, "remember_whatsapp_message", None)
    if callable(remember_legacy):
        try:
            remembered = await _call_agent_callable(remember_legacy, event.contact, event.text)
            logger.debug(
                "[WHATSAPP] Evento registrado via remember_whatsapp_message legacy id=%s contact=%s",
                event.message_id or "?",
                _safe(event.contact),
            )
            return remembered
        except Exception as exc:
            logger.warning(
                "[WHATSAPP] Falha em remember_whatsapp_message legacy: %s",
                _safe(str(exc)),
            )
    return None


def _sync_agent_last_message(agent, event: IncomingWhatsAppEvent) -> None:
    try:
        agent._last_whatsapp_contact = event.contact
        agent._last_whatsapp_message = event.text
    except Exception:
        return


async def _register_delivered_if_possible(agent, remembered: object) -> None:
    if remembered is None:
        return
    register_delivered = getattr(agent, "register_delivered_whatsapp_item", None)
    if callable(register_delivered):
        try:
            await _call_agent_callable(register_delivered, remembered)
        except Exception as exc:
            logger.warning(
                "[WHATSAPP] Falha ao registrar entrega estruturada: %s",
                _safe(str(exc)),
            )


async def _speak_with_fallback(agent, speech_coordinator, message: str) -> bool:
    summary = _normalize_text(message)
    if not summary:
        return False

    _tts_circuit_breaker = getattr(speech_coordinator, "_tts_circuit_breaker", None)
    is_ack_active = getattr(speech_coordinator, "is_outbound_tool_ack_active", None)
    if callable(is_ack_active) and bool(is_ack_active()):
        logger.info("WHATSAPP_INBOUND_NOTIFICATION_DEFERRED reason=outbound_tool_ack_active")
        await asyncio.sleep(1.2)
        logger.info("WHATSAPP_INBOUND_NOTIFICATION_ENQUEUED_AFTER_TOOL_ACK")
    delivered = False
    try:
        active_reply_id = getattr(speech_coordinator, "_active_reply_id", None)
        logger.info(
            "[WHATSAPP_INTERRUPT] speaking_notification preserving_context active_reply_id=%s",
            active_reply_id or "none",
        )
        logger.info("WHATSAPP_NOTIFICATION_QUEUED")
        delivered = bool(await speech_coordinator.speak_proactive_message(
            summary,
            label="WHATSAPP_NOTIFICATION",
            add_to_chat_ctx=False,
        ))
    except Exception as exc:
        logger.warning("[WHATSAPP] speak_proactive falhou: %s", _safe(str(exc)))
        delivered = await _generate_reply_fallback(agent, summary)
    else:
        if callable(is_ack_active):
            logger.info("WHATSAPP_INBOUND_NOTIFICATION_RESUMED")
        if delivered:
            logger.info("WHATSAPP_NOTIFICATION_PLAYED")
        logger.info("[CONTEXT_RESTORE] whatsapp_notification_spoken active_reply_id=%s", active_reply_id or "none")
        if _tts_circuit_breaker is not None and _tts_circuit_breaker.should_skip():
            delivered = await _generate_reply_fallback(agent, summary) or delivered
    return delivered


async def _generate_reply_fallback(agent, summary: str) -> bool:
    try:
        session = getattr(agent, "_session", None) or getattr(agent, "session", None)
        if session is None:
            return False
        await _await_if_needed(
            session.generate_reply(
                instructions=f"[NOTIFICACAO WHATSAPP - fale em voz alta agora] {summary}"
            )
        )
        return True
    except Exception as exc:
        logger.warning(
            "[WHATSAPP] Fallback generate_reply falhou: %s",
            _safe(str(exc)),
        )
        return False


async def _invoke_single_event_handler(
    agent,
    speech_coordinator,
    entry: _BufferedIncomingWhatsAppEvent,
) -> bool:
    handler = getattr(agent, "handle_incoming_whatsapp_event", None)
    if not callable(handler):
        return False

    try:
        raw_result = await _call_agent_callable(handler, entry.event, speech_coordinator)
    except Exception as exc:
        logger.warning(
            "[WHATSAPP] Falha em handle_incoming_whatsapp_event id=%s: %s",
            entry.event.message_id or "?",
            _safe(str(exc)),
        )
        return False

    outcome = _extract_handler_outcome(raw_result)
    if not outcome.handled:
        return False
    if outcome.speech:
        delivered = await _speak_with_fallback(agent, speech_coordinator, outcome.speech)
        if delivered:
            await _register_delivered_if_possible(agent, entry.remembered)
    logger.debug(
        "[WHATSAPP] Evento unitario tratado pelo agent handler id=%s contact=%s",
        entry.event.message_id or "?",
        _safe(entry.event.contact),
    )
    return True


async def _invoke_batch_handler(
    agent,
    speech_coordinator,
    entries: list[_BufferedIncomingWhatsAppEvent],
) -> bool:
    handler = getattr(agent, "handle_incoming_whatsapp_batch", None)
    if not callable(handler):
        handler = getattr(agent, "handle_incoming_whatsapp_events", None)
    if not callable(handler):
        return False

    events = [entry.event for entry in entries]
    try:
        raw_result = await _call_agent_callable(handler, events, speech_coordinator)
    except Exception as exc:
        logger.warning(
            "[WHATSAPP] Falha no batch handler de WhatsApp contact=%s size=%d: %s",
            _safe(events[0].contact if events else "?"),
            len(events),
            _safe(str(exc)),
        )
        return False

    outcome = _extract_handler_outcome(raw_result)
    if not outcome.handled:
        return False
    if outcome.speech:
        await _speak_with_fallback(agent, speech_coordinator, outcome.speech)
    logger.debug(
        "[WHATSAPP] Lote tratado pelo agent batch handler contact=%s size=%d",
        _safe(events[0].contact if events else "?"),
        len(events),
    )
    return True


def _build_single_fallback_speech(agent, event: IncomingWhatsAppEvent, remembered: object) -> str:
    build_delivery_speech = getattr(agent, "build_whatsapp_delivery_speech", None)
    if callable(build_delivery_speech) and remembered is not None:
        try:
            spoken = _normalize_text(build_delivery_speech(remembered))
            if spoken:
                return spoken
        except Exception as exc:
            logger.warning(
                "[WHATSAPP] Falha ao montar fala estruturada: %s",
                _safe(str(exc)),
            )
    return build_whatsapp_direct_notification_speech(
        event.contact,
        _normalize_text(event.text),
    )


def _build_batch_fallback_speech(entries: list[_BufferedIncomingWhatsAppEvent]) -> str:
    events = [entry.event for entry in entries]
    if not events:
        return ""
    latest = events[-1]
    return build_whatsapp_batch_notification_speech(
        latest.contact,
        len(events),
        _truncate_preview(latest.text),
    )


async def _deliver_single_event(
    agent,
    speech_coordinator,
    entry: _BufferedIncomingWhatsAppEvent,
) -> None:
    handled = await _invoke_single_event_handler(agent, speech_coordinator, entry)
    if handled:
        return

    speech = _build_single_fallback_speech(agent, entry.event, entry.remembered)
    delivered = await notify_whatsapp_message(agent, speech_coordinator, entry.event, speech)
    if delivered:
        await _register_delivered_if_possible(agent, entry.remembered)
    logger.info(
        "[WHATSAPP] Evento entregue id=%s contact=%s mode=%s preview=%s",
        entry.event.message_id or "?",
        _safe(entry.event.contact),
        "individual",
        _safe(_truncate_preview(entry.event.text)),
    )


async def _deliver_buffered_entries(
    agent,
    speech_coordinator,
    contact: str,
    entries: list[_BufferedIncomingWhatsAppEvent],
) -> None:
    if not entries:
        return

    if len(entries) == 1:
        await _deliver_single_event(agent, speech_coordinator, entries[0])
        return

    if any(_event_requires_individual_delivery(entry) for entry in entries):
        logger.info(
            "[WHATSAPP] Lote preservado individualmente contact=%s size=%d",
            _safe(contact),
            len(entries),
        )
        for entry in entries:
            await _deliver_single_event(agent, speech_coordinator, entry)
        return

    if await _invoke_batch_handler(agent, speech_coordinator, entries):
        return

    speech = _build_batch_fallback_speech(entries)
    await notify_whatsapp_message(agent, speech_coordinator, entries[-1].event, speech)
    logger.info(
        "[WHATSAPP] Lote comum entregue contact=%s size=%d latest=%s",
        _safe(contact),
        len(entries),
        _safe(_truncate_preview(entries[-1].event.text)),
    )


async def _flush_contact_events(
    agent,
    speech_coordinator,
    contact: str,
) -> None:
    entries = _pending_events.pop(contact, [])
    _pending_tasks.pop(contact, None)
    if not entries:
        return
    await _deliver_buffered_entries(agent, speech_coordinator, contact, entries)


async def _after_delay(
    agent,
    speech_coordinator,
    contact: str,
    delay: float,
) -> None:
    try:
        await _asyncio.sleep(delay)
        await _flush_contact_events(agent, speech_coordinator, contact)
    except _asyncio.CancelledError:
        return


def build_whatsapp_notifier(agent, speech_coordinator):
    """
    Retorna callable assincrono chamado quando chega mensagem do WhatsApp.
    A camada de runtime preserva o evento bruto e delega a decisao final ao agent.
    """

    async def _notifier(
        contact: str,
        raw_text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        scope_factory = getattr(speech_coordinator, "whatsapp_action_scope", None)
        if callable(scope_factory):
            async with scope_factory():
                await _handle_notifier_event(agent, speech_coordinator, contact, raw_text, metadata)
            return
        await _handle_notifier_event(agent, speech_coordinator, contact, raw_text, metadata)

    return _notifier


async def _handle_notifier_event(
    agent,
    speech_coordinator,
    contact: str,
    raw_text: str,
    metadata: dict[str, object] | None = None,
) -> None:
    event = _build_event(contact, raw_text, metadata)
    if not event.text:
        return

    is_group, reason = _group_guard.is_group(
        {
            "contact": event.contact,
            "text": event.text,
            "message": event.original_text or event.text,
            "is_group": event.is_group,
        }
    )
    if is_group:
        logger.warning(
            "[WPP_RUNTIME] GRUPO bloqueado no runtime - contact=%s reason=%s",
            _safe(event.contact),
            reason,
        )
        return

    should_speak = _event_flag(event, "speak", default=False)
    should_send_llm = _event_flag(event, "send_to_llm", default=False)
    if not should_speak and not should_send_llm:
        logger.info(
            "[WPP_RUNTIME] Modo silencioso completo - contact=%s speak=%s send_to_llm=%s",
            _safe(event.contact),
            should_speak,
            should_send_llm,
        )
        return

    logger.info(
        "[WHATSAPP] Evento recebido id=%s contact=%s timestamp=%s transport=%s preview=%s mode=%s speak=%s send_to_llm=%s",
        event.message_id or "?",
        _safe(event.contact),
        event.timestamp or "?",
        event.transport,
        _safe(_truncate_preview(event.text)),
        _normalize_text(event.metadata.get("mode") if isinstance(event.metadata, dict) else "") or "silent",
        should_speak,
        should_send_llm,
    )

    remembered = await _remember_event(agent, event)
    _sync_agent_last_message(agent, event)
    entry = _BufferedIncomingWhatsAppEvent(
        event=event,
        remembered=remembered,
    )

    if _event_requires_individual_delivery(entry):
        logger.info(
            "[WHATSAPP] Evento exigiu entrega individual id=%s contact=%s",
            event.message_id or "?",
            _safe(event.contact),
        )
        await _flush_contact_events(agent, speech_coordinator, event.contact)
        await _deliver_single_event(agent, speech_coordinator, entry)
        return

    _pending_events[event.contact].append(entry)

    old_task = _pending_tasks.get(event.contact)
    if old_task and not old_task.done():
        old_task.cancel()

    loop = _asyncio.get_event_loop()
    _pending_tasks[event.contact] = loop.create_task(
        _after_delay(
            agent,
            speech_coordinator,
            event.contact,
            _BATCH_WINDOW_SECONDS,
        )
    )
    logger.debug(
        "[WHATSAPP] Evento bufferizado contact=%s buffered=%d id=%s",
        _safe(event.contact),
        len(_pending_events[event.contact]),
        event.message_id or "?",
    )


async def send_whatsapp_message(
    contact: str,
    message: str,
    *,
    source: str = "voice_command",
    resolved_number: str = "",
    request_id: str = "",
    retries: int = 3,
) -> dict:
    """
    Envia mensagem pelo WhatsApp Controller via HTTP.
    Tenta ate `retries` vezes com intervalo de 2 segundos.
    """
    wa_logger = get_whatsapp_logger()
    dry_run = str(os.getenv("KIRA_WHATSAPP_DRY_RUN", "false")).strip().lower() in {"1", "true", "yes", "on"}
    logger.info("[BLOCO6] WHATSAPP_SEND_PREFLIGHT_START contact=%s", _safe(contact))

    bridge_status = await get_whatsapp_status()
    bridge_ok = bool(bridge_status.get("ok", True))
    bridge_connected = bool(bridge_status.get("connected"))
    bridge_send_ready = bool(bridge_status.get("send_ready"))
    bridge_search_ready = bool(bridge_status.get("search_ready"))
    logger.info(
        "WHATSAPP_BRIDGE_STATUS ok=%s connected=%s send_ready=%s search_ready=%s",
        str(bridge_ok).lower(),
        str(bridge_connected).lower(),
        str(bridge_send_ready).lower(),
        str(bridge_search_ready).lower(),
    )
    logger.info(
        "WHATSAPP_SEND_PREFLIGHT_STATUS connected=%s search_ready=%s send_ready=%s",
        str(bridge_connected).lower(),
        str(bridge_search_ready).lower(),
        str(bridge_send_ready).lower(),
    )
    wa_logger.log_bridge_status(bridge_connected, bridge_search_ready, bridge_send_ready)

    if not (bridge_ok and bridge_connected and bridge_send_ready):
        refreshed_status: dict[str, object] = {}
        for attempt in range(1, 4):
            logger.info("WHATSAPP_SEND_PREFLIGHT_REFRESH_REQUEST reason=send_not_ready")
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    refresh_resp = await client.get(f"{WHATSAPP_BRIDGE_URL}/status")
                    refresh_payload = refresh_resp.json()
                    refreshed_status = refresh_payload if isinstance(refresh_payload, dict) else {}
            except Exception:
                refreshed_status = {}

            bridge_connected = bool(refreshed_status.get("connected", bridge_connected))
            bridge_search_ready = bool(refreshed_status.get("search_ready", False))
            bridge_send_ready = bool(refreshed_status.get("send_ready", False))
            refresh_reason = str(refreshed_status.get("reason") or "")
            logger.info(
                "WHATSAPP_SEND_PREFLIGHT_REFRESH_RESULT connected=%s search_ready=%s send_ready=%s reason=%s",
                str(bridge_connected).lower(),
                str(bridge_search_ready).lower(),
                str(bridge_send_ready).lower(),
                refresh_reason or "none",
            )
            wa_logger.log_bridge_status(bridge_connected, bridge_search_ready, bridge_send_ready)
            if bridge_connected and bridge_send_ready:
                logger.info("WHATSAPP_SEND_PREFLIGHT_READY_AFTER_REFRESH")
                break
            if attempt < 3:
                logger.info("WHATSAPP_SEND_PREFLIGHT_RETRY attempt=%s", attempt + 1)
                await asyncio.sleep(0.8)

        if not (bridge_ok and bridge_connected and bridge_send_ready):
            reason = str(refreshed_status.get("reason") or "send_not_ready")
            if reason == "search_not_ready":
                error_code = "search_box_missing"
                user_message = "Não consegui acessar a busca do WhatsApp agora. Abre o painel do WhatsApp e confirma se a tela está carregada."
            elif reason == "not_logged_in":
                error_code = "not_logged_in"
                user_message = "O WhatsApp Web não parece logado. Confere o QR Code ou login."
            elif reason == "controller_missing":
                error_code = "controller_missing"
                user_message = "O controle do WhatsApp não está ativo agora."
            elif reason == "page_not_ready":
                error_code = "page_not_ready"
                user_message = "O WhatsApp abriu, mas a página ainda não carregou completamente."
            else:
                error_code = "send_not_ready"
                user_message = "O WhatsApp está conectado, mas a tela de envio ainda não está pronta."
            logger.warning("WHATSAPP_SEND_PREFLIGHT_FAILED_FINAL reason=%s", reason)
            wa_logger.log_error("preflight", f"{error_code}:{reason}")
            return {
                "ok": False,
                "success": False,
                "stage": "preflight",
                "error_code": error_code,
                "error": "WhatsApp não está pronto para envio.",
                "user_message": user_message,
                "contact": contact,
                "number": resolved_number,
                "source": source,
            }

    if dry_run:
        logger.info("WHATSAPP_BRIDGE_SEND_REQUEST dry_run=true contact=%s", _safe(contact))
        wa_logger.log_send_attempt(contact, message, True)
        result = {
            "ok": True,
            "success": True,
            "contact": contact,
            "number": resolved_number,
            "message": message,
            "bridge_status": "dry_run",
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "delivery_id": request_id or f"dry-{int(time.time() * 1000)}",
            "error": None,
            "dry_run": True,
            "source": source,
        }
        logger.info("WHATSAPP_BRIDGE_SEND_RESPONSE ok=true dry_run=true")
        wa_logger.log_send_result(True, contact)
        return result

    last_error = "Nenhuma tentativa realizada"
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                logger.info("WHATSAPP_BRIDGE_SEND_REQUEST contact=%s attempt=%s", _safe(contact), attempt)
                if attempt == 1:
                    wa_logger.log_send_attempt(contact, message, False)
                resp = await client.post(
                    f"{WHATSAPP_BRIDGE_URL}/send",
                    headers=_bridge_headers(),
                    json={
                        "contact": contact,
                        "message": message,
                        "source": source,
                        "number": resolved_number,
                        "request_id": request_id,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    payload.setdefault("ok", bool(payload.get("success")))
                    payload.setdefault("contact", contact)
                    payload.setdefault("number", resolved_number)
                    payload.setdefault("message", message)
                    payload.setdefault("bridge_status", "ok")
                    payload.setdefault("source", source)
                    payload.setdefault("request_id", request_id)
                    payload.setdefault("sent_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
                    logger.info(
                        "WHATSAPP_BRIDGE_SEND_RESPONSE ok=%s",
                        str(bool(payload.get("ok"))).lower(),
                    )
                    wa_logger.log_send_result(bool(payload.get("ok")), contact, str(payload.get("error") or ""))
                    return payload
                wa_logger.log_send_result(True, contact)
                return {
                    "ok": True,
                    "success": True,
                    "contact": contact,
                    "number": resolved_number,
                    "message": message,
                    "bridge_status": "ok",
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "delivery_id": request_id,
                    "error": None,
                    "source": source,
                }
        except httpx.HTTPStatusError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            try:
                error_body = str(exc.response.text or "").strip()
            except Exception:
                error_body = ""
            last_error = (
                f"HTTP {status_code}: {error_body}"
                if status_code is not None and error_body
                else str(exc)
            )
            logger.warning(
                "[WHATSAPP] Tentativa %d/%d falhou para %s: %s",
                attempt,
                retries,
                _safe(contact),
                _safe(last_error),
            )
            logger.error(
                "[WHATSAPP_BRIDGE_SEND_HTTP_ERROR] attempt=%d/%d status=%s body=%s",
                attempt,
                retries,
                status_code,
                _safe(error_body[:500]),
            )
            wa_logger.log_error("bridge_send_http", f"status={status_code} body={error_body[:200]}")
            if 400 <= int(status_code or 0) < 500 and int(status_code or 0) != 403:
                # A ponte respondeu e disse o motivo (contato ambíguo, não encontrado): repetir não muda nada, e
                # "a ponte não respondeu" escondia o que o Chefe precisa resolver ("Breno final 1234 ou 5678?").
                try:
                    reason = str(json.loads(error_body).get("message") or "").strip()
                except (ValueError, AttributeError):
                    reason = ""
                wa_logger.log_send_result(False, contact, last_error)
                return {
                    "ok": False, "success": False, "stage": "bridge_send", "error": "whatsapp_send_rejected",
                    "message": last_error, "user_message": f"Não mandei: {reason}." if reason else "", "contact": contact,
                    "number": resolved_number, "source": source,
                }
            if int(status_code or 0) == 403:
                logger.error(
                    "[WHATSAPP_BRIDGE_403_FORBIDDEN] contact=%s body=%s",
                    _safe(contact),
                    _safe(error_body[:500]),
                )
            if attempt < retries:
                await asyncio.sleep(2.0)
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "[WHATSAPP] Tentativa %d/%d falhou para %s: %s",
                attempt,
                retries,
                _safe(contact),
                _safe(str(exc)),
            )
            wa_logger.log_error("bridge_send_exception", str(exc))
            if attempt < retries:
                await asyncio.sleep(2.0)

    logger.error("[WHATSAPP] Falha apos %d tentativas: %s", retries, _safe(last_error))
    wa_logger.log_send_result(False, contact, last_error)
    return {
        "ok": False,
        "success": False,
        "stage": "bridge_send",
        "error": "whatsapp_bridge_unreachable",
        "message": last_error,
        "user_message": "a ponte do WhatsApp nao respondeu",
        "contact": contact,
        "number": resolved_number,
        "source": source,
    }

async def get_whatsapp_status() -> dict:
    """Retorna status da conexao com o WhatsApp."""

    wa_logger = get_whatsapp_logger()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{WHATSAPP_BRIDGE_URL}/status")
            payload = response.json()
            status = payload if isinstance(payload, dict) else {"connected": False}
            # O bridge "WhatsApp Agent" expõe apenas `connected` no /status; a
            # prontidao de envio (search box / compose) é tratada DENTRO do /send
            # (_wait_search_ready, espera ate 30s). Quando o bridge nao informa
            # send_ready/search_ready, derivamos de `connected` — senao o preflight
            # do Kira (que exige send_ready) falha sempre com send_not_ready e nunca
            # chega a postar /send. setdefault preserva valores caso um bridge futuro
            # passe a reportar esses flags.
            _connected = bool(status.get("connected", False))
            status.setdefault("send_ready", _connected)
            status.setdefault("search_ready", _connected)
            wa_logger.log_bridge_status(
                bool(status.get("connected", False)),
                bool(status.get("search_ready", False)),
                bool(status.get("send_ready", False)),
            )
            return status
    except Exception as exc:
        wa_logger.log_error("get_whatsapp_status", str(exc))
        return {"connected": False}


def get_whatsapp_bridge_status() -> dict[str, object]:
    """Consulta sincrona para uso em caminhos nao-async do assistant."""
    try:
        return asyncio.run(get_whatsapp_status())
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return {"connected": False, "search_ready": False, "send_ready": False, "reason": "event_loop_running"}
        except Exception:
            pass
        return {"connected": False, "search_ready": False, "send_ready": False}
    except Exception:
        return {"connected": False, "search_ready": False, "send_ready": False}
