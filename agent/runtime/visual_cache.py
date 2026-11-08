from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class _VisualCacheEntry:
    timestamp: float
    frame_hash: str
    signature: np.ndarray
    response: str


class VisualCache:
    def __init__(self, ttl_seconds=300, diff_threshold=0.10, enabled=True):
        self.enabled = bool(enabled)
        self.ttl_seconds = max(float(ttl_seconds or 0), 0.0)
        self.diff_threshold = max(float(diff_threshold or 0.0), 0.0)
        self._entries: dict[str, _VisualCacheEntry] = {}
        if self.enabled:
            logger.info(
                "VISUAL_CACHE_CONFIG enabled=true ttl_s=%.0f diff_threshold=%.3f",
                self.ttl_seconds,
                self.diff_threshold,
            )
        else:
            logger.info("VISUAL_CACHE_CONFIG enabled=false fallback=miss")

    def _normalize_question(self, question: str) -> str:
        return " ".join(str(question or "").strip().lower().split())

    def _frame_signature(self, frame: Any) -> np.ndarray:
        arr = np.asarray(frame)
        if arr.size == 0:
            return np.zeros((1,), dtype=np.uint8)
        if arr.ndim == 3:
            arr = arr[..., :3].mean(axis=2)
        arr = np.asarray(arr, dtype=np.float32)
        rows = np.linspace(0, arr.shape[0] - 1, num=min(16, arr.shape[0]), dtype=int)
        cols = np.linspace(0, arr.shape[1] - 1, num=min(16, arr.shape[1]), dtype=int) if arr.ndim >= 2 else np.array([0])
        sampled = arr[np.ix_(rows, cols)] if arr.ndim >= 2 else arr[rows]
        return np.clip(sampled, 0, 255).astype(np.uint8).reshape(-1)

    def _compute_frame_hash(self, frame: Any) -> str:
        signature = self._frame_signature(frame)
        return hashlib.sha1(signature.tobytes()).hexdigest()

    def _frame_diff_ratio(self, a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            return 1.0
        if a.size == 0:
            return 0.0
        return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))) / 255.0)

    def get(self, frame: Any, question: str, *args, **kwargs):
        return self.get_cached_response(frame, question)

    def store(self, frame: Any, question: str, response: str, *args, **kwargs):
        return self.store_response(frame, question, response)

    def get_cached_response(self, frame: Any, question: str, *args, **kwargs):
        if not self.enabled:
            logger.info("VISUAL_CACHE_MISS reason=disabled")
            return None
        key = self._normalize_question(question)
        entry = self._entries.get(key)
        if entry is None:
            logger.info("VISUAL_CACHE_MISS reason=no_entry")
            return None
        now = time.time()
        if self.ttl_seconds and (now - entry.timestamp) > self.ttl_seconds:
            self._entries.pop(key, None)
            logger.info("VISUAL_CACHE_MISS reason=ttl_expired")
            return None
        signature = self._frame_signature(frame)
        frame_hash = hashlib.sha1(signature.tobytes()).hexdigest()
        if frame_hash != entry.frame_hash:
            diff = self._frame_diff_ratio(signature, entry.signature)
            if diff > self.diff_threshold:
                self._entries.pop(key, None)
                logger.info("MISS - Frame mudou (invalidando cache) diff=%.3f threshold=%.3f", diff, self.diff_threshold)
                logger.info("VISUAL_CACHE_MISS reason=frame_changed diff=%.3f", diff)
                return None
        logger.info("VISUAL_CACHE_HIT question=%r", key[:80])
        return entry.response

    def store_response(self, frame: Any, question: str, response: str, *args, **kwargs):
        if not self.enabled:
            logger.info("VISUAL_CACHE_STORE_SKIPPED reason=disabled")
            return None
        key = self._normalize_question(question)
        signature = self._frame_signature(frame)
        self._entries[key] = _VisualCacheEntry(
            timestamp=time.time(),
            frame_hash=hashlib.sha1(signature.tobytes()).hexdigest(),
            signature=signature,
            response=str(response or ""),
        )
        logger.info("VISUAL_CACHE_STORE question=%r", key[:80])
        return None

    def clear(self):
        self._entries.clear()
        logger.info("VISUAL_CACHE_CLEARED")
        return None


__all__ = ["VisualCache"]
