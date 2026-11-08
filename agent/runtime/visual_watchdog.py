from __future__ import annotations

import logging
import threading
import time
from typing import Callable


logger = logging.getLogger(__name__)


class VisualWatchdog:
    """Monitora stalls do pipeline visual sem bloquear o loop principal."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 5.0,
        on_timeout: Callable[[float], None] | None = None,
    ) -> None:
        self._timeout_seconds = max(float(timeout_seconds), 0.1)
        self._poll_seconds = max(float(poll_seconds), 0.1)
        self._on_timeout = on_timeout
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._processing_active = False
        self._last_activity = time.monotonic()
        self._last_timeout_idle = 0.0
        self._timeout_signaled = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
            return bool(self._running and thread is not None and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._processing_active = False
            self._timeout_signaled = False
            self._last_activity = time.monotonic()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._watch_loop,
                name="kira-visual-watchdog",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "[VISUAL_WATCHDOG] started timeout_s=%.1f poll_s=%.1f",
            self._timeout_seconds,
            self._poll_seconds,
        )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._processing_active = False
            self._timeout_signaled = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        logger.info("[VISUAL_WATCHDOG] stopped")

    def mark_processing_started(self) -> None:
        with self._lock:
            self._processing_active = True
            self._timeout_signaled = False
            self._last_timeout_idle = 0.0
            self._last_activity = time.monotonic()
        logger.info("[VISUAL_WATCHDOG] processing_started")

    def mark_processing_finished(self) -> None:
        with self._lock:
            was_active = self._processing_active
            self._processing_active = False
            self._timeout_signaled = False
            self._last_timeout_idle = 0.0
            self._last_activity = time.monotonic()
        if was_active:
            logger.info("[VISUAL_WATCHDOG] processing_finished")

    def ping(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def get_status(self) -> dict[str, object]:
        with self._lock:
            idle_seconds = time.monotonic() - self._last_activity
            return {
                "is_running": self._running,
                "processing_active": self._processing_active,
                "timeout_seconds": self._timeout_seconds,
                "poll_seconds": self._poll_seconds,
                "idle_seconds": round(idle_seconds, 3),
                "last_timeout_idle_seconds": (
                    round(self._last_timeout_idle, 3) if self._last_timeout_idle > 0 else None
                ),
            }

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            idle_seconds = 0.0
            should_signal = False
            with self._lock:
                if not self._running or not self._processing_active:
                    continue
                idle_seconds = time.monotonic() - self._last_activity
                if idle_seconds > self._timeout_seconds and not self._timeout_signaled:
                    self._timeout_signaled = True
                    self._last_timeout_idle = idle_seconds
                    should_signal = True
            if not should_signal:
                continue
            logger.error(
                "[VISUAL_WATCHDOG] stall_detected idle_s=%.2f timeout_s=%.2f",
                idle_seconds,
                self._timeout_seconds,
            )
            if self._on_timeout is None:
                continue
            try:
                self._on_timeout(idle_seconds)
            except Exception:
                logger.exception("[VISUAL_WATCHDOG] on_timeout_failed")


__all__ = ["VisualWatchdog"]
