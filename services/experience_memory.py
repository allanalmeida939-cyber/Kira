"""Memória estruturada de preferências, procedimentos e experiências verificadas.

Este módulo é deliberadamente local e pequeno. Ele complementa ``memory_store``
sem substituir o Mem0: fatos explícitos continuam podendo ser espelhados para o
provider configurado, enquanto estratégias e resultados de ferramentas ficam
no SQLite da Kira para que o aprendizado não dependa de rede.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request
from services.memory_guard import is_sensitive, mask_sensitive_preview
from services.memory_store import _connect, _db_path, _ensure_column, add_audit_event, add_preference, init_memory_db


logger = logging.getLogger("kira.experience_memory")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_GOAL_CHARS = 500
MAX_JSON_CHARS = 6000
MAX_QUERY_ROWS = 120
STATUS_VALUES = {"pending", "success", "failure", "unknown"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fold(value: Any) -> str:
    raw = unicodedata.normalize("NFD", str(value or "").strip().lower())
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:/.-]", " ", raw)).strip()


def _terms(value: Any) -> set[str]:
    return {item for item in re.findall(r"[\w-]{3,}", _fold(value)) if item not in {
        "para", "com", "uma", "por", "que", "quando", "como", "isso", "essa", "esse",
        "faz", "fazer", "usar", "usa", "usar", "kira", "alan", "the", "and", "from",
    }}


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        return value[:MAX_JSON_CHARS]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:MAX_JSON_CHARS]
    except Exception:
        return json.dumps(default, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
        return parsed
    except Exception:
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return {key: row[key] for key in row.keys()} if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_row(item) or {} for item in rows]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _unique(values: list[Any], limit: int = 20) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = _fold(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


class ExperienceMemory:
    """SQLite-backed strategy memory with bounded, contextual retrieval."""

    def __init__(self, db_path: str | Path | None = None, *, user_id: str = "alan") -> None:
        self.db_path = _db_path(db_path)
        self.user_id = str(user_id or "alan").strip() or "alan"
        self.init()

    def init(self) -> Path:
        init_memory_db(self.db_path)
        with _connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_procedures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    normalized_goal TEXT NOT NULL,
                    app TEXT,
                    device TEXT,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    preconditions TEXT,
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT,
                    evidence TEXT,
                    known_failures_json TEXT NOT NULL DEFAULT '[]',
                    alternatives_json TEXT NOT NULL DEFAULT '[]',
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    explicit INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'experience',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS memory_experiences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    normalized_goal TEXT NOT NULL,
                    procedure_id INTEGER,
                    strategy TEXT,
                    app TEXT,
                    device TEXT,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    result TEXT,
                    error TEXT,
                    correction TEXT,
                    recommended_next TEXT,
                    evidence TEXT,
                    source TEXT NOT NULL DEFAULT 'experience',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    auto_learning_enabled INTEGER NOT NULL DEFAULT 1,
                    retention_days INTEGER,
                    record_tool_outcomes INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_procedures_goal
                    ON memory_procedures(active, normalized_goal, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memory_procedures_context
                    ON memory_procedures(active, app, device, confidence);
                CREATE INDEX IF NOT EXISTS idx_memory_experiences_goal
                    ON memory_experiences(normalized_goal, created_at);
                """
            )
            _ensure_column(conn, "memory_experiences", "result", "TEXT")
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_settings(id, auto_learning_enabled, retention_days,
                    record_tool_outcomes, updated_at) VALUES(1, 1, NULL, 1, ?)
                """,
                (_now(),),
            )
        self.purge_expired()
        return self.db_path

    def _setting(self, key: str, default: Any = None) -> Any:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM memory_settings WHERE id=1").fetchone()
        return default if row is None else row[key]

    def settings(self) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM memory_settings WHERE id=1").fetchone()
        payload = _row(row) or {
            "auto_learning_enabled": 1,
            "retention_days": None,
            "record_tool_outcomes": 1,
        }
        payload["auto_learning_enabled"] = bool(payload.get("auto_learning_enabled"))
        payload["record_tool_outcomes"] = bool(payload.get("record_tool_outcomes"))
        return payload

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        auto = bool(patch.get("auto_learning_enabled", current["auto_learning_enabled"]))
        record = bool(patch.get("record_tool_outcomes", current["record_tool_outcomes"]))
        retention = patch.get("retention_days", current.get("retention_days"))
        if retention in ("", None, 0, "0"):
            retention_value = None
        else:
            try:
                retention_value = max(1, min(3650, int(retention)))
            except (TypeError, ValueError):
                retention_value = current.get("retention_days")
        with _connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_settings SET auto_learning_enabled=?, retention_days=?, record_tool_outcomes=?, updated_at=? WHERE id=1",
                (int(auto), retention_value, int(record), _now()),
            )
        return self.settings()

    def purge_expired(self) -> int:
        """Remove apenas experiências antigas quando o usuário configurou retenção."""
        retention_days = self.settings().get("retention_days")
        if not retention_days:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=int(retention_days))).isoformat(timespec="seconds")
        with _connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM memory_experiences WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    @staticmethod
    def infer_preference_context(content: str, context: dict[str, Any] | None = None) -> dict[str, str]:
        result = {str(k): str(v).strip() for k, v in (context or {}).items() if str(v or "").strip()}
        folded = _fold(content)
        if not result.get("device") and any(term in folded for term in ("tv", "televisao", "televisão", "webos")):
            result["device"] = "TV LG webOS"
        if not result.get("task_type"):
            for marker, task in (("serie", "series"), ("filme", "filmes"), ("noticia", "noticias"), ("estudar", "estudos"), ("resposta", "comunicacao"), ("mensagem", "comunicacao")):
                if marker in folded:
                    result["task_type"] = task
                    break
        if not result.get("app"):
            for app in ("Netflix", "Prime Video", "Prime IPTV", "Globoplay", "Disney+", "YouTube", "Spotify"):
                if _fold(app) in folded:
                    result["app"] = app
                    break
        return result

    def remember_preference(
        self,
        content: str,
        *,
        context: dict[str, Any] | None = None,
        scope: str | None = None,
        source: str = "explicit",
        confidence: float = 1.0,
        explicit: bool = True,
        supersedes_id: int | None = None,
    ) -> dict[str, Any]:
        clean = str(content or "").strip()[:MAX_GOAL_CHARS]
        if not clean:
            raise ValueError("preference is required")
        if is_sensitive(clean):
            raise ValueError("sensitive_preference_blocked")
        inferred = self.infer_preference_context(clean, context)
        if not scope:
            if inferred.get("task_type"):
                scope = f"task:{inferred['task_type']}"
            elif inferred.get("app"):
                scope = f"app:{_fold(inferred['app'])}"
            elif inferred.get("device"):
                scope = f"device:{_fold(inferred['device'])}"
            else:
                scope = "general"
        specificity = len(inferred) + (1 if scope != "general" else 0)
        if supersedes_id is None:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id FROM preferences WHERE active=1 AND subject='Chefe' AND scope=?
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (scope,),
                ).fetchone()
            supersedes_id = int(row["id"]) if row is not None else None
        if supersedes_id:
            with _connect(self.db_path) as conn:
                conn.execute("UPDATE preferences SET active=0, updated_at=? WHERE id=?", (_now(), int(supersedes_id)))
        preference_id = add_preference(
            preference=clean,
            subject="Chefe",
            scope=scope,
            confidence=_clamp(confidence),
            source=source,
            kind="explicit" if explicit else "inferred",
            context=inferred,
            explicit=explicit,
            supersedes_id=supersedes_id,
            db_path=self.db_path,
        )
        return {
            "id": preference_id,
            "preference": clean,
            "subject": "Chefe",
            "scope": scope,
            "context": inferred,
            "confidence": _clamp(confidence),
            "explicit": bool(explicit),
            "source": source,
            "supersedes_id": supersedes_id,
            "updated_at": _now(),
        }

    def list_preferences(self, *, include_inactive: bool = False, limit: int = 80) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE active=1"
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM preferences {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        result = _rows(rows)
        for item in result:
            item["context"] = _loads(item.get("context_json"), {})
            item["explicit"] = bool(item.get("explicit", 1))
        return result

    def _compatible(self, row: dict[str, Any], *, app: str = "", device: str = "") -> bool:
        stored_app = _fold(row.get("app") or _loads(row.get("context_json"), {}).get("app"))
        stored_device = _fold(row.get("device") or _loads(row.get("context_json"), {}).get("device"))
        requested_app = _fold(app)
        requested_device = _fold(device)
        if stored_device and requested_device and stored_device not in requested_device and requested_device not in stored_device:
            return False
        if stored_app and requested_app and stored_app not in requested_app and requested_app not in stored_app:
            return False
        return True

    def relevant_preferences(self, goal: str, *, app: str = "", device: str = "", context: dict[str, Any] | None = None, limit: int = 5) -> list[dict[str, Any]]:
        goal_terms = _terms(" ".join([goal, app, device, json.dumps(context or {}, ensure_ascii=False)]))
        preferences = self.list_preferences(limit=120)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in preferences:
            if not self._compatible(row, app=app, device=device):
                continue
            row_context = row.get("context") if isinstance(row.get("context"), dict) else {}
            row_terms = _terms(" ".join([row.get("preference", ""), row.get("scope", ""), json.dumps(row_context, ensure_ascii=False)]))
            overlap = len(goal_terms & row_terms)
            specificity = len(row_context) + (2 if str(row.get("scope")) != "general" else 0)
            score = float(overlap * 2 + specificity + (1 if row.get("explicit", 1) else 0))
            if not goal_terms and specificity == 0:
                score = 1.0
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("updated_at") or "", int(pair[1].get("id") or 0)), reverse=True)
        return [row for _, row in ranked[: max(1, min(20, int(limit)))]]

    def _ensure_procedure(
        self,
        *,
        goal: str,
        app: str = "",
        device: str = "",
        context: dict[str, Any] | None = None,
        steps: list[Any] | None = None,
        tools: list[Any] | None = None,
        explicit: bool = False,
        source: str = "experience",
        confidence: float | None = None,
    ) -> dict[str, Any]:
        clean_goal = str(goal or "").strip()[:MAX_GOAL_CHARS] or "tarefa não identificada"
        normalized_goal = _fold(clean_goal)
        clean_steps = _unique([str(item).strip()[:500] for item in (steps or []) if str(item).strip()], 30)
        clean_tools = _unique([str(item).strip()[:120] for item in (tools or []) if str(item).strip()], 20)
        context_payload = {str(k): str(v)[:200] for k, v in (context or {}).items() if str(v or "").strip()}
        if not app:
            app = str(context_payload.get("app") or "")
        if not device:
            device = str(context_payload.get("device") or "")
        with _connect(self.db_path) as conn:
            candidates = conn.execute(
                """
                SELECT * FROM memory_procedures WHERE active=1 AND normalized_goal=?
                    AND COALESCE(app,'')=? AND COALESCE(device,'')=?
                ORDER BY updated_at DESC, id DESC LIMIT 20
                """,
                (normalized_goal, app, device),
            ).fetchall()
        steps_signature = _json(clean_steps, [])
        for candidate in candidates:
            if str(candidate["steps_json"] or "[]") == steps_signature and not explicit:
                return _row(candidate) or {}
        now = _now()
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory_procedures(
                    goal, normalized_goal, app, device, context_json, preconditions, steps_json,
                    tools_json, outcome, evidence, known_failures_json, alternatives_json,
                    confidence, explicit, source, created_at, updated_at, active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    clean_goal,
                    normalized_goal,
                    app or None,
                    device or None,
                    _json(context_payload, {}),
                    str(context_payload.get("preconditions") or "")[:1000] or None,
                    _json(clean_steps, []),
                    _json(clean_tools, []),
                    None,
                    None,
                    "[]",
                    "[]",
                    _clamp(confidence if confidence is not None else (0.95 if explicit else 0.5)),
                    int(explicit),
                    source or "experience",
                    now,
                    now,
                ),
            )
            procedure_id = int(cursor.lastrowid)
        add_audit_event(action="procedure_add", table_name="memory_procedures", memory_id=procedure_id, summary=mask_sensitive_preview(clean_goal), source=source, db_path=self.db_path)
        return self.get_procedure(procedure_id) or {}

    def get_procedure(self, procedure_id: int) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM memory_procedures WHERE id=?", (int(procedure_id),)).fetchone()
        return self._decorate_procedure(_row(row)) if row is not None else None

    def _decorate_procedure(self, row: dict[str, Any] | None) -> dict[str, Any]:
        item = dict(row or {})
        item["context"] = _loads(item.get("context_json"), {})
        item["steps"] = _loads(item.get("steps_json"), [])
        item["tools"] = _loads(item.get("tools_json"), [])
        item["known_failures"] = _loads(item.get("known_failures_json"), [])
        item["alternatives"] = _loads(item.get("alternatives_json"), [])
        item["explicit"] = bool(item.get("explicit"))
        item["confidence"] = round(float(item.get("confidence") or 0.0), 3)
        item["success_rate"] = round((int(item.get("success_count") or 0) / max(1, int(item.get("success_count") or 0) + int(item.get("failure_count") or 0))), 3)
        for key in ("context_json", "steps_json", "tools_json", "known_failures_json", "alternatives_json"):
            item.pop(key, None)
        return item

    def list_procedures(self, *, include_inactive: bool = False, limit: int = 80) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE active=1"
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_procedures {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [self._decorate_procedure(item) for item in _rows(rows)]

    def recommend(
        self,
        goal: str,
        *,
        app: str = "",
        device: str = "",
        context: dict[str, Any] | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        clean_goal = str(goal or "").strip()[:MAX_GOAL_CHARS]
        goal_terms = _terms(" ".join([clean_goal, app, device, json.dumps(context or {}, ensure_ascii=False)]))
        candidates = self.list_procedures(limit=MAX_QUERY_ROWS)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            if not self._compatible(item, app=app, device=device):
                continue
            procedure_terms = _terms(" ".join([
                str(item.get("goal") or ""),
                str(item.get("app") or ""),
                str(item.get("device") or ""),
                json.dumps(item.get("context", {}), ensure_ascii=False),
                " ".join(str(step) for step in (item.get("steps") or [])),
            ]))
            overlap = len(goal_terms & procedure_terms)
            exact = _fold(clean_goal) == _fold(item.get("goal"))
            if not exact and overlap <= 0:
                continue
            successes = int(item.get("success_count") or 0)
            failures = int(item.get("failure_count") or 0)
            score = float(overlap * 2.0 + (10.0 if exact else 0.0))
            score += float(item.get("confidence") or 0.0) * 5.0
            score += min(successes, 5) * 1.4
            score -= min(failures, 5) * 0.8
            score += 1.5 if item.get("explicit") else 0.0
            ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("last_success_at") or "", int(pair[1].get("id") or 0)), reverse=True)
        procedures = [dict(item, recommendation_score=round(score, 3)) for score, item in ranked[: max(1, min(10, int(limit)))]]
        preferences = self.relevant_preferences(clean_goal, app=app, device=device, context=context, limit=5)
        return {
            "ok": True,
            "goal": clean_goal,
            "procedures": procedures,
            "best": procedures[0] if procedures else None,
            "preferences": preferences,
            "source": "structured_local_memory",
        }

    def _confidence(self, successes: int, failures: int, explicit: bool = False) -> float:
        base = (successes + 1.0) / (successes + failures + 2.0)
        return _clamp(max(base, 0.95 if explicit and successes == 0 else base))

    def _status(self, status: str, confirmed: bool) -> tuple[str, bool]:
        normalized = _fold(status)
        if normalized in {"ok", "done", "success", "sucesso", "concluido", "concluida"}:
            if not confirmed:
                return "unknown", False
            return "success", True
        if normalized in {"failure", "failed", "falha", "erro", "error"}:
            return "failure", bool(confirmed)
        if normalized in {"pending", "andamento"}:
            return "pending", False
        return "unknown", False

    def record_attempt(
        self,
        *,
        goal: str,
        status: str,
        confirmed: bool = False,
        procedure_id: int | None = None,
        strategy: str = "",
        app: str = "",
        device: str = "",
        context: dict[str, Any] | None = None,
        steps: list[Any] | None = None,
        tools: list[Any] | None = None,
        duration_ms: int | None = None,
        result: str = "",
        error: str = "",
        correction: str = "",
        evidence: str = "",
        source: str = "experience",
    ) -> dict[str, Any]:
        manual_sources = {"explicit", "correction", "voice_correction", "text_correction", "settings", "test"}
        if source not in manual_sources:
            if not bool(self._setting("auto_learning_enabled", 1)):
                return {"ok": True, "recorded": False, "reason": "auto_learning_disabled"}
            if not bool(self._setting("record_tool_outcomes", 1)):
                return {"ok": True, "recorded": False, "reason": "tool_outcomes_disabled"}
        clean_goal = str(goal or "").strip()[:MAX_GOAL_CHARS] or "tarefa não identificada"
        raw_steps = [str(item).strip() for item in (steps or []) if str(item).strip()]
        raw_tools = [str(item).strip() for item in (tools or []) if str(item).strip()]
        context_text = json.dumps(context or {}, ensure_ascii=False, default=str)
        if any(is_sensitive(value) for value in (clean_goal, strategy, correction, error, evidence, context_text, *raw_steps, *raw_tools)):
            return {"ok": True, "recorded": False, "reason": "sensitive_content_blocked"}
        effective_status, effective_confirmed = self._status(status, bool(confirmed))
        procedure = self.get_procedure(int(procedure_id)) if procedure_id else None
        if procedure is None:
            procedure = self._ensure_procedure(
                goal=clean_goal,
                app=app,
                device=device,
                context=context,
                steps=steps,
                tools=tools,
                source=source,
            )
        procedure_id = int(procedure.get("id") or 0) or None
        now = _now()
        safe_error = str(error or "").strip()[:1000]
        safe_result = str(result or evidence or effective_status).strip()[:1200]
        safe_evidence = mask_sensitive_preview(str(evidence or "").strip()[:1600])
        safe_correction = str(correction or "").strip()[:1000]
        safe_strategy = str(strategy or "").strip()[:500] or ("; ".join(procedure.get("steps") or [])[:500])
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory_experiences(
                    goal, normalized_goal, procedure_id, strategy, app, device, context_json,
                    steps_json, tools_json, status, confirmed, duration_ms, result, error, correction,
                    recommended_next, evidence, source, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    clean_goal,
                    _fold(clean_goal),
                    procedure_id,
                    safe_strategy,
                    app or procedure.get("app"),
                    device or procedure.get("device"),
                    _json(context or procedure.get("context") or {}, {}),
                    _json(steps or procedure.get("steps") or [], []),
                    _json(tools or procedure.get("tools") or [], []),
                    effective_status,
                    int(effective_confirmed),
                    int(duration_ms) if duration_ms is not None else None,
                    safe_result or None,
                    safe_error or None,
                    safe_correction or None,
                    None,
                    safe_evidence or None,
                    source or "experience",
                    now,
                ),
            )
            attempt_id = int(cursor.lastrowid)
        if procedure_id and effective_confirmed and effective_status in {"success", "failure"}:
            self._apply_outcome(
                procedure_id,
                status=effective_status,
                error=safe_error,
                evidence=safe_evidence,
                correction=safe_correction,
                now=now,
            )
        recommendation = self.recommend(clean_goal, app=app, device=device, context=context, limit=1).get("best")
        recommended_next = ""
        if recommendation:
            recommended_next = str(recommendation.get("goal") or "")
            with _connect(self.db_path) as conn:
                conn.execute("UPDATE memory_experiences SET recommended_next=? WHERE id=?", (recommended_next[:500], attempt_id))
        add_audit_event(action="attempt_record", table_name="memory_experiences", memory_id=attempt_id, summary=mask_sensitive_preview(clean_goal), source=source, db_path=self.db_path)
        return {
            "ok": True,
            "recorded": True,
            "attempt_id": attempt_id,
            "procedure_id": procedure_id,
            "status": effective_status,
            "confirmed": effective_confirmed,
            "recommended_next": recommendation,
        }

    def _apply_outcome(self, procedure_id: int, *, status: str, error: str, evidence: str, correction: str, now: str) -> None:
        with _connect(self.db_path) as conn:
            current = conn.execute("SELECT * FROM memory_procedures WHERE id=?", (procedure_id,)).fetchone()
            if current is None:
                return
            successes = int(current["success_count"] or 0)
            failures = int(current["failure_count"] or 0)
            known_failures = _loads(current["known_failures_json"], [])
            alternatives = _loads(current["alternatives_json"], [])
            if status == "success":
                successes += 1
                outcome = "success"
                if evidence:
                    evidence_value = evidence
                else:
                    evidence_value = current["evidence"]
                conn.execute(
                    """
                    UPDATE memory_procedures SET success_count=?, confidence=?, last_success_at=?, outcome=?, evidence=?, updated_at=? WHERE id=?
                    """,
                    (successes, self._confidence(successes, failures, bool(current["explicit"])), now, outcome, evidence_value, now, procedure_id),
                )
            else:
                failures += 1
                if error or evidence:
                    known_failures = _unique(known_failures + [{"error": error, "evidence": evidence, "at": now}], 12)
                if correction:
                    alternatives = _unique(alternatives + [correction], 12)
                conn.execute(
                    """
                    UPDATE memory_procedures SET failure_count=?, confidence=?, last_failure_at=?, outcome=?, evidence=?,
                        known_failures_json=?, alternatives_json=?, updated_at=? WHERE id=?
                    """,
                    (
                        failures,
                        self._confidence(successes, failures, bool(current["explicit"])),
                        now,
                        "failure",
                        evidence or current["evidence"],
                        _json(known_failures, []),
                        _json(alternatives, []),
                        now,
                        procedure_id,
                    ),
                )

    def record_correction(
        self,
        *,
        correction: str,
        goal: str = "",
        steps: list[Any] | None = None,
        app: str = "",
        device: str = "",
        context: dict[str, Any] | None = None,
        source: str = "correction",
    ) -> dict[str, Any]:
        clean_correction = str(correction or "").strip()[:MAX_GOAL_CHARS]
        if is_sensitive(clean_correction):
            return {"ok": False, "error": "sensitive_content_blocked"}
        if not goal:
            with _connect(self.db_path) as conn:
                row = conn.execute("SELECT goal FROM memory_experiences ORDER BY id DESC LIMIT 1").fetchone()
            goal = str(row["goal"] if row is not None else "tarefa corrigida")
        procedure = self._ensure_procedure(
            goal=goal,
            app=app,
            device=device,
            context=context,
            steps=steps or [clean_correction],
            explicit=True,
            source=source,
            confidence=0.95,
        )
        result = self.record_attempt(
            goal=goal,
            status="unknown",
            confirmed=False,
            procedure_id=int(procedure.get("id") or 0),
            strategy=clean_correction,
            app=app,
            device=device,
            context=context,
            steps=steps or [clean_correction],
            correction=clean_correction,
            evidence="orientação explícita do usuário; aguardando validação",
            source=source,
        )
        result["procedure"] = self.get_procedure(int(procedure.get("id") or 0))
        return result

    def explain(self, goal: str = "") -> dict[str, Any]:
        clean_goal = str(goal or "").strip()
        if not clean_goal:
            with _connect(self.db_path) as conn:
                row = conn.execute("SELECT goal FROM memory_experiences ORDER BY id DESC LIMIT 1").fetchone()
            clean_goal = str(row["goal"] if row is not None else "")
        recommendation = self.recommend(clean_goal, limit=5) if clean_goal else {"procedures": [], "preferences": [], "best": None}
        return {"ok": True, "goal": clean_goal, **recommendation}

    def forget(self, *, kind: str = "", memory_id: int | None = None, query: str = "") -> int:
        normalized_kind = _fold(kind)
        count = 0
        if memory_id and normalized_kind in {"preference", "preferences"}:
            with _connect(self.db_path) as conn:
                cursor = conn.execute("UPDATE preferences SET active=0, updated_at=? WHERE id=? AND active=1", (_now(), int(memory_id)))
                count = int(cursor.rowcount or 0)
            table = "preferences"
        elif memory_id and normalized_kind in {"procedure", "procedures"}:
            with _connect(self.db_path) as conn:
                cursor = conn.execute("UPDATE memory_procedures SET active=0, updated_at=? WHERE id=? AND active=1", (_now(), int(memory_id)))
                count = int(cursor.rowcount or 0)
            table = "memory_procedures"
        elif normalized_kind in {"procedure", "procedures"} or query:
            needle = f"%{str(query or '').strip()}%"
            with _connect(self.db_path) as conn:
                rows = conn.execute("SELECT id FROM memory_procedures WHERE active=1 AND (goal LIKE ? OR app LIKE ? OR device LIKE ?)", (needle, needle, needle)).fetchall()
                ids = [int(item["id"]) for item in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    cursor = conn.execute(f"UPDATE memory_procedures SET active=0, updated_at=? WHERE id IN ({placeholders})", [_now(), *ids])
                    count = int(cursor.rowcount or 0)
            table = "memory_procedures"
        else:
            with _connect(self.db_path) as conn:
                if query:
                    like = f"%{query}%"
                    cursor = conn.execute("UPDATE preferences SET active=0, updated_at=? WHERE active=1 AND (preference LIKE ? OR scope LIKE ?)", (_now(), like, like))
                else:
                    cursor = conn.execute("UPDATE preferences SET active=0, updated_at=? WHERE active=1 AND id=(SELECT id FROM preferences WHERE active=1 ORDER BY updated_at DESC, id DESC LIMIT 1)", (_now(),))
                count = int(cursor.rowcount or 0)
            table = "preferences"
        if count:
            add_audit_event(action="forget", table_name=table, memory_id=memory_id, summary=mask_sensitive_preview(query), source="manual", db_path=self.db_path)
        return count

    def list_experiences(self, *, limit: int = 80) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM memory_experiences ORDER BY created_at DESC, id DESC LIMIT ?", (max(1, min(200, int(limit))),)).fetchall()
        result = _rows(rows)
        for item in result:
            item["context"] = _loads(item.get("context_json"), {})
            item["steps"] = _loads(item.get("steps_json"), [])
            item["tools"] = _loads(item.get("tools_json"), [])
            for key in ("context_json", "steps_json", "tools_json"):
                item.pop(key, None)
        return result

    def patterns(self, *, limit: int = 12) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT normalized_goal, goal, status, COUNT(*) AS total
                FROM memory_experiences WHERE confirmed=1
                GROUP BY normalized_goal, goal, status ORDER BY total DESC, normalized_goal LIMIT ?
                """,
                (max(1, min(100, int(limit) * 4)),),
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for item in _rows(rows):
            key = str(item.get("normalized_goal") or "")
            current = grouped.setdefault(key, {"goal": item.get("goal"), "successes": 0, "failures": 0, "total": 0})
            count = int(item.get("total") or 0)
            current["total"] += count
            if item.get("status") == "success":
                current["successes"] += count
            elif item.get("status") == "failure":
                current["failures"] += count
        result = []
        for item in sorted(grouped.values(), key=lambda value: (value["total"], value["successes"]), reverse=True)[: max(1, int(limit))]:
            item["success_rate"] = round(item["successes"] / max(1, item["successes"] + item["failures"]), 3)
            item["failure_rate"] = round(item["failures"] / max(1, item["successes"] + item["failures"]), 3)
            result.append(item)
        return result

    def summary(self) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            counts = {
                "preferences": int(conn.execute("SELECT COUNT(*) FROM preferences WHERE active=1").fetchone()[0]),
                "procedures": int(conn.execute("SELECT COUNT(*) FROM memory_procedures WHERE active=1").fetchone()[0]),
                "experiences": int(conn.execute("SELECT COUNT(*) FROM memory_experiences").fetchone()[0]),
                "confirmed_successes": int(conn.execute("SELECT COUNT(*) FROM memory_experiences WHERE status='success' AND confirmed=1").fetchone()[0]),
                "confirmed_failures": int(conn.execute("SELECT COUNT(*) FROM memory_experiences WHERE status='failure' AND confirmed=1").fetchone()[0]),
            }
        counts["success_rate"] = round(counts["confirmed_successes"] / max(1, counts["confirmed_successes"] + counts["confirmed_failures"]), 3)
        return {**counts, "settings": self.settings(), "provider": "structured_local_memory", "path": str(self.db_path)}

    def manager_payload(self, *, limit: int = 60) -> dict[str, Any]:
        return {
            "ok": True,
            "summary": self.summary(),
            "preferences": self.list_preferences(limit=limit),
            "procedures": self.list_procedures(limit=limit),
            "experiences": self.list_experiences(limit=limit),
            "patterns": self.patterns(limit=12),
        }

    def record_backend_attempt(self, path: str, payload: dict[str, Any] | None, result: dict[str, Any] | None, *, duration_ms: int | None = None, source: str = "tool") -> dict[str, Any]:
        """Record a bounded outcome from a local tool endpoint.

        Explicit ``ok/success`` fields are the confirmation boundary. An
        ambiguous response is stored as ``unknown`` and never improves a
        procedure's confidence.
        """
        if not bool(self._setting("record_tool_outcomes", 1)):
            return {"ok": True, "recorded": False, "reason": "disabled"}
        path_text = str(path or "").strip()
        if not path_text or path_text in {"/api/runtime-status", "/api/session/timing"}:
            return {"ok": True, "recorded": False, "reason": "probe"}
        payload = payload if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        ignored = ("/status", "/state", "/health", "/ready", "/debug", "/events")
        if any(path_text.endswith(item) or item in path_text for item in ignored):
            return {"ok": True, "recorded": False, "reason": "read_only_probe"}
        goal = str(payload.get("goal") or payload.get("objetivo") or payload.get("task") or payload.get("query") or payload.get("destination") or payload.get("value") or payload.get("text") or payload.get("action") or path_text).strip()
        if is_sensitive(goal):
            return {"ok": True, "recorded": False, "reason": "sensitive_content_blocked"}
        status_value = result.get("status")
        if result.get("ok") is True or result.get("success") is True or result.get("accepted") is True:
            status = "success"
            confirmed = True
        elif result.get("ok") is False or result.get("success") is False or result.get("error") or (isinstance(status_value, int) and status_value >= 400):
            status = "failure"
            confirmed = True
        else:
            status = "unknown"
            confirmed = False
        app = str(payload.get("app") or payload.get("application") or payload.get("program") or payload.get("aparelho") or payload.get("device") or "").strip()
        device = str(payload.get("device") or payload.get("aparelho") or "").strip()
        safe_result = {key: value for key, value in result.items() if key not in {"token", "password", "secret", "credential", "raw"}}
        evidence = json.dumps(safe_result, ensure_ascii=False, default=str)[:1600]
        return self.record_attempt(
            goal=goal,
            status=status,
            confirmed=confirmed,
            app=app,
            device=device,
            context={"endpoint": path_text, "app": app, "device": device},
            steps=[f"endpoint {path_text}", str(payload.get("action") or payload.get("acao") or "")[:200]],
            tools=[path_text],
            duration_ms=duration_ms,
            result=evidence,
            error=str(result.get("error") or result.get("message") or "")[:1000],
            evidence=evidence,
            source=source,
        )


def _text_result(text: str, *, source: str = "memory") -> str:
    return str(text or "").strip()


def create_memory_router(
    store: ExperienceMemory,
    *,
    token_provider: Callable[[], str],
    publish: Callable[[dict[str, Any]], Any] | None = None,
) -> APIRouter:
    router = APIRouter()

    def allow(request: Request, token: str | None = None) -> None:
        if not (is_local_request(request) or (token and token == token_provider())):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    @router.get("/api/memory/manager")
    async def manager(request: Request, limit: int = 60) -> dict[str, Any]:
        allow(request)
        return store.manager_payload(limit=limit)

    @router.get("/api/memory/recommend")
    async def recommend(request: Request, goal: str = "", app: str = "", device: str = "") -> dict[str, Any]:
        allow(request)
        return store.recommend(goal, app=app, device=device)

    @router.post("/api/memory/preferences")
    async def preference(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        try:
            saved = store.remember_preference(
                str(body.get("content") or body.get("preference") or ""),
                context=body.get("context") if isinstance(body.get("context"), dict) else {},
                scope=str(body.get("scope") or "") or None,
                source=str(body.get("source") or "settings")[:80],
                confidence=float(body.get("confidence", 1.0)),
                explicit=bool(body.get("explicit", True)),
                supersedes_id=int(body["supersedes_id"]) if body.get("supersedes_id") else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "preference": saved}

    @router.patch("/api/memory/preferences/{memory_id}")
    async def edit_preference(memory_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        with _connect(store.db_path) as conn:
            updates: list[str] = ["updated_at=?"]
            params: list[Any] = [_now()]
            if "content" in body or "preference" in body:
                content = str(body.get("content") or body.get("preference") or "").strip()
                if not content or is_sensitive(content):
                    raise HTTPException(status_code=422, detail="preferência inválida ou sensível")
                updates.append("preference=?")
                params.append(content[:MAX_GOAL_CHARS])
            if "scope" in body:
                updates.append("scope=?")
                params.append(str(body.get("scope") or "general")[:120])
            if "active" in body:
                updates.append("active=?")
                params.append(int(bool(body.get("active"))))
            params.append(int(memory_id))
            cursor = conn.execute(f"UPDATE preferences SET {', '.join(updates)} WHERE id=?", params)
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="memória não encontrada")
        return {"ok": True, "manager": store.manager_payload()}

    @router.delete("/api/memory/{kind}/{memory_id}")
    async def forget(kind: str, memory_id: int, request: Request, x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        count = store.forget(kind=kind, memory_id=memory_id)
        return {"ok": True, "removed": count, "manager": store.manager_payload()}

    @router.post("/api/memory/procedures")
    async def procedure(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        goal = str(body.get("goal") or body.get("objetivo") or "").strip()
        if not goal:
            raise HTTPException(status_code=422, detail="objetivo obrigatório")
        item = store._ensure_procedure(
            goal=goal,
            app=str(body.get("app") or ""),
            device=str(body.get("device") or ""),
            context=body.get("context") if isinstance(body.get("context"), dict) else {},
            steps=list(body.get("steps") or body.get("acoes") or []),
            tools=list(body.get("tools") or body.get("ferramentas") or []),
            explicit=bool(body.get("explicit", True)),
            source=str(body.get("source") or "settings"),
            confidence=float(body.get("confidence", 0.95)),
        )
        return {"ok": True, "procedure": store._decorate_procedure(item)}

    @router.post("/api/memory/attempts")
    async def attempt(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return store.record_attempt(
            goal=str(body.get("goal") or body.get("objetivo") or ""),
            status=str(body.get("status") or "unknown"),
            confirmed=bool(body.get("confirmed", False)),
            procedure_id=int(body["procedure_id"]) if body.get("procedure_id") else None,
            strategy=str(body.get("strategy") or body.get("estrategia") or ""),
            app=str(body.get("app") or ""),
            device=str(body.get("device") or ""),
            context=body.get("context") if isinstance(body.get("context"), dict) else {},
            steps=list(body.get("steps") or body.get("acoes") or []),
            tools=list(body.get("tools") or body.get("ferramentas") or []),
            duration_ms=int(body["duration_ms"]) if body.get("duration_ms") is not None else None,
            result=str(body.get("result") or body.get("resultado") or ""),
            error=str(body.get("error") or body.get("erro") or ""),
            correction=str(body.get("correction") or body.get("correcao") or ""),
            evidence=str(body.get("evidence") or body.get("evidencia") or ""),
            source=str(body.get("source") or "settings"),
        )

    @router.post("/api/memory/correction")
    async def correction(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return store.record_correction(
            correction=str(body.get("correction") or body.get("correcao") or body.get("content") or ""),
            goal=str(body.get("goal") or body.get("objetivo") or ""),
            steps=list(body.get("steps") or body.get("acoes") or []),
            app=str(body.get("app") or ""),
            device=str(body.get("device") or ""),
            context=body.get("context") if isinstance(body.get("context"), dict) else {},
        )

    @router.get("/api/memory/settings")
    async def settings(request: Request) -> dict[str, Any]:
        allow(request)
        return {"ok": True, "settings": store.settings()}

    @router.put("/api/memory/settings")
    async def update_settings(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "settings": store.update_settings(body)}

    @router.post("/api/memory/explain")
    async def explain(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return store.explain(str(body.get("goal") or body.get("objetivo") or ""))

    return router


_INSTANCE: ExperienceMemory | None = None


def get_experience_memory() -> ExperienceMemory:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ExperienceMemory()
    return _INSTANCE


def record_tool_backend_attempt(path: str, payload: dict[str, Any] | None, result: dict[str, Any] | None, *, duration_ms: int | None = None, source: str = "tool") -> dict[str, Any]:
    try:
        return get_experience_memory().record_backend_attempt(path, payload, result, duration_ms=duration_ms, source=source)
    except Exception as exc:  # Memória nunca pode quebrar a ferramenta principal.
        logger.warning("EXPERIENCE_MEMORY_RECORD_FAILED error_type=%s", exc.__class__.__name__)
        return {"ok": False, "recorded": False, "reason": exc.__class__.__name__}


__all__ = ["ExperienceMemory", "create_memory_router", "get_experience_memory", "record_tool_backend_attempt"]
