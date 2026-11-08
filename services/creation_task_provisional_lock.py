from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class CreationTaskProvisionalLock:
    def __init__(self) -> None:
        self._kind = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def set(self, kind: str, text: str, *, ttl_s: float = 30.0) -> bool:
        normalized_kind = str(kind or "").strip().lower()
        if not normalized_kind:
            return False
        with self._lock:
            newly_detected = not self._kind or self._kind != normalized_kind or time.monotonic() > self._expires_at
            self._kind = normalized_kind
            self._expires_at = time.monotonic() + max(float(ttl_s), 0.5)
        if newly_detected:
            logger.info(
                "CREATION_TASK_PROVISIONAL_DETECTED source=partial kind=%s text_preview=%r",
                normalized_kind,
                str(text or "").strip()[:120],
            )
        logger.info("CREATION_TASK_PROVISIONAL_LOCK_SET kind=%s ttl_s=%s", normalized_kind, int(ttl_s))
        return True

    def active_kind(self) -> str | None:
        with self._lock:
            if not self._kind:
                return None
            if time.monotonic() <= self._expires_at:
                return self._kind
            self._kind = ""
            self._expires_at = 0.0
        logger.info("CREATION_TASK_PROVISIONAL_RELEASED reason=timeout")
        return None

    def confirm(self) -> str | None:
        kind = self.active_kind()
        if not kind:
            return None
        with self._lock:
            self._kind = ""
            self._expires_at = 0.0
        logger.info("CREATION_TASK_PROVISIONAL_CONFIRMED")
        return kind

    def release(self, reason: str = "final_not_creation") -> bool:
        with self._lock:
            had_lock = bool(self._kind)
            self._kind = ""
            self._expires_at = 0.0
        if had_lock:
            logger.info("CREATION_TASK_PROVISIONAL_RELEASED reason=%s", reason)
        return had_lock

    def blocks_route(self, route: str) -> bool:
        if not self.active_kind():
            return False
        event = {
            "gemini_auto": "GEMINI_AUTO_REPLY_BLOCKED_BY_CREATION_PROVISIONAL",
            "normal_reply": "NORMAL_REPLY_BLOCKED_BY_CREATION_PROVISIONAL",
            "fuzzy": "FUZZY_ROUTE_BLOCKED_BY_CREATION_PROVISIONAL",
            "internet": "INTERNET_ROUTE_BLOCKED_BY_CREATION_PROVISIONAL",
        }.get(str(route or "").strip().lower())
        if event:
            logger.info(event)
        return True


creation_task_provisional_lock = CreationTaskProvisionalLock()
