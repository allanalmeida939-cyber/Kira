from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

if __package__ and __package__.startswith("agent"):
    from agent.runtime.voice_health_probe import VoiceHealthProbe, VoiceHealthSnapshot
    from agent.runtime.voice_sync_recovery import VoiceSyncRecovery
else:  # pragma: no cover
    from runtime.voice_health_probe import VoiceHealthProbe, VoiceHealthSnapshot
    from runtime.voice_sync_recovery import VoiceSyncRecovery

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AdmissionResult:
    admitted: bool
    reason: str
    recovered: bool = False
    recovery_attempted: bool = False
    deferred: bool = False
    snapshot: dict[str, Any] = field(default_factory=dict)


class VoiceTurnAdmissionGate:
    def __init__(
        self,
        *,
        health_probe: VoiceHealthProbe,
        recovery: VoiceSyncRecovery,
        health_timeout_seconds: float = 2.0,
        recovery_timeout_seconds: float = 10.0,
    ) -> None:
        self._health_probe = health_probe
        self._recovery = recovery
        self._health_timeout_seconds = max(float(health_timeout_seconds), 0.1)
        self._recovery_timeout_seconds = max(float(recovery_timeout_seconds), 0.1)
        self._lock = asyncio.Lock()

    async def request_admission(
        self,
        *,
        turn_id: str,
        source: str,
        expects_voice: bool = True,
        own_speech_id: str = "",
    ) -> AdmissionResult:
        """own_speech_id: a fala que está pedindo a vez (o rastreio de áudio dela já começou). Ela não conta como
        "outra fala tocando"; antes, com a voz marcada como suspeita, o aviso se barrava sozinho para sempre."""
        normalized_turn_id = str(turn_id or "none").strip() or "none"
        normalized_source = str(source or "unknown").strip() or "unknown"
        own = str(own_speech_id or "").strip()
        if not expects_voice:
            logger.info("ADMISSION_BYPASSED_TEXT_ONLY turn_id=%s source=%s", normalized_turn_id, normalized_source)
            return AdmissionResult(admitted=True, reason="text_only")

        logger.info("ADMISSION_CHECK_BEGIN turn_id=%s source=%s", normalized_turn_id, normalized_source)
        try:
            async with asyncio.timeout(self._health_timeout_seconds):
                snapshot = self._health_probe.snapshot(exclude_speech_id=own)
        except TimeoutError:
            logger.warning("ADMISSION_DENIED reason=health_check_timeout recovery_attempted=false turn_id=%s", normalized_turn_id)
            return AdmissionResult(admitted=False, reason="health_check_timeout")
        except Exception as exc:
            logger.warning("ADMISSION_DENIED reason=health_check_failed:%s recovery_attempted=false turn_id=%s", exc, normalized_turn_id)
            return AdmissionResult(admitted=False, reason="health_check_failed")

        if snapshot.healthy:
            logger.info("ADMISSION_OK reason=healthy turn_id=%s", normalized_turn_id)
            return AdmissionResult(admitted=True, reason="healthy", snapshot=snapshot.to_dict())

        reason = snapshot.block_reason()
        if snapshot.has_active_speech:
            logger.warning(
                "ADMISSION_DEFERRED_AGENT_SPEAKING turn_id=%s reason=%s",
                normalized_turn_id,
                self._active_audio_reason(snapshot),
            )
            return AdmissionResult(admitted=False, reason="agent_speaking", deferred=True, snapshot=snapshot.to_dict())

        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._health_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("ADMISSION_DENIED reason=recovery_lock_timeout recovery_attempted=false turn_id=%s", normalized_turn_id)
            return AdmissionResult(admitted=False, reason="recovery_lock_timeout", snapshot=snapshot.to_dict())

        try:
            second_snapshot = self._health_probe.snapshot(exclude_speech_id=own)
            if second_snapshot.healthy:
                logger.info("ADMISSION_OK reason=healthy_after_lock turn_id=%s", normalized_turn_id)
                return AdmissionResult(admitted=True, reason="healthy_after_lock", snapshot=second_snapshot.to_dict())
            if second_snapshot.has_active_speech:
                logger.warning(
                    "ADMISSION_DEFERRED_AGENT_SPEAKING turn_id=%s reason=%s",
                    normalized_turn_id,
                    self._active_audio_reason(second_snapshot),
                )
                return AdmissionResult(admitted=False, reason="agent_speaking", deferred=True, snapshot=second_snapshot.to_dict())

            reason = second_snapshot.block_reason() or reason or "voice_not_available"
            logger.warning(
                "ADMISSION_BLOCKED_RECOVERY_START reason=%s turn_id=%s",
                reason,
                normalized_turn_id,
            )
            recovery_result = await self._recovery.recover(
                reason=reason,
                turn_id=normalized_turn_id,
                timeout_seconds=self._recovery_timeout_seconds,
                own_speech_id=own,
            )
            if bool(recovery_result.get("deferred")):
                return AdmissionResult(
                    admitted=False,
                    reason="agent_speaking",
                    recovery_attempted=True,
                    deferred=True,
                    snapshot=dict(recovery_result.get("snapshot") or second_snapshot.to_dict()),
                )
            if not bool(recovery_result.get("ok")):
                denied_reason = str(recovery_result.get("reason") or reason or "voice_not_available")
                logger.warning(
                    "ADMISSION_DENIED reason=%s recovery_attempted=true turn_id=%s",
                    denied_reason,
                    normalized_turn_id,
                )
                return AdmissionResult(
                    admitted=False,
                    reason=denied_reason,
                    recovery_attempted=True,
                    snapshot=dict(recovery_result.get("snapshot") or second_snapshot.to_dict()),
                )

            final_snapshot = self._health_probe.snapshot(exclude_speech_id=own)
            if final_snapshot.healthy:
                logger.warning("ADMISSION_RECOVERY_OK turn_id=%s", normalized_turn_id)
                logger.info("ADMISSION_OK reason=recovered turn_id=%s", normalized_turn_id)
                return AdmissionResult(
                    admitted=True,
                    reason="recovered",
                    recovered=True,
                    recovery_attempted=True,
                    snapshot=final_snapshot.to_dict(),
                )
            final_reason = final_snapshot.block_reason() or "voice_not_available"
            logger.warning("ADMISSION_DENIED reason=%s recovery_attempted=true turn_id=%s", final_reason, normalized_turn_id)
            return AdmissionResult(
                admitted=False,
                reason=final_reason,
                recovery_attempted=True,
                snapshot=final_snapshot.to_dict(),
            )
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

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
