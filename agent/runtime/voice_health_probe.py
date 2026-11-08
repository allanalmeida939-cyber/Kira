from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

if __package__ and __package__.startswith("agent"):
    from agent.runtime.audio_confirmation import get_audio_confirmation
    from agent.runtime.voice_output_manager import get_voice_output_manager
else:  # pragma: no cover
    from runtime.audio_confirmation import get_audio_confirmation
    from runtime.voice_output_manager import get_voice_output_manager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VoiceHealthSnapshot:
    voice_available: bool
    voice_degraded: bool
    voice_degraded_reason: str = ""
    pre_turn_pending: bool = False
    recovery_in_progress: bool = False
    ready_to_speak_stale: bool = False
    audio_hooks_valid: bool = False
    gemini_ready: bool = False
    voice_pipeline_ready: bool = False
    agent_speaking: bool = False
    active_speech_id: str = ""
    voice_output_manager_state: str = "unknown"
    voice_output_manager_state_age: float = 0.0
    last_had_first_chunk: bool = False
    audio_output_active: bool = False
    tool_ack_speaking: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def has_active_speech(self) -> bool:
        return bool(
            self.agent_speaking
            or self.audio_output_active
            or self.tool_ack_speaking
            or (self.active_speech_id and not self.ready_to_speak_stale)
            or self.voice_output_manager_state in {"speaking", "draining"}
        )

    @property
    def healthy(self) -> bool:
        return bool(
            self.voice_available
            and not self.voice_degraded
            and self.gemini_ready
            and self.voice_pipeline_ready
            and self.audio_hooks_valid
        )

    def block_reason(self) -> str:
        if self.healthy:
            return ""
        if self.voice_degraded:
            return self.voice_degraded_reason or "voice_degraded"
        if not self.voice_available:
            return self.voice_degraded_reason or "voice_not_available"
        if self.recovery_in_progress:
            return "recovery_in_progress"
        if self.pre_turn_pending:
            return self.voice_degraded_reason or "pre_turn_pending"
        if not self.gemini_ready:
            return "gemini_not_ready"
        if not self.voice_pipeline_ready:
            return "voice_pipeline_not_ready"
        if not self.audio_hooks_valid:
            return "audio_hooks_not_installed"
        if self.ready_to_speak_stale:
            return "ready_to_speak_stale"
        return "voice_not_ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_available": self.voice_available,
            "voice_degraded": self.voice_degraded,
            "voice_degraded_reason": self.voice_degraded_reason,
            "pre_turn_pending": self.pre_turn_pending,
            "recovery_in_progress": self.recovery_in_progress,
            "ready_to_speak_stale": self.ready_to_speak_stale,
            "audio_hooks_valid": self.audio_hooks_valid,
            "gemini_ready": self.gemini_ready,
            "voice_pipeline_ready": self.voice_pipeline_ready,
            "agent_speaking": self.agent_speaking,
            "active_speech_id": self.active_speech_id,
            "voice_output_manager_state": self.voice_output_manager_state,
            "voice_output_manager_state_age": round(self.voice_output_manager_state_age, 3),
            "last_had_first_chunk": self.last_had_first_chunk,
            "audio_output_active": self.audio_output_active,
            "tool_ack_speaking": self.tool_ack_speaking,
            "healthy": self.healthy,
            "block_reason": self.block_reason(),
            "details": self.details,
        }


class VoiceHealthProbe:
    def __init__(
        self,
        *,
        realtime_session_provider: Callable[[], object | None] | None = None,
        runtime_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        audio_confirmation=None,
        voice_output_manager=None,
        stale_ready_to_speak_seconds: float = 8.0,
    ) -> None:
        self._realtime_session_provider = realtime_session_provider
        self._runtime_snapshot_provider = runtime_snapshot_provider
        self._audio_confirmation = audio_confirmation or get_audio_confirmation()
        self._voice_output_manager = voice_output_manager or get_voice_output_manager()
        self._stale_ready_to_speak_seconds = max(float(stale_ready_to_speak_seconds), 1.0)

    def snapshot(self, *, exclude_speech_id: str = "") -> VoiceHealthSnapshot:
        """exclude_speech_id: fala que está pedindo a vez; o rastreio dela não conta como fala de outra tocando."""
        realtime = self._safe_realtime_session()
        manager_snapshot = self._safe_manager_snapshot()
        audio_status = self._safe_audio_status()
        runtime_snapshot = self._safe_runtime_snapshot()
        now = time.time()
        own = str(exclude_speech_id or "").strip()

        manager_state = str(manager_snapshot.get("state") or "unknown").strip().lower() or "unknown"
        current_started_at = manager_snapshot.get("current_started_at") or manager_snapshot.get("last_started_at")
        state_age = 0.0
        try:
            if current_started_at:
                state_age = max(0.0, now - float(current_started_at))
        except Exception:
            state_age = 0.0

        audio_tracking = audio_status.get("tracking") if isinstance(audio_status, dict) else {}
        if not isinstance(audio_tracking, dict):
            audio_tracking = {}
        audio_active_id = str(audio_status.get("active_speech_id") or "").strip() if isinstance(audio_status, dict) else ""
        manager_active_id = str(manager_snapshot.get("active_speech_id") or "").strip()
        runtime_active_id = str(runtime_snapshot.get("current_speech_id") or "").strip()
        runtime_is_own = bool(own) and runtime_active_id == own
        if own:
            audio_active_id = "" if audio_active_id == own else audio_active_id
            manager_active_id = "" if manager_active_id == own else manager_active_id
            runtime_active_id = "" if runtime_is_own else runtime_active_id
        active_speech_id = manager_active_id or audio_active_id or runtime_active_id

        economy_standby = bool(getattr(realtime, "economy_standby", False))
        live_connected = bool(getattr(realtime, "is_live_connected", False))
        recovery_in_progress = bool(getattr(realtime, "_voice_refresh_in_progress", False))
        voice_degraded = bool(getattr(realtime, "voice_audio_degraded", False))
        reason = str(getattr(realtime, "voice_audio_degraded_reason", "") or "")
        if economy_standby and not reason:
            reason = "economy_standby"
        voice_available = bool(live_connected and not economy_standby and not voice_degraded)

        agent_speaking = bool(
            manager_snapshot.get("agent_speaking")
            or manager_snapshot.get("is_speaking")
            or (runtime_snapshot.get("voice_speech_priority_active") and not runtime_is_own)
        )
        audio_output_active = bool(
            manager_snapshot.get("is_speaking")
            or manager_state in {"speaking", "draining"}
            or (audio_active_id and not bool(audio_tracking.get("finished")))
        )
        tool_ack_speaking = bool(runtime_snapshot.get("tool_ack_active"))
        ready_to_speak_stale = False
        runtime_state = str(runtime_snapshot.get("state") or "").strip().lower()
        try:
            state_age_ms = runtime_snapshot.get("state_age_ms")
            if runtime_state == "ready_to_speak" and state_age_ms is not None:
                ready_to_speak_stale = (float(state_age_ms) / 1000.0) >= self._stale_ready_to_speak_seconds
        except Exception:
            ready_to_speak_stale = False

        hooks_valid = bool(getattr(self._audio_confirmation, "hooks_valid", lambda: False)())
        last_had_first_chunk = bool(getattr(self._audio_confirmation, "last_had_first_chunk", lambda: False)())
        if not reason and ready_to_speak_stale:
            reason = "ready_to_speak_stale"
        pre_turn_pending = str(reason).startswith("pre_turn_pending")

        return VoiceHealthSnapshot(
            voice_available=voice_available,
            voice_degraded=voice_degraded or ready_to_speak_stale,
            voice_degraded_reason=reason,
            pre_turn_pending=pre_turn_pending,
            recovery_in_progress=recovery_in_progress,
            ready_to_speak_stale=ready_to_speak_stale,
            audio_hooks_valid=hooks_valid,
            gemini_ready=bool(live_connected and not economy_standby),
            voice_pipeline_ready=bool(live_connected and hooks_valid),
            agent_speaking=agent_speaking,
            active_speech_id=active_speech_id,
            voice_output_manager_state=manager_state,
            voice_output_manager_state_age=state_age,
            last_had_first_chunk=last_had_first_chunk,
            audio_output_active=audio_output_active,
            tool_ack_speaking=tool_ack_speaking,
            details={
                "economy_standby": economy_standby,
                "live_connected": live_connected,
                "manager": manager_snapshot,
                "audio_confirmation": audio_status,
                "runtime": runtime_snapshot,
            },
        )

    def _safe_realtime_session(self) -> object | None:
        if self._realtime_session_provider is None:
            return None
        try:
            return self._realtime_session_provider()
        except Exception as exc:
            logger.warning("VOICE_HEALTH_PROBE_REALTIME_FAILED reason=%s", exc)
            return None

    def _safe_manager_snapshot(self) -> dict[str, Any]:
        try:
            return dict(self._voice_output_manager.snapshot())
        except Exception as exc:
            logger.warning("VOICE_HEALTH_PROBE_MANAGER_FAILED reason=%s", exc)
            return {}

    def _safe_audio_status(self) -> dict[str, Any]:
        try:
            return dict(self._audio_confirmation.get_status())
        except Exception as exc:
            logger.warning("VOICE_HEALTH_PROBE_AUDIO_FAILED reason=%s", exc)
            return {}

    def _safe_runtime_snapshot(self) -> dict[str, Any]:
        if self._runtime_snapshot_provider is None:
            return {}
        try:
            return dict(self._runtime_snapshot_provider() or {})
        except Exception as exc:
            logger.warning("VOICE_HEALTH_PROBE_RUNTIME_FAILED reason=%s", exc)
            return {}
