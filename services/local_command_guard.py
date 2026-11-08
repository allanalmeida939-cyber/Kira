from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

_blocked_until = 0.0
_blocked_source = ""
_blocked_turn_id: str | None = None
_pc_operator_active = False
_pc_operator_task_id = ""
_pc_operator_last_finished_at = 0.0
_pc_operator_last_result = ""
_last_local_handled_at = 0.0
_last_local_handled_source = ""
_last_local_handled_turn_id: str | None = None
_rejected_turns: dict[str, tuple[float, str]] = {}
_REJECTED_TURN_TTL_SECONDS = 20.0


@dataclass(frozen=True)
class LocalCommandResult:
    handled: bool = True
    action: str = ""
    target: str = ""
    text_response: str = ""
    speak: bool = False
    block_gemini: bool = True
    block_voice: bool = True
    block_generate_reply: bool = True
    block_proactive: bool = True
    command_intercepted: bool = True
    local_command_handled: bool = True
    text_only: bool = True

    def flags(self) -> dict[str, object]:
        return asdict(self)


def mark_local_command(
    source: str,
    *,
    seconds: float = 3.0,
    turn_id: str | None = None,
    voice_required: bool = False,
    lane: str = "tool_ack",
    clarification: bool = False,
) -> LocalCommandResult:
    global _blocked_until, _blocked_source, _blocked_turn_id
    _blocked_source = str(source or "local_command").strip() or "local_command"
    _blocked_turn_id = str(turn_id or "").strip() or None
    _blocked_until = time.monotonic() + max(0.5, float(seconds or 3.0))
    mark_local_handled(_blocked_source, turn_id=_blocked_turn_id)
    logger.info("LOCAL_COMMAND_HANDLED_EARLY_RETURN source=%s", _blocked_source)
    logger.info("NORMAL_REPLY_BLOCKED_BY_LOCAL_COMMAND source=%s", _blocked_source)
    logger.info("AUTO_REPLY_BLOCKED_BY_LOCAL_COMMAND source=%s", _blocked_source)
    logger.info("GHOST_REPLY_BLOCKED_BY_LOCAL_COMMAND source=%s", _blocked_source)
    if voice_required:
        if clarification:
            logger.info("LOCAL_COMMAND_SAFE_CLARIFICATION source=%s", _blocked_source)
        logger.info("LOCAL_COMMAND_VOICE_LANE_USED lane=%s", lane or "tool_ack")
        logger.info("LOCAL_COMMAND_NO_SPEECH_STATE_CHANGE")
    else:
        logger.info("LOCAL_COMMAND_NO_VOICE_REQUEST_CREATED")
        logger.info("LOCAL_COMMAND_NO_SPEECH_STATE_CHANGE")
    return LocalCommandResult(
        action=_blocked_source,
        speak=bool(voice_required),
        block_voice=not bool(voice_required),
        text_only=not bool(voice_required),
    )


def mark_local_handled(source: str, *, turn_id: str | None = None) -> None:
    global _last_local_handled_at, _last_local_handled_source, _last_local_handled_turn_id
    _last_local_handled_at = time.monotonic()
    _last_local_handled_source = str(source or "local_command").strip() or "local_command"
    _last_local_handled_turn_id = str(turn_id or "").strip() or None
    logger.info("LOCAL_COMMAND_HANDLED_MARKED source=%s turn_id=%s", _last_local_handled_source, _last_local_handled_turn_id or "none")
    logger.info("LOCAL_TURN_HANDLED_MARKED source=%s turn_id=%s", _last_local_handled_source, _last_local_handled_turn_id or "none")


def recent_local_handled(max_age_seconds: float = 15.0) -> dict[str, object] | None:
    if _last_local_handled_at <= 0.0:
        return None
    age = time.monotonic() - _last_local_handled_at
    if age > max(0.0, float(max_age_seconds or 0.0)):
        return None
    return {
        "source": _last_local_handled_source or "local_command",
        "age_seconds": age,
        "turn_id": _last_local_handled_turn_id,
    }


def should_ignore_gemini_silence_after_local_command(
    reason: str,
    *,
    max_age_seconds: float = 15.0,
) -> dict[str, object] | None:
    if str(reason or "") != "gemini_silence_gt_10s":
        return None
    recent = recent_local_handled(max_age_seconds)
    if not recent:
        return None
    logger.warning(
        "GEMINI_SILENCE_IGNORED_AFTER_LOCAL_COMMAND source=%s age_s=%.1f",
        recent.get("source") or "local_command",
        float(recent.get("age_seconds") or 0.0),
    )
    logger.warning(
        "GEMINI_SILENCE_SUPPRESSED_AFTER_LOCAL_TURN source=%s age_s=%.1f",
        recent.get("source") or "local_command",
        float(recent.get("age_seconds") or 0.0),
    )
    return recent


def begin_local_command(
    source: str,
    *,
    turn_id: str | None = None,
    action: str = "",
    target: str = "",
    seconds: float = 3.0,
    voice_required: bool = False,
    lane: str = "tool_ack",
    clarification: bool = False,
) -> LocalCommandResult:
    result = mark_local_command(
        source,
        seconds=seconds,
        turn_id=turn_id,
        voice_required=voice_required,
        lane=lane,
        clarification=clarification,
    )
    result = LocalCommandResult(
        action=action or result.action,
        target=target,
        speak=result.speak,
        block_voice=result.block_voice,
        text_only=result.text_only,
    )
    logger.info(
        "LOCAL_COMMAND_GATE_MATCHED source=%s turn_id=%s action=%s target=%s",
        _blocked_source,
        _blocked_turn_id or "none",
        result.action or "none",
        result.target or "none",
    )
    if voice_required:
        logger.info("LOCAL_COMMAND_VOICE_ALLOWED_BEFORE_EXEC source=%s lane=%s", _blocked_source, lane or "tool_ack")
    else:
        logger.info("LOCAL_COMMAND_VOICE_BLOCKED_BEFORE_EXEC source=%s", _blocked_source)
    logger.info("LOCAL_COMMAND_NORMAL_REPLY_BLOCKED reason=command_intercepted source=%s", _blocked_source)
    return result


def active_source() -> str | None:
    if _pc_operator_active:
        return "desktop_operator"
    if time.monotonic() <= _blocked_until:
        return _blocked_source or "local_command"
    return None


def active_flags(*, turn_id: str | None = None) -> dict[str, object] | None:
    source = active_source()
    if not source:
        return None
    normalized_turn_id = str(turn_id or "").strip() or None
    if normalized_turn_id and _blocked_turn_id and normalized_turn_id != _blocked_turn_id:
        return None
    return LocalCommandResult(action=source).flags()


def is_command_intercepted(*, turn_id: str | None = None) -> bool:
    return bool(active_flags(turn_id=turn_id))


def mark_pc_operator_active(task_id: str) -> None:
    global _pc_operator_active, _pc_operator_task_id
    _pc_operator_active = True
    _pc_operator_task_id = str(task_id or "")
    mark_local_command("desktop_operator", seconds=20.0)
    logger.info("PC_OPERATOR_ACTIVE_SET true task_id=%s", _pc_operator_task_id)
    logger.info("PC_OPERATOR_BLOCKED_GENERATE_REPLY")
    logger.info("PC_OPERATOR_BLOCKED_PROACTIVE")
    logger.info("PC_OPERATOR_BLOCKED_AUTO_REPLY")


def clear_pc_operator_active(result: str, summary: str = "") -> None:
    global _pc_operator_active, _pc_operator_task_id, _pc_operator_last_finished_at, _pc_operator_last_result
    _pc_operator_active = False
    _pc_operator_task_id = ""
    _pc_operator_last_finished_at = time.monotonic()
    _pc_operator_last_result = str(result or "")
    mark_local_command("desktop_operator", seconds=20.0)
    logger.info("PC_OPERATOR_ACTIVE_SET false result=%s summary=%s", result, str(summary or "")[:100])


def is_pc_operator_active() -> bool:
    return bool(_pc_operator_active)


def should_block_turnless_generation(user_initiated: bool) -> str | None:
    source = active_source()
    if source and not bool(user_initiated):
        logger.info("LOCAL_COMMAND_TURNLESS_GENERATION_BLOCKED source=%s", source)
        logger.info("TURNLESS_GENERATION_BLOCKED reason=local_command_recently_handled")
        return source
    return None


def should_block_generation(*, turn_id: str | None, source: str, text_only: bool = False) -> str | None:
    rejected_reason = should_block_rejected_turn(turn_id=turn_id, source=source)
    if rejected_reason:
        return rejected_reason
    active = active_source()
    if text_only:
        logger.info("GENERATE_REPLY_BLOCKED_TEXT_ONLY source=%s turn_id=%s", source, turn_id or "none")
        return active or "text_only"
    if active and (not turn_id or (_blocked_turn_id and str(turn_id) == _blocked_turn_id)):
        logger.info("GENERATE_REPLY_BLOCKED_COMMAND_INTERCEPTED source=%s turn_id=%s", source, turn_id or "none")
        return active
    return None


def mark_turn_rejected(turn_id: str | None, *, reason: str) -> None:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return
    _cleanup_rejected_turns()
    clean_reason = str(reason or "rejected_transcript").strip() or "rejected_transcript"
    _rejected_turns[normalized_turn_id] = (time.monotonic(), clean_reason)
    logger.info("TURN_REJECTED_SET turn_id=%s reason=%s", normalized_turn_id, clean_reason)


def _cleanup_rejected_turns() -> None:
    now = time.monotonic()
    for turn_id, (created_at, _reason) in list(_rejected_turns.items()):
        if now - created_at > _REJECTED_TURN_TTL_SECONDS:
            _rejected_turns.pop(turn_id, None)


def should_block_rejected_turn(*, turn_id: str | None, source: str = "") -> str | None:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return None
    _cleanup_rejected_turns()
    data = _rejected_turns.get(normalized_turn_id)
    if not data:
        return None
    reason = data[1]
    logger.info("AUTO_REPLY_BLOCKED_TURN_REJECTED turn_id=%s source=%s reason=%s", normalized_turn_id, source or "unknown", reason)
    logger.info("GEMINI_REPLY_BLOCKED_TURN_REJECTED turn_id=%s source=%s reason=%s", normalized_turn_id, source or "unknown", reason)
    return f"turn_rejected:{reason}"


def should_block_audio(source: str | None = None) -> str | None:
    active = active_source()
    normalized_source = str(source or "").strip().lower()
    if normalized_source in {"creation_task_ack", "manus_completion_notify"} and active in {"creation_task", "artifact_command"}:
        logger.info("CREATION_TASK_ACK_LOCAL_GUARD_BYPASS source=%s", normalized_source)
        return None
    if active:
        logger.info("LOCAL_COMMAND_AUDIO_OUTPUT_BLOCKED source=%s", active)
        logger.info("GHOST_REPLY_BLOCKED_BY_LOCAL_COMMAND source=%s", active)
        logger.info("LOCAL_COMMAND_NO_SPEECH_STATE_CHANGE")
        return active
    return None
