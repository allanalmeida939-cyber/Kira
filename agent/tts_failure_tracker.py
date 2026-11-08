from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from time_utils import now_brasilia


logger = logging.getLogger(__name__)


class TTSFailureTracker:
    """
    Evita reenvio em loop da mesma frase que acabou de falhar no TTS.
    """

    FAILURE_TTL_MINUTES = 60
    MAX_RETRY_PER_TEXT = 1

    def __init__(self) -> None:
        self.failed_texts: dict[str, dict[str, object]] = {}

    def hash_text(self, text: str) -> str:
        return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()

    def _cleanup(self) -> None:
        now = now_brasilia()
        expired = [
            key
            for key, payload in self.failed_texts.items()
            if payload.get("until") is not None and payload["until"] < now
        ]
        for key in expired:
            self.failed_texts.pop(key, None)

    def should_skip(self, text: str) -> bool:
        self._cleanup()
        text_hash = self.hash_text(text)
        payload = self.failed_texts.get(text_hash)
        if not payload:
            return False
        failures = int(payload.get("count", 0) or 0)
        if failures >= self.MAX_RETRY_PER_TEXT:
            logger.warning(
                "[TTS_FAILURE] Pulando fala repetida que ja falhou count=%s preview=%r",
                failures,
                str(text or "")[:80],
            )
            return True
        return False

    def register_failure(self, text: str, *, reason: str = "quota") -> None:
        self._cleanup()
        text_hash = self.hash_text(text)
        now = now_brasilia()
        payload = self.failed_texts.setdefault(
            text_hash,
            {
                "count": 0,
                "until": now + timedelta(minutes=self.FAILURE_TTL_MINUTES),
                "text_preview": str(text or "")[:100],
                "reason": reason,
            },
        )
        payload["count"] = int(payload.get("count", 0) or 0) + 1
        payload["until"] = now + timedelta(minutes=self.FAILURE_TTL_MINUTES)
        payload["reason"] = reason
        logger.warning(
            "[TTS_FAILURE] Falha registrada count=%s reason=%s preview=%r",
            payload["count"],
            reason,
            str(text or "")[:80],
        )

    def register_success(self, text: str) -> None:
        self.failed_texts.pop(self.hash_text(text), None)

    def get_status(self) -> dict[str, object]:
        self._cleanup()
        return {
            "tracked_failures": len(self.failed_texts),
            "max_retry_per_text": self.MAX_RETRY_PER_TEXT,
        }
