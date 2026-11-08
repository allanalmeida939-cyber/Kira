from __future__ import annotations

import logging
import os
from datetime import time

from time_utils import now_brasilia


logger = logging.getLogger(__name__)


class SilenceWindow:
    """
    Janela de silencio para proatividade por voz.
    """

    def __init__(self) -> None:
        self.silence_start_hour = int(os.getenv("KIRA_SILENCE_START_HOUR", "0"))
        self.silence_end_hour = int(os.getenv("KIRA_SILENCE_END_HOUR", "8"))

    def is_silent_hour(self) -> bool:
        current = now_brasilia().time()
        start = time(self.silence_start_hour, 0)
        end = time(self.silence_end_hour, 0)
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def should_block_voice_proactive(self) -> bool:
        if self.is_silent_hour():
            logger.info(
                "[SILENCE_WINDOW] Voz proativa bloqueada horario=%02d:00-%02d:00",
                self.silence_start_hour,
                self.silence_end_hour,
            )
            return True
        return False

    def get_status(self) -> dict[str, object]:
        now = now_brasilia()
        return {
            "is_silent": self.is_silent_hour(),
            "current_time": now.strftime("%H:%M:%S"),
            "silence_window": f"{self.silence_start_hour:02d}:00 - {self.silence_end_hour:02d}:00",
        }
