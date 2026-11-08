from __future__ import annotations

import time
from typing import Any


WAKE_QUERY_REPEAT_PROMPT = "To aqui, chefe. Pode repetir a pergunta?"


def build_wake_query_dispatch_response(wake_result: dict[str, Any], *, timestamp: float | None = None) -> dict[str, Any] | None:
    remaining_query = str(wake_result.get("remaining_user_query") or "").strip()
    if not remaining_query or not bool(wake_result.get("ok")):
        return None
    return {
        "status": "wake_with_query",
        "reply": "",
        "handled": True,
        "reason": "economy_wake_query_dispatch_client",
        "voice": False,
        "gemini": False,
        "economy_standby": False,
        "wake_with_query": True,
        "wake_word": str(wake_result.get("wake_word") or wake_result.get("word") or ""),
        "remaining_user_query": remaining_query,
        "state": wake_result.get("state"),
        "timestamp": time.time() if timestamp is None else timestamp,
    }


def build_wake_query_partial_response(
    reason: str,
    wake_result: dict[str, Any] | None = None,
    *,
    timestamp: float | None = None,
) -> dict[str, Any]:
    wake_result = wake_result or {}
    remaining_query = str(wake_result.get("remaining_user_query") or "").strip()
    return {
        "status": "partial",
        "reply": WAKE_QUERY_REPEAT_PROMPT,
        "handled": True,
        "reason": "economy_wake_query_dispatch_failed",
        "detail": str(reason or "unknown"),
        "voice": False,
        "gemini": False,
        "economy_standby": False,
        "wake_with_query": bool(remaining_query),
        "wake_word": str(wake_result.get("wake_word") or wake_result.get("word") or ""),
        "remaining_user_query": remaining_query,
        "state": wake_result.get("state"),
        "timestamp": time.time() if timestamp is None else timestamp,
    }
