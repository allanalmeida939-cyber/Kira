"""Non-blocking clean dialogue log for final user and Kira messages."""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


logger = logging.getLogger("kira.dialogue_log")

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "Kira"
DEFAULT_MAX_QUEUE_SIZE = 500
DEFAULT_DEDUPE_WINDOW_SECONDS = 2.0
DEFAULT_MAX_TEXT_CHARS = 4000
_INTERNAL_MARKERS = ("SYSTEM_NOTE_FOR_REASONING_ONLY", "SYSTEM_NOTE:")
_STOP = object()


@dataclass(frozen=True)
class _DialogueEntry:
    speaker: str
    text: str
    source: str
    created_at: float


class DialogueLogWriter:
    """Write final dialogue events on a small independent background thread."""

    def __init__(
        self,
        log_dir: Path,
        *,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        dedupe_window_seconds: float = DEFAULT_DEDUPE_WINDOW_SECONDS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        write_multiline: bool = True,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.max_queue_size = max(1, int(max_queue_size))
        self.dedupe_window_seconds = max(0.0, float(dedupe_window_seconds))
        self.max_text_chars = max(1, int(max_text_chars))
        self.write_multiline = bool(write_multiline)
        self.log_file_path = self.log_dir / f"kira_dialogue_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
        self._queue: queue.Queue[object] = queue.Queue(maxsize=self.max_queue_size)
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._recent: dict[str, float] = {}

    def start(self) -> None:
        """Start the writer thread without performing filesystem I/O."""
        try:
            with self._state_lock:
                if self._thread is not None and self._thread.is_alive():
                    return
                if self._stop_event.is_set():
                    return
                self._thread = threading.Thread(
                    target=self._run,
                    name="KiraDialogueLogWriter",
                    daemon=True,
                )
                self._thread.start()
        except Exception as exc:
            logger.warning("DIALOGUE_LOG_START_FAILED ignored=true error=%s", exc)

    def stop(self, *, timeout: float = 1.0) -> None:
        """Ask the daemon to drain queued events and stop; intended for shutdown/tests."""
        try:
            self._stop_event.set()
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                pass
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.0, float(timeout)))
        except Exception as exc:
            logger.warning("DIALOGUE_LOG_STOP_FAILED ignored=true error=%s", exc)

    def log_user(self, text: str, *, source: str = "") -> None:
        self.enqueue("USER", text, source=source)

    def log_kira(self, text: str, *, source: str = "") -> None:
        self.enqueue("KIRA", text, source=source)

    def enqueue(self, speaker: str, text: str, source: str = "") -> None:
        """Enqueue a final line without allowing logging failures to escape."""
        try:
            normalized_speaker = str(speaker or "").strip().upper()
            if normalized_speaker not in {"USER", "KIRA"}:
                return
            cleaned = self._sanitize_text(text)
            if not cleaned:
                return
            if self._stop_event.is_set():
                return

            self.start()
            now = time.monotonic()
            key = f"{normalized_speaker}:{self._dedupe_text(cleaned)}"
            with self._state_lock:
                last_seen = self._recent.get(key)
                if last_seen is not None and now - last_seen < self.dedupe_window_seconds:
                    logger.debug("DIALOGUE_LOG_DEDUPE speaker=%s", normalized_speaker)
                    return
                self._prune_recent(now)
                try:
                    self._queue.put_nowait(
                        _DialogueEntry(
                            speaker=normalized_speaker,
                            text=cleaned,
                            source=str(source or "").strip(),
                            created_at=time.time(),
                        )
                    )
                except queue.Full:
                    logger.warning("DIALOGUE_LOG_QUEUE_FULL dropped=true speaker=%s", normalized_speaker)
                    return
                self._recent[key] = now
        except Exception as exc:
            logger.warning("DIALOGUE_LOG_ENQUEUE_FAILED ignored=true error=%s", exc)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                if item is not _STOP:
                    self._write_entry(item)
            finally:
                self._queue.task_done()
            if self._stop_event.is_set() and self._queue.empty():
                return

    def _write_entry(self, item: object) -> None:
        if not isinstance(item, _DialogueEntry):
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.fromtimestamp(item.created_at).strftime("%H:%M:%S")
            line = f"[{timestamp}] {item.speaker}: {item.text}\n"
            with self.log_file_path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line)
        except Exception as exc:
            logger.warning("DIALOGUE_LOG_WRITE_FAILED ignored=true error=%s", exc)

    def _sanitize_text(self, text: str) -> str:
        value = str(text or "")
        if any(marker in value for marker in _INTERNAL_MARKERS):
            return ""
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = "".join(
            char
            for char in value
            if char in {"\n", "\t"} or unicodedata.category(char) != "Cc"
        )
        value = value.replace("\t", "    ").strip()
        if not value:
            return ""
        if self.write_multiline:
            value = "\n    ".join(part.rstrip() for part in value.split("\n"))
        else:
            value = " ".join(value.split())
        if len(value) > self.max_text_chars:
            suffix = "... [truncated]"
            keep = max(0, self.max_text_chars - len(suffix))
            value = value[:keep] + suffix
        return value

    @staticmethod
    def _dedupe_text(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def _prune_recent(self, now: float) -> None:
        expiry = max(self.dedupe_window_seconds * 2.0, 2.0)
        self._recent = {
            key: seen
            for key, seen in self._recent.items()
            if now - seen <= expiry
        }


_dialogue_writer: DialogueLogWriter | None = None
_dialogue_writer_lock = threading.Lock()


class NoOpDialogueLogWriter:
    """Disabled dialogue logger that keeps callers safe without writing files."""

    log_file_path = None

    def start(self) -> None:
        logger.info("DIALOGUE_LOG_WRITER_NOOP active=true")

    def stop(self, *, timeout: float = 1.0) -> None:
        del timeout

    def log_user(self, text: str, *, source: str = "") -> None:
        self.enqueue("USER", text, source=source)

    def log_kira(self, text: str, *, source: str = "") -> None:
        self.enqueue("KIRA", text, source=source)

    def enqueue(self, speaker: str, text: str, source: str = "") -> None:
        del speaker, text, source
        logger.info("DIALOGUE_LOG_WRITER_NOOP active=true")


def _dialogue_log_enabled() -> bool:
    return str(os.getenv("KIRA_DIALOGUE_LOG_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_dialogue_log_writer() -> DialogueLogWriter:
    """Return the process-wide lazy dialogue writer and start it on first use."""
    global _dialogue_writer
    if not _dialogue_log_enabled():
        logger.info("DIALOGUE_LOG_DISABLED reason=user_requested_no_dialogue_file")
        return NoOpDialogueLogWriter()  # type: ignore[return-value]
    with _dialogue_writer_lock:
        if _dialogue_writer is None:
            _dialogue_writer = DialogueLogWriter(DEFAULT_LOG_DIR)
        writer = _dialogue_writer
    writer.start()
    return writer
