"""Camada local e prioritária da memória da Kira.

Esta camada mantém fatos e conversas em SQLite dentro de ``memory/local``.
Ela não depende de Mem0, não envia dados para a nuvem e continua disponível
quando o provedor remoto estiver lento ou indisponível.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "memory" / "local" / "kira_local_memory.sqlite"


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class LocalPriorityMemory:
    """Banco local pequeno, indexado e resiliente para fatos e histórico."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        configured = str(os.getenv("KIRA_LOCAL_MEMORY_DB", "") or "").strip()
        self.db_path = Path(db_path or configured or DEFAULT_DB_PATH).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    summary TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source TEXT NOT NULL DEFAULT 'conversation',
                    updated_at REAL NOT NULL,
                    UNIQUE(category, subject, predicate, object_value)
                );
                CREATE TABLE IF NOT EXISTS people (
                    person_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_profile (
                    profile_key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'conversation',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts(category, subject, predicate);
                """
            )

    def record_session_start(self, *, user_id: str, session_id: str | None = None) -> str:
        sid = str(session_id or f"session-{uuid.uuid4().hex[:16]}")
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions(session_id,user_id,started_at,ended_at,status,summary) VALUES(?,?,?,?,?,COALESCE((SELECT summary FROM sessions WHERE session_id=?),''))",
                (sid, str(user_id or "default"), now, None, "active", sid),
            )
            db.execute("INSERT INTO events(event_type,details_json,created_at) VALUES(?,?,?)", ("session_started", _json({"session_id": sid, "user_id": user_id}), now))
        return sid

    def record_session_end(self, *, session_id: str, summary: str = "", status: str = "ended") -> None:
        now = _now()
        with self._lock, self._connect() as db:
            db.execute("UPDATE sessions SET ended_at=?, status=?, summary=? WHERE session_id=?", (now, status, summary, session_id))
            db.execute("INSERT INTO events(event_type,details_json,created_at) VALUES(?,?,?)", ("session_ended", _json({"session_id": session_id, "status": status}), now))

    def record_message(self, *, session_id: str, role: str, content: str, source: str) -> None:
        clean = str(content or "").strip()
        if not clean:
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO messages(session_id,role,source,content,created_at) VALUES(?,?,?,?,?)", (session_id, role if role in {"user", "assistant", "system"} else "system", source or "unknown", clean, _now()))

    def remember_fact(self, *, category: str, subject: str, predicate: str, object_value: str, source: str = "conversation", confidence: float = 1.0) -> None:
        if not subject or not predicate or not object_value:
            return
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO facts(category,subject,predicate,object_value,confidence,source,updated_at) VALUES(?,?,?,?,?,?,?)",
                (category or "general", subject, predicate, object_value, max(0.0, min(1.0, float(confidence))), source or "conversation", _now()),
            )

    def remember_person(self, *, name: str, details: dict[str, Any], person_key: str | None = None) -> None:
        clean_name = str(name or "").strip()
        if not clean_name:
            return
        key = str(person_key or clean_name.casefold()).strip()
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO people(person_key,name,details_json,updated_at) VALUES(?,?,?,?)", (key, clean_name, _json(details), _now()))

    def remember_user_profile(self, *, key: str, value: str, source: str = "conversation") -> None:
        if not str(key or "").strip() or not str(value or "").strip():
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO user_profile(profile_key,value,source,updated_at) VALUES(?,?,?,?)", (key.strip(), value.strip(), source or "conversation", _now()))

    def recent_context(self, *, limit_messages: int = 12, limit_facts: int = 40) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            messages = [dict(row) for row in db.execute("SELECT role,source,content,created_at FROM messages ORDER BY created_at DESC LIMIT ?", (max(1, min(100, limit_messages)),)).fetchall()]
            facts = [dict(row) for row in db.execute("SELECT category,subject,predicate,object_value,confidence,source FROM facts ORDER BY updated_at DESC LIMIT ?", (max(1, min(200, limit_facts)),)).fetchall()]
            profile = [dict(row) for row in db.execute("SELECT profile_key,value,source FROM user_profile ORDER BY updated_at DESC").fetchall()]
            people = []
            for row in db.execute("SELECT person_key,name,details_json,updated_at FROM people ORDER BY updated_at DESC").fetchall():
                item = dict(row)
                try:
                    item["details"] = json.loads(item.pop("details_json") or "{}")
                except Exception:
                    item["details"] = {}
                people.append(item)
            last_session_row = db.execute("SELECT session_id,user_id,started_at,ended_at,status,summary FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
        return {
            "messages": list(reversed(messages)),
            "facts": facts,
            "profile": profile,
            "people": people,
            "last_session": dict(last_session_row) if last_session_row is not None else {},
        }

    def status(self) -> dict[str, Any]:
        context = self.recent_context(limit_messages=1, limit_facts=1)
        return {"provider": "local_priority", "path": str(self.db_path), "messages": len(context["messages"]), "facts": len(context["facts"]), "people": len(context["people"]), "profile_items": len(context["profile"])}

    def prompt_context(self, *, max_chars: int = 5000) -> str:
        """Converte a memória local em um bloco curto para o contexto da Kira."""
        context = self.recent_context(limit_messages=12, limit_facts=40)
        lines = ["[MEMORIA_LOCAL_PRIORITARIA]"]
        last_session = context.get("last_session") or {}
        if last_session:
            lines.append(
                "Última sessão registrada — início: "
                f"{last_session.get('started_at')}; fim: {last_session.get('ended_at') or 'em andamento'}; "
                f"status: {last_session.get('status') or 'desconhecido'}."
            )
        for item in context["profile"][:12]:
            lines.append(f"Perfil do usuário — {item.get('profile_key')}: {item.get('value')}")
        for item in context["people"][:12]:
            lines.append(f"Pessoa — {item.get('name')}: {json.dumps(item.get('details') or {}, ensure_ascii=False)}")
        for item in context["facts"][:40]:
            lines.append(f"Fato — {item.get('subject')} / {item.get('predicate')}: {item.get('object_value')}")
        for item in context["messages"][-12:]:
            lines.append(f"{str(item.get('role') or 'system').upper()} ({item.get('source') or 'unknown'}): {item.get('content')}")
        return "\n".join(lines)[:max(500, int(max_chars))]


_INSTANCE: LocalPriorityMemory | None = None
_INSTANCE_LOCK = threading.RLock()


def get_local_priority_memory() -> LocalPriorityMemory:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = LocalPriorityMemory()
        return _INSTANCE


def record_local_message(*, session_id: str, role: str, content: str, source: str) -> None:
    try:
        get_local_priority_memory().record_message(session_id=session_id, role=role, content=content, source=source)
    except Exception:
        # Memória é auxiliar: nunca bloqueia fala, texto ou encerramento.
        return None
