from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Awaitable, Callable

if __package__ and __package__.startswith("agent"):
    from agent.runtime.voice_output_manager import get_voice_output_manager
    from agent.runtime.voice_state_machine import VoiceState, VoiceStateMachine
else:  # pragma: no cover - suporte a execucao direta em agent/
    from runtime.voice_output_manager import get_voice_output_manager
    from runtime.voice_state_machine import VoiceState, VoiceStateMachine

logger = logging.getLogger(__name__)


# Tool de dados em andamento (clima, busca...): a Kira está esperando o resultado, não travada.
TOOL_WAIT_MAX_SECONDS = 30.0
_tool_wait = {"pending": 0, "started_at": 0.0, "finished_at": 0.0}


def note_tool_call(count: int = 1) -> None:
    if _tool_wait["pending"] == 0:
        _tool_wait["started_at"] = time.monotonic()
    _tool_wait["pending"] += max(int(count), 0)


def note_tool_response(count: int = 1) -> None:
    _tool_wait["pending"] = max(0, _tool_wait["pending"] - max(int(count), 0))
    _tool_wait["finished_at"] = time.monotonic()


def tool_wait_state(now: float | None = None) -> tuple[bool, float]:
    """(esperando tool agora, segundos desde a última resposta de tool)."""
    current = time.monotonic() if now is None else now
    waiting = _tool_wait["pending"] > 0 and current - _tool_wait["started_at"] < TOOL_WAIT_MAX_SECONDS
    since = current - _tool_wait["finished_at"] if _tool_wait["finished_at"] else float("inf")
    return waiting, since


def speech_silence_seconds(last_chunk_at: float, speech_started_at: float, now: float) -> float:
    """Segundos sem áudio saindo desde o início da fala; um chunk de uma fala anterior não conta."""
    return max(0.0, now - max(float(last_chunk_at or 0.0), float(speech_started_at)))

READY_TO_SPEAK_TIMEOUT_SECONDS = float(
    os.getenv("KIRA_READY_TO_SPEAK_TIMEOUT_SECONDS", "15.0")
)
FIRST_TURN_READY_TO_SPEAK_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_READY_TO_SPEAK_FIRST_TURN_TIMEOUT_SECONDS", "18.0")),
    READY_TO_SPEAK_TIMEOUT_SECONDS,
)
GHOST_READY_TO_SPEAK_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_GHOST_READY_TO_SPEAK_TIMEOUT_SECONDS", "1.5")),
    0.5,
)

_PROGRESS_LOG_INTERVAL_SECONDS = 5.0
_STATE_LOG_INTERVAL_SECONDS = 5.0
WATCHDOG_FAILED_WARNING_SECONDS = 10.0
WATCHDOG_FAILED_HARD_RECOVERY_SECONDS = 30.0
_REPLY_FINISH_DRAIN_SECONDS = max(
    float(os.getenv("KIRA_REPLY_FINISH_DRAIN_MS", "300")) / 1000.0,
    0.05,
)
RECENT_AUDIO_CHUNK_SUPPRESS_SECONDS = max(
    float(os.getenv("KIRA_VOICE_WATCHDOG_RECENT_CHUNK_SUPPRESS_SECONDS", "8.0")),
    1.0,
)


class VoiceWatchdog:
    def __init__(
        self,
        *,
        state_machine: VoiceStateMachine,
        on_force_recovery: Callable[[str], Awaitable[None] | None] | None = None,
        on_hard_recovery: Callable[[str], Awaitable[bool] | Awaitable[None] | bool | None] | None = None,
        is_hard_recovery_in_progress: Callable[[], bool] | None = None,
        on_reply_finish_release: Callable[[str | None], Awaitable[None] | None] | None = None,
        poll_seconds: float = 0.5,
        thinking_timeout_seconds: float = 30.0,
        speaking_timeout_seconds: float = 15.0,
        ready_to_speak_timeout_seconds: float = READY_TO_SPEAK_TIMEOUT_SECONDS,
    ) -> None:
        self._state_machine = state_machine
        self._on_force_recovery = on_force_recovery
        self._on_hard_recovery = on_hard_recovery
        self._is_hard_recovery_in_progress = is_hard_recovery_in_progress
        self._on_reply_finish_release = on_reply_finish_release
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._thinking_timeout_seconds = max(float(thinking_timeout_seconds), self._poll_seconds)
        self._speaking_timeout_seconds = max(float(speaking_timeout_seconds), self._poll_seconds)
        self._ready_to_speak_timeout_seconds = max(float(ready_to_speak_timeout_seconds), 1.0)
        self._task: asyncio.Task | None = None
        self._recovery_count = 0
        self._last_recovery_reason: str | None = None
        self._last_recovery_at = 0.0
        self._recovering = False
        self._speaking_started_at = 0.0
        self._last_audio_chunk_at = 0.0
        self._current_speech_id: str | None = None
        self._last_progress_log_at = 0.0
        self._last_state_log_at = 0.0
        self._reply_finish_at = 0.0
        self._reply_finish_speech_id: str | None = None
        self._reply_finish_release_inflight = False
        self._current_source: str | None = None
        self._recovered_speech_ids: set[str] = set()
        self._ghost_reply_started_at = 0.0
        self._ghost_reply_speech_id: str | None = None
        self._first_turn_warmup_pending = True
        self._first_turn_warmup_deferred_logged = False
        self._tool_wait_deferred_logged = False
        self._failed_since: float | None = None
        self._failed_stuck_warning_logged = False
        self._failed_hard_recovery_triggered = False
        logger.info(
            "VOICE_READY_TO_SPEAK_TIMEOUT_CONFIG timeout_s=%.1f",
            self._ready_to_speak_timeout_seconds,
        )

    @property
    def is_running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.create_task(self._monitor_loop(), name="kira-runtime-voice-watchdog")
        logger.info(
            "[VOICE_WATCHDOG] started poll_s=%.2f thinking_timeout_s=%.1f speaking_timeout_s=%.1f ready_to_speak_timeout_s=%.1f",
            self._poll_seconds,
            self._thinking_timeout_seconds,
            self._speaking_timeout_seconds,
            self._ready_to_speak_timeout_seconds,
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("[VOICE_WATCHDOG] stopped")

    async def _monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._poll_seconds)
                await self._check_state()
        except asyncio.CancelledError:
            raise

    async def _check_state(self) -> None:
        if self._recovering:
            return
        state = self._state_machine.current_state
        age_seconds = self._state_machine.state_age_seconds
        now = time.monotonic()

        if state == VoiceState.FAILED:
            await self._check_failed_stuck(now=now)
            return

        if self._failed_since is not None:
            self._failed_since = None
            self._failed_stuck_warning_logged = False
            self._failed_hard_recovery_triggered = False

        # Reset chunk tracking when leaving SPEAKING
        if state != VoiceState.SPEAKING:
            self._speaking_started_at = 0.0
            self._last_audio_chunk_at = 0.0

        # Rate-limited state log
        if now - self._last_state_log_at >= _STATE_LOG_INTERVAL_SECONDS:
            self._last_state_log_at = now
            chunk_age_ms: float | None = None
            if self._last_audio_chunk_at > 0.0:
                chunk_age_ms = (now - self._last_audio_chunk_at) * 1000.0
            logger.info(
                "VOICE_WATCHDOG_STATE state=%s speech_id=%s age_ms=%.0f last_chunk_age_ms=%s",
                state.value,
                self._current_speech_id or "none",
                age_seconds * 1000.0,
                f"{chunk_age_ms:.0f}" if chunk_age_ms is not None else "none",
            )

        # THINKING timeout
        if state == VoiceState.THINKING and age_seconds > self._thinking_timeout_seconds:
            try:
                _thinking_snap = get_voice_output_manager().snapshot()
            except Exception:
                _thinking_snap = {}
            _thinking_chunk_at = float(_thinking_snap.get("last_chunk_at") or 0.0) if isinstance(_thinking_snap, dict) else 0.0
            if _thinking_chunk_at > 0.0:
                _thinking_chunk_age = time.time() - _thinking_chunk_at
                if _thinking_chunk_age <= RECENT_AUDIO_CHUNK_SUPPRESS_SECONDS:
                    logger.info(
                        "VOICE_WATCHDOG_THINKING_CHUNK_ACTIVE speech_id=%s last_chunk_age_ms=%.0f",
                        self._current_speech_id or "none",
                        _thinking_chunk_age * 1000.0,
                    )
                    return
            await self._force_recovery(reason="thinking_timeout", age_seconds=age_seconds)
            return

        # READY_TO_SPEAK timeout
        if (
            state == VoiceState.READY_TO_SPEAK
            and self._ghost_reply_started_at > 0.0
            and age_seconds > GHOST_READY_TO_SPEAK_TIMEOUT_SECONDS
        ):
            logger.warning(
                "READY_TO_SPEAK_GHOST_TIMEOUT speech_id=%s elapsed_ms=%.0f timeout_ms=%.0f",
                self._current_speech_id or self._ghost_reply_speech_id or "none",
                age_seconds * 1000.0,
                GHOST_READY_TO_SPEAK_TIMEOUT_SECONDS * 1000.0,
            )
            await self._state_machine.force_idle(reason="ghost_reply_start")
            self.clear_ghost_reply()
            logger.warning(
                "VOICE_STATE_RELEASED_GHOST_REPLY speech_id=%s",
                self._current_speech_id or "none",
            )
            return
        ready_timeout_seconds = self._ready_to_speak_timeout_seconds
        if str(self._current_source or "").strip().lower() in {"kira", "fuzzy_open_web", "stt_retry_prompt"}:
            ready_timeout_seconds = max(ready_timeout_seconds, 25.0)
        elif self._first_turn_warmup_pending:
            ready_timeout_seconds = max(ready_timeout_seconds, FIRST_TURN_READY_TO_SPEAK_TIMEOUT_SECONDS)
            if (
                state == VoiceState.READY_TO_SPEAK
                and age_seconds > self._ready_to_speak_timeout_seconds
                and not self._first_turn_warmup_deferred_logged
            ):
                self._first_turn_warmup_deferred_logged = True
                logger.info(
                    "VOICE_READY_TO_SPEAK_TIMEOUT_DEFERRED reason=first_turn_warmup speech_id=%s elapsed_ms=%.0f grace_timeout_s=%.1f",
                    self._current_speech_id or "none",
                    age_seconds * 1000.0,
                    ready_timeout_seconds,
                )
        tool_waiting, since_tool = tool_wait_state()
        if state == VoiceState.READY_TO_SPEAK and age_seconds > ready_timeout_seconds and (tool_waiting or since_tool < ready_timeout_seconds):
            if not self._tool_wait_deferred_logged:
                self._tool_wait_deferred_logged = True
                logger.info(
                    "VOICE_READY_TO_SPEAK_TIMEOUT_DEFERRED reason=tool_in_flight speech_id=%s elapsed_ms=%.0f",
                    self._current_speech_id or "none",
                    age_seconds * 1000.0,
                )
            return
        if state == VoiceState.READY_TO_SPEAK and age_seconds > ready_timeout_seconds:
            self._first_turn_warmup_pending = False
            logger.warning(
                "VOICE_READY_TO_SPEAK_TIMEOUT speech_id=%s elapsed_ms=%.0f",
                self._current_speech_id or "none",
                age_seconds * 1000.0,
            )
            logger.warning(
                "VOICE_READY_TO_SPEAK_TIMEOUT_CONFIRMED speech_id=%s elapsed_ms=%.0f timeout_s=%.1f",
                self._current_speech_id or "none",
                age_seconds * 1000.0,
                ready_timeout_seconds,
            )
            await self._force_recovery(reason="ready_to_speak_timeout", age_seconds=age_seconds)
            return

        # SPEAKING timeout — suppressed when chunks are recent
        if (
            state == VoiceState.SPEAKING
            and self._reply_finish_at > 0.0
            and not self._reply_finish_release_inflight
        ):
            same_speech = (
                self._reply_finish_speech_id is None
                or self._current_speech_id is None
                or self._reply_finish_speech_id == self._current_speech_id
            )
            chunk_age_seconds = (now - self._last_audio_chunk_at) if self._last_audio_chunk_at > 0.0 else None
            if same_speech and (now - self._reply_finish_at) >= _REPLY_FINISH_DRAIN_SECONDS:
                if chunk_age_seconds is None or chunk_age_seconds >= _REPLY_FINISH_DRAIN_SECONDS:
                    logger.info(
                        "VOICE_WATCHDOG_RELEASED_AFTER_REPLY_FINISH speech_id=%s drain_ms=%.0f chunk_age_ms=%s",
                        self._current_speech_id or self._reply_finish_speech_id or "none",
                        _REPLY_FINISH_DRAIN_SECONDS * 1000.0,
                        f"{chunk_age_seconds * 1000.0:.0f}" if chunk_age_seconds is not None else "none",
                    )
                    if self._on_reply_finish_release is not None:
                        self._reply_finish_release_inflight = True
                        try:
                            maybe_result = self._on_reply_finish_release(
                                self._current_speech_id or self._reply_finish_speech_id
                            )
                            if inspect.isawaitable(maybe_result):
                                await maybe_result
                        finally:
                            self._reply_finish_release_inflight = False
                    return

        if state == VoiceState.SPEAKING and age_seconds > self._speaking_timeout_seconds:
            try:
                manager_snapshot = get_voice_output_manager().snapshot()
            except Exception:
                manager_snapshot = {}
            manager_last_chunk_at = float(manager_snapshot.get("last_chunk_at") or 0.0) if isinstance(manager_snapshot, dict) else 0.0
            if str(manager_snapshot.get("state") or "") == "speaking" and manager_last_chunk_at > 0.0:
                manager_chunk_age = time.time() - manager_last_chunk_at
                if manager_chunk_age <= RECENT_AUDIO_CHUNK_SUPPRESS_SECONDS:
                    logger.info(
                        "VOICE_WATCHDOG_MANAGER_PROGRESS speech_id=%s last_chunk_age_ms=%.0f",
                        self._current_speech_id or "none",
                        manager_chunk_age * 1000.0,
                    )
                    return
                logger.warning(
                    "VOICE_WATCHDOG_MANAGER_STALL_DETECTED speech_id=%s last_chunk_age_ms=%.0f",
                    self._current_speech_id or "none",
                    manager_chunk_age * 1000.0,
                )
            if self._last_audio_chunk_at > 0.0:
                time_since_last_chunk = now - self._last_audio_chunk_at
                if time_since_last_chunk <= RECENT_AUDIO_CHUNK_SUPPRESS_SECONDS:
                    logger.info(
                        "VOICE_WATCHDOG_SPEAKING_STUCK_SUPPRESSED reason=recent_audio_chunk speech_id=%s last_chunk_age_ms=%.0f",
                        self._current_speech_id or "none",
                        time_since_last_chunk * 1000.0,
                    )
                    return
                logger.warning(
                    "VOICE_WATCHDOG_SPEAKING_STUCK_CONFIRMED reason=no_recent_audio_chunk speech_id=%s age_ms=%.0f last_chunk_age_ms=%.0f",
                    self._current_speech_id or "none",
                    age_seconds * 1000.0,
                    time_since_last_chunk * 1000.0,
                )
            else:
                logger.warning(
                    "VOICE_WATCHDOG_SPEAKING_STUCK_CONFIRMED reason=no_recent_audio_chunk speech_id=%s age_ms=%.0f last_chunk_age_ms=none",
                    self._current_speech_id or "none",
                    age_seconds * 1000.0,
                )
            await self._force_recovery(reason="speaking_timeout", age_seconds=age_seconds)
            return

    async def _force_recovery(self, *, reason: str, age_seconds: float) -> None:
        if self._recovering:
            return
        self._recovering = True
        self._recovery_count += 1
        self._last_recovery_reason = reason
        self._last_recovery_at = time.monotonic()
        state = self._state_machine.current_state
        target_speech_id = self._current_speech_id or "none"
        if self._current_speech_id:
            self._recovered_speech_ids.add(self._current_speech_id)
        if state == VoiceState.SPEAKING:
            logger.error(
                "[WATCHDOG] SPEAKING_STUCK age=%.1fs forcando recovery",
                age_seconds,
            )
        elif state == VoiceState.THINKING:
            logger.error(
                "[WATCHDOG] THINKING_STUCK age=%.1fs forcando recovery",
                age_seconds,
            )
        elif state == VoiceState.READY_TO_SPEAK:
            logger.error(
                "[WATCHDOG] READY_TO_SPEAK_STUCK age=%.1fs forcando recovery",
                age_seconds,
            )
        logger.error(
            "[VOICE_WATCHDOG] stall_detected state=%s reason=%s age_s=%.2f speech_id=%s recovery_count=%s",
            state.value,
            reason,
            age_seconds,
            target_speech_id,
            self._recovery_count,
        )
        _diag_chunk_age = (
            round((time.monotonic() - self._last_audio_chunk_at) * 1000.0)
            if self._last_audio_chunk_at > 0.0 else None
        )
        logger.error(
            "SPEECH_QUEUE_DIAGNOSTIC reason=%s state=%s current_speech=%s last_chunk_age_ms=%s recovery_count=%s",
            reason,
            state.value,
            target_speech_id,
            _diag_chunk_age if _diag_chunk_age is not None else "none",
            self._recovery_count,
        )
        _chunk_age_ms = (
            round((time.monotonic() - self._last_audio_chunk_at) * 1000.0)
            if self._last_audio_chunk_at > 0.0 else None
        )
        logger.error(
            "VOICE_DIAGNOSTIC_SNAPSHOT reason=%s state=%s speech_id=%s age_ms=%.0f last_chunk_age_ms=%s recovery_count=%s",
            reason,
            state.value,
            target_speech_id,
            age_seconds * 1000.0,
            _chunk_age_ms if _chunk_age_ms is not None else "none",
            self._recovery_count,
        )
        # Cleanup guard: log whether the target speech_id is still current
        if self._current_speech_id:
            logger.warning(
                "VOICE_CLEANUP_GUARD_OK speech_id=%s reason=%s",
                self._current_speech_id,
                reason,
            )
        else:
            logger.warning(
                "VOICE_CLEANUP_GUARD_SKIP reason=no_current_request target_speech_id=%s",
                target_speech_id,
            )
        try:
            await self._state_machine.transition_to(VoiceState.FAILED, reason=f"watchdog:{reason}", force=True)
            if self._on_force_recovery is not None:
                maybe_result = self._on_force_recovery(reason)
                if inspect.isawaitable(maybe_result):
                    await maybe_result
            await self._state_machine.force_idle(reason=f"watchdog_recovered:{reason}")
            logger.warning("[VOICE_WATCHDOG] recovery_completed reason=%s speech_id=%s", reason, target_speech_id)
        finally:
            self._recovering = False

    async def _check_failed_stuck(self, *, now: float) -> None:
        if self._failed_since is None:
            self._failed_since = now
            self._failed_stuck_warning_logged = False
            self._failed_hard_recovery_triggered = False
            return

        failed_for_s = max(0.0, now - self._failed_since)
        speech_id = self._current_speech_id or "none"
        if (
            failed_for_s >= WATCHDOG_FAILED_WARNING_SECONDS
            and not self._failed_stuck_warning_logged
        ):
            self._failed_stuck_warning_logged = True
            logger.warning(
                "WATCHDOG_FAILED_STUCK speech_id=%s failed_for_s=%.1f",
                speech_id,
                failed_for_s,
            )

        if (
            failed_for_s < WATCHDOG_FAILED_HARD_RECOVERY_SECONDS
            or self._failed_hard_recovery_triggered
        ):
            return

        if self._on_hard_recovery is None:
            logger.warning(
                "WATCHDOG_HARD_RECOVERY_SKIPPED_RESTART_IN_PROGRESS speech_id=%s failed_for_s=%.1f reason=no_callback",
                speech_id,
                failed_for_s,
            )
            self._failed_hard_recovery_triggered = True
            return

        if self._is_hard_recovery_in_progress is not None:
            try:
                if bool(self._is_hard_recovery_in_progress()):
                    logger.warning(
                        "WATCHDOG_HARD_RECOVERY_SKIPPED_RESTART_IN_PROGRESS speech_id=%s failed_for_s=%.1f",
                        speech_id,
                        failed_for_s,
                    )
                    return
            except Exception as exc:
                logger.warning("WATCHDOG_HARD_RECOVERY_IN_PROGRESS_CHECK_FAILED reason=%s", exc)

        self._failed_hard_recovery_triggered = True
        reason = "watchdog_failed_stuck_30s"
        logger.warning(
            "WATCHDOG_HARD_RECOVERY_TRIGGER speech_id=%s failed_for_s=%.1f reason=%s",
            speech_id,
            failed_for_s,
            reason,
        )
        maybe_result = self._on_hard_recovery(reason)
        if inspect.isawaitable(maybe_result):
            await maybe_result

    def reset(self, reason: str = "hard_recovery") -> None:
        self._current_speech_id = None
        self._current_source = None
        self._failed_since = None
        self._failed_stuck_warning_logged = False
        self._failed_hard_recovery_triggered = False
        self._recovering = False
        self._speaking_started_at = 0.0
        self._last_audio_chunk_at = 0.0
        self._reply_finish_at = 0.0
        self._reply_finish_speech_id = None
        self._reply_finish_release_inflight = False
        self._ghost_reply_started_at = 0.0
        self._ghost_reply_speech_id = None
        self._last_recovery_reason = str(reason or "hard_recovery")
        self._last_recovery_at = time.monotonic()
        logger.warning(
            "WATCHDOG_RESET_AFTER_HARD_RECOVERY reason=%s",
            self._last_recovery_reason,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._state_machine.force_idle(reason=f"watchdog_reset:{self._last_recovery_reason}"),
                name="kira-watchdog-reset-force-idle",
            )
        except RuntimeError:
            logger.debug("WATCHDOG_RESET_FORCE_IDLE_SKIPPED reason=no_running_loop")

    def get_status(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "recovery_count": self._recovery_count,
            "last_recovery_reason": self._last_recovery_reason,
            "last_recovery_at_monotonic": round(self._last_recovery_at, 3) if self._last_recovery_at else None,
            "state": self._state_machine.current_state.value,
            "state_age_seconds": round(self._state_machine.state_age_seconds, 3),
            "current_speech_id": self._current_speech_id,
            "failed_since_monotonic": round(self._failed_since, 3) if self._failed_since else None,
            "failed_hard_recovery_triggered": self._failed_hard_recovery_triggered,
        }

    def on_speaking_started(self) -> None:
        self._speaking_started_at = time.monotonic()
        self._last_audio_chunk_at = 0.0

    def on_audio_chunk_sent(
        self,
        *,
        speech_id: str | None = None,
        chunks: int | None = None,
        timestamp: float | None = None,
    ) -> None:
        now = timestamp if timestamp is not None else time.monotonic()
        if speech_id and speech_id in self._recovered_speech_ids:
            logger.warning(
                "LATE_AUDIO_CHUNK_AFTER_RECOVERY speech_id=%s action=ignored_or_reconciled",
                speech_id,
            )
            self._recovered_speech_ids.discard(speech_id)
        self._last_audio_chunk_at = now
        if speech_id:
            self._current_speech_id = speech_id
        self._tool_wait_deferred_logged = False
        if self._first_turn_warmup_pending:
            self._first_turn_warmup_pending = False
            self._first_turn_warmup_deferred_logged = False
            logger.info("VOICE_FIRST_TURN_WARMUP_GRACE_USED speech_id=%s", speech_id or "unknown")
        # Rate-limited progress log (5s interval)
        if now - self._last_progress_log_at >= _PROGRESS_LOG_INTERVAL_SECONDS:
            self._last_progress_log_at = now
            state = self._state_machine.current_state
            age_ms = (now - self._speaking_started_at) * 1000.0 if self._speaking_started_at > 0 else 0.0
            logger.info(
                "VOICE_WATCHDOG_AUDIO_PROGRESS speech_id=%s chunks=%s state=%s age_ms=%.0f",
                speech_id or "unknown",
                chunks if chunks is not None else "?",
                state.value,
                age_ms,
            )

    def on_speaking_finished(self) -> None:
        self._speaking_started_at = 0.0
        self._last_audio_chunk_at = 0.0
        self._reply_finish_at = 0.0
        self._reply_finish_speech_id = None
        self._reply_finish_release_inflight = False
        self._current_source = None
        self.clear_ghost_reply()

    def on_reply_finish(self, *, speech_id: str | None = None, timestamp: float | None = None) -> None:
        self._reply_finish_at = timestamp if timestamp is not None else time.monotonic()
        self._reply_finish_speech_id = speech_id or self._current_speech_id
        logger.info(
            "VOICE_WATCHDOG_REPLY_FINISH_SEEN speech_id=%s drain_ms=%.0f",
            self._reply_finish_speech_id or "none",
            _REPLY_FINISH_DRAIN_SECONDS * 1000.0,
        )

    def on_ready_to_speak_entered(self, *, speech_id: str, source: str | None = None) -> None:
        self._current_speech_id = speech_id
        self._current_source = str(source or "").strip().lower() or None
        first_turn_timeout = (
            FIRST_TURN_READY_TO_SPEAK_TIMEOUT_SECONDS
            if self._first_turn_warmup_pending and self._current_source not in {"kira", "fuzzy_open_web", "stt_retry_prompt"}
            else self._ready_to_speak_timeout_seconds
        )
        if self._first_turn_warmup_pending:
            logger.info(
                "VOICE_FIRST_TURN_WARMUP_BEGIN speech_id=%s source=%s timeout_s=%.1f",
                speech_id,
                self._current_source or "unknown",
                first_turn_timeout,
            )
        logger.info(
            "VOICE_READY_TO_SPEAK_ENTERED speech_id=%s timeout_s=%.1f",
            speech_id,
            max(
                first_turn_timeout,
                25.0 if self._current_source in {"kira", "fuzzy_open_web", "stt_retry_prompt"} else self._ready_to_speak_timeout_seconds,
            ),
        )

    def mark_ghost_reply(self, *, speech_id: str | None = None) -> None:
        self._ghost_reply_started_at = time.monotonic()
        self._ghost_reply_speech_id = speech_id or self._current_speech_id

    def clear_ghost_reply(self) -> None:
        self._ghost_reply_started_at = 0.0
        self._ghost_reply_speech_id = None
