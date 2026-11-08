from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from services.mem0_cloud_provider import Mem0CloudProvider, should_block_cloud_memory


logger = logging.getLogger("kira.app")

_LOCK = threading.RLock()
_BUFFER: list[dict[str, str]] = []
_LAST_FLUSH_AT = time.time()
_SAVING = False


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _turn_threshold() -> int:
    try:
        return max(1, int(os.getenv("KIRA_MEMORY_AUTO_SAVE_TURNS", "10") or "10"))
    except Exception:
        return 10


def _interval_seconds() -> float:
    try:
        minutes = max(1.0, float(os.getenv("KIRA_MEMORY_AUTO_SAVE_INTERVAL_MINUTES", "10") or "10"))
    except Exception:
        minutes = 10.0
    return minutes * 60.0


def _looks_operational_noise(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if len(normalized) < 8:
        return True
    operational_terms = (
        "abre ",
        "abrir ",
        "fecha ",
        "fechar ",
        "clica ",
        "clique ",
        "volume",
        "messages",
        "status",
        "painel",
        "internet",
    )
    return any(term in normalized for term in operational_terms)


def record_memory_turn(
    role: str,
    content: str,
    *,
    source: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    if not _env_bool("KIRA_MEMORY_AUTO_SAVE_ENABLED", True):
        logger.info("MEM0_AUTO_SAVE_SKIPPED reason=disabled")
        return {"ok": True, "added": False, "reason": "disabled"}
    clean = str(content or "").strip()
    if not clean:
        logger.info("MEM0_AUTO_SAVE_SKIPPED reason=empty")
        return {"ok": True, "added": False, "reason": "empty"}
    if should_block_cloud_memory(clean):
        logger.info("MEM0_SAVE_BLOCKED_SENSITIVE source=auto_session_memory")
        logger.info("MEM0_AUTO_SAVE_SKIPPED reason=blocked_sensitive")
        return {"ok": True, "added": False, "reason": "blocked_sensitive"}
    if _looks_operational_noise(clean):
        logger.info("MEM0_AUTO_SAVE_SKIPPED reason=operational_noise")
        return {"ok": True, "added": False, "reason": "operational_noise"}
    message = {"role": role if role in {"user", "assistant"} else "user", "content": clean}
    trigger = ""
    with _LOCK:
        _BUFFER.append(message)
        turns_count = len(_BUFFER)
        elapsed = time.time() - _LAST_FLUSH_AT
        logger.info("MEM0_AUTO_SAVE_BUFFER_ADDED turns_count=%s", turns_count)
        if turns_count >= _turn_threshold():
            trigger = "turns"
        elif elapsed >= _interval_seconds():
            trigger = "time"
    if trigger:
        flush_async(trigger=trigger, session_id=session_id)
    return {"ok": True, "added": True, "turns_count": turns_count, "trigger": trigger}


def flush_async(*, trigger: str, session_id: str | None = None) -> None:
    thread = threading.Thread(
        target=lambda: flush_pending(trigger=trigger, session_id=session_id, timeout_seconds=8),
        name="mem0-auto-save",
        daemon=True,
    )
    thread.start()


def flush_pending(
    *,
    trigger: str = "manual",
    session_id: str | None = None,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    global _LAST_FLUSH_AT, _SAVING
    if not _env_bool("KIRA_MEMORY_SAVE_ON_SHUTDOWN", True) and trigger == "shutdown":
        logger.info("MEM0_SHUTDOWN_SAVE_SKIPPED reason=disabled")
        return {"ok": True, "saved": False, "reason": "disabled"}
    with _LOCK:
        if _SAVING:
            logger.info("MEM0_AUTO_SAVE_SKIPPED reason=already_saving")
            return {"ok": True, "saved": False, "reason": "already_saving"}
        if not _BUFFER:
            if trigger == "shutdown":
                logger.info("MEM0_SHUTDOWN_SAVE_SKIPPED reason=empty")
            else:
                logger.info("MEM0_AUTO_SAVE_SKIPPED reason=empty")
            return {"ok": True, "saved": False, "reason": "empty"}
        messages = list(_BUFFER)
        _BUFFER.clear()
        _SAVING = True
    log_prefix = "MEM0_SHUTDOWN_SAVE" if trigger == "shutdown" else "MEM0_AUTO_SAVE"
    if trigger == "shutdown":
        logger.info("MEM0_SHUTDOWN_SAVE_BEGIN pending_turns=%s", len(messages))
    else:
        logger.info("MEM0_AUTO_SAVE_BEGIN trigger=%s", trigger)
    started = time.monotonic()
    provider = Mem0CloudProvider()
    try:
        result = provider.add_messages(
            messages,
            user_id=provider.user_id,
            metadata={
                "source": "auto_session_memory",
                "session_id": str(session_id or ""),
                "trigger": trigger,
            },
        )
    except Exception as exc:
        logger.warning("%s_FAILED error_type=%s", log_prefix, exc.__class__.__name__)
        with _LOCK:
            _BUFFER[:0] = messages
        return {"ok": False, "saved": False, "error_type": exc.__class__.__name__}
    finally:
        provider.close()
        with _LOCK:
            _SAVING = False
    elapsed = time.monotonic() - started
    if elapsed > timeout_seconds:
        logger.warning("%s_TIMEOUT elapsed=%.2f", log_prefix, elapsed)
    if bool(result.get("blocked_sensitive")):
        logger.info("MEM0_SAVE_BLOCKED_SENSITIVE source=auto_session_memory")
        return {"ok": True, "saved": False, "reason": "blocked_sensitive"}
    if not bool(result.get("ok")):
        logger.warning("%s_FAILED error_type=%s", log_prefix, result.get("error_type") or "unknown")
        with _LOCK:
            _BUFFER[:0] = messages
        return {"ok": False, "saved": False, "result": result}
    _LAST_FLUSH_AT = time.time()
    if bool(result.get("pending")):
        logger.info("%s_PENDING event_id=%s", log_prefix, result.get("event_id") or "")
    logger.info("%s_OK", log_prefix)
    return {"ok": True, "saved": True, "pending": bool(result.get("pending")), "event_id": result.get("event_id")}


def pending_count() -> int:
    with _LOCK:
        return len(_BUFFER)
