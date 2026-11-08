from __future__ import annotations

import json
import logging
import hashlib
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROUTE_DEBUG_PATH = PROJECT_ROOT / "runtime" / "last_command_route.json"
_lock = threading.Lock()
_last_route: dict[str, Any] = {}


def _route_id_for(text: str, timestamp: float) -> str:
    digest = hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"cmd-{int(timestamp * 1000)}-{digest}"


def _log_command_route_event(event: str, data: dict[str, Any]) -> None:
    try:
        from services.kira_log_manager import log_command_event
        log_command_event(event, data)
    except Exception as exc:
        logger.debug("COMMAND_ROUTE_JSONL_DROPPED event=%s error=%s", event, exc)


def _normalize_text(text: str) -> str:
    lowered = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def begin_route(user_text: str, *, source: str = "unknown") -> dict[str, Any]:
    timestamp = time.time()
    route = {
        "timestamp": timestamp,
        "route_id": _route_id_for(str(user_text or ""), timestamp),
        "source": str(source or "unknown"),
        "user_text": str(user_text or ""),
        "normalized_text": _normalize_text(str(user_text or "")),
        "gates": [],
        "handled": False,
        "handled_by": "",
        "blocked_normal_reply": False,
        "blocked_voice": False,
        "final_action": "",
    }
    with _lock:
        _last_route.clear()
        _last_route.update(route)
    logger.info("COMMAND_ROUTE_BEGIN source=%s user_text=%r", route["source"], route["user_text"][:180])
    _persist()
    _log_command_route_event(
        "COMMAND_ROUTE_BEGIN",
        {
            "route_id": route["route_id"],
            "source": route["source"],
            "user_text_preview": route["user_text"][:180],
            "normalized_text": route["normalized_text"][:180],
        },
    )
    return route


def record_gate_attempt(
    gate_name: str,
    user_text: str = "",
    normalized_text: str = "",
    matched: bool = False,
    *,
    reason: str | None = None,
    action: str | None = None,
    target: str | None = None,
    handled: bool = False,
) -> None:
    gate = {
        "gate": str(gate_name or "UNKNOWN_GATE"),
        "matched": bool(matched),
        "reason": str(reason or ""),
        "action": str(action or ""),
        "target": str(target or ""),
        "handled": bool(handled),
    }
    if not _last_route:
        begin_route(user_text, source="implicit")
    with _lock:
        if user_text and not _last_route.get("user_text"):
            _last_route["user_text"] = str(user_text)
        if normalized_text:
            _last_route["normalized_text"] = str(normalized_text)
        _last_route.setdefault("gates", []).append(gate)
        route_id = str(_last_route.get("route_id") or "")
        route_source = str(_last_route.get("source") or "")
    if matched:
        logger.info("COMMAND_GATE_MATCHED gate=%s action=%s target=%s", gate["gate"], gate["action"], gate["target"])
    else:
        logger.info("COMMAND_GATE_REJECTED gate=%s reason=%s", gate["gate"], gate["reason"] or "not_matched")
    logger.info(
        "COMMAND_GATE_ATTEMPT gate=%s matched=%s reason=%s",
        gate["gate"],
        str(bool(matched)).lower(),
        gate["reason"] or "",
    )
    _persist()
    _log_command_route_event(
        "COMMAND_GATE_ATTEMPT",
        {
            "route_id": route_id,
            "source": route_source,
            **gate,
        },
    )


def record_route_final(
    handled_by: str,
    *,
    handled: bool,
    blocked_normal_reply: bool,
    blocked_voice: bool = False,
    final_action: str = "",
) -> None:
    if not _last_route:
        begin_route("", source="implicit")
    with _lock:
        _last_route.update(
            {
                "handled": bool(handled),
                "handled_by": str(handled_by or ""),
                "blocked_normal_reply": bool(blocked_normal_reply),
                "blocked_voice": bool(blocked_voice),
                "final_action": str(final_action or ""),
            }
        )
        payload = dict(_last_route)
    if handled:
        logger.info("COMMAND_ROUTE_HANDLED_BY gate=%s final_action=%s", handled_by, final_action)
    else:
        logger.info("COMMAND_ROUTE_FALLBACK_GEMINI reason=no_gate_matched")
    _persist()
    _log_command_route_event(
        "COMMAND_ROUTE_FINAL",
        {
            "route_id": str(payload.get("route_id") or ""),
            "source": str(payload.get("source") or ""),
            "handled": bool(handled),
            "handled_by": str(handled_by or ""),
            "blocked_normal_reply": bool(blocked_normal_reply),
            "blocked_voice": bool(blocked_voice),
            "final_action": str(final_action or ""),
            "gate_count": len(payload.get("gates") or []),
        },
    )


def get_last_route() -> dict[str, Any]:
    with _lock:
        if _last_route:
            return dict(_last_route)
    try:
        return json.loads(ROUTE_DEBUG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"timestamp": 0, "gates": [], "handled": False, "handled_by": ""}


def _persist() -> None:
    try:
        ROUTE_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            payload = dict(_last_route)
        ROUTE_DEBUG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("COMMAND_ROUTE_DEBUG_PERSIST_DROPPED error=%s", exc)
