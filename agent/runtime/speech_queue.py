from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from itertools import count
from typing import Any, Callable

_QUEUE_STATUS_LOG_INTERVAL_SECONDS = 5.0

logger = logging.getLogger(__name__)

_QUEUE_SEQUENCE = count(1)
MAX_QUEUE_SIZE = 2
MAX_TURN_AGE_S = 8.0

# BLOCO 5B1 — Lanes nomeadas
LANE_NORMAL_REPLY = "normal_reply"
LANE_TOOL_ACK = "tool_ack"
LANE_WHATSAPP_TOOL = "whatsapp_tool"
LANE_PROACTIVE = "proactive"
LANE_VISION_COMMENT = "vision_comment"
LANE_SYSTEM_NOTIFICATION = "system_notification"

VALID_LANES = {
    LANE_NORMAL_REPLY,
    LANE_TOOL_ACK,
    LANE_WHATSAPP_TOOL,
    LANE_PROACTIVE,
    LANE_VISION_COMMENT,
    LANE_SYSTEM_NOTIFICATION,
}


class SpeechPriority(IntEnum):
    SYSTEM = 1
    PROACTIVE = 2
    CONFIRMATION = 3
    USER_REPLY = 5


@dataclass(slots=True)
class SpeechRequest:
    id: str
    text: str
    source: str
    priority: SpeechPriority
    protected: bool
    payload: Any = None
    dedupe_key: str = ""
    created_at: float = field(default_factory=time.monotonic)
    metadata: dict[str, Any] = field(default_factory=dict)
    lane: str = "normal_reply"  # BLOCO5B1: lane nomeada
    _sequence: int = field(default_factory=lambda: next(_QUEUE_SEQUENCE), repr=False)


class SpeechQueue:
    def __init__(self, *, name: str = "speech-queue") -> None:
        self._name = str(name or "speech-queue")
        self._heap: list[tuple[int, float, int, str]] = []
        self._requests: dict[str, SpeechRequest] = {}
        self._active_request_id: str | None = None
        self._enqueued_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._dropped_count = 0
        self._last_status_log_at: float = 0.0
        # BLOCO5B1: contadores por lane
        self._lane_stats: dict[str, dict[str, int]] = {
            lane: {"enqueued": 0, "dequeued": 0, "deduped": 0} for lane in VALID_LANES
        }

    @property
    def active_request_id(self) -> str | None:
        return self._active_request_id

    @property
    def size(self) -> int:
        return len(self._requests)

    def enqueue(self, request: SpeechRequest) -> bool:
        # BLOCO5B1: normaliza lane
        if request.lane not in VALID_LANES:
            request.lane = LANE_NORMAL_REPLY
        if request.id in self._requests:
            logger.debug("[QUEUE] duplicate_enqueue_ignored id=%s name=%s", request.id, self._name)
            try:
                self._lane_stats[request.lane]["deduped"] += 1
            except Exception:
                pass
            return False
        now = time.monotonic()
        if str(request.source or "").strip().lower() != "ui_panel":
            expired = [
                req for req in list(self._requests.values())
                if str(req.source or "").strip().lower() != "ui_panel"
                and (now - req.created_at) > MAX_TURN_AGE_S
            ]
            for req in expired:
                self._requests.pop(req.id, None)
                self._dropped_count += 1
                logger.info(
                    "QUEUE_TURN_EXPIRED_DROPPED turn_id=%s source=%s age=%.1fs",
                    req.id,
                    req.source,
                    now - req.created_at,
                )
            normal = [req for req in self._requests.values() if str(req.source or "").strip().lower() != "ui_panel"]
            if len(normal) >= MAX_QUEUE_SIZE:
                oldest = min(normal, key=lambda req: req.created_at)
                self._requests.pop(oldest.id, None)
                self._dropped_count += 1
                logger.info("QUEUE_OVERFLOW_DROP turn_id=%s source=%s", oldest.id, oldest.source)
            self._heap = [item for item in self._heap if item[3] in self._requests]
            heapq.heapify(self._heap)
        self._requests[request.id] = request
        heapq.heappush(
            self._heap,
            (-int(request.priority), request.created_at, request._sequence, request.id),
        )
        self._enqueued_count += 1
        # BLOCO5B1: contabiliza e loga por lane
        try:
            self._lane_stats[request.lane]["enqueued"] += 1
        except Exception:
            pass
        logger.info(
            "[QUEUE] enqueued id=%s priority=%s size=%s source=%s lane=%s",
            request.id,
            int(request.priority),
            self.size,
            request.source,
            request.lane,
        )
        logger.info("[BLOCO5B1] enqueue lane=%s priority=%s text_len=%s", request.lane, int(request.priority), len(request.text))
        self._log_queue_status()
        return True

    def get_next(self) -> SpeechRequest | None:
        while self._heap:
            _, _, _, request_id = heapq.heappop(self._heap)
            request = self._requests.pop(request_id, None)
            if request is None:
                continue
            self._active_request_id = request.id
            # BLOCO5B1: contabiliza dequeue por lane
            try:
                self._lane_stats[request.lane]["dequeued"] += 1
            except Exception:
                pass
            logger.info(
                "[QUEUE] processing id=%s priority=%s remaining=%s source=%s lane=%s",
                request.id,
                int(request.priority),
                self.size,
                request.source,
                request.lane,
            )
            self._log_queue_status()
            return request
        self._active_request_id = None
        return None

    def mark_completed(self, request_id: str) -> None:
        if self._active_request_id == request_id:
            self._active_request_id = None
        self._completed_count += 1
        logger.info("[QUEUE] completed id=%s completed=%s", request_id, self._completed_count)

    def mark_failed(self, request_id: str, *, reason: str) -> None:
        if self._active_request_id == request_id:
            self._active_request_id = None
        self._failed_count += 1
        logger.warning("[QUEUE] failed id=%s reason=%s failed=%s", request_id, reason, self._failed_count)

    def clear(self) -> list[SpeechRequest]:
        dropped: list[SpeechRequest] = list(self._requests.values())
        self._heap.clear()
        self._requests.clear()
        self._active_request_id = None
        if dropped:
            self._dropped_count += len(dropped)
            logger.warning("[QUEUE] cleared dropped=%s name=%s", len(dropped), self._name)
        return dropped

    def remove_where(self, predicate: Callable[[SpeechRequest], bool]) -> list[SpeechRequest]:
        dropped: list[SpeechRequest] = []
        kept_ids: set[str] = set()
        for request_id, request in list(self._requests.items()):
            if predicate(request):
                dropped.append(request)
                self._requests.pop(request_id, None)
            else:
                kept_ids.add(request_id)
        if dropped:
            self._dropped_count += len(dropped)
            self._heap = [item for item in self._heap if item[3] in kept_ids]
            heapq.heapify(self._heap)
            logger.warning("[QUEUE] removed dropped=%s remaining=%s", len(dropped), self.size)
        return dropped

    def drop_stale(self, *, max_age_seconds: float) -> list[SpeechRequest]:
        now = time.monotonic()
        return self.remove_where(
            lambda request: (now - request.created_at) > max(max_age_seconds, 0.0)
        )

    def _log_queue_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_status_log_at) < _QUEUE_STATUS_LOG_INTERVAL_SECONDS:
            return
        self._last_status_log_at = now
        pending = list(self._requests.values())
        pending_ids = [r.id for r in pending]
        sources = list({r.source for r in pending})
        logger.info(
            "QUEUE_STATUS name=%s size=%s processing=%s pending_ids=%s sources=%s enqueued=%s completed=%s failed=%s dropped=%s",
            self._name,
            self.size,
            self._active_request_id or "none",
            pending_ids or [],
            sources or [],
            self._enqueued_count,
            self._completed_count,
            self._failed_count,
            self._dropped_count,
        )

    def get_lane_stats(self) -> dict[str, dict[str, int]]:
        """Retorna snapshot das estatísticas por lane. BLOCO5B1."""
        try:
            return {lane: dict(stats) for lane, stats in self._lane_stats.items()}
        except Exception:
            return {}

    def get_stats(self) -> dict[str, object]:
        return {
            "name": self._name,
            "queued": self.size,
            "active_request_id": self._active_request_id,
            "enqueued_count": self._enqueued_count,
            "completed_count": self._completed_count,
            "failed_count": self._failed_count,
            "dropped_count": self._dropped_count,
        }
