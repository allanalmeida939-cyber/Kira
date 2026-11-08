from __future__ import annotations

import logging
import threading
import time


logger = logging.getLogger(__name__)


class UserActivityMonitor:
    """Monitora atividade recente do usuario para proteger o modo visao."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_activity = time.monotonic()
        self._last_source = "startup"
        logger.info("[ACTIVITY_MONITOR] Inicializado")

    def update_activity(self, source: str = "unknown") -> None:
        normalized_source = str(source or "unknown").strip() or "unknown"
        with self._lock:
            self._last_activity = time.monotonic()
            self._last_source = normalized_source
        logger.debug(
            "[ACTIVITY_MONITOR] Atividade detectada - source=%s",
            normalized_source,
        )

    def get_idle_time(self) -> float:
        with self._lock:
            return max(time.monotonic() - self._last_activity, 0.0)

    def is_idle(self, threshold_seconds: float) -> bool:
        return self.get_idle_time() > max(float(threshold_seconds), 0.0)

    def get_last_activity_source(self) -> str:
        with self._lock:
            return self._last_source


__all__ = ["UserActivityMonitor"]
