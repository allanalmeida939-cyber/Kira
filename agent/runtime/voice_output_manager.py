from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


TEXT_DEDUPE_WINDOW_S = 5.0


class VoiceOutputSource(str, Enum):
    NORMAL_REPLY = "normal_reply"
    COMMAND_ACK = "command_ack"
    WHATSAPP_TOOL = "whatsapp_tool"
    WHATSAPP_NOTIFICATION = "whatsapp_notification"
    REMINDER = "reminder"
    PROACTIVE = "proactive"
    STT_RETRY_PROMPT = "stt_retry_prompt"
    SYSTEM_WARNING = "system_warning"
    MUSIC_ANNOUNCEMENT = "music_announcement"
    VISION_RESPONSE = "vision_response"
    TOOL_ACK = "tool_ack"
    BROWSER = "browser"
    CREATION_TASK_ACK = "creation_task_ack"
    MANUS_COMPLETION_NOTIFY = "manus_completion_notify"
    WAKE_ACK_CACHE = "wake_ack_cache"
    UNKNOWN = "unknown"


class VoiceOutputState(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RESERVED = "reserved"
    SPEAKING = "speaking"
    DRAINING = "draining"
    FINISHED = "finished"
    CANCELING = "canceling"
    ERROR = "error"


class VoiceOutputPriority:
    VOICE_PRIORITY_EMERGENCY = 0
    VOICE_PRIORITY_USER_REPLY = 1
    VOICE_PRIORITY_COMMAND_ACK = 2
    VOICE_PRIORITY_TOOL_ACK = 3
    VOICE_PRIORITY_IMPORTANT_NOTIFICATION = 4
    VOICE_PRIORITY_NOTIFICATION = 5
    VOICE_PRIORITY_CASUAL = 6
    VOICE_PRIORITY_PROACTIVE = 7
    VOICE_PRIORITY_LOW = 8


@dataclass(order=True)
class VoiceOutputRequest:
    priority: int
    created_at: float = field(default_factory=time.time, compare=True)

    request_id: str = field(default_factory=lambda: f"voice_{uuid.uuid4().hex}", compare=False)
    source: str = field(default=VoiceOutputSource.UNKNOWN.value, compare=False)
    text: str = field(default="", compare=False)

    turn_id: str | None = field(default=None, compare=False)
    response_id: str | None = field(default=None, compare=False)
    dedupe_key: str | None = field(default=None, compare=False)

    exact_text: bool = field(default=False, compare=False)
    can_interrupt: bool = field(default=False, compare=False)
    must_finish: bool = field(default=True, compare=False)
    allow_barge_in: bool = field(default=False, compare=False)

    user_initiated: bool = field(default=False, compare=False)
    may_merge_with_next: bool = field(default=True, compare=False)
    timeout_s: float = field(default=30.0, compare=False)

    metadata: dict[str, Any] = field(default_factory=dict, compare=False)
    result_fut: asyncio.Future["VoiceOutputResult"] | None = field(default=None, compare=False)


@dataclass
class VoiceOutputResult:
    ok: bool
    request_id: str
    source: str
    state: str
    error: str | None = None
    started_at: float | None = None
    first_chunk_at: float | None = None
    finished_at: float | None = None
    chunks: int = 0


class VoiceOutputManager:
    def __init__(self, speaker: Callable[[VoiceOutputRequest], Awaitable[VoiceOutputResult]] | None = None):
        self._queue: asyncio.PriorityQueue[VoiceOutputRequest] = asyncio.PriorityQueue()
        self._speaker = speaker
        self._state = VoiceOutputState.IDLE
        self._current: VoiceOutputRequest | None = None
        self._current_started_at: float | None = None
        self._last_chunk_at: float | None = None
        self._last_finished_at: float | None = None
        self._current_chunk_count = 0
        self._dedupe_recent: dict[str, float] = {}
        self._text_dedupe_recent: dict[str, float] = {}
        self._dedupe_ttl_s = max(float(os.getenv("KIRA_VOICE_DEDUPE_TTL_SECONDS", "15.0")), 1.0)
        self._user_input_active = False
        self._agent_speaking = False
        self._runner_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._mode = str(os.getenv("KIRA_VOICE_OUTPUT_MANAGER_MODE", "partial")).strip().lower() or "partial"
        self._authorized_generations: set[str] = set()
        self._blocked_unowned_count = 0
        self._shadow_would_block_count = 0
        self._last_started_request: VoiceOutputRequest | None = None
        self._last_started_at: float | None = None
        self._last_finished_request_id: str | None = None
        self._last_started_speech_id: str | None = None
        self._last_started_turn_id: str | None = None
        self._last_started_source: str | None = None
        self._last_finished_speech_id: str | None = None
        self._recent_speech_history: deque[dict[str, Any]] = deque(maxlen=5)
        self._external_audio_count = 0
        self._speech_aliases: dict[str, str] = {}
        self._speech_alias_sets: dict[str, set[str]] = {}
        self._last_snapshot_log_at: float = 0.0
        self._snapshot_log_min_interval_s: float = 5.0
        self._speech_priority_mode = False
        self._speech_priority_started_at: float | None = None
        self._suppressed_speaking_lag_count = 0
        self._suppressed_speaking_lag_max_ms = 0.0
        self._deferred_tasks_after_speech: list[tuple[str, Callable[[], Any]]] = []
        self._last_deferred_flush_count = 0
        self._chain_gap_target_ms = 150
        self._owner_setter: Callable[[VoiceOutputRequest], None] | None = None
        self._owner_releaser: Callable[[VoiceOutputRequest | None], None] | None = None
        self._event_loop_monitor_task: asyncio.Task | None = None
        self._event_loop_lag_warn_ms = float(os.getenv("KIRA_VOICE_EVENT_LOOP_LAG_WARN_MS", "150"))
        self._event_loop_lag_critical_ms = float(os.getenv("KIRA_VOICE_EVENT_LOOP_LAG_CRITICAL_MS", "400"))
        self._event_loop_lag_interval_s = float(os.getenv("KIRA_VOICE_EVENT_LOOP_LAG_INTERVAL_MS", "250")) / 1000.0
        self._last_event_loop_pressure_log_at = 0.0
        self._event_loop_lag_consecutive_critical = 0
        self._mouse_input_clear_handle: asyncio.TimerHandle | None = None
        self._last_browser_block_log: dict[str, float] = {}
        self._safe_cleanup_counts: dict[str, int] = {}
        self._stale_cleanup_counts: dict[str, int] = {}
        logger.info("VOICE_OUTPUT_MANAGER_INIT mode=%s", self._mode)

    def _log_browser_queue_blocked(self, reason: str, request: VoiceOutputRequest | None = None) -> None:
        now = time.monotonic()
        normalized = str(reason or "unknown").strip().lower() or "unknown"
        phase = ""
        owner = "none"
        if request is not None:
            phase = str((request.metadata or {}).get("phase") or "").strip()
            owner = str(request.turn_id or request.request_id or "none").strip() or "none"
        key = f"{normalized}:{phase}:{owner}"
        if (now - float(self._last_browser_block_log.get(key) or 0.0)) < 1.5:
            return
        self._last_browser_block_log[key] = now
        if phase:
            logger.info("BROWSER_VOICE_QUEUE_BLOCKED reason=%s owner=%s phase=%s", normalized, owner, phase)
        else:
            logger.info("BROWSER_VOICE_QUEUE_BLOCKED reason=%s owner=%s", normalized, owner)

    def _resolve_speech_alias(self, speech_id: str | None) -> str:
        normalized = str(speech_id or "").strip()
        if not normalized:
            return ""
        return self._speech_aliases.get(normalized, normalized)

    def _make_provisional_speech_id(self, turn_id: str | None, source: str) -> str:
        normalized_turn_id = str(turn_id or "").strip() or "no_turn"
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        raw = f"provisional:{normalized_turn_id}:{normalized_source}"
        short = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"speech_{short}"

    def _should_log_snapshot(self) -> bool:
        now = time.monotonic()
        if self.is_speaking() or self._speech_priority_mode:
            if (now - self._last_snapshot_log_at) < self._snapshot_log_min_interval_s:
                return False
        self._last_snapshot_log_at = now
        return True

    def _note_suppressed_speaking_lag(self, lag_ms: float) -> None:
        self._suppressed_speaking_lag_count += 1
        self._suppressed_speaking_lag_max_ms = max(self._suppressed_speaking_lag_max_ms, lag_ms)

    def _enter_speech_priority_mode(self, speech_id: str | None) -> None:
        if self._speech_priority_mode:
            return
        self._speech_priority_mode = True
        self._speech_priority_started_at = time.monotonic()
        normalized = str(speech_id or "").strip() or "none"
        logger.info("VOICE_SPEECH_PRIORITY_ENTER speech_id=%s", normalized)
        logger.info("VOICE_SPEECH_PRIORITY_ACTIVE=true speech_id=%s", normalized)

    def _reset_speech_priority_mode(self, *, reason: str) -> None:
        was_active = self._speech_priority_mode
        speech_id = self._last_started_speech_id or self._last_finished_speech_id or "none"
        if self._suppressed_speaking_lag_count:
            logger.info(
                "VOICE_EVENT_LOOP_LAG_SPEAKING_SUMMARY suppressed_count=%s max_lag_ms=%.0f reason=%s",
                self._suppressed_speaking_lag_count,
                self._suppressed_speaking_lag_max_ms,
                reason,
            )
        self._speech_priority_mode = False
        self._speech_priority_started_at = None
        self._suppressed_speaking_lag_count = 0
        self._suppressed_speaking_lag_max_ms = 0.0
        if was_active:
            logger.info("VOICE_SPEECH_PRIORITY_EXIT speech_id=%s reason=%s", speech_id, reason)
            logger.info("VOICE_SPEECH_PRIORITY_ACTIVE=false speech_id=%s reason=%s", speech_id, reason)
            self._flush_deferred_tasks_after_speech()

    def is_speech_priority_active(self) -> bool:
        return bool(self._speech_priority_mode)

    def get_speech_priority_status(self) -> dict[str, object]:
        started_at = self._speech_priority_started_at
        elapsed_s = max(0.0, time.monotonic() - started_at) if started_at is not None else 0.0
        return {
            "active": bool(self._speech_priority_mode),
            "elapsed_s": elapsed_s,
            "lag_max_ms": float(self._suppressed_speaking_lag_max_ms),
            "deferred_tasks": len(self._deferred_tasks_after_speech),
            "last_deferred_flush_count": self._last_deferred_flush_count,
        }

    def defer_until_speech_done(self, name: str, factory: Callable[[], Any]) -> None:
        task_name = str(name or "deferred_task").strip() or "deferred_task"
        self._deferred_tasks_after_speech.append((task_name, factory))

    def _flush_deferred_tasks_after_speech(self) -> None:
        pending = list(self._deferred_tasks_after_speech)
        self._deferred_tasks_after_speech.clear()
        self._last_deferred_flush_count = len(pending)
        logger.info("DEFERRED_TASKS_FLUSHED_AFTER_SPEECH count=%s", len(pending))
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("DEFERRED_TASKS_FLUSH_FAILED reason=no_running_loop count=%s", len(pending))
            return
        for name, factory in pending:
            try:
                result = factory()
                if inspect.isawaitable(result):
                    loop.create_task(result, name=f"kira-deferred-after-speech-{name}")
            except Exception as exc:
                logger.warning("DEFERRED_TASK_AFTER_SPEECH_FAILED name=%s detail=%s", name, exc)

    def _add_provider_alias(self, *, logical_id: str, provider_id: str, source: str) -> None:
        logical = str(logical_id or "").strip()
        provider = str(provider_id or "").strip()
        if not logical or not provider or logical == provider:
            return
        if self._speech_aliases.get(provider) == logical:
            return
        self._speech_aliases[provider] = logical
        self._speech_alias_sets.setdefault(logical, set()).add(provider)
        logger.info(
            "VOICE_OUTPUT_PROVIDER_ALIAS_ADDED logical_id=%s provider_id=%s source=%s",
            logical,
            provider,
            source or "unknown",
        )

    @property
    def mode(self) -> str:
        return self._mode

    def is_active_for_source(self, source: str) -> bool:
        normalized = str(source or "").strip().lower()
        if self._mode in {"off", "shadow"}:
            return False
        if self._mode == "strict":
            return True
        return normalized in {
            "stt_retry_prompt",
            "proactive",
            "whatsapp_tool",
            "whatsapp_notification",
            "system_warning",
            "tool_ack",
            "browser",
            "creation_task_ack",
            "manus_completion_notify",
        }

    def has_drain_path(self, source: str) -> bool:
        return self.is_active_for_source(source) and callable(self._speaker)

    def configure(
        self,
        *,
        speaker: Callable[[VoiceOutputRequest], Awaitable[VoiceOutputResult]] | None = None,
        owner_setter: Callable[[VoiceOutputRequest], None] | None = None,
        owner_releaser: Callable[[VoiceOutputRequest | None], None] | None = None,
    ) -> None:
        if speaker is not None:
            self._speaker = speaker
        if owner_setter is not None:
            self._owner_setter = owner_setter
        if owner_releaser is not None:
            self._owner_releaser = owner_releaser

    async def start(self) -> None:
        async with self._lock:
            if self._runner_task is not None and not self._runner_task.done():
                return
            self._runner_task = asyncio.create_task(self._run_loop(), name="kira-voice-output-manager")

    async def submit(self, request: VoiceOutputRequest) -> VoiceOutputResult | None:
        source = str(request.source or VoiceOutputSource.UNKNOWN.value).strip().lower() or VoiceOutputSource.UNKNOWN.value
        request.source = source
        if request.result_fut is None:
            try:
                request.result_fut = asyncio.get_running_loop().create_future()
            except RuntimeError:
                request.result_fut = None
        if source not in {s.value for s in VoiceOutputSource}:
            logger.warning("VOICE_OUTPUT_UNKNOWN_SOURCE source=%s request_id=%s", source, request.request_id)
        logger.info("VOICE_OUTPUT_REQUEST_SUBMITTED request_id=%s source=%s priority=%s", request.request_id, source, request.priority)

        if request.dedupe_key:
            now = time.monotonic()
            self._cleanup_dedupe(now)
            prev = self._dedupe_recent.get(request.dedupe_key)
            if prev is not None and (now - prev) <= self._dedupe_ttl_s:
                logger.info("VOICE_OUTPUT_REQUEST_DEDUPED request_id=%s source=%s dedupe_key=%s", request.request_id, source, request.dedupe_key)
                result = VoiceOutputResult(ok=False, request_id=request.request_id, source=source, state=VoiceOutputState.FINISHED.value, error="deduped")
                if request.result_fut is not None and not request.result_fut.done():
                    request.result_fut.set_result(result)
                return result
            self._dedupe_recent[request.dedupe_key] = now

        if not request.dedupe_key and source != VoiceOutputSource.REMINDER.value:
            text_key = " ".join(str(request.text or "").lower().split())
            if text_key:
                now = time.monotonic()
                self._text_dedupe_recent = {k: ts for k, ts in self._text_dedupe_recent.items() if (now - ts) <= TEXT_DEDUPE_WINDOW_S}
                if text_key in self._text_dedupe_recent:
                    logger.info("VOICE_OUTPUT_REQUEST_DEDUPED request_id=%s source=%s reason=same_text_window", request.request_id, source)
                    result = VoiceOutputResult(ok=False, request_id=request.request_id, source=source, state=VoiceOutputState.FINISHED.value, error="deduped")
                    if request.result_fut is not None and not request.result_fut.done():
                        request.result_fut.set_result(result)
                    return result
                self._text_dedupe_recent[text_key] = now

        await self._queue.put(request)
        self._set_state(VoiceOutputState.QUEUED)
        logger.info("VOICE_OUTPUT_REQUEST_QUEUED request_id=%s source=%s", request.request_id, source)
        await self.start()
        return None

    async def submit_and_wait(self, request: VoiceOutputRequest) -> VoiceOutputResult:
        result = await self.submit(request)
        if result is not None:
            return result
        fut = request.result_fut
        if fut is None:
            return VoiceOutputResult(
                ok=False,
                request_id=request.request_id,
                source=request.source,
                state=VoiceOutputState.ERROR.value,
                error="missing_result_future",
            )
        try:
            return await asyncio.wait_for(fut, timeout=max(float(request.timeout_s or 30.0), 1.0) + 2.0)
        except asyncio.TimeoutError:
            logger.warning("VOICE_OUTPUT_REQUEST_WAIT_TIMEOUT request_id=%s source=%s", request.request_id, request.source)
            return VoiceOutputResult(
                ok=False,
                request_id=request.request_id,
                source=request.source,
                state=VoiceOutputState.ERROR.value,
                error="wait_timeout",
            )

    async def _run_loop(self) -> None:
        while True:
            req = await self._queue.get()
            _req_source = str(req.source or "").strip().lower()
            _is_browser_source = _req_source in {"browser", "browser_voice", "browser_confirm", "browser_result"}
            if self._user_input_active and req.priority >= VoiceOutputPriority.VOICE_PRIORITY_USER_REPLY:
                if _is_browser_source:
                    # Browser tasks take 20-30s — user_input_active is stale by then. Let through.
                    logger.info("BROWSER_VOICE_USER_INPUT_BYPASS source=%s phase=%s", _req_source, str((req.metadata or {}).get("phase") or ""))
                else:
                    await self._queue.put(req)
                    await asyncio.sleep(0.05)
                    continue
            if self._agent_speaking and not (req.priority == VoiceOutputPriority.VOICE_PRIORITY_EMERGENCY and req.can_interrupt):
                if _is_browser_source:
                    self._log_browser_queue_blocked("agent_speaking", req)
                await self._queue.put(req)
                await asyncio.sleep(0.05)
                continue
            logger.info("VOICE_OUTPUT_MANAGER_DEQUEUE request_id=%s source=%s", req.request_id, req.source)
            await self._speak_request(req)

    async def _speak_request(self, request: VoiceOutputRequest) -> None:
        self._current = request
        self._last_started_request = request
        self._last_started_at = time.time()
        self._current_started_at = time.time()
        self._current_chunk_count = 0
        self._last_chunk_at = None
        self._set_state(VoiceOutputState.SPEAKING)
        self._enter_speech_priority_mode(request.request_id)
        self._ensure_event_loop_monitor()
        self._authorized_generations.add(request.request_id)
        if callable(self._owner_setter):
            try:
                self._owner_setter(request)
            except Exception:
                logger.debug("VOICE_OUTPUT_OWNER_SETTER_FAILED request_id=%s", request.request_id, exc_info=True)
        logger.info("VOICE_OUTPUT_CURRENT_STARTED request_id=%s source=%s", request.request_id, request.source)

        if self._speaker is None:
            await asyncio.sleep(0)
            self.on_finished(request.request_id)
            logger.info("VOICE_OUTPUT_CURRENT_FINISHED request_id=%s source=%s", request.request_id, request.source)
            if request.result_fut is not None and not request.result_fut.done():
                request.result_fut.set_result(
                    VoiceOutputResult(
                        ok=False,
                        request_id=request.request_id,
                        source=request.source,
                        state=VoiceOutputState.ERROR.value,
                        error="missing_speaker",
                        finished_at=time.time(),
                    )
                )
            return

        result = VoiceOutputResult(
            ok=False,
            request_id=request.request_id,
            source=request.source,
            state=VoiceOutputState.ERROR.value,
            error="speaker_not_completed",
            started_at=self._current_started_at,
        )
        try:
            result = await self._speaker(request)
            if isinstance(result, VoiceOutputResult):
                if result.first_chunk_at:
                    self._last_chunk_at = result.first_chunk_at
                self._current_chunk_count = max(self._current_chunk_count, int(result.chunks or 0))
                self._last_finished_at = result.finished_at or time.time()
                if not result.ok:
                    logger.warning(
                        "VOICE_OUTPUT_FAILED request_id=%s source=%s reason=%s",
                        request.request_id,
                        request.source,
                        result.error or "speaker_reported_failure",
                    )
            else:
                result = VoiceOutputResult(
                    ok=True,
                    request_id=request.request_id,
                    source=request.source,
                    state=VoiceOutputState.FINISHED.value,
                    finished_at=time.time(),
                    chunks=self._current_chunk_count,
                )
                self._last_finished_at = time.time()
            logger.info("VOICE_OUTPUT_CURRENT_FINISHED request_id=%s source=%s", request.request_id, request.source)
        except Exception as exc:
            self._set_state(VoiceOutputState.ERROR)
            logger.exception("VOICE_OUTPUT_ERROR request_id=%s source=%s error=%s", request.request_id, request.source, exc)
            result = VoiceOutputResult(
                ok=False,
                request_id=request.request_id,
                source=request.source,
                state=VoiceOutputState.ERROR.value,
                error=str(exc)[:160],
                finished_at=time.time(),
                chunks=self._current_chunk_count,
            )
        finally:
            if request.result_fut is not None and not request.result_fut.done():
                request.result_fut.set_result(result)
            self._authorized_generations.discard(request.request_id)
            if callable(self._owner_releaser):
                try:
                    self._owner_releaser(request)
                except Exception:
                    logger.debug("VOICE_OUTPUT_OWNER_RELEASER_FAILED request_id=%s", request.request_id, exc_info=True)
            self._last_finished_request_id = request.request_id
            self._current = None
            self._set_state(VoiceOutputState.IDLE)
            self._reset_speech_priority_mode(reason="speak_request_finished")
            if self._queue.qsize() > 0:
                gap_ms = int(max((time.time() - (self._last_finished_at or time.time())) * 1000.0, 0.0))
                logger.info("VOICE_OUTPUT_CHAIN_NEXT previous_request_id=%s next_pending=%s", request.request_id, self._queue.qsize())
                logger.info("VOICE_OUTPUT_CHAIN_GAP_MS gap_ms=%s target_ms=%s", gap_ms, self._chain_gap_target_ms)

    def set_user_input_active(self, active: bool, reason: str = "") -> None:
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason == "mouse":
            logger.info("VOICE_INPUT_MOUSE_ACTIVE_SEPARATED_FROM_VOICE")
            if active:
                try:
                    loop = asyncio.get_running_loop()
                    if self._mouse_input_clear_handle is not None:
                        self._mouse_input_clear_handle.cancel()
                    self._mouse_input_clear_handle = loop.call_later(2.5, self._auto_clear_mouse_input_active)
                    logger.info("VOICE_INPUT_MOUSE_ACTIVE_TTL_STARTED ttl_ms=2500")
                except RuntimeError:
                    pass
            else:
                if self._mouse_input_clear_handle is not None:
                    self._mouse_input_clear_handle.cancel()
                    self._mouse_input_clear_handle = None
            return
        self._user_input_active = bool(active)
        logger.info("VOICE_INPUT_ACTIVE_SET active=%s reason=%s", str(bool(active)).lower(), reason or "none")

    def _auto_clear_mouse_input_active(self) -> None:
        self._mouse_input_clear_handle = None
        logger.info("VOICE_INPUT_MOUSE_ACTIVE_AUTO_CLEARED")

    def set_agent_speaking(self, speaking: bool, reason: str = "") -> None:
        self._agent_speaking = bool(speaking)
        logger.info("VOICE_AGENT_SPEAKING_SET active=%s reason=%s", str(bool(speaking)).lower(), reason or "none")

    def is_speaking(self) -> bool:
        return self._state in {VoiceOutputState.SPEAKING, VoiceOutputState.DRAINING} or self._agent_speaking

    def should_defer_heavy_task(self, reason: str) -> bool:
        defer = self.is_speaking()
        if defer:
            logger.info(
                "HEAVY_TASK_DEFERRED_DURING_SPEECH task=%s speech_id=%s",
                str(reason or "unknown").strip() or "unknown",
                self._current.request_id if self._current else "none",
            )
        return defer

    async def wait_until_heavy_task_allowed(
        self,
        reason: str,
        *,
        timeout_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> bool:
        task = str(reason or "unknown").strip() or "unknown"
        if not self.should_defer_heavy_task(task):
            return True
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        while self.is_speaking() and time.monotonic() < deadline:
            await asyncio.sleep(max(float(poll_s), 0.05))
        if self.is_speaking():
            logger.warning(
                "HEAVY_TASK_RESUMED_AFTER_SPEECH task=%s timed_out=true speech_id=%s",
                task,
                self._current.request_id if self._current else "none",
            )
            return False
        logger.info("HEAVY_TASK_RESUMED_AFTER_SPEECH task=%s", task)
        return True

    def _ensure_event_loop_monitor(self) -> None:
        task = self._event_loop_monitor_task
        if task is not None and not task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            self._event_loop_monitor_task = asyncio.create_task(
                self._event_loop_lag_monitor(),
                name="kira-voice-event-loop-lag-monitor",
            )
        except RuntimeError:
            return

    async def _event_loop_lag_monitor(self) -> None:
        interval = max(self._event_loop_lag_interval_s, 0.1)
        last_warn_at = 0.0
        last_critical_at = 0.0
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        while self.is_speaking():
            await asyncio.sleep(interval)
            now = loop.time()
            lag_ms = max(0.0, (now - expected) * 1000.0)
            expected = now + interval
            speech_id = self._current.request_id if self._current else self._last_started_speech_id or "none"
            wall = time.monotonic()
            if self._state == VoiceOutputState.SPEAKING and lag_ms >= 500.0:
                self._speech_priority_mode = True
            if lag_ms >= self._event_loop_lag_critical_ms and (wall - last_critical_at) >= 2.0:
                last_critical_at = wall
                self._event_loop_lag_consecutive_critical += 1
                self._log_event_loop_pressure_snapshot(lag_ms=lag_ms, speech_id=speech_id, suppressed=self._state == VoiceOutputState.SPEAKING and lag_ms < 800.0)
                if self._state == VoiceOutputState.SPEAKING and lag_ms < 800.0:
                    self._note_suppressed_speaking_lag(lag_ms)
                    logger.warning(
                        "VOICE_EVENT_LOOP_LAG_CRITICAL_SUPPRESSED_DURING_SPEECH lag_ms=%.0f state=%s current_speech_id=%s mitigation=speech_priority_defer_heavy_tasks",
                        lag_ms,
                        self._state.value,
                        speech_id,
                    )
                    continue
                logger.warning(
                    "VOICE_EVENT_LOOP_LAG_CRITICAL lag_ms=%.0f state=%s current_speech_id=%s consecutive=%s mitigation=defer_heavy_tasks",
                    lag_ms,
                    self._state.value,
                    speech_id,
                    self._event_loop_lag_consecutive_critical,
                )
            elif lag_ms >= self._event_loop_lag_warn_ms and (wall - last_warn_at) >= 2.0:
                last_warn_at = wall
                self._event_loop_lag_consecutive_critical = 0
                if self._state == VoiceOutputState.SPEAKING:
                    self._note_suppressed_speaking_lag(lag_ms)
                    continue
                logger.warning(
                    "VOICE_EVENT_LOOP_LAG_WARN lag_ms=%.0f state=%s current_speech_id=%s",
                    lag_ms,
                    self._state.value,
                    speech_id,
                )
            elif lag_ms < self._event_loop_lag_warn_ms:
                self._event_loop_lag_consecutive_critical = 0

    def _log_event_loop_pressure_snapshot(self, *, lag_ms: float, speech_id: str, suppressed: bool) -> None:
        now = time.monotonic()
        if (now - self._last_event_loop_pressure_log_at) < 2.0:
            return
        self._last_event_loop_pressure_log_at = now
        try:
            tasks = [task for task in asyncio.all_tasks() if not task.done()]
        except RuntimeError:
            tasks = []
        names: list[str] = []
        for task in tasks[:8]:
            try:
                names.append(str(task.get_name() or "unnamed")[:80])
            except Exception:
                names.append("unnamed")
        logger.warning(
            "VOICE_EVENT_LOOP_LAG_SOURCE_SNAPSHOT lag_ms=%.0f active_tasks=%s sample=%s speech_id=%s suppressed=%s",
            lag_ms,
            len(tasks),
            ",".join(names) or "none",
            speech_id or "none",
            str(bool(suppressed)).lower(),
        )
        logger.info(
            "VOICE_EVENT_LOOP_LAG_MITIGATION action=defer_heavy_tasks speech_priority=%s deferred_tasks=%s",
            str(bool(self._speech_priority_mode)).lower(),
            len(self._deferred_tasks_after_speech),
        )

    def reserve_generation_request(self, request: VoiceOutputRequest) -> bool:
        if self._current is not None and self._current.request_id != request.request_id:
            if self._state == VoiceOutputState.RESERVED and not self._agent_speaking:
                logger.info(
                    "VOICE_OUTPUT_STALE_RESERVATION_REPLACED old_request_id=%s new_request_id=%s",
                    self._current.request_id,
                    request.request_id,
                )
                self._authorized_generations.discard(self._current.request_id)
                self._current = None
            else:
                logger.info(
                    "GENERATE_REPLY_BLOCKED_VOM_BUSY request_id=%s active_request_id=%s source=%s",
                    request.request_id,
                    self._current.request_id,
                    request.source,
                )
                return False
        request.metadata["_native_generation_reserved"] = True
        self._current = request
        self._current_started_at = time.time()
        self._last_started_request = request
        self._last_started_at = self._current_started_at
        self._authorized_generations.add(request.request_id)
        self._set_state(VoiceOutputState.RESERVED)
        logger.info(
            "VOICE_OUTPUT_LOGICAL_SPEECH_CREATED logical_id=%s turn_id=%s source=%s",
            request.request_id,
            request.turn_id or "none",
            request.source,
        )
        logger.info(
            "VOICE_OUTPUT_REQUESTED request_id=%s speech_id=%s turn_id=%s source=%s",
            request.request_id,
            request.request_id,
            request.turn_id or "none",
            request.source,
        )
        return True

    def can_generate_reply(
        self,
        *,
        request_id: str | None,
        turn_id: str | None,
        source: str,
        command_intercepted: bool = False,
        text_only: bool = False,
    ) -> bool:
        if command_intercepted:
            logger.info("GENERATE_REPLY_BLOCKED_COMMAND_INTERCEPTED turn_id=%s source=%s", turn_id or "none", source)
            return False
        if text_only:
            logger.info("GENERATE_REPLY_BLOCKED_TEXT_ONLY turn_id=%s source=%s", turn_id or "none", source)
            return False
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id or self._current is None or self._current.request_id != normalized_request_id:
            logger.warning("GENERATE_REPLY_BLOCKED_NO_VOICE_REQUEST turn_id=%s source=%s", turn_id or "none", source)
            return False
        if self._state not in {VoiceOutputState.RESERVED, VoiceOutputState.SPEAKING}:
            logger.info("GENERATE_REPLY_BLOCKED_VOM_BUSY turn_id=%s source=%s state=%s", turn_id or "none", source, self._state.value)
            return False
        logger.info(
            "GENERATE_REPLY_ALLOWED_BY_VOM request_id=%s turn_id=%s source=%s",
            normalized_request_id,
            turn_id or "none",
            source,
        )
        return True

    def mark_audio_started(
        self,
        *,
        speech_id: str | None,
        turn_id: str | None,
        source: str,
        request_id: str | None = None,
        user_initiated: bool = False,
        command_intercepted: bool = False,
        queue_size: int = 0,
    ) -> str:
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        provider_id = str(speech_id or request_id or "").strip()
        resolved_id = self._resolve_speech_alias(provider_id)
        if self._current is not None:
            logical_id = self._current.request_id
            if provider_id and provider_id != logical_id:
                self._add_provider_alias(logical_id=logical_id, provider_id=provider_id, source=normalized_source)
            resolved_id = logical_id
            logger.info(
                "VOICE_OUTPUT_START_MAPPED logical_id=%s provider_id=%s source=%s",
                resolved_id,
                provider_id or resolved_id,
                normalized_source,
            )
        if not resolved_id:
            resolved_id = self._make_provisional_speech_id(turn_id, normalized_source)
            logger.info(
                "VOICE_OUTPUT_PROVISIONAL_AUDIO_TRACKED logical_id=%s source=%s turn_id=%s",
                resolved_id,
                normalized_source,
                turn_id or "none",
            )
        if self._current is None:
            self._current = VoiceOutputRequest(
                priority=VoiceOutputPriority.VOICE_PRIORITY_USER_REPLY,
                request_id=resolved_id,
                source=normalized_source,
                turn_id=turn_id,
                metadata={"provisional_audio": True, "_native_generation_reserved": True},
            )
            if turn_id:
                logger.info(
                    "VOICE_OUTPUT_TURN_LINKED speech_id=%s turn_id=%s method=pending_user_speech",
                    resolved_id,
                    turn_id,
                )
        self._current_started_at = time.time()
        self._last_started_request = self._current
        self._last_started_at = self._current_started_at
        self._remember_audio_started(
            speech_id=resolved_id,
            turn_id=turn_id,
            source=normalized_source,
            timestamp=self._current_started_at,
        )
        self._set_state(VoiceOutputState.SPEAKING)
        self._enter_speech_priority_mode(resolved_id)
        self._ensure_event_loop_monitor()
        self._agent_speaking = True
        logger.info("VOICE_OUTPUT_LOGICAL_SPEECH_CREATED logical_id=%s turn_id=%s source=%s", resolved_id, turn_id or "none", normalized_source)
        logger.info("AUDIO_OUTPUT_START_MAPPED speech_id=%s turn_id=%s", resolved_id, turn_id or "none")
        logger.info("VOICE_OUTPUT_MANAGER_AUDIO_STARTED speech_id=%s source=%s", resolved_id, normalized_source)
        logger.info("VOICE_OUTPUT_MANAGER_STATE_SYNCED_FROM_AUDIO_START state=speaking speech_id=%s", resolved_id)
        return resolved_id

    def finish_audio(self, speech_id: str | None = None, *, source: str = "reply_finish") -> str | None:
        event_id = str(speech_id or "").strip()
        resolved_id = self._resolve_speech_alias(event_id)
        if self._current is not None and resolved_id and self._current.request_id != resolved_id and self._agent_speaking:
            logger.info(
                "VOICE_OUTPUT_FINISH_BACKFILLED_FROM_REPLY_FINISH speech_id=%s event_speech_id=%s source=%s",
                self._current.request_id,
                event_id or resolved_id,
                source,
            )
            if event_id:
                self._add_provider_alias(logical_id=self._current.request_id, provider_id=event_id, source=source)
            resolved_id = self._current.request_id
        if not resolved_id and self._current is not None:
            resolved_id = self._current.request_id
            logger.info("VOICE_OUTPUT_FINISH_BACKFILLED_FROM_REPLY_FINISH speech_id=%s source=%s", resolved_id, source)
            logger.info(
                "VOICE_SPEECH_ID_ASSOCIATED_BY_FALLBACK speech_id=%s source=%s method=current_request",
                resolved_id,
                source,
            )
        logger.info("AUDIO_OUTPUT_FINISH_INTERCEPTED speech_id=%s source=%s", resolved_id or "none", source)
        if not resolved_id:
            self._agent_speaking = False
            self._set_state(VoiceOutputState.IDLE)
            count_key = str(source or "unknown").strip() or "unknown"
            count = int(self._safe_cleanup_counts.get(count_key, 0)) + 1
            self._safe_cleanup_counts[count_key] = count
            logger.info("VOICE_FINISH_IDEMPOTENT_IGNORED source=%s count=%s", source, count)
            logger.info("VOICE_SAFE_CLEANUP_COUNT key=%s count=%s", count_key, count)
            if count > 3:
                logger.warning("VOICE_STALE_LOOP_DETECTED type=safe_cleanup key=%s count=%s", count_key, count)
            return None
        logger.info("VOICE_OUTPUT_FINISHED speech_id=%s", resolved_id)
        self.on_finished(resolved_id, time.time())
        if self._current is not None and self._current.request_id == resolved_id:
            self._authorized_generations.discard(resolved_id)
            self._current = None
            self._set_state(VoiceOutputState.IDLE)
        self._reset_speech_priority_mode(reason="finish_audio")
        if resolved_id in self._speech_alias_sets:
            for alias in self._speech_alias_sets.pop(resolved_id, set()):
                self._speech_aliases.pop(alias, None)
        self._agent_speaking = False
        logger.info("VOICE_OUTPUT_CLEANUP_OK logical_id=%s", resolved_id)
        if str(resolved_id).startswith("unknown_audio_"):
            logger.info("VOICE_OUTPUT_UNKNOWN_AUDIO_CLEANUP_OK logical_id=%s", resolved_id)
        return resolved_id

    def force_cleanup_stale_speech(self, speech_id: str | None = None, *, reason: str = "stale_speech") -> bool:
        normalized = self._resolve_speech_alias(str(speech_id or "").strip())
        active_id = self._current.request_id if self._current is not None else ""
        target_id = normalized or active_id or self._last_started_speech_id or "none"
        changed = bool(
            self._current is not None
            or self._agent_speaking
            or self._state != VoiceOutputState.IDLE
            or self._speech_priority_mode
        )
        if self._current is not None and (not normalized or self._current.request_id == normalized):
            self._authorized_generations.discard(self._current.request_id)
            self._current = None
        elif normalized:
            self._authorized_generations.discard(normalized)
        self._agent_speaking = False
        self._user_input_active = False
        self._set_state(VoiceOutputState.IDLE)
        self._reset_speech_priority_mode(reason=reason)
        now = time.time()
        if normalized:
            self._remember_audio_finished(normalized, now)
        count_key = str(normalized or active_id or target_id or "none").strip() or "none"
        count = int(self._stale_cleanup_counts.get(count_key, 0)) + 1
        self._stale_cleanup_counts[count_key] = count
        if changed:
            logger.info("VOICE_STALE_SPEECH_CLEANED speech_id=%s reason=%s", target_id, reason)
        else:
            logger.info("VOICE_STALE_EVENT_IGNORED_ALREADY_CLOSED speech_id=%s reason=%s", target_id, reason)
        logger.info("VOICE_STALE_EVENT_COUNT key=%s count=%s", count_key, count)
        if count > 3:
            logger.warning("VOICE_STALE_LOOP_DETECTED type=stale_cleanup key=%s count=%s", count_key, count)
        logger.info("VOICE_STALE_CLEANUP_REASON speech_id=%s reason=%s changed=%s", target_id, reason, str(changed).lower())
        logger.info("KIRA_PENDING_RELEASED_AFTER_STALE_SPEECH speech_id=%s", target_id)
        return changed

    def force_reset_confirmed_stale(
        self,
        *,
        max_age_s: float = 20.0,
        max_chunk_age_s: float = 20.0,
        reason: str = "confirmed_stale",
    ) -> bool:
        if self._queue.qsize() > 0:
            logger.info("VOM_RESET_SKIPPED_ACTIVE_AUDIO reason=queue_not_empty")
            return False
        if self._agent_speaking or self._speech_priority_mode:
            logger.info("VOM_RESET_SKIPPED_ACTIVE_AUDIO reason=agent_speaking")
            return False
        if self._state in {VoiceOutputState.SPEAKING, VoiceOutputState.DRAINING}:
            now = time.time()
            started_at = float(self._current_started_at or self._last_started_at or now)
            last_chunk_at = float(self._last_chunk_at or started_at)
            age_s = max(0.0, now - started_at)
            chunk_age_s = max(0.0, now - last_chunk_at)
            if age_s < max_age_s or chunk_age_s < max_chunk_age_s:
                logger.info(
                    "VOM_RESET_SKIPPED_ACTIVE_AUDIO reason=not_stale age_s=%.1f chunk_age_s=%.1f",
                    age_s,
                    chunk_age_s,
                )
                return False
        logger.info("VOM_FORCE_RESET_CONFIRMED_STALE reason=%s", reason)
        return self.force_cleanup_stale_speech(reason=reason)

    def cleanup_stale_active_speech(self, *, max_age_s: float = 20.0, max_chunk_age_s: float = 20.0) -> bool:
        if self._current is None or self._state != VoiceOutputState.SPEAKING:
            return False
        now = time.time()
        started_at = float(self._current_started_at or self._last_started_at or now)
        last_chunk_at = float(self._last_chunk_at or started_at)
        age_s = max(0.0, now - started_at)
        chunk_age_s = max(0.0, now - last_chunk_at)
        if age_s < max_age_s or chunk_age_s < max_chunk_age_s:
            return False
        speech_id = self._current.request_id
        logger.info("VOICE_STALE_SPEECH_DETECTED speech_id=%s age_s=%.1f", speech_id, age_s)
        return self.force_cleanup_stale_speech(speech_id, reason="stale_active_speech")

    def resolve_reply_finish_speech_id(
        self,
        *,
        turn_id: str | None = None,
        source: str | None = None,
        max_age_s: float = 30.0,
    ) -> str | None:
        if self._current is not None:
            logger.info("REPLY_FINISH_ACTIVE_SPEECH_FOUND speech_id=%s", self._current.request_id)
            return self._current.request_id

        now = time.time()
        normalized_turn_id = str(turn_id or "").strip() or None
        normalized_source = str(source or "").strip().lower() or None
        candidates: list[dict[str, Any]] = []
        for item in reversed(self._recent_speech_history):
            speech_id = str(item.get("speech_id") or "").strip()
            if not speech_id:
                continue
            if item.get("finished_at") is not None:
                logger.info("REPLY_FINISH_BACKFILL_SKIPPED_ALREADY_FINISHED speech_id=%s", speech_id)
                continue
            age_s = max(0.0, now - float(item.get("started_at") or now))
            if age_s > max_age_s:
                logger.info("REPLY_FINISH_BACKFILL_SKIPPED_TOO_OLD speech_id=%s age_s=%.2f", speech_id, age_s)
                continue
            item_turn_id = str(item.get("turn_id") or "").strip() or None
            item_source = str(item.get("source") or "").strip().lower() or None
            if normalized_turn_id and item_turn_id and item_turn_id != normalized_turn_id:
                continue
            if normalized_source and item_source and item_source != normalized_source:
                logger.info(
                    "REPLY_FINISH_BACKFILL_SKIPPED_SOURCE_MISMATCH speech_id=%s expected=%s got=%s",
                    speech_id,
                    normalized_source,
                    item_source,
                )
                continue
            candidates.append(item)
        if len(candidates) != 1:
            return None
        speech_id = str(candidates[0]["speech_id"])
        logger.info("REPLY_FINISH_BACKFILLED_LAST_SPEECH_ID speech_id=%s", speech_id)
        return speech_id

    def reset_stale_idle_state(self, *, reason: str) -> bool:
        if self._state == VoiceOutputState.SPEAKING or self._queue.qsize() > 0:
            return False
        changed = bool(self._current is not None or self._agent_speaking or self._state != VoiceOutputState.IDLE)
        if self._current is not None:
            self._authorized_generations.discard(self._current.request_id)
        self._current = None
        self._agent_speaking = False
        self._set_state(VoiceOutputState.IDLE)
        self._reset_speech_priority_mode(reason="stale_idle_state")
        if changed:
            logger.info("VOICE_OUTPUT_MANAGER_STALE_STATE_CLEARED reason=%s", reason)
        return changed

    def cleanup_after_timeout(self, *, request_id: str | None = None, reason: str = "timeout_cleanup") -> None:
        normalized = str(request_id or "").strip()
        if self._current is not None and (not normalized or self._current.request_id == normalized):
            logger.info("VOICE_OUTPUT_STALE_RESERVATION_CLEARED request_id=%s reason=%s", self._current.request_id, reason)
            self._authorized_generations.discard(self._current.request_id)
            self._current = None
        self._agent_speaking = False
        self._user_input_active = False
        self._set_state(VoiceOutputState.IDLE)
        self._reset_speech_priority_mode(reason="timeout_cleanup")
        logger.info("VOICE_STATE_SYNC_OK reason=timeout_cleanup")

    def on_first_chunk(self, request_id: str, timestamp: float | None = None) -> None:
        request_id = self._resolve_speech_alias(request_id)
        if self._current is None or self._current.request_id != request_id:
            return
        self._last_chunk_at = timestamp or time.time()
        self._current_chunk_count = max(self._current_chunk_count, 1)
        logger.info("VOICE_OUTPUT_MANAGER_FIRST_CHUNK request_id=%s source=%s", request_id, self._current.source)
        phase = str((self._current.metadata or {}).get("phase") or "").strip()
        if phase:
            logger.info("VOICE_OUTPUT_FIRST_CHUNK source=%s phase=%s", self._current.source, phase)
            if self._current.source == "browser" and phase == "pre_ack":
                logger.info("BROWSER_PRE_ACK_VOICE_STARTED")
            elif self._current.source == "browser" and phase == "final":
                logger.info("BROWSER_FINAL_VOICE_STARTED")
        else:
            logger.info("VOICE_OUTPUT_FIRST_CHUNK source=%s", self._current.source)

    def on_audio_chunk(self, request_id: str, timestamp: float | None = None) -> None:
        request_id = self._resolve_speech_alias(request_id)
        if self._current is None or self._current.request_id != request_id:
            return
        self._last_chunk_at = timestamp or time.time()
        self._current_chunk_count += 1
        logger.debug("VOICE_OUTPUT_MANAGER_CHUNK_PROGRESS request_id=%s chunks=%s", request_id, self._current_chunk_count)

    def on_finished(self, request_id: str, timestamp: float | None = None) -> None:
        request_id = self._resolve_speech_alias(request_id)
        finished_at = timestamp or time.time()
        self._remember_audio_finished(request_id, finished_at)
        if self._current is None or self._current.request_id != request_id:
            self._last_finished_at = finished_at
            self._last_finished_request_id = request_id
            logger.info("VOICE_OUTPUT_MANAGER_FINISHED request_id=%s source=%s", request_id, "external")
            return
        self._last_finished_at = finished_at
        self._last_finished_request_id = request_id
        logger.info("VOICE_OUTPUT_MANAGER_FINISHED request_id=%s source=%s", request_id, self._current.source)
        phase = str((self._current.metadata or {}).get("phase") or "").strip()
        if phase:
            logger.info("VOICE_OUTPUT_FINISHED source=%s phase=%s", self._current.source, phase)
        else:
            logger.info("VOICE_OUTPUT_FINISHED source=%s", self._current.source)

    def _remember_audio_started(self, *, speech_id: str, turn_id: str | None, source: str, timestamp: float) -> None:
        self._last_started_speech_id = speech_id
        self._last_started_turn_id = str(turn_id or "").strip() or None
        self._last_started_source = str(source or "").strip().lower() or None
        if self._recent_speech_history and self._recent_speech_history[-1].get("speech_id") == speech_id:
            item = self._recent_speech_history[-1]
            item.update({"turn_id": self._last_started_turn_id, "source": self._last_started_source, "started_at": timestamp})
            return
        self._recent_speech_history.append(
            {
                "speech_id": speech_id,
                "turn_id": self._last_started_turn_id,
                "source": self._last_started_source,
                "started_at": timestamp,
                "finished_at": None,
            }
        )

    def _remember_audio_finished(self, speech_id: str, timestamp: float) -> None:
        self._last_finished_speech_id = speech_id
        for item in reversed(self._recent_speech_history):
            if str(item.get("speech_id") or "") == speech_id:
                item["finished_at"] = timestamp
                break

    def should_block_unowned_generation(self, *, turn_id: str | None, source: str, user_initiated: bool) -> bool:
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        should_block = (
            normalized_source in {"generate_reply", "auto", "unknown"}
            and not turn_id
            and not bool(user_initiated)
            and self._current is None
        )
        if should_block:
            if self._mode == "off":
                return False
            if self._mode == "shadow":
                self._shadow_would_block_count += 1
                logger.warning(
                    "VOICE_OUTPUT_SHADOW_WOULD_BLOCK_UNOWNED_GENERATION source=%s turn_id=%s user_initiated=%s",
                    normalized_source,
                    turn_id,
                    str(bool(user_initiated)).lower(),
                )
                return False
            self._blocked_unowned_count += 1
            logger.warning(
                "VOICE_OUTPUT_BLOCKED_UNOWNED_GENERATION source=%s turn_id=%s user_initiated=%s",
                normalized_source,
                turn_id,
                str(bool(user_initiated)).lower(),
            )
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        snap = {
            "mode": self._mode,
            "state": self._state.value,
            "queue_size": self._queue.qsize(),
            "current_request_id": self._current.request_id if self._current else None,
            "current_source": self._current.source if self._current else None,
            "current_started_at": self._current_started_at,
            "last_started_at": self._last_started_at,
            "last_chunk_at": self._last_chunk_at,
            "last_finished_at": self._last_finished_at,
            "current_chunk_count": self._current_chunk_count,
            "user_input_active": self._user_input_active,
            "agent_speaking": self._agent_speaking,
            "blocked_unowned_count": self._blocked_unowned_count,
            "shadow_would_block_count": self._shadow_would_block_count,
            "last_finished_request_id": self._last_finished_request_id,
            "active_speech_id": self._current.request_id if self._current else None,
            "is_speaking": self._state == VoiceOutputState.SPEAKING or self._agent_speaking,
            "last_started_speech_id": self._last_started_speech_id,
            "last_started_turn_id": self._last_started_turn_id,
            "last_started_source": self._last_started_source,
            "last_finished_speech_id": self._last_finished_speech_id,
            "recent_speech_history": list(self._recent_speech_history),
            "external_audio_count": self._external_audio_count,
        }
        if self._should_log_snapshot():
            # O dict completo (com recent_speech_history) so vai para DEBUG. Em
            # idle, logar o dict inteiro em INFO a cada ~10s serializava uma
            # estrutura grande no event loop e inflava o log; agora INFO leva
            # apenas um resumo curto.
            logger.debug("VOICE_OUTPUT_SNAPSHOT %s", snap)
            if not self.is_speaking():
                logger.info(
                    "VOICE_OUTPUT_SNAPSHOT_SUMMARY state=%s queue=%s is_speaking=%s "
                    "last_finished=%s external_audio=%s",
                    snap["state"],
                    snap["queue_size"],
                    snap["is_speaking"],
                    snap["last_finished_speech_id"],
                    snap["external_audio_count"],
                )
        return snap

    def _set_state(self, state: VoiceOutputState) -> None:
        if self._state == state:
            return
        old = self._state
        self._state = state
        logger.info("VOICE_OUTPUT_STATE_CHANGED from=%s to=%s", old.value, state.value)

    def _cleanup_dedupe(self, now: float) -> None:
        stale = [k for k, ts in self._dedupe_recent.items() if (now - ts) > self._dedupe_ttl_s]
        for key in stale:
            self._dedupe_recent.pop(key, None)


_MANAGER: VoiceOutputManager | None = None


def get_voice_output_manager() -> VoiceOutputManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = VoiceOutputManager()
    return _MANAGER
