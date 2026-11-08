from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta

from time_utils import now_brasilia


logger = logging.getLogger(__name__)


class ProactiveThrottle:
    """
    Limita falas proativas autonomas por voz.
    """

    MAX_VOICE_PROACTIVE_PER_HOUR = 5

    def __init__(self) -> None:
        self.voice_timestamps = deque(maxlen=20)

    def _cleanup_old_timestamps(self) -> None:
        cutoff = now_brasilia() - timedelta(hours=1)
        while self.voice_timestamps and self.voice_timestamps[0] < cutoff:
            self.voice_timestamps.popleft()

    def can_speak_proactively(self) -> bool:
        self._cleanup_old_timestamps()
        count_last_hour = len(self.voice_timestamps)
        if count_last_hour >= self.MAX_VOICE_PROACTIVE_PER_HOUR:
            logger.info(
                "[PROACTIVE_THROTTLE] Bloqueado %s/%s falas proativas na ultima hora.",
                count_last_hour,
                self.MAX_VOICE_PROACTIVE_PER_HOUR,
            )
            return False
        return True

    def register_voice_proactive(self) -> None:
        self.voice_timestamps.append(now_brasilia())
        logger.info(
            "[PROACTIVE_THROTTLE] Fala proativa registrada total=%s/%s",
            len(self.voice_timestamps),
            self.MAX_VOICE_PROACTIVE_PER_HOUR,
        )

    def get_status(self) -> dict[str, object]:
        self._cleanup_old_timestamps()
        count = len(self.voice_timestamps)
        return {
            "count_last_hour": count,
            "limit": self.MAX_VOICE_PROACTIVE_PER_HOUR,
            "remaining": max(0, self.MAX_VOICE_PROACTIVE_PER_HOUR - count),
            "can_speak": count < self.MAX_VOICE_PROACTIVE_PER_HOUR,
        }
