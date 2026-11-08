from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import time
import unicodedata
import re
from typing import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


BLOCKED_RESULT_FLAGS = {
    "block_gemini": True,
    "block_manus": True,
    "block_browser": True,
    "block_pc_operator": True,
    "block_proactive": True,
    "stop_routing": True,
}


@dataclass(frozen=True)
class CommandGate:
    name: str
    handler: Callable[[str, dict], Awaitable[object] | object]


_RECENT_HANDLED: dict[str, float] = {}
_DEDUPE_WINDOW_SECONDS = 1.0


def normalize_router_text(text: str | None) -> str:
    value = str(text or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _hash(source: str, normalized_text: str) -> str:
    digest = hashlib.sha1(f"{source}\0{normalized_text}".encode("utf-8", errors="ignore")).hexdigest()
    return digest[:16]


def _cleanup(now: float) -> None:
    expired = [key for key, ts in _RECENT_HANDLED.items() if now - ts > _DEDUPE_WINDOW_SECONDS]
    for key in expired:
        _RECENT_HANDLED.pop(key, None)


async def _await_if_needed(value: object) -> object:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _coerce_gate_result(gate: str, value: object) -> dict[str, object]:
    if isinstance(value, dict):
        result = dict(value)
    else:
        result = {"handled": bool(value)}
    handled = bool(result.get("handled") or result.get("stop_routing"))
    if not handled:
        return {"handled": False, "stop_routing": False, "gate": gate}
    result.setdefault("kind", gate)
    result.setdefault("source", f"{gate}_gate")
    result.setdefault("voice_allowed", False)
    result.update(BLOCKED_RESULT_FLAGS)
    result["handled"] = True
    result["gate"] = gate
    return result


def _safe_preview(text: str, limit: int = 160) -> str:
    try:
        from services.memory_guard import safe_log_text_preview
        return safe_log_text_preview(text, max_chars=limit)
    except Exception:
        return str(text or "").strip()[:limit]


def _command_event_name(gate_name: str) -> str:
    normalized = str(gate_name or "unknown").strip().lower()
    mapping = {
        "safety": "SAFETY_GATE",
        "ui_panel": "UI_PANEL_GATE",
        "local_command": "LOCAL_COMMAND_GATE",
        "volume": "VOLUME_GATE",
        "creation_task": "CREATION_TASK_GATE",
        "artifact_command": "ARTIFACT_COMMAND_GATE",
        "whatsapp": "WHATSAPP_INTENT",
        "music": "MUSIC_GATE",
        "browser_task": "BROWSER_TASK_GATE",
        "dedupe": "COMMAND_DEDUPE_GATE",
    }
    return mapping.get(normalized, f"{normalized.upper()}_GATE")


def _log_command_route_event(event: str, data: dict[str, object]) -> None:
    try:
        from services.kira_log_manager import log_command_event
        log_command_event(event, data)
    except Exception as exc:
        logger.info("COMMAND_JSONL_WRITE_FAILED event=%s reason=%s", event, str(exc)[:160])


async def route_user_command(
    text: str,
    *,
    source: str = "unknown",
    gates: Iterable[CommandGate],
    context: dict | None = None,
    dedupe: bool = True,
) -> dict[str, object]:
    normalized = normalize_router_text(text)
    route_context = dict(context or {})
    route_context.setdefault("source", source)
    route_context.setdefault("normalized_text", normalized)
    now = time.monotonic()
    route_hash = _hash(source, normalized)
    _cleanup(now)

    logger.info("COMMAND_ROUTER_BEGIN source=%s", source)
    _log_command_route_event(
        "COMMAND_ROUTE_BEGIN",
        {
            "source": source,
            "raw_text_preview": _safe_preview(text),
            "normalized_text_preview": _safe_preview(normalized),
            "route_final": "",
            "handled": False,
            "blocked_next_routes": [],
            "reason": "router_begin",
        },
    )
    if dedupe and route_hash in _RECENT_HANDLED:
        logger.info("COMMAND_ROUTER_DEDUPE_HIT source=%s hash=%s", source, route_hash)
        _log_command_route_event(
            "COMMAND_DEDUPE_GATE",
            {
                "source": source,
                "raw_text_preview": _safe_preview(text),
                "normalized_text_preview": _safe_preview(normalized),
                "gate": "dedupe",
                "matched": True,
                "handled": True,
                "route_final": "dedupe",
                "blocked_next_routes": ["gemini_normal_reply"],
                "reason": "recently_handled",
            },
        )
        return {
            "handled": True,
            "kind": "dedupe",
            "source": "command_router",
            "gate": "dedupe",
            "hash": route_hash,
            **BLOCKED_RESULT_FLAGS,
        }

    for gate in gates:
        logger.info("COMMAND_ROUTER_GATE_TRY gate=%s", gate.name)
        gate_event = _command_event_name(gate.name)
        try:
            gate_value = await _await_if_needed(gate.handler(text, route_context))
        except Exception as exc:
            logger.warning("COMMAND_ROUTER_GATE_ERROR gate=%s error=%s", gate.name, exc)
            _log_command_route_event(
                gate_event,
                {
                    "source": source,
                    "raw_text_preview": _safe_preview(text),
                    "normalized_text_preview": _safe_preview(normalized),
                    "gate": gate.name,
                    "matched": False,
                    "handled": False,
                    "route_final": "",
                    "blocked_next_routes": [],
                    "reason": f"gate_error:{str(exc)[:80]}",
                },
            )
            continue
        result = _coerce_gate_result(gate.name, gate_value)
        if not bool(result.get("handled") or result.get("stop_routing")):
            _log_command_route_event(
                gate_event,
                {
                    "source": source,
                    "raw_text_preview": _safe_preview(text),
                    "normalized_text_preview": _safe_preview(normalized),
                    "gate": gate.name,
                    "matched": False,
                    "handled": False,
                    "route_final": "",
                    "blocked_next_routes": [],
                    "reason": str(result.get("reason") or "not_matched"),
                },
            )
            continue
        logger.info("COMMAND_ROUTER_GATE_MATCHED gate=%s", gate.name)
        if gate.name == "safety":
            logger.info("SAFETY_GATE_STOP_ROUTING")
        elif gate.name == "ui_panel":
            logger.info("UI_PANEL_GATE_STOP_ROUTING")
        elif gate.name == "local_command":
            logger.info("LOCAL_COMMAND_STOP_ROUTING")
        elif gate.name == "volume":
            logger.info("VOLUME_GATE_STOP_ROUTING")
        elif gate.name == "creation_task":
            logger.info("CREATION_TASK_STOP_ROUTING")
        elif gate.name == "artifact_command":
            logger.info("ARTIFACT_COMMAND_STOP_ROUTING")
        elif gate.name == "whatsapp":
            logger.info("WHATSAPP_GATE_STOP_ROUTING")
        elif gate.name == "music":
            logger.info("MUSIC_GATE_STOP_ROUTING")
        elif gate.name == "browser_task":
            logger.info("BROWSER_TASK_STOP_ROUTING")
        logger.info("COMMAND_ROUTER_STOP_ROUTING gate=%s", gate.name)
        logger.info("COMMAND_ROUTER_RESULT handled=true gate=%s", gate.name)
        _log_command_route_event(
            gate_event,
            {
                "source": source,
                "raw_text_preview": _safe_preview(text),
                "normalized_text_preview": _safe_preview(normalized),
                "gate": gate.name,
                "matched": True,
                "handled": True,
                "route_final": gate.name,
                "blocked_next_routes": ["gemini_normal_reply"],
                "reason": str(result.get("reason") or result.get("kind") or "handled"),
            },
        )
        _log_command_route_event(
            "COMMAND_ROUTE_FINAL",
            {
                "source": source,
                "raw_text_preview": _safe_preview(text),
                "normalized_text_preview": _safe_preview(normalized),
                "gate": gate.name,
                "matched": True,
                "handled": True,
                "route_final": gate.name,
                "blocked_next_routes": ["gemini_normal_reply"],
                "reason": "gate_handled",
            },
        )
        if dedupe:
            _RECENT_HANDLED[route_hash] = now
        return result

    logger.info("COMMAND_ROUTER_NO_MATCH route=gemini")
    logger.info("COMMAND_ROUTER_RESULT handled=false gate=none")
    _log_command_route_event(
        "GEMINI_NORMAL_REPLY",
        {
            "source": source,
            "raw_text_preview": _safe_preview(text),
            "normalized_text_preview": _safe_preview(normalized),
            "gate": "gemini_normal_reply",
            "matched": True,
            "handled": False,
            "route_final": "gemini_normal_reply",
            "blocked_next_routes": [],
            "reason": "no_gate_matched",
        },
    )
    _log_command_route_event(
        "COMMAND_ROUTE_FINAL",
        {
            "source": source,
            "raw_text_preview": _safe_preview(text),
            "normalized_text_preview": _safe_preview(normalized),
            "gate": "none",
            "matched": False,
            "handled": False,
            "route_final": "gemini_normal_reply",
            "blocked_next_routes": [],
            "reason": "no_gate_matched",
        },
    )
    return {"handled": False, "stop_routing": False, "route": "gemini", "block_gemini": False}


def reset_command_router_dedupe() -> None:
    _RECENT_HANDLED.clear()


__all__ = ["CommandGate", "route_user_command", "normalize_router_text", "reset_command_router_dedupe"]
