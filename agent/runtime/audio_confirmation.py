from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

if __package__ and __package__.startswith("agent"):
    from agent.runtime.voice_output_manager import get_voice_output_manager
    from agent.runtime.vision_frame_store import get_vision_frame_store
else:  # pragma: no cover - suporte a execucao direta em agent/
    from runtime.voice_output_manager import get_voice_output_manager
    from runtime.vision_frame_store import get_vision_frame_store

logger = logging.getLogger(__name__)


FIRST_CHUNK_WARN_TIMEOUT_S = float(os.getenv("KIRA_FIRST_CHUNK_WARN_TIMEOUT_SECONDS", "3.0"))
FIRST_CHUNK_HARD_TIMEOUT_S = float(os.getenv("KIRA_FIRST_CHUNK_HARD_TIMEOUT_SECONDS", "18.0"))
FIRST_CHUNK_TIMEOUT_SECONDS = FIRST_CHUNK_HARD_TIMEOUT_S
CONFIRMATION_WINDOW_SECONDS = 12.0
SILENCE_TIMEOUT_SECONDS = CONFIRMATION_WINDOW_SECONDS
MIN_VALID_AUDIO_CHUNKS = 3
MIN_VALID_AUDIO_BYTES = int(os.getenv("KIRA_MIN_VALID_AUDIO_BYTES", "9600"))
VISION_FIRST_CHUNK_TIMEOUT_SECONDS = float(os.getenv("KIRA_VISION_FIRST_CHUNK_TIMEOUT_SECONDS", "5.0"))
VISION_SILENCE_TIMEOUT_SECONDS = 2.0
AUDIO_CHUNK_GAP_WARN_MS = float(os.getenv("KIRA_AUDIO_CHUNK_GAP_WARN_MS", "450"))
AUDIO_CHUNK_GAP_STUTTER_MS = float(os.getenv("KIRA_AUDIO_CHUNK_GAP_STUTTER_MS", "900"))
AUDIO_CHUNK_GAP_TOLERATED_MS = float(os.getenv("KIRA_AUDIO_CHUNK_GAP_TOLERATED_MS", "300"))
AUDIO_HIGH_GAP_MS = float(os.getenv("KIRA_AUDIO_HIGH_GAP_MS", "700"))
VOICE_SMOOTHING_NORMAL_BUFFER_MS = int(os.getenv("KIRA_VOICE_SMOOTHING_NORMAL_BUFFER_MS", "150"))
VOICE_SMOOTHING_INCREASED_BUFFER_MS = int(os.getenv("KIRA_VOICE_SMOOTHING_INCREASED_BUFFER_MS", "250"))


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off"}


@dataclass(slots=True)
class AudioConfirmationEvent:
    speech_id: str
    source: str
    chunk_count: int
    byte_count: int
    latency_seconds: float | None
    duration_seconds: float | None
    interrupted: bool
    valid_audio: bool


@dataclass(slots=True)
class _TrackingState:
    speech_id: str
    source: str
    started_at_monotonic: float
    started_at_wallclock: float
    chunk_count: int = 0
    byte_count: int = 0
    first_chunk_at_monotonic: float | None = None
    first_chunk_created_at: float | None = None
    last_chunk_at_monotonic: float | None = None
    last_gap_ms: float | None = None
    max_gap_ms: float = 0.0
    total_gap_ms: float = 0.0
    gap_count: int = 0
    gap_warn_logged: bool = False
    stutter_logged: bool = False
    last_chunk_size: int = 0
    finished_at_monotonic: float | None = None
    _last_progress_log_at: float = 0.0
    playback_position_seconds: float = 0.0
    interrupted: bool = False
    first_chunk_event: asyncio.Event = field(default_factory=asyncio.Event)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def latency_seconds(self) -> float | None:
        if self.first_chunk_at_monotonic is None:
            return None
        return max(0.0, self.first_chunk_at_monotonic - self.started_at_monotonic)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at_monotonic is None:
            return None
        return max(0.0, self.finished_at_monotonic - self.started_at_monotonic)

    @property
    def valid_audio(self) -> bool:
        return self.chunk_count >= MIN_VALID_AUDIO_CHUNKS or (
            self.first_chunk_event.is_set()
            and self.byte_count >= MIN_VALID_AUDIO_BYTES
        )

    @property
    def avg_gap_ms(self) -> float:
        if self.gap_count <= 0:
            return 0.0
        return self.total_gap_ms / float(self.gap_count)


class AudioConfirmation:
    def __init__(self) -> None:
        self._active_speech_id: str | None = None
        self._trackings: dict[str, _TrackingState] = {}
        self._timed_out_speech_ids: set[str] = set()
        self._expired_speech_ids: set[str] = set()
        self._on_first_chunk: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None
        self._on_chunk: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None
        self._on_finished: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None
        self._on_first_chunk_warn: Callable[[str, str], Awaitable[None] | None] | None = None
        self._warn_fired: dict[str, bool] = {}
        self._hooks_installed = False
        self._chunk_log_count = 0
        self._CHUNK_LOG_EVERY_N = max(int(os.getenv("KIRA_AUDIO_CHUNK_PROGRESS_EVERY", "200")), 1)
        self._gap_tolerated_logged = False
        self._last_max_gap_ms = 0.0
        self._last_lag_max_ms = 0.0
        self._last_speech_had_gap = False
        self._adaptive_smoothing_level = 0
        self._current_smoothing_buffer_ms = VOICE_SMOOTHING_NORMAL_BUFFER_MS
        self._current_high_gap_detected = False
        self._current_segments_total = 1
        self._last_had_first_chunk = False
        # Hora (monotônica) do último pedaço de áudio da Kira que tocou, rastreado ou não: prova de que a voz funciona.
        self._last_audio_frame_at = 0.0
        self._bypass_confirmation_when_vision_active = _env_flag(
            "KIRA_BYPASS_AUDIO_CONFIRMATION_WITH_VISION",
            default=True,
        )
        self._soft_fail_when_vision_active = _env_flag(
            "KIRA_SOFT_FAIL_AUDIO_CONFIRMATION_WITH_VISION",
            default=True,
        )
        logger.info(
            "AUDIO_CONF_FIRST_CHUNK_TIMEOUT_CONFIG timeout_s=%.1f vision_timeout_s=%.1f",
            FIRST_CHUNK_TIMEOUT_SECONDS,
            VISION_FIRST_CHUNK_TIMEOUT_SECONDS,
        )

    def is_vision_active(self) -> bool:
        frame_store = get_vision_frame_store()
        if frame_store is None:
            return False
        try:
            status = frame_store.get_status()
        except Exception as exc:
            logger.debug("[AUDIO_CONF] vision_status_failed detail=%s", exc)
            return False
        return str(status.get("active_source") or "none").strip().lower() != "none"

    def get_first_chunk_timeout_seconds(self, vision_active: bool = False) -> float:
        if vision_active or self.is_vision_active():
            return VISION_FIRST_CHUNK_TIMEOUT_SECONDS
        return FIRST_CHUNK_HARD_TIMEOUT_S

    def get_valid_audio_timeout_seconds(self) -> float:
        if self.is_vision_active():
            return VISION_SILENCE_TIMEOUT_SECONDS
        return SILENCE_TIMEOUT_SECONDS

    def should_bypass_confirmation(self) -> bool:
        return self._bypass_confirmation_when_vision_active and self.is_vision_active()

    def should_soft_fail_confirmation(self) -> bool:
        return self._soft_fail_when_vision_active and self.is_vision_active()

    def set_callbacks(
        self,
        *,
        on_first_chunk: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None,
        on_chunk: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None,
        on_finished: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None = None,
        on_first_chunk_warn: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._on_first_chunk = on_first_chunk
        self._on_chunk = on_chunk
        self._on_finished = on_finished
        self._on_first_chunk_warn = on_first_chunk_warn

    def install_livekit_hooks(self) -> dict[str, str]:
        if self._hooks_installed:
            return {
                "generation_hook": "livekit.agents.voice.generation.perform_audio_forwarding",
                "output_hook": "livekit.agents.voice.room_io._output._ParticipantAudioOutput.capture_frame",
            }

        from livekit.agents.voice import generation as voice_generation
        from livekit.agents.voice.room_io import _output as room_output

        original_perform_audio_forwarding = voice_generation.perform_audio_forwarding
        original_capture_frame = room_output._ParticipantAudioOutput.capture_frame
        confirmation = self

        if not getattr(original_perform_audio_forwarding, "_kira_audio_confirmation_patched", False):
            def _wrapped_perform_audio_forwarding(*args, **kwargs):
                logger.info("[AUDIO_CONF] perform_audio_forwarding_start")
                try:
                    task, out = original_perform_audio_forwarding(*args, **kwargs)
                except Exception as exc:
                    logger.critical(
                        "[AUDIO_CONF] perform_audio_forwarding_crash_prevented reason=%s",
                        exc,
                        exc_info=True,
                    )

                    async def _failed_forwarding_task() -> None:
                        return None

                    loop = asyncio.get_running_loop()
                    task = loop.create_task(_failed_forwarding_task())
                    return task, None
                audio_output = kwargs.get("audio_output")
                if audio_output is None and args:
                    audio_output = args[0]

                def _on_started(ev) -> None:
                    active = confirmation._get_active_tracking()
                    logger.info(
                        "AUDIO_OUTPUT_START_CORRELATION has_active_generation=%s active_speech_id=%s event_speech_id=%s is_real_audio=true",
                        str(active is not None).lower(),
                        active.speech_id if active is not None else "none",
                        str(getattr(ev, "speech_id", "") or getattr(ev, "id", "") or "none"),
                    )
                    confirmation.record_playback_started(created_at=float(ev.created_at))

                def _on_finished(ev) -> None:
                    confirmation.record_playback_finished(
                        playback_position=float(getattr(ev, "playback_position", 0.0) or 0.0),
                        interrupted=bool(getattr(ev, "interrupted", False)),
                    )

                if audio_output is not None and callable(getattr(audio_output, "on", None)):
                    audio_output.on("playback_started", _on_started)
                    audio_output.on("playback_finished", _on_finished)

                    def _cleanup(_task: asyncio.Task) -> None:
                        try:
                            audio_output.off("playback_started", _on_started)
                            audio_output.off("playback_finished", _on_finished)
                        except Exception:
                            pass

                    task.add_done_callback(_cleanup)
                def _log_forwarding_done(_task: asyncio.Task) -> None:
                    try:
                        exc = _task.exception()
                    except asyncio.CancelledError:
                        logger.info("[AUDIO_CONF] perform_audio_forwarding_cancelled")
                        return
                    except Exception as done_exc:
                        logger.warning(
                            "[AUDIO_CONF] perform_audio_forwarding_done_check_failed reason=%s",
                            done_exc,
                            exc_info=True,
                        )
                        return
                    if exc is not None:
                        logger.error(
                            "[AUDIO_CONF] perform_audio_forwarding_task_failed reason=%s",
                            exc,
                            exc_info=True,
                        )
                    else:
                        logger.info("[AUDIO_CONF] perform_audio_forwarding_done")

                task.add_done_callback(_log_forwarding_done)
                return task, out

            _wrapped_perform_audio_forwarding._kira_audio_confirmation_patched = True  # type: ignore[attr-defined]
            voice_generation.perform_audio_forwarding = _wrapped_perform_audio_forwarding

        if not getattr(original_capture_frame, "_kira_audio_confirmation_patched", False):
            async def _wrapped_capture_frame(output_self, frame):
                active = confirmation._get_active_tracking()
                if active is not None and not confirmation.on_audio_chunk_received(active.speech_id):
                    return
                next_chunk_index = (active.chunk_count + 1) if active is not None else 0
                should_log_frame = active is None or next_chunk_index == 1
                if should_log_frame:
                    frame_data = getattr(frame, "data", b"") or b""
                    logger.debug(
                        "AUDIO_FORWARDING_FRAME speech_id=%s frame_ms=%s bytes=%s",
                        active.speech_id if active is not None else "none",
                        str(getattr(frame, "duration", "") or getattr(frame, "samples_per_channel", "") or "unknown"),
                        len(frame_data),
                    )
                suppress_playout = bool(
                    active is not None
                    and str(active.source or "").strip().lower()
                    in {"post_restart_warmup", "preventive_rotation_warmup"}
                )
                if suppress_playout and should_log_frame:
                    logger.info(
                        "AUDIO_PLAYOUT_SUPPRESSED_FOR_INTERNAL_PROBE speech_id=%s source=%s",
                        active.speech_id,
                        active.source,
                    )
                else:
                    try:
                        await original_capture_frame(output_self, frame)
                    except Exception as exc:
                        logger.critical(
                            "[AUDIO_CONF] capture_frame_crash_prevented reason=%s",
                            exc,
                            exc_info=True,
                        )
                        return
                try:
                    if active is not None and should_log_frame:
                        logger.debug(
                            "AUDIO_CONFIRMATION_RECORD_CHUNK_CALL speech_id=%s chunk_index=%s",
                            active.speech_id,
                            next_chunk_index,
                        )
                    confirmation.record_output_chunk(frame)
                except Exception as exc:
                    logger.error(
                        "[AUDIO_CONF] record_output_chunk_failed reason=%s",
                        exc,
                        exc_info=True,
                    )

            _wrapped_capture_frame._kira_audio_confirmation_patched = True  # type: ignore[attr-defined]
            room_output._ParticipantAudioOutput.capture_frame = _wrapped_capture_frame

        self._hooks_installed = True
        logger.info(
            "[AUDIO_CONF] hooks_installed generation=perform_audio_forwarding output=_ParticipantAudioOutput.capture_frame"
        )
        return {
            "generation_hook": "livekit.agents.voice.generation.perform_audio_forwarding",
            "output_hook": "livekit.agents.voice.room_io._output._ParticipantAudioOutput.capture_frame",
        }

    def reinstall_livekit_hooks(self) -> dict[str, str]:
        logger.warning("VOICE_DEGRADATION_RECOVERY_REINSTALL_AUDIO_HOOKS")
        self._hooks_installed = False
        hooks = self.install_livekit_hooks()
        logger.info(
            "AUDIO_CONF_HOOKS_REINSTALLED generation=%s output=%s",
            hooks.get("generation_hook", "unknown"),
            hooks.get("output_hook", "unknown"),
        )
        return hooks

    def hooks_valid(self) -> bool:
        return bool(self._hooks_installed)

    def last_had_first_chunk(self) -> bool:
        return bool(self._last_had_first_chunk)

    def last_audio_frame_at(self) -> float:
        return float(self._last_audio_frame_at)

    def abort_first_chunk_wait(self, speech_id: str, *, reason: str) -> None:
        normalized_speech_id = str(speech_id or "").strip()
        if not normalized_speech_id:
            return
        self._timed_out_speech_ids.add(normalized_speech_id)
        self._expired_speech_ids.add(normalized_speech_id)
        self._release_active(normalized_speech_id)
        logger.warning(
            "FIRST_CHUNK_EARLY_ABORT speech_id=%s reason=%s",
            normalized_speech_id,
            str(reason or "unknown").strip() or "unknown",
        )

    def reset_for_gemini_restart(self, *, speech_id: str | None = None, reason: str = "gemini_session_restart") -> None:
        normalized_speech_id = str(speech_id or "").strip()
        reset_reason = str(reason or "gemini_session_restart").strip() or "gemini_session_restart"
        target_speech_id = normalized_speech_id or self._active_speech_id or ""
        if target_speech_id:
            self._timed_out_speech_ids.add(target_speech_id)
            self._expired_speech_ids.add(target_speech_id)
            tracking = self._trackings.get(target_speech_id)
            if tracking is not None and not tracking.finished_event.is_set():
                tracking.interrupted = True
                tracking.finished_at_monotonic = time.monotonic()
                tracking.finished_event.set()
        if self._active_speech_id == target_speech_id or not normalized_speech_id:
            self._active_speech_id = None
        self._warn_fired.clear()
        self._chunk_log_count = 0
        self._gap_tolerated_logged = False
        self._current_high_gap_detected = False
        self._current_segments_total = 1
        self._last_had_first_chunk = False
        logger.warning(
            "GEMINI_SESSION_RESTART_AUDIO_CONFIRMATION_RESET speech_id=%s reason=%s",
            target_speech_id or "none",
            reset_reason,
        )

    def start_tracking(self, *, speech_id: str, source: str) -> None:
        active_speech_id = self._active_speech_id
        if active_speech_id and active_speech_id != speech_id:
            previous = self._trackings.get(active_speech_id)
            if previous is not None and not previous.finished_event.is_set():
                logger.info(
                    "VOICE_OUTPUT_TRACKING_REUSED reason=new_speech_started previous_speech_id=%s new_speech_id=%s",
                    active_speech_id,
                    speech_id,
                )
                previous.interrupted = True
                previous.finished_at_monotonic = time.monotonic()
                previous.finished_event.set()
        tracking = _TrackingState(
            speech_id=speech_id,
            source=source,
            started_at_monotonic=time.monotonic(),
            started_at_wallclock=time.time(),
        )
        self._trackings[speech_id] = tracking
        self._active_speech_id = speech_id
        self._chunk_log_count = 0
        self._gap_tolerated_logged = False
        self._current_high_gap_detected = False
        self._current_segments_total = 1
        if self._last_speech_had_gap:
            self._adaptive_smoothing_level = 1
            self._current_smoothing_buffer_ms = min(VOICE_SMOOTHING_INCREASED_BUFFER_MS, 300)
            logger.info(
                "VOICE_ADAPTIVE_SMOOTHING_INCREASED previous_gap_ms=%.0f buffer_ms=%s",
                self._last_max_gap_ms,
                self._current_smoothing_buffer_ms,
            )
        else:
            self._adaptive_smoothing_level = 0
            self._current_smoothing_buffer_ms = VOICE_SMOOTHING_NORMAL_BUFFER_MS
            logger.info("VOICE_ADAPTIVE_SMOOTHING_NORMAL buffer_ms=%s", self._current_smoothing_buffer_ms)
        self._expired_speech_ids.discard(speech_id)
        self._warn_fired.pop(speech_id, None)
        logger.info("[AUDIO_CONF] tracking_started speech_id=%s source=%s", speech_id, source)
        logger.info("VOICE_AUDIO_SMOOTHING_APPLIED buffer_ms=%s", self._current_smoothing_buffer_ms)
        logger.info("VOICE_AUDIO_SMOOTHING_ENABLED buffer_ms=%s tolerated_gap_ms=%.0f", self._current_smoothing_buffer_ms, AUDIO_CHUNK_GAP_TOLERATED_MS)
        logger.info(
            "VOICE_OUTPUT_TRACKING_STARTED speech_id=%s response_id=%s source=%s",
            speech_id,
            speech_id,
            source,
        )

    def set_current_segments_total(self, total: int) -> None:
        try:
            self._current_segments_total = max(int(total), 1)
        except Exception:
            self._current_segments_total = 1

    def record_output_chunk(self, frame) -> None:
        self._last_audio_frame_at = time.monotonic()
        tracking = self._get_active_tracking()
        if tracking is None:
            return
        size = len(getattr(frame, "data", b"") or b"")
        _now = time.monotonic()
        if tracking.last_chunk_at_monotonic is not None:
            gap_ms = max(0.0, (_now - tracking.last_chunk_at_monotonic) * 1000.0)
            tracking.last_gap_ms = gap_ms
            tracking.max_gap_ms = max(tracking.max_gap_ms, gap_ms)
            tracking.total_gap_ms += gap_ms
            tracking.gap_count += 1
            self._maybe_log_chunk_gap(tracking, gap_ms)
        tracking.chunk_count += 1
        self._chunk_log_count = tracking.chunk_count
        tracking.byte_count += size
        tracking.last_chunk_at_monotonic = _now
        tracking.last_chunk_size = size
        if tracking.chunk_count <= MIN_VALID_AUDIO_CHUNKS:
            logger.debug(
                "[AUDIO_CONF] AUDIO_CHUNK_COUNT speech_id=%s chunks=%s bytes=%s",
                tracking.speech_id,
                tracking.chunk_count,
                tracking.byte_count,
            )
        if tracking.chunk_count == 1:
            logger.debug(
                "AUDIO_CONFIRMATION_CHUNK_RECORDED speech_id=%s chunk_index=%s first=%s delta_ms=%.0f",
                tracking.speech_id,
                tracking.chunk_count,
                str(tracking.chunk_count == 1).lower(),
                (_now - tracking.started_at_monotonic) * 1000.0,
            )
        if self._chunk_log_count % self._CHUNK_LOG_EVERY_N == 0:
            tracking._last_progress_log_at = _now
            logger.info(
                "AUDIO_CHUNK_PROGRESS speech_id=%s count=%s age_ms=%.0f last_gap_ms=%s max_gap_ms=%.0f avg_gap_ms=%.0f",
                tracking.speech_id,
                self._chunk_log_count,
                (_now - tracking.started_at_monotonic) * 1000.0,
                f"{tracking.last_gap_ms:.0f}" if tracking.last_gap_ms is not None else "none",
                tracking.max_gap_ms,
                tracking.avg_gap_ms,
            )
        try:
            get_voice_output_manager().on_audio_chunk(tracking.speech_id, time.time())
        except Exception:
            logger.warning("VOICE_OUTPUT_MANAGER_AUDIO_EVENT_UNMATCHED event=chunk speech_id=%s", tracking.speech_id)
        self._fire_callback(
            self._on_chunk,
            AudioConfirmationEvent(
                speech_id=tracking.speech_id,
                source=tracking.source,
                chunk_count=tracking.chunk_count,
                byte_count=tracking.byte_count,
                latency_seconds=tracking.latency_seconds,
                duration_seconds=None,
                interrupted=False,
                valid_audio=tracking.valid_audio,
            ),
        )
        self._mark_first_chunk(
            tracking,
            created_at=None,
            trigger="output_chunk",
        )

    def _maybe_log_chunk_gap(self, tracking: _TrackingState, gap_ms: float) -> None:
        if gap_ms >= AUDIO_HIGH_GAP_MS and not self._current_high_gap_detected:
            self._current_high_gap_detected = True
            logger.warning(
                "VOICE_HIGH_GAP_DETECTED gap_ms=%.0f speech_id=%s",
                gap_ms,
                tracking.speech_id,
            )
        if gap_ms < AUDIO_CHUNK_GAP_TOLERATED_MS:
            return
        if gap_ms < AUDIO_CHUNK_GAP_WARN_MS:
            if not self._gap_tolerated_logged:
                self._gap_tolerated_logged = True
                logger.info(
                    "VOICE_AUDIO_GAP_TOLERATED speech_id=%s gap_ms=%.0f",
                    tracking.speech_id,
                    gap_ms,
                )
            return
        if gap_ms >= AUDIO_CHUNK_GAP_STUTTER_MS:
            if not tracking.stutter_logged:
                tracking.stutter_logged = True
                queue_size = "unknown"
                state = "unknown"
                try:
                    manager_snapshot = get_voice_output_manager().snapshot()
                    queue_size = str(manager_snapshot.get("queue_size", "unknown"))
                    state = str(manager_snapshot.get("state", "unknown"))
                except Exception:
                    pass
                logger.warning(
                    "VOICE_AUDIO_STUTTER_DETECTED speech_id=%s gap_ms=%.0f chunk_count=%s state=%s queue=%s",
                    tracking.speech_id,
                    gap_ms,
                    tracking.chunk_count + 1,
                    state,
                    queue_size,
                )
                self._log_resource_snapshot(tracking)
            return
        if gap_ms >= AUDIO_CHUNK_GAP_WARN_MS and not tracking.gap_warn_logged:
            tracking.gap_warn_logged = True
            logger.warning(
                "VOICE_AUDIO_CHUNK_GAP_WARN speech_id=%s gap_ms=%.0f chunk_count=%s source=%s",
                tracking.speech_id,
                gap_ms,
                tracking.chunk_count + 1,
                tracking.source,
            )

    def _log_resource_snapshot(self, tracking: _TrackingState) -> None:
        cpu_proc: object = "unavailable"
        cpu_total: object = "unavailable"
        ram_proc_mb: object = "unavailable"
        ram_total_pct: object = "unavailable"
        try:
            import psutil  # type: ignore

            proc = psutil.Process(os.getpid())
            cpu_proc = round(float(proc.cpu_percent(interval=None)), 1)
            cpu_total = round(float(psutil.cpu_percent(interval=None)), 1)
            ram_proc_mb = round(float(proc.memory_info().rss) / (1024.0 * 1024.0), 1)
            ram_total_pct = round(float(psutil.virtual_memory().percent), 1)
        except Exception:
            pass
        try:
            manager_snapshot = get_voice_output_manager().snapshot()
        except Exception:
            manager_snapshot = {}
        logger.warning(
            "VOICE_STUTTER_RESOURCE_SNAPSHOT speech_id=%s cpu_proc=%s cpu_total=%s ram_proc_mb=%s ram_total_pct=%s threads=%s queue=%s current_source=%s",
            tracking.speech_id,
            cpu_proc,
            cpu_total,
            ram_proc_mb,
            ram_total_pct,
            threading.active_count(),
            manager_snapshot.get("queue_size", "unknown") if isinstance(manager_snapshot, dict) else "unknown",
            tracking.source,
        )

    def record_playback_started(self, *, created_at: float) -> None:
        tracking = self._get_active_tracking()
        if tracking is None:
            return
        self._mark_first_chunk(
            tracking,
            created_at=created_at,
            trigger="playback_started",
        )

    def _mark_first_chunk(
        self,
        tracking: _TrackingState,
        *,
        created_at: float | None,
        trigger: str,
    ) -> None:
        if tracking.first_chunk_event.is_set():
            if tracking.first_chunk_created_at is None and created_at is not None:
                tracking.first_chunk_created_at = created_at
            return
        tracking.first_chunk_at_monotonic = time.monotonic()
        tracking.first_chunk_created_at = created_at
        tracking.first_chunk_event.set()
        self._last_had_first_chunk = True
        latency_seconds = tracking.latency_seconds
        if tracking.speech_id in self._timed_out_speech_ids:
            logger.warning(
                "AUDIO_CONF_LATE_FIRST_CHUNK_AFTER_TIMEOUT speech_id=%s latency=%.2fs via=%s state=late_arrival",
                tracking.speech_id,
                latency_seconds or 0.0,
                trigger,
            )
            logger.warning("LATE_AUDIO_CHUNK_AFTER_RECOVERY_IGNORED speech_id=%s", tracking.speech_id)
            return
        logger.debug(
            "[AUDIO_CONF] AUDIO_FIRST_CHUNK_OUT speech_id=%s latency=%.2fs size=%s via=%s",
            tracking.speech_id,
            latency_seconds or 0.0,
            tracking.last_chunk_size,
            trigger,
        )
        logger.info(
            "VOICE_OUTPUT_FIRST_CHUNK logical_id=%s latency_ms=%.0f via=%s",
            tracking.speech_id,
            (latency_seconds or 0.0) * 1000.0,
            trigger,
        )
        try:
            get_voice_output_manager().on_first_chunk(tracking.speech_id, tracking.first_chunk_created_at or time.time())
        except Exception:
            logger.warning("VOICE_OUTPUT_MANAGER_AUDIO_EVENT_UNMATCHED event=first_chunk speech_id=%s", tracking.speech_id)
        self._fire_callback(
            self._on_first_chunk,
            AudioConfirmationEvent(
                speech_id=tracking.speech_id,
                source=tracking.source,
                chunk_count=tracking.chunk_count,
                byte_count=tracking.byte_count,
                latency_seconds=latency_seconds,
                duration_seconds=None,
                interrupted=False,
                valid_audio=tracking.valid_audio,
            ),
        )

    def record_playback_finished(self, *, playback_position: float, interrupted: bool) -> None:
        tracking = self._get_active_tracking()
        if tracking is None or tracking.finished_event.is_set():
            return
        self._complete_tracking(
            tracking,
            playback_position=playback_position,
            interrupted=interrupted,
        )

    def backfill_playback_finished(
        self,
        speech_id: str,
        *,
        playback_position: float | None = None,
        interrupted: bool = False,
        reason: str = "reply_finish_without_voice_output_finished",
    ) -> bool:
        tracking = self._trackings.get(str(speech_id or "").strip())
        if tracking is None or tracking.finished_event.is_set():
            return False
        logger.info(
            "VOICE_OUTPUT_FINISH_BACKFILLED_FROM_REPLY_FINISH speech_id=%s reason=%s",
            tracking.speech_id,
            reason,
        )
        self._complete_tracking(
            tracking,
            playback_position=(
                tracking.playback_position_seconds
                if playback_position is None
                else float(playback_position)
            ),
            interrupted=interrupted,
        )
        return True

    def _complete_tracking(
        self,
        tracking: _TrackingState,
        *,
        playback_position: float,
        interrupted: bool,
    ) -> None:
        if tracking.finished_event.is_set():
            return
        tracking.finished_at_monotonic = time.monotonic()
        tracking.playback_position_seconds = playback_position
        tracking.interrupted = interrupted
        tracking.finished_event.set()
        logger.debug(
            "[AUDIO_CONF] AUDIO_FINISHED speech_id=%s chunks=%s duration=%.2fs valid=%s interrupted=%s",
            tracking.speech_id,
            tracking.chunk_count,
            tracking.duration_seconds or 0.0,
            str(tracking.valid_audio).lower(),
            str(interrupted).lower(),
        )
        logger.info(
            "VOICE_OUTPUT_FINISHED logical_id=%s chunks=%s total_ms=%.0f interrupted=%s max_gap_ms=%.0f avg_gap_ms=%.0f",
            tracking.speech_id,
            tracking.chunk_count,
            (tracking.duration_seconds or 0.0) * 1000.0,
            str(interrupted).lower(),
            tracking.max_gap_ms,
            tracking.avg_gap_ms,
        )
        try:
            priority_status = get_voice_output_manager().get_speech_priority_status()
        except Exception:
            priority_status = {}
        lag_max_ms = float(priority_status.get("lag_max_ms") or 0.0) if isinstance(priority_status, dict) else 0.0
        max_gap_ms = float(tracking.max_gap_ms)
        if max_gap_ms > 700.0 or lag_max_ms > 700.0:
            speech_health = "bad"
        elif max_gap_ms >= 300.0 or lag_max_ms >= 300.0:
            speech_health = "warn"
        else:
            speech_health = "good"
        if speech_health == "good" and self._last_speech_had_gap:
            logger.info("VOICE_ADAPTIVE_SMOOTHING_RESET reason=healthy_speech")
        self._last_max_gap_ms = max_gap_ms
        self._last_lag_max_ms = lag_max_ms
        self._last_speech_had_gap = bool(max_gap_ms > 500.0 or lag_max_ms > 500.0)
        logger.info(
            "VOICE_SPEECH_HEALTH_SUMMARY speech_id=%s total_ms=%.0f chunks=%s max_gap_ms=%.0f lag_max_ms=%.0f deferred_tasks=%s priority_mode=%s smoothing_buffer_ms=%s segments_total=%s speech_health=%s",
            tracking.speech_id,
            (tracking.duration_seconds or 0.0) * 1000.0,
            tracking.chunk_count,
            max_gap_ms,
            lag_max_ms,
            int(priority_status.get("deferred_tasks") or 0) if isinstance(priority_status, dict) else 0,
            str(bool(priority_status.get("active")) if isinstance(priority_status, dict) else False).lower(),
            self._current_smoothing_buffer_ms,
            self._current_segments_total,
            speech_health,
        )
        if self._current_high_gap_detected:
            logger.info("VOICE_CONTINUATION_SKIPPED reason=no_pending_text speech_id=%s", tracking.speech_id)
        logger.info(
            "VOICE_OUTPUT_TRACKING_FINISHED speech_id=%s interrupted=%s",
            tracking.speech_id,
            str(interrupted).lower(),
        )
        try:
            get_voice_output_manager().on_finished(tracking.speech_id, time.time())
        except Exception:
            logger.warning("VOICE_OUTPUT_MANAGER_AUDIO_EVENT_UNMATCHED event=finished speech_id=%s", tracking.speech_id)
        self._fire_callback(
            self._on_finished,
            AudioConfirmationEvent(
                speech_id=tracking.speech_id,
                source=tracking.source,
                chunk_count=tracking.chunk_count,
                byte_count=tracking.byte_count,
                latency_seconds=tracking.latency_seconds,
                duration_seconds=tracking.duration_seconds,
                interrupted=interrupted,
                valid_audio=tracking.valid_audio,
            ),
        )
        if self._active_speech_id == tracking.speech_id:
            self._active_speech_id = None

    async def wait_for_first_chunk(self, speech_id: str, *, timeout_seconds: float = FIRST_CHUNK_TIMEOUT_SECONDS, source: str = "unknown") -> bool:
        if not str(speech_id or "").strip() or str(speech_id).strip().lower() == "none":
            logger.warning("GENERATE_REPLY_NO_GENERATION_CREATED_CLEANUP speech_id=%s", speech_id or "none")
            return False
        tracking = self._trackings.get(speech_id)
        if tracking is None:
            return False
        wait_started_at = time.monotonic()
        logger.info(
            "AUDIOCONF_WAIT_FIRST_CHUNK_BEGIN speech_id=%s source=%s timeout_s=%.1f",
            speech_id,
            source,
            max(timeout_seconds, 0.1),
        )
        wait_result = False
        if tracking.first_chunk_event.is_set():
            latency = tracking.latency_seconds or 0.0
            logger.info(
                "AUDIO_CONF_FIRST_CHUNK_WAIT_OK speech_id=%s latency=%.2fs already_ready=true",
                speech_id,
                latency,
            )
            wait_result = True
        else:
            started_at = time.monotonic()
            warn_timeout = FIRST_CHUNK_WARN_TIMEOUT_S
            hard_timeout = max(timeout_seconds, 0.1)
            logger.info("AUDIO_CONF_FIRST_CHUNK_WAIT_START speech_id=%s timeout_s=%.1f", speech_id, hard_timeout)
            while True:
                if tracking.first_chunk_event.is_set():
                    latency = tracking.latency_seconds or 0.0
                    logger.info("AUDIO_CONF_FIRST_CHUNK_WAIT_OK speech_id=%s latency=%.2fs", speech_id, latency)
                    wait_result = True
                    break
                elapsed = time.monotonic() - started_at
                if speech_id in self._timed_out_speech_ids:
                    logger.warning("AUDIO_CONF_FIRST_CHUNK_WAIT_ABORTED speech_id=%s elapsed=%.2fs", speech_id, elapsed)
                    break
                if elapsed >= hard_timeout:
                    self._timed_out_speech_ids.add(speech_id)
                    self._expired_speech_ids.add(speech_id)
                    self._release_active(speech_id)
                    logger.error("FIRST_CHUNK_HARD_TIMEOUT speech_id=%s elapsed=%.2fs", speech_id, elapsed)
                    break
                if elapsed >= warn_timeout and not self._warn_fired.get(speech_id):
                    self._warn_fired[speech_id] = True
                    logger.warning("FIRST_CHUNK_WARN speech_id=%s elapsed=%.2fs", speech_id, elapsed)
                    if self._on_first_chunk_warn is not None:
                        try:
                            maybe = self._on_first_chunk_warn(speech_id, source)
                            if asyncio.iscoroutine(maybe):
                                await maybe
                        except Exception:
                            pass
                await asyncio.sleep(0.1)
        logger.info(
            "AUDIOCONF_WAIT_FIRST_CHUNK_END speech_id=%s elapsed_ms=%.0f result=%s",
            speech_id,
            (time.monotonic() - wait_started_at) * 1000.0,
            str(bool(wait_result)).lower(),
        )
        return wait_result

    async def wait_until_valid_or_finished(
        self,
        speech_id: str,
        *,
        timeout_seconds: float = SILENCE_TIMEOUT_SECONDS,
    ) -> bool:
        tracking = self._trackings.get(speech_id)
        if tracking is None:
            return False
        wait_started_at = time.monotonic()
        logger.info(
            "AUDIOCONF_VALIDATION_BEGIN speech_id=%s timeout_s=%.1f",
            speech_id,
            max(timeout_seconds, 0.1),
        )
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        result = False
        while time.monotonic() < deadline:
            if tracking.valid_audio:
                result = True
                break
            if tracking.finished_event.is_set():
                result = tracking.valid_audio
                break
            await asyncio.sleep(0.05)
        else:
            result = tracking.valid_audio
        logger.info(
            "AUDIOCONF_VALIDATION_END speech_id=%s elapsed_ms=%.0f result=%s",
            speech_id,
            (time.monotonic() - wait_started_at) * 1000.0,
            str(bool(result)).lower(),
        )
        return result

    def is_audio_valid(self, speech_id: str) -> bool:
        tracking = self._trackings.get(speech_id)
        if tracking is None:
            return False
        return tracking.valid_audio

    def is_audio_real(self, speech_id: str) -> bool:
        return self.is_audio_valid(speech_id)

    def on_audio_chunk_received(self, speech_id: str) -> bool:
        if speech_id in self._expired_speech_ids:
            logger.warning(
                "AUDIO_CHUNK_DISCARDED_EXPIRED speech_id=%s — chunk tardio ignorado",
                speech_id,
            )
            return False
        return True

    def get_status(self, speech_id: str | None = None) -> dict[str, object]:
        tracking = self._trackings.get(speech_id or self._active_speech_id or "")
        if tracking is None:
            return {
                "active_speech_id": self._active_speech_id,
                "tracking": None,
            }
        return {
            "active_speech_id": self._active_speech_id,
            "tracking": {
                "speech_id": tracking.speech_id,
                "source": tracking.source,
                "chunk_count": tracking.chunk_count,
                "byte_count": tracking.byte_count,
                "latency_seconds": round(tracking.latency_seconds or 0.0, 3) if tracking.latency_seconds is not None else None,
                "duration_seconds": round(tracking.duration_seconds or 0.0, 3) if tracking.duration_seconds is not None else None,
                "last_chunk_age_seconds": (
                    round(max(0.0, time.monotonic() - tracking.last_chunk_at_monotonic), 3)
                    if tracking.last_chunk_at_monotonic is not None
                    else None
                ),
                "last_gap_ms": round(tracking.last_gap_ms or 0.0, 1) if tracking.last_gap_ms is not None else None,
                "max_gap_ms": round(tracking.max_gap_ms, 1),
                "avg_gap_ms": round(tracking.avg_gap_ms, 1),
                "high_gap_detected": self._current_high_gap_detected,
                "smoothing_buffer_ms": self._current_smoothing_buffer_ms,
                "first_chunk_seen": tracking.first_chunk_event.is_set(),
                "finished": tracking.finished_event.is_set(),
                "interrupted": tracking.interrupted,
                "valid_audio": tracking.valid_audio,
                "playback_position_seconds": round(tracking.playback_position_seconds or 0.0, 3),
            },
        }

    def _release_active(self, speech_id: str) -> None:
        """Fala que não começou e foi abandonada deixa de ser a ativa. Se ficasse, todo áudio seguinte (respostas novas
        do Gemini) era descartado como "chunk tardio" dela e a Kira ficava muda (sessão de 2026-10-24 12:33)."""
        if self._active_speech_id == speech_id:
            self._active_speech_id = None
            logger.warning("AUDIO_CONF_ACTIVE_RELEASED speech_id=%s reason=expired", speech_id)

    def _get_active_tracking(self) -> _TrackingState | None:
        active_speech_id = self._active_speech_id
        if not active_speech_id:
            return None
        return self._trackings.get(active_speech_id)

    def _fire_callback(
        self,
        callback: Callable[[AudioConfirmationEvent], Awaitable[None] | None] | None,
        event: AudioConfirmationEvent,
    ) -> None:
        if callback is None:
            return
        try:
            maybe_result = callback(event)
            if asyncio.iscoroutine(maybe_result):
                asyncio.create_task(maybe_result, name=f"kira-audio-confirmation-{event.speech_id}")
        except Exception as exc:
            logger.debug("[AUDIO_CONF] callback_failed speech_id=%s detail=%s", event.speech_id, exc)


_AUDIO_CONFIRMATION_SINGLETON: AudioConfirmation | None = None


def get_audio_confirmation() -> AudioConfirmation:
    global _AUDIO_CONFIRMATION_SINGLETON
    if _AUDIO_CONFIRMATION_SINGLETON is None:
        _AUDIO_CONFIRMATION_SINGLETON = AudioConfirmation()
    return _AUDIO_CONFIRMATION_SINGLETON
