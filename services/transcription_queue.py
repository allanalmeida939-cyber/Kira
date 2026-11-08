"""
Transcription Queue — Fila persistente em SQLite.

Resolve o problema de o agente (subprocesso) não conseguir publicar
diretamente no transcription_bus do backend (processos separados não
compartilham singleton em memória).

Fluxo:
  agent → enqueue() grava em SQLite
  backend → dequeue_pending() lê e publica em /ws/transcription
          → mark_delivered() marca como entregue
  Limpeza automática por TTL (24h).

Resiliência:
  Backend caiu? Eventos ficam no SQLite até ele voltar.
  Duplicatas? dedupe_hash impede.
  Mensagens velhas? TTL descarta automaticamente.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = "data/transcription_queue.db"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h


class TranscriptionQueue:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcription_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    message_id TEXT,
                    speaker TEXT,
                    text TEXT NOT NULL,
                    source TEXT,
                    is_final INTEGER DEFAULT 1,
                    dedupe_hash TEXT UNIQUE,
                    delivered INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    delivered_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivered ON transcription_events(delivered, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON transcription_events(created_at)")

    def _make_dedupe_hash(self, speaker: str, text: str, session_id: Optional[str]) -> str:
        raw = f"{session_id or ''}|{speaker}|{text.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def enqueue(
        self,
        speaker: str,
        text: str,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        source: str = "agent",
        is_final: bool = True,
    ) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        dedupe = self._make_dedupe_hash(speaker, text, session_id)
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO transcription_events
                       (session_id, message_id, speaker, text, source, is_final, dedupe_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, message_id, speaker, text, source, 1 if is_final else 0, dedupe, time.time()),
                )
            return True
        except Exception:
            return False

    def dequeue_pending(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM transcription_events
                   WHERE delivered = 0
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_delivered(self, event_ids: List[int]) -> None:
        if not event_ids:
            return
        with self._lock, self._conn() as conn:
            placeholders = ",".join("?" * len(event_ids))
            conn.execute(
                f"UPDATE transcription_events SET delivered = 1, delivered_at = ? WHERE id IN ({placeholders})",
                [time.time(), *event_ids],
            )

    def cleanup_old(self) -> int:
        cutoff = time.time() - self._ttl_seconds
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM transcription_events WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0

    def stats(self) -> Dict[str, int]:
        with self._lock, self._conn() as conn:
            pending = conn.execute("SELECT COUNT(*) FROM transcription_events WHERE delivered = 0").fetchone()[0]
            delivered = conn.execute("SELECT COUNT(*) FROM transcription_events WHERE delivered = 1").fetchone()[0]
            return {"pending": pending, "delivered": delivered}


_queue: Optional[TranscriptionQueue] = None
_queue_lock = threading.Lock()


def get_transcription_queue() -> TranscriptionQueue:
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = TranscriptionQueue()
    return _queue
