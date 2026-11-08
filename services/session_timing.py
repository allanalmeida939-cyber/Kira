from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIMING_PATH = PROJECT_ROOT / "logs" / "Kira" / "session_timing.jsonl"
_LOCK = threading.RLock()
_STARTUP_LOCK = threading.RLock()
_STARTUP_REFERENCES: dict[str, dict[str, object]] = {}
_STARTUP_ROOMS: dict[str, str] = {}
_STARTUP_STAGE_ALIASES = {
    "room_connected": "livekit_room_connected",
    "session_room_connected": "livekit_room_connected",
    "frontend_start_clicked": "click_start",
    "frontend_prepare_done": "prepare_done",
    "frontend_start_done": "start_done",
    "frontend_agent_process_spawned": "agent_process_spawned",
    "frontend_agent_imports_done": "agent_imports_done",
    "frontend_job_accepted": "job_accepted",
    "frontend_room_connected": "livekit_room_connected",
    "frontend_livekit_room_connected": "livekit_room_connected",
    "frontend_ui_room_connected": "ui_room_connected",
    "frontend_microphone_published": "mic_published",
    "frontend_mic_published": "mic_published",
    "frontend_gemini_connect_start": "gemini_connect_start",
    "frontend_gemini_ready": "gemini_ready",
    "frontend_functional_ready": "functional_ready",
    "frontend_memory_loaded": "memory_loaded",
    "frontend_mic_stable_check_start": "mic_stable_check_start",
    "frontend_mic_stable_check_ok": "mic_stable_check_ok",
    "frontend_mic_stable_check_cancel": "mic_stable_check_cancel",
    "frontend_greeting_requested": "greeting_requested",
    "frontend_greeting_first_audio": "greeting_first_audio",
}
_logger = logging.getLogger("kira.startup")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record_timing(*, event: str, timing_id: str = "", source: str = "backend", **details: Any) -> str:
    current_id = str(timing_id or uuid.uuid4().hex)
    payload = {
        "timestamp": _now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "timing_id": current_id,
        "event": str(event or "unknown"),
        "source": str(source or "unknown"),
        **details,
    }
    try:
        with _LOCK:
            TIMING_PATH.parent.mkdir(parents=True, exist_ok=True)
            with TIMING_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Timing nunca pode interromper a inicializacao.
        pass
    return current_id


def startup_timing_mark(
    *,
    event: str,
    timing_id: str = "",
    room: str = "",
    source: str = "backend",
    elapsed_ms: int | float | None = None,
    details: dict[str, Any] | None = None,
) -> tuple[str, int | None]:
    """Record a cheap startup milestone relative to the user's click.

    The first ``click_start`` mark creates the monotonic reference. Later
    process notifications can resolve it by timing id or room name, so the
    session/agent contracts do not need a new handshake field.
    """

    raw_event = str(event or "startup_stage").strip().lower() or "startup_stage"
    stage = _STARTUP_STAGE_ALIASES.get(raw_event, raw_event.removeprefix("startup_"))
    normalized_room = str(room or "").strip()
    normalized_id = str(timing_id or "").strip()
    now_ns = time.monotonic_ns()
    with _STARTUP_LOCK:
        if stage == "click_start":
            normalized_id = normalized_id or uuid.uuid4().hex
            _STARTUP_REFERENCES[normalized_id] = {
                "started_monotonic_ns": now_ns,
                "room": normalized_room,
            }
        else:
            if not normalized_id and normalized_room:
                normalized_id = _STARTUP_ROOMS.get(normalized_room, "")
            reference = _STARTUP_REFERENCES.get(normalized_id) if normalized_id else None
            if reference is None and normalized_id:
                reference = {"started_monotonic_ns": now_ns, "room": normalized_room}
                _STARTUP_REFERENCES[normalized_id] = reference
        if normalized_id and normalized_room:
            _STARTUP_ROOMS[normalized_room] = normalized_id
            reference = _STARTUP_REFERENCES.get(normalized_id)
            if reference is not None and not reference.get("room"):
                reference["room"] = normalized_room
        reference = _STARTUP_REFERENCES.get(normalized_id) if normalized_id else None
        reference_start_ns = int(reference.get("started_monotonic_ns") or now_ns) if reference else now_ns

    measured_ms = elapsed_ms
    if measured_ms is None:
        measured_ms = max(0, int((now_ns - reference_start_ns) / 1_000_000))
    measured_ms = int(max(0, round(float(measured_ms))))
    record_timing(
        event=f"startup_{stage}",
        timing_id=normalized_id,
        source=source,
        room=normalized_room,
        elapsed_ms=measured_ms,
        details=dict(details or {}),
    )
    _logger.info("STARTUP_T %s t=%s", stage, measured_ms)
    return normalized_id, measured_ms

