"""
Boot guard for Kira.

Keeps optional modules degraded instead of fatal, and exposes a small
readiness contract that future changes can validate without touching the
LiveKit/Gemini/voice boot path.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("kira.boot_guard")


BOOT_CRITICAL_MANIFEST: dict[str, Any] = {
    "files": [
        "Kira.bat",
        "kira_app.py",
        "boot_manager.py",
        "agent/agent.py",
        "agent/runtime/speech_pipeline.py",
        "agent/runtime/event_handlers.py",
        "agent/runtime/turn_coordinator.py",
        "agent/core/realtime.py",
        "interface/index.html",
    ],
    "functions": [
        "kira_app.run_server",
        "kira_app.start_agent",
        "kira_app.ensure_livekit_server_ready",
        "kira_app._start_session_from_payload",
        "agent.runtime.session_start.start_agent_session",
    ],
    "endpoints": [
        "/health",
        "/api/boot/state",
        "/api/ready",
        "/api/session/preflight",
        "/api/session/start",
        "/api/livekit/token",
        "/api/token",
        "/ws/transcription",
    ],
    "logs": [
        "SESSION_ROOM_CONNECTED",
        "GEMINI_READY",
        "VOICE_PIPELINE_READY",
        "SESSION_RUNTIME_READY",
        "FUNCTIONAL_READY",
    ],
    "pre_session_flags": [
        "backend_ready",
        "livekit_ready",
        "agent_ready",
        "can_start_session",
    ],
    "session_flags": [
        "token_issued",
        "room_prepared",
        "room_connected",
        "gemini_ready",
        "voice_pipeline_ready",
        "runtime_ready",
        "functional_ready",
    ],
}

BOOT_OPTIONAL_SERVICES = {
    "messages",
    "vision",
    "system_vitals",
    "system_status",
    "music",
    "internet",
    "avatar",
    "omniparser",
    "proactive",
    "reminders",
    "activity_monitor",
}


@dataclass
class DegradedService:
    service: str
    reason: str
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "reason": self.reason,
            "error": self.error,
            "timestamp": self.timestamp,
        }


_degraded_services: dict[str, DegradedService] = {}
_guard_started = False


def start_boot_guard() -> None:
    global _guard_started
    if _guard_started:
        return
    _guard_started = True
    logger.info("BOOT_GUARD_START")


def record_degraded_service(service: str, reason: str, error: object = "") -> None:
    normalized_service = str(service or "unknown").strip().lower() or "unknown"
    normalized_reason = str(reason or "unknown").strip() or "unknown"
    error_text = str(error or "").strip()
    _degraded_services[normalized_service] = DegradedService(
        service=normalized_service,
        reason=normalized_reason,
        error=error_text,
    )
    logger.warning(
        "BOOT_GUARD_OPTIONAL_DEGRADED service=%s reason=%s error=%s",
        normalized_service,
        normalized_reason,
        error_text,
    )


def get_degraded_services() -> dict[str, dict[str, Any]]:
    return {name: item.as_dict() for name, item in _degraded_services.items()}


def safe_import_optional(module_name: str, *, service: str, attr: str | None = None) -> Any:
    try:
        module = importlib.import_module(module_name)
        if attr:
            return getattr(module, attr)
        return module
    except Exception as exc:
        record_degraded_service(service, f"optional_import_failed:{module_name}", exc)
        return None


def run_optional_task(
    service: str,
    fn: Callable[..., Any],
    *args: Any,
    fallback: Any = None,
    reason: str = "optional_task_failed",
    **kwargs: Any,
) -> Any:
    logger.info("BOOT_GUARD_OPTIONAL_START service=%s", service)
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        record_degraded_service(service, reason, exc)
        return fallback


def check_ready_contract(state: dict[str, Any] | None) -> tuple[bool, list[str]]:
    logger.info("BOOT_GUARD_CRITICAL_CHECK_START")
    snapshot = dict(state or {})
    missing = [
        flag
        for flag in BOOT_CRITICAL_MANIFEST["pre_session_flags"]
        if not bool(snapshot.get(flag))
    ]
    if missing:
        logger.warning("BOOT_GUARD_READY_CONTRACT_FAILED missing=%s", ",".join(missing))
        return False, missing
    logger.info("BOOT_GUARD_CRITICAL_CHECK_OK")
    logger.info("BOOT_GUARD_READY_CONTRACT_OK")
    return True, []


def check_session_contract(state: dict[str, Any] | None) -> tuple[bool, list[str]]:
    snapshot = dict(state or {})
    missing = [
        flag
        for flag in BOOT_CRITICAL_MANIFEST["session_flags"]
        if not bool(snapshot.get(flag))
    ]
    if missing:
        logger.warning("BOOT_GUARD_SESSION_CONTRACT_FAILED missing=%s", ",".join(missing))
        return False, missing
    logger.info("BOOT_GUARD_SESSION_CONTRACT_OK")
    logger.info("BOOT_GUARD_FUNCTIONAL_READY_CONFIRMED")
    return True, []


def optional_endpoint_error(service: str, exc: object) -> dict[str, Any]:
    record_degraded_service(service, "optional_endpoint_failed", exc)
    return {
        "success": False,
        "ok": False,
        "degraded": True,
        "service": service,
        "error": str(exc or "optional_endpoint_failed"),
        "timestamp": time.time(),
    }

