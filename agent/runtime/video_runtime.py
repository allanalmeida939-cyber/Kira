from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2
import numpy as np
from PIL import Image
from livekit import rtc

from resilience import log_exception_safely

from .screen_context_feed import ScreenContextFeed
from .vision_frame_store import ensure_vision_frame_store, get_vision_frame_store
from .voice_output_manager import get_voice_output_manager

try:
    from vision.screen_context_analyzer import ScreenContextAnalyzer
    from vision.screen_proactive_engine import ScreenProactiveEngine
    from vision.screen_vision_engine import ScreenVisionEngine
except Exception:  # pragma: no cover - ambiente parcial
    ScreenVisionEngine = None
    ScreenContextAnalyzer = None
    ScreenProactiveEngine = None


logger = logging.getLogger(__name__)

SCREEN_FRAME_SAMPLE_SECONDS = 0.5
# 1 quadro/s parado: a cada quadro a tela inteira vira JPEG (até 2134x1366); 2/s gastava CPU à toa.
def _active_realtime_session():
    try:
        from core.realtime import active_session
    except ImportError:  # pragma: no cover - execução como pacote
        from agent.core.realtime import active_session
    return active_session()


SCREEN_FRAME_IDLE_SAMPLE_SECONDS = max(float(os.getenv("KIRA_SCREEN_SAMPLE_SECONDS", "1.0") or 1.0), 0.25)
SCREEN_FRAME_SPEAKING_SAMPLE_SECONDS = 5.0
SCREEN_FIRST_FRAME_TIMEOUT_SECONDS = 2.0
SCREEN_FRAME_DEBUG_LOG_EVERY = 10
SCREEN_TRACK_SOURCE_NAMES = {"screenshare", "screen_share", "screen", "source_screenshare"}
CAMERA_TRACK_SOURCE_NAMES = {"camera", "cam", "source_camera", "webcam"}


def _track_source_label(source: object) -> str:
    if source is None:
        return "unknown"
    try:
        name = rtc.TrackSource.Name(source).lower()
    except Exception:
        name = str(source).strip().lower()
    return name or "unknown"


def _is_video_track(track: object, publication: object | None = None) -> bool:
    kind = getattr(track, "kind", None)
    if kind is None and publication is not None:
        kind = getattr(publication, "kind", None)
    return kind == rtc.TrackKind.KIND_VIDEO


def _is_screen_share_publication(publication: object | None) -> bool:
    if publication is None:
        return False
    source = _track_source_label(getattr(publication, "source", None))
    return any(token in source for token in SCREEN_TRACK_SOURCE_NAMES)


def _is_camera_publication(publication: object | None) -> bool:
    if publication is None:
        return False
    source = _track_source_label(getattr(publication, "source", None))
    return any(token in source for token in CAMERA_TRACK_SOURCE_NAMES)


def _visual_source_from_publication(publication: object | None) -> str:
    return "livekit_screen_share" if _is_screen_share_publication(publication) else "livekit_camera"


def _is_livekit_visual_publication(publication: object | None) -> bool:
    return _is_screen_share_publication(publication) or _is_camera_publication(publication)


class RoomVideoBridge:
    """Bridge de video do LiveKit com store centralizado e fallback DXCam no pipeline."""

    def __init__(
        self,
        *,
        room,
        observer,
        signal_handler=None,
        visual_runtime=None,
        voice_state_provider=None,
    ) -> None:
        self._room = room
        self._observer = observer
        self._signal_handler = signal_handler
        self._visual_runtime = visual_runtime
        self._voice_state_provider = voice_state_provider
        self._registered = False
        self._active_tasks: set[asyncio.Task] = set()
        self._visual_refresh_tasks: dict[str, asyncio.Task] = {}
        self._visual_refresh_deferred_keys: set[str] = set()
        self._last_visual_task_deferred_log_at = 0.0
        self._vision_metrics = {
            "frames_captured": 0,
            "frames_analyzed": 0,
            "frames_skipped": 0,
            "skip_reasons": {},
        }
        self._screen_context_events: deque[dict[str, object]] = deque(maxlen=16)
        self._screen_vision_events: deque[dict[str, object]] = deque(maxlen=16)
        self._screen_proactive_events: deque[dict[str, object]] = deque(maxlen=16)
        self._screen_vision_engine = ScreenVisionEngine() if ScreenVisionEngine is not None else None
        self._screen_context_analyzer = ScreenContextAnalyzer() if ScreenContextAnalyzer is not None else None
        self._screen_proactive_engine = ScreenProactiveEngine() if ScreenProactiveEngine is not None else None
        self._visual_proactivity_enabled = False
        logger.info("VISION_PROACTIVITY_DISABLED_BY_DEFAULT")
        self._analysis_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="kira-vision",
        )
        self._screen_share_tasks: dict[str, asyncio.Task] = {}
        self._track_sources: dict[str, str] = {}
        self._frame_store = ensure_vision_frame_store(debug_dir="debug_vision")
        self._screen_feed = ScreenContextFeed(_active_realtime_session)
        logger.info("VISION_FRAME_STORE_BOUND source=video_runtime")
        logger.info("VISION_FRAME_STORE_AVAILABLE source=video_runtime")
        self._frame_store_available_logged = True
        self._last_livekit_frame_log_at: dict[str, float] = {}
        self._last_frame_convert_failed_log_at = 0.0
        self._last_track_kept_alive_log_at = 0.0
        self._ultra_light_mode_active = False
        self._last_visual_proactive_comment_at = 0.0
        self._last_light_context_update_at = 0.0
        self._first_frame_urgent_started_at: dict[str, float] = {}
        self._first_frame_urgent_completed: set[str] = set()
        self._adaptive_fps = 2.0
        self._last_adaptive_fps_reason = ""
        self._adaptive_fps_reduced_until = 0.0
        self._fps_window_started_at = time.monotonic()
        self._fps_window_stored = 0
        self._fps_window_skipped = 0
        self._active_track_sid = ""
        self._active_track_label = ""

    def _is_speech_priority_active(self) -> bool:
        try:
            return get_voice_output_manager().is_speech_priority_active()
        except Exception:
            return False

    def _log_visual_deferred_during_speech(self, task: str) -> None:
        now = time.monotonic()
        if now - self._last_visual_task_deferred_log_at >= 5.0:
            self._last_visual_task_deferred_log_at = now
            logger.info("SPEECH_PRIORITY_BLOCKED_HEAVY_TASK task=%s", task)
            logger.info("SPEECH_PRIORITY_DEFERRED_TASK task=%s", task)
            logger.info("NON_ESSENTIAL_TASK_BLOCKED_DURING_SPEECH task=%s", task)
            logger.info("VISUAL_TASK_DEFERRED_DURING_SPEECH task=%s", task)

    def _get_frame_store(self):
        store = self._frame_store or get_vision_frame_store()
        if store is None:
            store = ensure_vision_frame_store(debug_dir="debug_vision")
            logger.info("VISION_FRAME_STORE_BOUND source=video_runtime")
            self._frame_store_available_logged = False
        self._frame_store = store
        if not self._frame_store_available_logged:
            logger.info("VISION_FRAME_STORE_AVAILABLE source=video_runtime")
            self._frame_store_available_logged = True
        return store

    def _set_adaptive_fps(self, fps: float, *, reason: str) -> None:
        normalized_reason = str(reason or "unknown").strip() or "unknown"
        fps = max(float(fps), 0.0)
        if abs(self._adaptive_fps - fps) < 0.01 and self._last_adaptive_fps_reason == normalized_reason:
            return
        self._adaptive_fps = fps
        self._last_adaptive_fps_reason = normalized_reason
        try:
            set_store_fps = getattr(self._get_frame_store(), "set_adaptive_fps", None)
            if callable(set_store_fps):
                set_store_fps(fps)
        except Exception:
            pass
        logger.info("VISION_ADAPTIVE_FPS_SET fps=%s reason=%s", ("%g" % fps), normalized_reason)

    def _log_fps_summary_if_needed(self) -> None:
        now = time.monotonic()
        elapsed = max(now - self._fps_window_started_at, 0.001)
        if elapsed < 10.0:
            return
        fps = self._fps_window_stored / elapsed
        logger.info(
            "VISION_FRAME_STORE_FPS_SUMMARY fps=%.2f stored=%s skipped=%s",
            fps,
            self._fps_window_stored,
            self._fps_window_skipped,
        )
        self._fps_window_started_at = now
        self._fps_window_stored = 0
        self._fps_window_skipped = 0

    def register_handlers(self) -> None:
        if self._registered:
            return
        self._registered = True
        self._room.on("participant_connected", self._on_participant_connected)
        self._room.on("track_published", self._on_track_published)
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("track_unsubscribed", self._on_track_unsubscribed)
        self._room.on("track_subscription_failed", self._on_track_subscription_failed)

        for participant in list(getattr(self._room, "remote_participants", {}).values()):
            self._on_participant_connected(participant)
            for publication in list(getattr(participant, "track_publications", {}).values()):
                if not _is_livekit_visual_publication(publication):
                    continue
                if getattr(publication, "track", None) is not None:
                    self._on_track_subscribed(publication.track, publication, participant)
                else:
                    self._ensure_screen_subscription(publication, participant, reason="register_existing_publication")

        logger.info("[VIDEO_RUNTIME] Handlers de video registrados - screen share ativo por LiveKit")

    async def shutdown(self) -> None:
        if self._registered:
            try:
                self._room.off("participant_connected", self._on_participant_connected)
                self._room.off("track_published", self._on_track_published)
                self._room.off("track_subscribed", self._on_track_subscribed)
                self._room.off("track_unsubscribed", self._on_track_unsubscribed)
                self._room.off("track_subscription_failed", self._on_track_subscription_failed)
            except Exception:
                logger.debug("[VIDEO_RUNTIME] Ignorando erro ao remover handlers", exc_info=True)
            self._registered = False

        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*list(self._active_tasks), return_exceptions=True)
        self._active_tasks.clear()
        self._visual_refresh_tasks.clear()
        self._screen_share_tasks.clear()
        self._analysis_pool.shutdown(wait=False, cancel_futures=True)

    def pop_screen_vision_events(self) -> list[dict[str, object]]:
        items = list(self._screen_vision_events)
        self._screen_vision_events.clear()
        return items

    def pop_screen_context_events(self) -> list[dict[str, object]]:
        items = list(self._screen_context_events)
        self._screen_context_events.clear()
        return items

    def pop_screen_proactive_events(self) -> list[dict[str, object]]:
        items = list(self._screen_proactive_events)
        self._screen_proactive_events.clear()
        return items

    def get_vision_metrics(self) -> dict[str, object]:
        return {
            "frames_captured": int(self._vision_metrics["frames_captured"]),
            "frames_analyzed": int(self._vision_metrics["frames_analyzed"]),
            "frames_skipped": int(self._vision_metrics["frames_skipped"]),
            "skip_reasons": dict(self._vision_metrics["skip_reasons"]),
        }

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        for publication in list(getattr(participant, "track_publications", {}).values()):
            if _is_livekit_visual_publication(publication):
                self._ensure_screen_subscription(publication, participant, reason="participant_connected")

    def _on_track_published(
        self,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if not _is_livekit_visual_publication(publication):
            return
        logger.info(
            "[VIDEO_RUNTIME] screen_track_published participant=%s sid=%s source=%s subscribed=%s",
            getattr(participant, "identity", ""),
            getattr(publication, "sid", ""),
            _track_source_label(getattr(publication, "source", None)),
            bool(getattr(publication, "subscribed", False)),
        )
        self._ensure_screen_subscription(publication, participant, reason="track_published")

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if not _is_video_track(track, publication) or not _is_livekit_visual_publication(publication):
            return

        track_sid = str(getattr(publication, "sid", "") or getattr(track, "sid", "") or "").strip()
        visual_source = _visual_source_from_publication(publication)
        old_sid = self._active_track_sid
        if old_sid and old_sid != track_sid:
            old_task = self._screen_share_tasks.get(old_sid)
            if old_task is not None and not old_task.done():
                logger.info(
                    "[VIDEO_RUNTIME] VISION_TRACK_ACTIVE_SET sid=%s cancelling_old_sid=%s",
                    track_sid or "unknown",
                    old_sid,
                )
                old_task.cancel()
            else:
                logger.info(
                    "[VIDEO_RUNTIME] VISION_TRACK_ACTIVE_SET sid=%s old_sid=%s old_task_done=%s",
                    track_sid or "unknown",
                    old_sid,
                    old_task is None or old_task.done(),
                )
        elif not old_sid:
            logger.info("[VIDEO_RUNTIME] VISION_TRACK_ACTIVE_SET sid=%s (first_track)", track_sid or "unknown")
        self._active_track_sid = track_sid or self._active_track_sid
        if track_sid:
            self._track_sources[track_sid] = visual_source
        frame_store = self._get_frame_store()
        if frame_store is not None:
            try:
                frame_store.set_track_active(
                    visual_source,
                    True,
                    track_sid=track_sid,
                    label=str(getattr(participant, "identity", "") or ""),
                )
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] frame_store_track_active_failed detail=%s", exc)
        self._active_track_label = str(getattr(participant, "identity", "") or "").strip()
        logger.info(
            "[VIDEO_RUNTIME] screen_track_subscribed participant=%s track_sid=%s source=%s",
            getattr(participant, "identity", ""),
            track_sid or "unknown",
            _track_source_label(getattr(publication, "source", None)),
        )
        logger.info(
            "VISION_TRACK_SUBSCRIBED source=%s participant=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
            getattr(participant, "identity", "") or "unknown",
        )
        logger.info(
            "VISION_LIVEKIT_VIDEO_TRACK_SUBSCRIBED source=%s participant=%s track_sid=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
            getattr(participant, "identity", "") or "unknown",
            track_sid or "unknown",
        )
        logger.info(
            "VISION_TRACK_SUBSCRIBED_WAITING_FRAME source=%s participant=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
            getattr(participant, "identity", "") or "unknown",
        )
        logger.info(
            "VISION_CONTINUOUS_STREAM_ACTIVE source=%s state=waiting_first_frame",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
        )
        self._first_frame_urgent_started_at[track_sid] = time.monotonic()
        self._first_frame_urgent_completed.discard(track_sid)
        logger.info(
            "VISION_FIRST_FRAME_URGENT_BEGIN source=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
        )
        self._notify_visual_context_state(
            enabled=True,
            track_sid=track_sid,
            label=self._active_track_label,
            width=int(getattr(track, "width", 0) or 0),
            height=int(getattr(track, "height", 0) or 0),
            visual_source=visual_source,
        )
        logger.info(
            "VISION_CONTEXT_NOTIFY_OK source=%s participant=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
            getattr(participant, "identity", "") or "unknown",
        )
        if self._visual_runtime is not None:
            try:
                self._visual_runtime.on_track_subscribed(
                    track_sid=track_sid,
                    source=visual_source,
                    width=int(getattr(track, "width", 0) or 0),
                    height=int(getattr(track, "height", 0) or 0),
                )
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] visual_runtime_on_track_subscribed_failed detail=%s", exc)

        task = asyncio.create_task(self._consume_screen_share(track, publication, participant))
        self._track_task(task)
        if track_sid:
            self._screen_share_tasks[track_sid] = task

    def _on_track_unsubscribed(
        self,
        track: rtc.RemoteTrack | None,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        del participant
        if not _is_livekit_visual_publication(publication):
            return
        track_sid = str(getattr(publication, "sid", "") or getattr(track, "sid", "") or "").strip()
        logger.info(
            "[VIDEO_RUNTIME] screen_track_unsubscribed track_sid=%s source=%s",
            track_sid or "unknown",
            _track_source_label(getattr(publication, "source", None)),
        )
        logger.info(
            "VISION_TRACK_UNSUBSCRIBED source=%s track_sid=%s",
            "screen_share" if _is_screen_share_publication(publication) else "webcam",
            track_sid or "unknown",
        )
        self._finalize_track(track_sid=track_sid, reason="track_unsubscribed")

    def _on_track_subscription_failed(
        self,
        participant: rtc.RemoteParticipant,
        track_sid: str,
        error: str,
    ) -> None:
        logger.warning(
            "[VIDEO_RUNTIME] screen_track_subscription_failed participant=%s sid=%s error=%s",
            getattr(participant, "identity", ""),
            track_sid,
            error,
        )

    def _ensure_screen_subscription(
        self,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
        *,
        reason: str,
    ) -> None:
        if not _is_livekit_visual_publication(publication):
            return
        if bool(getattr(publication, "subscribed", False)):
            return
        logger.info(
            "[VIDEO_RUNTIME] solicitando subscribe de screen share participant=%s sid=%s reason=%s",
            getattr(participant, "identity", ""),
            getattr(publication, "sid", ""),
            reason,
        )
        try:
            publication.set_subscribed(True)
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao solicitar subscribe da track de tela sid={getattr(publication, 'sid', '')}",
                exc,
                level=logging.WARNING,
            )

    async def _consume_screen_share(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        track_sid = str(getattr(publication, "sid", "") or getattr(track, "sid", "") or "").strip()
        stream = None
        frame_count = 0
        last_sample_at = 0.0
        try:
            stream = rtc.VideoStream.from_track(
                track=track,
                format=rtc.VideoBufferType.RGBA,
            )
            async for event in stream:
                if track_sid and track_sid not in self._track_sources:
                    # Quadro atrasado de uma tela que já parou: guardá-lo religava a "visão ativa" e a Kira dizia
                    # "a tela está compartilhada" sem compartilhamento (sessão de 2026-10-23 06:03).
                    logger.info("VISION_LATE_FRAME_DROPPED track_sid=%s", track_sid)
                    break
                now = time.monotonic()
                frame_count += 1
                frame = event.frame
                visual_source = _visual_source_from_publication(publication)
                frame_source_label = "screen_share" if visual_source == "livekit_screen_share" else "webcam"
                voice_active_for_visual = self._is_speech_priority_active() or self._should_skip_screen_analysis_for_voice()
                urgent_started_at = float(self._first_frame_urgent_started_at.get(track_sid, 0.0) or 0.0)
                first_frame_urgent = bool(urgent_started_at and track_sid not in self._first_frame_urgent_completed)
                if first_frame_urgent:
                    elapsed_ms = int((now - urgent_started_at) * 1000.0)
                    if elapsed_ms > int(SCREEN_FIRST_FRAME_TIMEOUT_SECONDS * 1000.0):
                        self._first_frame_urgent_completed.add(track_sid)
                        logger.warning("VISION_FIRST_FRAME_URGENT_TIMEOUT elapsed_ms=%s", elapsed_ms)
                        first_frame_urgent = False
                if first_frame_urgent:
                    target_interval = 0.0
                elif voice_active_for_visual:
                    target_interval = SCREEN_FRAME_SPEAKING_SAMPLE_SECONDS
                elif time.monotonic() < self._adaptive_fps_reduced_until:
                    target_interval = 1.0
                else:
                    target_interval = SCREEN_FRAME_IDLE_SAMPLE_SECONDS
                if now - last_sample_at < target_interval:
                    self._fps_window_skipped += 1
                    self._log_fps_summary_if_needed()
                    continue
                last_sample_at = now
                if voice_active_for_visual:
                    self._set_adaptive_fps(0.2, reason="kira_speaking")
                elif time.monotonic() < self._adaptive_fps_reduced_until:
                    self._set_adaptive_fps(1.0, reason="voice_lag")
                else:
                    self._set_adaptive_fps(2.0, reason="idle")
                if voice_active_for_visual:
                    frame_store = self._get_frame_store()
                    try:
                        record_light_frame = getattr(frame_store, "record_light_frame", None)
                        if callable(record_light_frame):
                            record_light_frame(
                                visual_source,
                                width=int(getattr(frame, "width", 0) or 0),
                                height=int(getattr(frame, "height", 0) or 0),
                                track_sid=track_sid,
                            )
                    except Exception as exc:
                        logger.debug("[VIDEO_RUNTIME] light_frame_metadata_failed detail=%s", exc)
                    if not self._ultra_light_mode_active:
                        self._ultra_light_mode_active = True
                        logger.info("VISION_CONTEXT_FREEZE_ENTER reason=kira_speaking")
                        logger.info("VISION_EVENT_LOOP_PROTECTION_ACTIVE")
                        logger.info("VOICE_PRIORITY_SUPREME_ACTIVE reason=vision_enabled")
                        logger.info("VISION_ALL_HEAVY_WORK_BLOCKED_DURING_SPEECH")
                        logger.info("VISION_ULTRA_LIGHT_MODE_ENTER_DURING_SPEECH source=%s", frame_source_label)
                    self._record_vision_skip(reason="voice_active")
                    self._log_visual_deferred_during_speech("frame_processing")
                    if not first_frame_urgent and now - self._last_track_kept_alive_log_at >= 5.0:
                        self._last_track_kept_alive_log_at = now
                        logger.info("VISION_FRAME_FULL_PROCESSING_SKIPPED_DURING_SPEECH source=%s", frame_source_label)
                        logger.info("VISION_FRAME_PROCESSING_DEFERRED_DURING_SPEECH source=%s", frame_source_label)
                        logger.info("VISION_HEAVY_WORK_SKIPPED_DURING_SPEECH")
                        logger.info("VISION_CONTEXT_SKIPPED reason=kira_speaking")
                        logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=kira_speaking")
                        logger.info("VISION_TRACK_KEPT_ALIVE_DURING_SPEECH source=%s", frame_source_label)
                    if first_frame_urgent:
                        logger.info("VISION_FRAME_LOG_RATE_LIMITED_DURING_SPEECH")
                    self._log_fps_summary_if_needed()
                    continue
                if self._ultra_light_mode_active:
                    self._ultra_light_mode_active = False
                    logger.info("VISION_CONTEXT_FREEZE_EXIT reason=speech_finished")
                    logger.info("VISION_ULTRA_LIGHT_MODE_EXIT_AFTER_SPEECH source=%s", frame_source_label)
                processing_started_at = time.monotonic()
                raw_width = int(getattr(frame, "width", 0) or 0)
                raw_height = int(getattr(frame, "height", 0) or 0)
                if raw_width <= 0 or raw_height <= 0:
                    logger.info("VISION_FRAME_INVALID_SKIPPED reason=zero_size")
                    logger.info("VISION_CONTEXT_SKIPPED reason=invalid_frame")
                    continue
                frame_bgr, image_bytes = self._frame_to_bgr_and_jpeg(frame)
                if frame_bgr is None or not image_bytes:
                    if now - self._last_frame_convert_failed_log_at >= 5.0:
                        self._last_frame_convert_failed_log_at = now
                        logger.warning(
                            "VISION_FRAME_STORE_UPDATE_FAILED reason=frame_convert_failed source=%s track_sid=%s",
                            frame_source_label,
                            track_sid or "unknown",
                        )
                    continue

                height, width = frame_bgr.shape[:2]
                if width <= 0 or height <= 0:
                    logger.info("VISION_FRAME_INVALID_SKIPPED reason=zero_size")
                    logger.info("VISION_CONTEXT_SKIPPED reason=invalid_frame")
                    continue
                frame_store = self._get_frame_store()
                if frame_store is not None:
                    try:
                        frame_store.update(
                            image_bytes=image_bytes,
                            source=visual_source,
                            width=width,
                            height=height,
                            metadata={
                                "participant": str(getattr(participant, "identity", "") or ""),
                                "track_sid": track_sid,
                                "frame_number": frame_count,
                                "track_source": _track_source_label(getattr(publication, "source", None)),
                                "sampled_at": time.time(),
                            },
                            frame_array=frame_bgr.copy(),
                            save_debug=False,
                        )
                        set_native_video_active = getattr(frame_store, "set_native_video_active", None)
                        if callable(set_native_video_active):
                            set_native_video_active(visual_source, True)
                        try:
                            from services.vision_livekit_state import mark_livekit_frame_received
                        except Exception:
                            mark_livekit_frame_received = None
                        if callable(mark_livekit_frame_received):
                            mark_livekit_frame_received(
                                source=visual_source,
                                width=width,
                                height=height,
                        )
                        self._fps_window_stored += 1
                        if not voice_active_for_visual:
                            await self._screen_feed.offer(frame_bgr)
                        processing_ms = int((time.monotonic() - processing_started_at) * 1000.0)
                        if processing_ms > 300:
                            self._adaptive_fps_reduced_until = time.monotonic() + 10.0
                            self._set_adaptive_fps(1.0, reason="voice_lag")
                            logger.info("VISION_ADAPTIVE_FPS_REDUCED reason=voice_lag")
                        if voice_active_for_visual:
                            logger.info("VISION_CONTEXT_SKIPPED reason=kira_speaking")
                            logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=kira_speaking")
                        elif now - self._last_light_context_update_at >= 10.0:
                            self._last_light_context_update_at = now
                            title = str(getattr(getattr(self._observer, "state", None), "active_window_title", "") or "").strip()
                            summary = (
                                f"janela ativa: {title}; frame {width}x{height}"
                                if title
                        else f"screen share ativo; frame {width}x{height}"
                            )
                            update_light_context = getattr(frame_store, "update_light_visual_context", None)
                            if callable(update_light_context):
                                update_light_context(source=visual_source, summary=summary, age_ms=0)
                            logger.info("VISION_CONTEXT_LIGHT_UPDATE_OK age_ms=0 summary=%s", summary[:160])
                        else:
                            remaining_ms = int(max(10.0 - (now - self._last_light_context_update_at), 0.0) * 1000.0)
                            logger.debug("VISION_CONTEXT_RATE_LIMITED_STRONG remaining_ms=%s", remaining_ms)
                        if first_frame_urgent:
                            elapsed_ms = int((time.monotonic() - urgent_started_at) * 1000.0)
                            self._first_frame_urgent_completed.add(track_sid)
                            logger.info("VISION_FIRST_FRAME_URGENT_STORED elapsed_ms=%s", elapsed_ms)
                    except Exception as exc:
                        logger.warning(
                            "VISION_FRAME_STORE_UPDATE_FAILED reason=%s source=%s track_sid=%s",
                            str(exc)[:120] or "unknown",
                            frame_source_label,
                            track_sid or "unknown",
                        )
                        continue
                    should_log_frame = (
                        frame_count == 1
                        or now - float(self._last_livekit_frame_log_at.get(visual_source, 0.0) or 0.0) >= 5.0
                    )
                    if should_log_frame:
                        self._last_livekit_frame_log_at[visual_source] = now
                        logger.info(
                            "VISION_LIVEKIT_FRAME_RECEIVED source=%s width=%s height=%s age_ms=0",
                            frame_source_label,
                            width,
                            height,
                        )
                        logger.info(
                            "VISION_FRAME_CACHE_UPDATED source=%s age_ms=0 width=%s height=%s",
                            visual_source,
                            width,
                            height,
                        )
                        logger.info(
                            "VISION_FRAME_STORE_LIGHT_UPDATE source=%s age_ms=0 width=%s height=%s",
                            visual_source,
                            width,
                            height,
                        )
                else:
                    logger.warning(
                        "VISION_FRAME_STORE_UPDATE_FAILED reason=store_unavailable source=%s track_sid=%s",
                        frame_source_label,
                        track_sid or "unknown",
                    )

                self._vision_metrics["frames_captured"] += 1
                if frame_count == 1:
                    logger.info("[VIDEO_RUNTIME] first_frame_received track_sid=%s size=%sx%s", track_sid or "unknown", width, height)
                    logger.info("VISION_CONTINUOUS_STREAM_ACTIVE source=%s state=receiving_frames", frame_source_label)
                elif frame_count % SCREEN_FRAME_DEBUG_LOG_EVERY == 0:
                    logger.info(
                        "[VIDEO_RUNTIME] sampled_frame track_sid=%s count=%s size=%sx%s",
                        track_sid or "unknown",
                        frame_count,
                        width,
                        height,
                    )

                self._log_fps_summary_if_needed()
                if self._visual_runtime is not None and not voice_active_for_visual:
                    try:
                        self._visual_runtime.observe_frame(
                            frame=frame,
                            source=visual_source,
                            track_sid=track_sid,
                        )
                    except Exception as exc:
                        logger.debug("[VIDEO_RUNTIME] visual_runtime_observe_failed detail=%s", exc)
                    self._schedule_visual_runtime_refresh(track_sid=track_sid)
                elif self._visual_runtime is not None:
                    self._log_visual_deferred_during_speech("visual_runtime_observe")

                try:
                    self._observer.observe_video_frame(
                        participant_identity=str(getattr(participant, "identity", "") or ""),
                        width=width,
                        height=height,
                        source=visual_source,
                        change_score=0.0,
                    )
                except Exception as exc:
                    logger.debug("[VIDEO_RUNTIME] observer_video_frame_failed detail=%s", exc)

                self._notify_visual_context_state(
                    enabled=True,
                    track_sid=track_sid,
                    label=str(getattr(participant, "identity", "") or ""),
                    width=width,
                    height=height,
                    visual_source=visual_source,
                )

                if self._screen_vision_engine is None:
                    continue
                if voice_active_for_visual:
                    self._record_vision_skip(reason="voice_active")
                    self._log_visual_deferred_during_speech("screen_analysis")
                    continue
                try:
                    await self._analyze_screen_frame(
                        frame=frame_bgr,
                        participant_identity=str(getattr(participant, "identity", "") or ""),
                        track_sid=track_sid,
                    )
                except Exception as exc:
                    logger.debug("[VIDEO_RUNTIME] screen_analysis_failed detail=%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha no consumo da track de tela sid={track_sid or 'unknown'}",
                exc,
                level=logging.WARNING,
            )
        finally:
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    logger.debug("[VIDEO_RUNTIME] Ignorando erro ao fechar VideoStream", exc_info=True)
            self._finalize_track(track_sid=track_sid, reason="stream_ended")

    async def _analyze_screen_frame(
        self,
        *,
        frame: np.ndarray,
        participant_identity: str,
        track_sid: str,
    ) -> None:
        if self._is_speech_priority_active() or self._should_skip_screen_analysis_for_voice():
            self._log_visual_deferred_during_speech("screen_analysis")
            logger.info("VISION_HEAVY_WORK_SKIPPED_DURING_SPEECH")
            logger.info("VISION_CONTEXT_SKIPPED reason=kira_speaking")
            logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=kira_speaking")
            return
        text_sources = [
            str(getattr(getattr(self._observer, "state", None), "active_window_title", "") or ""),
            str(getattr(getattr(self._observer, "state", None), "screen_summary", "") or ""),
        ]
        result, context_result, duplicate_context, proactive = await self._run_screen_analysis_in_pool(
            frame=frame,
            text_sources=[text for text in text_sources if text],
            now=time.monotonic(),
        )
        if result is not None and getattr(result, "event", None):
            payload = dict(getattr(result, "event", {}) or {})
            payload.update(
                {
                    "participant_identity": participant_identity,
                    "track_sid": track_sid,
                    "source": "livekit_screen_share",
                }
            )
            self._screen_vision_events.append(payload)
        if context_result is not None and hasattr(context_result, "as_dict") and not duplicate_context:
            now = time.monotonic()
            if now - self._last_light_context_update_at < 10.0:
                remaining_ms = int(max(10.0 - (now - self._last_light_context_update_at), 0.0) * 1000.0)
                logger.debug("VISION_CONTEXT_RATE_LIMITED_STRONG remaining_ms=%s", remaining_ms)
                return
            payload = dict(context_result.as_dict() or {})
            payload.update(
                {
                    "participant_identity": participant_identity,
                    "track_sid": track_sid,
                    "source": "livekit_screen_share",
                }
            )
            self._screen_context_events.append(payload)
            if self._is_speech_priority_active() or self._should_skip_screen_analysis_for_voice():
                logger.info("VISION_CONTEXT_SKIPPED reason=kira_speaking")
            else:
                logger.info("VISION_CONTEXT_INJECTED reason=relevant_to_conversation")
        if proactive is not None and hasattr(proactive, "as_dict"):
            proactive_payload = dict(proactive.as_dict() or {})
            if proactive_payload.get("should_notify"):
                if not self._visual_proactivity_enabled:
                    logger.info("VISION_SCREEN_CHANGE_SILENT_UPDATE")
                    logger.info("VISION_PROACTIVE_SPEECH_BLOCKED reason=disabled_by_default")
                    return
                now = time.monotonic()
                if self._is_speech_priority_active() or self._should_skip_screen_analysis_for_voice():
                    logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=kira_speaking")
                    return
                if now - self._last_visual_proactive_comment_at < 60.0:
                    logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=cooldown")
                    return
                self._last_visual_proactive_comment_at = now
                logger.info("VISION_PROACTIVE_COMMENT_ALLOWED")
                proactive_payload.setdefault("participant_identity", participant_identity)
                proactive_payload.setdefault("track_sid", track_sid)
                proactive_payload.setdefault("source", "livekit_screen_share")
                self._screen_proactive_events.append(proactive_payload)
            else:
                logger.info("VISION_PROACTIVE_COMMENT_SKIPPED reason=not_relevant")

    def _finalize_track(self, *, track_sid: str, reason: str) -> None:
        frame_store = self._get_frame_store()
        visual_source = self._track_sources.pop(track_sid, "livekit_screen_share")
        if frame_store is not None:
            try:
                frame_store.set_track_active(visual_source, False, track_sid=track_sid, label=self._active_track_label)
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] frame_store_track_inactive_failed detail=%s", exc)
            frame_store.clear_source(visual_source)
        logger.info(
            "VISION_STATE_RESET reason=track_ended source=%s track_sid=%s",
            "screen_share" if visual_source == "livekit_screen_share" else "webcam",
            track_sid or "unknown",
        )
        if self._visual_runtime is not None:
            try:
                self._visual_runtime.on_track_ended(
                    track_sid=track_sid,
                    source=visual_source,
                    reason=reason,
                )
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] visual_runtime_on_track_ended_failed detail=%s", exc)
        self._notify_visual_context_state(
            enabled=False,
            track_sid=track_sid,
            label=self._active_track_label,
            width=0,
            height=0,
            visual_source=visual_source,
        )
        self._screen_share_tasks.pop(track_sid, None)
        if track_sid and track_sid == self._active_track_sid:
            self._active_track_sid = ""
            self._active_track_label = ""

    def _notify_visual_context_state(
        self,
        *,
        enabled: bool,
        track_sid: str,
        label: str,
        width: int,
        height: int,
        visual_source: str = "livekit_screen_share",
    ) -> None:
        if not callable(self._signal_handler):
            return
        visual_source = str(visual_source or "livekit_screen_share").strip().lower() or "livekit_screen_share"
        payload = {
            "type": "visual_context_state",
            "enabled": bool(enabled),
            "surface": "monitor",
            "label": str(label or "").strip(),
            "track_sid": str(track_sid or "").strip(),
            "source": visual_source,
            "width": int(width or 0),
            "height": int(height or 0),
        }
        try:
            self._signal_handler(payload)
        except Exception as exc:
            logger.debug("[VIDEO_RUNTIME] visual_context_signal_failed detail=%s", exc)

    def _schedule_visual_runtime_refresh(self, *, track_sid: str) -> None:
        if self._visual_runtime is None:
            return
        key = str(track_sid or "default")
        if self._is_speech_priority_active():
            if key not in self._visual_refresh_deferred_keys:
                self._visual_refresh_deferred_keys.add(key)
                get_voice_output_manager().defer_until_speech_done(
                    "visual_runtime_refresh",
                    lambda key=key: self._flush_deferred_visual_refresh(key),
                )
            self._log_visual_deferred_during_speech("visual_runtime_refresh")
            return
        existing = self._visual_refresh_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def _runner() -> None:
            try:
                await self._visual_runtime.refresh_observer_state(require_ui=False)
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] visual_runtime_refresh_failed track_sid=%s detail=%s", key, exc)
            finally:
                self._visual_refresh_tasks.pop(key, None)

        task = asyncio.create_task(_runner())
        self._visual_refresh_tasks[key] = task
        self._track_task(task)

    def _flush_deferred_visual_refresh(self, key: str) -> None:
        self._visual_refresh_deferred_keys.discard(key)
        self._schedule_visual_runtime_refresh(track_sid=key)

    async def _run_screen_analysis_in_pool(
        self,
        *,
        frame,
        text_sources: list[str],
        now: float,
    ) -> tuple[object | None, object | None, bool, object | None]:
        loop = asyncio.get_running_loop()

        def _work() -> tuple[object | None, object | None, bool, object | None]:
            result = None
            context_result = None
            duplicate_context = False
            proactive = None
            if self._screen_vision_engine is not None:
                result = self._screen_vision_engine.process_screen_frame(
                    frame,
                    text_sources=list(text_sources),
                    now=now,
                )
            if result is not None and self._screen_context_analyzer is not None:
                context_result = self._screen_context_analyzer.analyze_screen_context(getattr(result, "event", {}) or {})
                duplicate_checker = getattr(self._screen_context_analyzer, "is_duplicate_context", None)
                if callable(duplicate_checker):
                    duplicate_context = bool(duplicate_checker(context_result, now=now))
            if context_result is not None and self._screen_proactive_engine is not None:
                proactive = self._screen_proactive_engine.evaluate_screen_proactivity(
                    context_result.as_dict(),
                    now=now,
                )
            return result, context_result, duplicate_context, proactive

        result = await loop.run_in_executor(self._analysis_pool, _work)
        self._vision_metrics["frames_analyzed"] += 1
        return result

    def _should_skip_screen_analysis_for_voice(self) -> bool:
        provider = self._voice_state_provider
        if not callable(provider):
            return False
        try:
            state = provider()
        except Exception:
            return False
        normalized = str(getattr(state, "value", state) or "").strip().lower()
        return normalized in {"speaking", "processing"}

    def _record_vision_skip(self, *, reason: str) -> None:
        self._vision_metrics["frames_skipped"] += 1
        counters = self._vision_metrics["skip_reasons"]
        counters[reason] = int(counters.get(reason, 0)) + 1

    def _track_task(self, task: asyncio.Task) -> None:
        self._active_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            self._active_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("[VIDEO_RUNTIME] background_task_failed detail=%s", exc)

        task.add_done_callback(_cleanup)

    @staticmethod
    def _frame_to_bgr_and_jpeg(frame: rtc.VideoFrame) -> tuple[np.ndarray | None, bytes | None]:
        try:
            converted = frame if frame.type == rtc.VideoBufferType.RGBA else frame.convert(rtc.VideoBufferType.RGBA)
            rgba = Image.frombytes("RGBA", (frame.width, frame.height), bytes(converted.data))
            rgb = rgba.convert("RGB")
            frame_bgr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
            ok, encoded = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                return frame_bgr, None
            return frame_bgr, encoded.tobytes()
        except Exception as exc:
            logger.debug("[VIDEO_RUNTIME] frame_convert_failed detail=%s", exc)
            return None, None


__all__ = ["RoomVideoBridge"]
