from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

if __package__ and __package__.startswith("agent"):
    from agent.runtime.voice_health_probe import VoiceHealthProbe, VoiceHealthSnapshot
else:  # pragma: no cover
    from runtime.voice_health_probe import VoiceHealthProbe, VoiceHealthSnapshot

logger = logging.getLogger(__name__)


class VoiceSyncRecovery:
    def __init__(
        self,
        *,
        health_probe: VoiceHealthProbe,
        realtime_session_provider: Callable[[], object | None] | None = None,
        audio_confirmation=None,
        voice_output_manager=None,
        poll_seconds: float = 0.5,
    ) -> None:
        self._health_probe = health_probe
        self._realtime_session_provider = realtime_session_provider
        self._audio_confirmation = audio_confirmation
        self._voice_output_manager = voice_output_manager
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._own_speech_id = ""

    def _snapshot(self):
        return self._health_probe.snapshot(exclude_speech_id=self._own_speech_id)

    async def recover(
        self,
        *,
        reason: str,
        turn_id: str,
        timeout_seconds: float = 10.0,
        own_speech_id: str = "",
    ) -> dict[str, Any]:
        """own_speech_id: a fala que pediu a vez; o rastreio dela não conta como outra fala tocando."""
        timeout_seconds = max(float(timeout_seconds), 0.1)
        self._own_speech_id = str(own_speech_id or "").strip()
        before = self._snapshot()
        if before.has_active_speech:
            logger.warning(
                "ADMISSION_DEFERRED_AGENT_SPEAKING turn_id=%s reason=%s",
                turn_id or "none",
                self._active_audio_reason(before),
            )
            return {"ok": False, "reason": "agent_speaking", "deferred": True, "snapshot": before.to_dict()}

        logger.warning("VOICE_SYNC_RECOVERY_START turn_id=%s reason=%s", turn_id or "none", reason or "unknown")
        try:
            result = await asyncio.wait_for(
                self._recover_inner(reason=reason, turn_id=turn_id, timeout_seconds=timeout_seconds),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("VOICE_SYNC_RECOVERY_TIMEOUT turn_id=%s timeout_s=%.1f", turn_id or "none", timeout_seconds)
            return {"ok": False, "reason": "recovery_timeout"}
        except Exception as exc:
            logger.warning("VOICE_SYNC_RECOVERY_FAILED turn_id=%s reason=%s", turn_id or "none", exc)
            return {"ok": False, "reason": str(exc) or exc.__class__.__name__}

        if result.get("ok"):
            logger.warning("VOICE_SYNC_RECOVERY_OK turn_id=%s", turn_id or "none")
        else:
            logger.warning("VOICE_SYNC_RECOVERY_FAILED turn_id=%s reason=%s", turn_id or "none", result.get("reason") or "unknown")
        return result

    async def _recover_inner(self, *, reason: str, turn_id: str, timeout_seconds: float) -> dict[str, Any]:
        realtime = self._safe_realtime_session()
        if realtime is None:
            return {"ok": False, "reason": "realtime_session_unavailable"}

        snapshot = self._snapshot()
        if snapshot.has_active_speech:
            return {"ok": False, "reason": "agent_speaking", "deferred": True}

        if not snapshot.audio_hooks_valid:
            if snapshot.has_active_speech:
                logger.warning("AUDIO_HOOK_REINSTALL_SKIPPED_ACTIVE_AUDIO")
            else:
                reinstall = getattr(self._audio_confirmation, "reinstall_livekit_hooks", None)
                if callable(reinstall):
                    reinstall()

        if bool(getattr(realtime, "economy_standby", False)):
            wake = getattr(realtime, "wake_from_economy_standby", None)
            if not callable(wake):
                return {"ok": False, "reason": "wake_unavailable"}
            logger.warning("VOICE_SYNC_RECOVERY_WAKE_GEMINI turn_id=%s", turn_id or "none")
            wake_result = await wake(timeout_seconds=min(timeout_seconds, 10.0))
            if isinstance(wake_result, dict) and wake_result.get("ok") is False:
                return {"ok": False, "reason": wake_result.get("reason") or "wake_failed"}
        elif snapshot.voice_degraded or not snapshot.voice_available:
            refresh = getattr(realtime, "refresh_after_voice_degradation", None)
            if callable(refresh):
                logger.warning("VOICE_SYNC_RECOVERY_REFRESH_GEMINI turn_id=%s reason=%s", turn_id or "none", reason)
                result = await refresh(reason=f"admission:{reason or 'voice_not_available'}", timeout_seconds=min(timeout_seconds, 10.0))
                if isinstance(result, dict) and result.get("ok") is False:
                    return {"ok": False, "reason": result.get("reason") or "refresh_failed"}

        return await self._wait_until_healthy(timeout_seconds=min(timeout_seconds, 10.0))

    async def _wait_until_healthy(self, *, timeout_seconds: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.1)
        last = None
        while loop.time() < deadline:
            last = self._snapshot()
            if last.healthy:
                return {"ok": True, "snapshot": last.to_dict()}
            if last.has_active_speech:
                logger.warning("ADMISSION_DEFERRED_AGENT_SPEAKING turn_id=none reason=%s", self._active_audio_reason(last))
                return {"ok": False, "reason": "agent_speaking", "deferred": True, "snapshot": last.to_dict()}
            await asyncio.sleep(self._poll_seconds)
        return {"ok": False, "reason": (last.block_reason() if last else "voice_not_ready"), "snapshot": last.to_dict() if last else {}}

    def _safe_realtime_session(self) -> object | None:
        if self._realtime_session_provider is None:
            return None
        try:
            return self._realtime_session_provider()
        except Exception as exc:
            logger.warning("VOICE_SYNC_RECOVERY_REALTIME_FAILED reason=%s", exc)
            return None

    @staticmethod
    def _active_audio_reason(snapshot: VoiceHealthSnapshot) -> str:
        if snapshot.tool_ack_speaking:
            return "tool_ack_speaking"
        if snapshot.audio_output_active:
            return "active_audio_output"
        if snapshot.agent_speaking:
            return "agent_speaking"
        if snapshot.active_speech_id:
            return "active_speech_id"
        return "active_speech"
