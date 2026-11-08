"""Buffer incremental para streaming de resposta do Gemini."""
from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

_SENTENCE_END_RE = re.compile(r"[.!?](?:\s+|$)")


class StreamingResponseBuffer:
    """Agrupa deltas de texto em chunks prontos para TTS/UI."""

    def __init__(self, on_chunk_ready: Callable[[str], None], min_words: int = 4) -> None:
        self.on_chunk_ready = on_chunk_ready
        self.min_words = max(int(min_words), 1)
        self._buffer = ""
        self._chunks_sent = 0

    @property
    def chunks_sent(self) -> int:
        return self._chunks_sent

    def add_text(self, text: str) -> None:
        incoming = str(text or "")
        if not incoming:
            return
        self._buffer += incoming
        self._drain_ready_chunks()

    def _drain_ready_chunks(self) -> None:
        while True:
            sentence_match = _SENTENCE_END_RE.search(self._buffer)
            if sentence_match:
                chunk = self._buffer[: sentence_match.end()].strip()
                words = chunk.split()
                if len(words) >= self.min_words:
                    self._emit(chunk)
                    self._buffer = self._buffer[sentence_match.end() :]
                    continue

            words = self._buffer.split()
            if len(words) > 25:
                self._emit(self._buffer.strip())
                self._buffer = ""
            return

    def _emit(self, chunk: str) -> None:
        normalized = str(chunk or "").strip()
        if not normalized:
            return
        self._chunks_sent += 1
        logger.info("[STREAMING] Chunk #%d - texto=%r", self._chunks_sent, normalized[:80])
        self.on_chunk_ready(normalized)

    def flush(self) -> None:
        if self._buffer.strip():
            self._emit(self._buffer.strip())
            self._buffer = ""


StreamingBuffer = StreamingResponseBuffer

__all__ = ["StreamingBuffer", "StreamingResponseBuffer"]
