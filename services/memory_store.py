from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from services.memory_guard import classify_memory_text, mask_sensitive_preview


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_DB_PATH = PROJECT_ROOT / "data" / "memory" / "kira_memory.sqlite"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _db_path(db_path: str | Path | None = None) -> Path:
    configured = db_path or os.getenv("KIRA_MEMORY_DB_PATH") or DEFAULT_MEMORY_DB_PATH
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _normalize(text: str | None) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _json_text(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Migração aditiva para bancos criados por versões anteriores da Kira."""
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_memory_db(db_path: str | Path | None = None) -> Path:
    path = _db_path(db_path)
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                subject TEXT,
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                source TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                persistence TEXT NOT NULL DEFAULT 'persistent',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                tags TEXT
            );

            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                subject TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                promoted_to_persistent INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sensitive_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                category TEXT,
                encrypted_value TEXT NOT NULL,
                masked_preview TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS learned_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT,
                error TEXT,
                symptom TEXT,
                cause TEXT,
                solution TEXT NOT NULL,
                files TEXT,
                validation TEXT,
                result TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags TEXT
            );

            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                aliases TEXT,
                relation_to_alan TEXT,
                notes TEXT,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS project_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT,
                fact TEXT NOT NULL,
                file_path TEXT,
                status TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                preference TEXT NOT NULL,
                scope TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                source TEXT NOT NULL DEFAULT 'manual',
                kind TEXT NOT NULL DEFAULT 'explicit',
                context_json TEXT NOT NULL DEFAULT '{}',
                explicit INTEGER NOT NULL DEFAULT 1,
                supersedes_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS memory_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                table_name TEXT NOT NULL,
                memory_id INTEGER,
                summary TEXT,
                source TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_active_type ON memories(active, type);
            CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
            CREATE INDEX IF NOT EXISTS idx_people_active_name ON people(active, name);
            CREATE INDEX IF NOT EXISTS idx_project_module ON project_knowledge(active, module);
            CREATE INDEX IF NOT EXISTS idx_preferences_subject ON preferences(active, subject);
            """
        )
        _ensure_column(conn, "preferences", "source", "TEXT NOT NULL DEFAULT 'manual'")
        _ensure_column(conn, "preferences", "kind", "TEXT NOT NULL DEFAULT 'explicit'")
        _ensure_column(conn, "preferences", "context_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "preferences", "explicit", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "preferences", "supersedes_id", "INTEGER")
    return path


def add_audit_event(
    *,
    action: str,
    table_name: str,
    memory_id: int | None = None,
    summary: str | None = None,
    source: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_audit (action, table_name, memory_id, summary, source, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action, table_name, memory_id, summary, source, _now()),
        )
        return int(cursor.lastrowid)


def add_memory(
    *,
    content: str,
    memory_type: str | None = None,
    category: str | None = None,
    subject: str | None = None,
    source: str | None = "manual",
    confidence: float = 0.7,
    sensitivity: str | None = None,
    persistence: str = "persistent",
    tags: list[str] | str | None = None,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("content is required")
    classification = classify_memory_text(clean_content)
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memories (
                type, category, subject, content, normalized_content, source, confidence,
                sensitivity, persistence, created_at, updated_at, last_used_at, active, tags
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?)
            """,
            (
                memory_type or classification.memory_type,
                category or classification.category,
                subject,
                clean_content,
                _normalize(clean_content),
                source,
                float(confidence),
                sensitivity or classification.sensitivity,
                persistence,
                now,
                now,
                _json_text(tags),
            ),
        )
        memory_id = int(cursor.lastrowid)
    add_audit_event(
        action="add",
        table_name="memories",
        memory_id=memory_id,
        summary=mask_sensitive_preview(clean_content),
        source=source,
        db_path=db_path,
    )
    return memory_id


def get_memory(memory_id: int, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    init_memory_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        return _row_to_dict(row)


def search_memories(
    query: str | None = None,
    *,
    memory_type: str | None = None,
    category: str | None = None,
    subject: str | None = None,
    include_inactive: bool = False,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_memory_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("active = 1")
    if memory_type:
        clauses.append("type = ?")
        params.append(memory_type)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if subject:
        clauses.append("subject LIKE ?")
        params.append(f"%{subject}%")
    if query:
        like = f"%{_normalize(query)}%"
        clauses.append("(normalized_content LIKE ? OR subject LIKE ? OR tags LIKE ?)")
        params.extend([like, f"%{query}%", f"%{query}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM memories{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def update_memory(
    memory_id: int,
    *,
    content: str | None = None,
    subject: str | None = None,
    category: str | None = None,
    confidence: float | None = None,
    tags: list[str] | str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    init_memory_db(db_path)
    updates: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now()]
    if content is not None:
        updates.extend(["content = ?", "normalized_content = ?", "sensitivity = ?"])
        classification = classify_memory_text(content)
        params.extend([content, _normalize(content), classification.sensitivity])
    if subject is not None:
        updates.append("subject = ?")
        params.append(subject)
    if category is not None:
        updates.append("category = ?")
        params.append(category)
    if confidence is not None:
        updates.append("confidence = ?")
        params.append(float(confidence))
    if tags is not None:
        updates.append("tags = ?")
        params.append(_json_text(tags))
    params.append(int(memory_id))
    with _connect(db_path) as conn:
        cursor = conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
        changed = cursor.rowcount > 0
    if changed:
        add_audit_event(action="update", table_name="memories", memory_id=memory_id, db_path=db_path)
    return changed


def deactivate_memory(
    memory_id: int,
    *,
    source: str | None = "manual",
    db_path: str | Path | None = None,
) -> bool:
    init_memory_db(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE memories SET active = 0, updated_at = ? WHERE id = ?",
            (_now(), int(memory_id)),
        )
        changed = cursor.rowcount > 0
    if changed:
        add_audit_event(action="deactivate", table_name="memories", memory_id=memory_id, source=source, db_path=db_path)
    return changed


def add_person(
    *,
    name: str,
    aliases: list[str] | str | None = None,
    relation_to_alan: str | None = None,
    notes: str | None = None,
    sensitivity: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    note_text = notes or clean_name
    classification = classify_memory_text(note_text)
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO people (name, aliases, relation_to_alan, notes, sensitivity, created_at, updated_at, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                clean_name,
                _json_text(aliases),
                relation_to_alan,
                notes,
                sensitivity or classification.sensitivity,
                now,
                now,
            ),
        )
        person_id = int(cursor.lastrowid)
    add_audit_event(action="add", table_name="people", memory_id=person_id, summary=clean_name, db_path=db_path)
    return person_id


def search_people(
    query: str | None = None,
    *,
    include_inactive: bool = False,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_memory_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("active = 1")
    if query:
        clauses.append("(name LIKE ? OR aliases LIKE ? OR notes LIKE ? OR relation_to_alan LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like, like])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM people{where} ORDER BY updated_at DESC, id DESC LIMIT ?", params).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def add_learned_lesson(
    *,
    solution: str,
    domain: str | None = None,
    error: str | None = None,
    symptom: str | None = None,
    cause: str | None = None,
    files: list[str] | str | None = None,
    validation: str | None = None,
    result: str | None = None,
    confidence: float = 0.7,
    tags: list[str] | str | None = None,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    clean_solution = str(solution or "").strip()
    if not clean_solution:
        raise ValueError("solution is required")
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO learned_lessons (
                domain, error, symptom, cause, solution, files, validation, result,
                confidence, created_at, updated_at, tags
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                error,
                symptom,
                cause,
                clean_solution,
                _json_text(files),
                validation,
                result,
                float(confidence),
                now,
                now,
                _json_text(tags),
            ),
        )
        lesson_id = int(cursor.lastrowid)
    add_audit_event(
        action="add",
        table_name="learned_lessons",
        memory_id=lesson_id,
        summary=mask_sensitive_preview(clean_solution),
        db_path=db_path,
    )
    return lesson_id


def search_learned_lessons(
    query: str | None = None,
    *,
    domain: str | None = None,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_memory_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if domain:
        clauses.append("domain LIKE ?")
        params.append(f"%{domain}%")
    if query:
        like = f"%{query}%"
        clauses.append("(error LIKE ? OR symptom LIKE ? OR cause LIKE ? OR solution LIKE ? OR tags LIKE ?)")
        params.extend([like, like, like, like, like])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM learned_lessons{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def add_project_fact(
    *,
    fact: str,
    module: str | None = None,
    file_path: str | None = None,
    status: str | None = None,
    confidence: float = 0.7,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    clean_fact = str(fact or "").strip()
    if not clean_fact:
        raise ValueError("fact is required")
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_knowledge (module, fact, file_path, status, confidence, created_at, updated_at, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (module, clean_fact, file_path, status, float(confidence), now, now),
        )
        fact_id = int(cursor.lastrowid)
    add_audit_event(action="add", table_name="project_knowledge", memory_id=fact_id, summary=clean_fact, db_path=db_path)
    return fact_id


def search_project_facts(
    query: str | None = None,
    *,
    module: str | None = None,
    include_inactive: bool = False,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_memory_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("active = 1")
    if module:
        clauses.append("module LIKE ?")
        params.append(f"%{module}%")
    if query:
        like = f"%{query}%"
        clauses.append("(module LIKE ? OR fact LIKE ? OR file_path LIKE ? OR status LIKE ?)")
        params.extend([like, like, like, like])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM project_knowledge{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def add_preference(
    *,
    preference: str,
    subject: str | None = "Chefe",
    scope: str | None = None,
    confidence: float = 0.7,
    source: str | None = "manual",
    kind: str | None = "explicit",
    context: dict[str, Any] | None = None,
    explicit: bool = True,
    supersedes_id: int | None = None,
    db_path: str | Path | None = None,
) -> int:
    init_memory_db(db_path)
    clean_preference = str(preference or "").strip()
    if not clean_preference:
        raise ValueError("preference is required")
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO preferences (
                subject, preference, scope, confidence, source, kind, context_json, explicit,
                supersedes_id, created_at, updated_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                subject,
                clean_preference,
                scope,
                float(confidence),
                source or "manual",
                kind or "explicit",
                _json_text(context or {}),
                int(bool(explicit)),
                int(supersedes_id) if supersedes_id else None,
                now,
                now,
            ),
        )
        preference_id = int(cursor.lastrowid)
    add_audit_event(
        action="add",
        table_name="preferences",
        memory_id=preference_id,
        summary=mask_sensitive_preview(clean_preference),
        db_path=db_path,
    )
    return preference_id


def search_preferences(
    query: str | None = None,
    *,
    subject: str | None = None,
    include_inactive: bool = False,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_memory_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("active = 1")
    if subject:
        clauses.append("subject LIKE ?")
        params.append(f"%{subject}%")
    if query:
        like = f"%{query}%"
        clauses.append("(preference LIKE ? OR scope LIKE ?)")
        params.extend([like, like])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM preferences{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def add_sensitive_memory(
    *,
    subject: str,
    value: str,
    category: str | None = None,
    source: str | None = "manual",
    db_path: str | Path | None = None,
) -> int:
    """Store a sensitive value through the vault interface.

    TODO: replace the placeholder encrypted_value with Windows DPAPI or another
    local encryption provider before exposing this outside explicit test/manual use.
    Never log or return the raw value from this function.
    """
    init_memory_db(db_path)
    clean_value = str(value or "").strip()
    if not clean_value:
        raise ValueError("value is required")
    now = _now()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO sensitive_memories (
                subject, category, encrypted_value, masked_preview, source, created_at, updated_at, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                subject,
                category or "sensitive",
                "__ENCRYPTION_TODO__",
                mask_sensitive_preview(clean_value),
                source,
                now,
                now,
            ),
        )
        sensitive_id = int(cursor.lastrowid)
    add_audit_event(
        action="add",
        table_name="sensitive_memories",
        memory_id=sensitive_id,
        summary=mask_sensitive_preview(clean_value),
        source=source,
        db_path=db_path,
    )
    return sensitive_id
