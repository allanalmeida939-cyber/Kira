from __future__ import annotations

import logging
from collections import deque
from datetime import datetime


logger = logging.getLogger(__name__)


class VoiceMetrics:
    def __init__(self) -> None:
        self.response_times = deque(maxlen=100)
        self.completion_rate = deque(maxlen=100)
        self.interruptions = deque(maxlen=100)

    def record_response_time(self, seconds: float) -> None:
        clamped = max(0.0, float(seconds))
        self.response_times.append(clamped)
        if clamped > 2.0:
            logger.warning("[VOICE_METRICS] response_slow seconds=%.2f", clamped)

    def record_completion(self, completed: bool) -> None:
        self.completion_rate.append(1 if completed else 0)

    def record_interruption(self, reason: str) -> None:
        normalized_reason = str(reason or "unknown").strip() or "unknown"
        self.interruptions.append(
            {
                "reason": normalized_reason,
                "timestamp": datetime.now(),
            }
        )
        logger.warning("[VOICE_METRICS] interruption reason=%s", normalized_reason)

    def get_health_report(self) -> dict[str, object]:
        if not self.response_times:
            return {
                "status": "no_data",
                "avg_response_time": 0.0,
                "completion_rate": 100.0,
                "recent_interruptions": 0,
            }

        avg_response = sum(self.response_times) / max(len(self.response_times), 1)
        completion_pct = (
            (sum(self.completion_rate) / len(self.completion_rate)) * 100
            if self.completion_rate
            else 100.0
        )
        recent_interruptions = len(
            [
                item
                for item in self.interruptions
                if (datetime.now() - item["timestamp"]).total_seconds() < 300
            ]
        )

        if avg_response < 1.5 and completion_pct > 95:
            status = "excellent"
        elif avg_response < 2.5 and completion_pct > 85:
            status = "good"
        elif avg_response < 4.0 and completion_pct > 75:
            status = "degraded"
        else:
            status = "poor"

        return {
            "status": status,
            "avg_response_time": round(avg_response, 2),
            "completion_rate": round(completion_pct, 1),
            "recent_interruptions": recent_interruptions,
        }


_voice_metrics: VoiceMetrics | None = None


def get_voice_metrics() -> VoiceMetrics:
    global _voice_metrics
    if _voice_metrics is None:
        _voice_metrics = VoiceMetrics()
    return _voice_metrics
