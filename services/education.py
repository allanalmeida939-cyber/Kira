"""Núcleo persistente do workspace educacional da Kira.

O módulo complementa ``services.tutor`` sem duplicar a biblioteca de documentos,
FTS5 ou o gerador de simulados já existentes. Cursos, sessões, tarefas,
revisões, competências e histórico ficam no mesmo SQLite de Estudos para que a
interface e as ferramentas de voz consultem a mesma fonte de verdade.

Nenhum indicador é preenchido com valores inventados: quando não há registros,
o dashboard devolve zero/``None`` e a UI mostra um estado vazio útil.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "runtime" / "tutor.sqlite3"


def fold(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(value or "").lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


def _now() -> float:
    return time.time()


def _iso(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="minutes")


def _parse_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None


def _date_ts(value: Any, fallback: float | None = None) -> float:
    parsed = _parse_date(value)
    if not parsed:
        return fallback if fallback is not None else _now()
    return datetime.fromisoformat(parsed).replace(hour=9).timestamp()


def _int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _float(value: Any, default: float = 0.0, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


class EducationStore:
    """SQLite store compartilhado por navegador, ferramentas e relatórios."""

    def __init__(self, db: Path = DB, user_id: str = "") -> None:
        self.db_path = Path(db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_id = str(user_id or "").strip() or "alan"
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS education_courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'curso',
                    target TEXT NOT NULL DEFAULT '',
                    organization TEXT NOT NULL DEFAULT '',
                    level TEXT NOT NULL DEFAULT '',
                    exam_date TEXT,
                    weekly_hours REAL NOT NULL DEFAULT 0,
                    daily_goal_minutes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, name)
                );
                CREATE TABLE IF NOT EXISTS education_subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1,
                    mastery REAL NOT NULL DEFAULT 0,
                    target_mastery REAL NOT NULL DEFAULT 80,
                    created_at REAL NOT NULL,
                    UNIQUE(course_id, name)
                );
                CREATE TABLE IF NOT EXISTS education_sessions (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES education_subjects(id) ON DELETE SET NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    minutes REAL NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'estudo',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS education_tasks (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES education_subjects(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'estudo',
                    due_at REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    minutes INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS education_reviews (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES education_subjects(id) ON DELETE SET NULL,
                    topic TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    completed_at REAL,
                    interval_days INTEGER NOT NULL DEFAULT 1,
                    ease REAL NOT NULL DEFAULT 2.5,
                    source TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS education_materials (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'material',
                    source TEXT NOT NULL DEFAULT '',
                    official INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    added_at REAL NOT NULL,
                    UNIQUE(course_id, title, url)
                );
                CREATE TABLE IF NOT EXISTS education_attempts (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES education_subjects(id) ON DELETE SET NULL,
                    topic TEXT NOT NULL,
                    question_id TEXT NOT NULL DEFAULT '',
                    answer TEXT NOT NULL DEFAULT '',
                    correct INTEGER NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS education_errors (
                    id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES education_courses(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES education_subjects(id) ON DELETE SET NULL,
                    topic TEXT NOT NULL,
                    question_id TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    next_review_at REAL,
                    resolved_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS education_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER REFERENCES education_courses(id) ON DELETE CASCADE,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS education_tasks_due ON education_tasks(course_id, due_at, status);
                CREATE INDEX IF NOT EXISTS education_reviews_due ON education_reviews(course_id, scheduled_at, completed_at);
                CREATE INDEX IF NOT EXISTS education_attempts_topic ON education_attempts(course_id, topic, created_at);
                """
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _write(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            # INSERT devolve o id novo; UPDATE/DELETE quantas linhas mudaram
            return int(cur.lastrowid or 0) if sql.lstrip().upper().startswith("INSERT") else int(cur.rowcount or 0)

    def _course(self, course_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM education_courses WHERE id=? AND user_id=?", (int(course_id), self.user_id))

    def _course_by_name(self, name: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM education_courses WHERE user_id=? AND lower(name)=lower(?)", (self.user_id, str(name).strip()))

    @staticmethod
    def _official(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return (
            host.endswith(".gov.br")
            or host.endswith(".jus.br")
            or host.endswith(".leg.br")
            or host.endswith(".edu.br")
            or host == "uece.br"
            or host.endswith(".uece.br")
        )

    def create_course(self, data: dict[str, Any]) -> dict[str, Any]:
        name = re.sub(r"\s+", " ", str(data.get("name") or data.get("curso") or "").strip())[:120]
        if not name:
            raise ValueError("informe o nome do curso ou concurso")
        now = _now()
        values = (
            self.user_id,
            name,
            str(data.get("kind") or "curso")[:40],
            str(data.get("target") or data.get("objetivo") or "")[:180],
            str(data.get("organization") or data.get("banca") or "")[:120],
            str(data.get("level") or data.get("nivel") or "")[:40],
            _parse_date(data.get("exam_date") or data.get("data_prova")),
            _float(data.get("weekly_hours"), 0, 0, 168),
            _int(data.get("daily_goal_minutes"), 0, 0, 1440),
            now,
            now,
        )
        with self._lock:
            self._db.execute(
                """INSERT INTO education_courses
                (user_id,name,kind,target,organization,level,exam_date,weekly_hours,daily_goal_minutes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,name) DO UPDATE SET
                  kind=excluded.kind, target=excluded.target, organization=excluded.organization,
                  level=excluded.level, exam_date=excluded.exam_date, weekly_hours=excluded.weekly_hours,
                  daily_goal_minutes=excluded.daily_goal_minutes, updated_at=excluded.updated_at""",
                values,
            )
            self._db.commit()
        course = self._course_by_name(name)
        if not course:
            raise RuntimeError("curso não foi salvo")
        self._write(
            "INSERT INTO education_history(course_id,event,detail,created_at) VALUES(?,?,?,?)",
            (course["id"], "course_created", json.dumps({"name": name}, ensure_ascii=False), now),
        )
        return self.course_detail(int(course["id"]))

    def list_courses(self) -> list[dict[str, Any]]:
        return [self._course_summary(row) for row in self._rows("SELECT * FROM education_courses WHERE user_id=? ORDER BY updated_at DESC", (self.user_id,))]

    def _existing_quiz_stats(self, course_name: str) -> tuple[int, int, float | None, float | None, list[dict[str, Any]]]:
        try:
            rows = self._rows("SELECT id, topic, result, created FROM quizzes WHERE course=? ORDER BY created DESC", (course_name,))
        except sqlite3.Error:
            return 0, 0, None, None, []
        completed = [row for row in rows if row.get("result")]
        scores: list[float] = []
        correct = total = 0
        history: list[dict[str, Any]] = []
        for row in rows[:20]:
            result = json.loads(row["result"]) if row.get("result") else None
            if result:
                scores.append(_float(result.get("nota"), 0, 0, 10))
                correct += _int(result.get("acertos"), 0, 0, 100000)
                total += _int(result.get("total"), 0, 0, 100000)
            history.append({"id": row["id"], "topic": row["topic"], "score": result.get("nota") if result else None, "created_at": _iso(row.get("created"))})
        return len(rows), len(completed), (round(sum(scores) / len(scores), 1) if scores else None), (round(100 * correct / total, 1) if total else None), history

    def _quiz_stats(self, course: dict[str, Any]) -> tuple[int, int, float | None, float | None, list[dict[str, Any]]]:
        stats = self._existing_quiz_stats(course["name"])
        if stats[0]:
            return stats
        rows = self._rows(
            "SELECT detail, created_at FROM education_history WHERE course_id=? AND event='quiz_graded' ORDER BY created_at DESC LIMIT 50",
            (course["id"],),
        )
        if not rows:
            return stats
        scores: list[float] = []
        correct = total = 0
        history: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row.get("detail") or "{}")
            except json.JSONDecodeError:
                detail = {}
            score = detail.get("score")
            if score is not None:
                scores.append(_float(score, 0, 0, 10))
            correct += _int(detail.get("correct"), 0, 0, 100000)
            total += _int(detail.get("total"), 0, 0, 100000)
            history.append({"topic": "simulado", "score": score, "created_at": _iso(row.get("created_at"))})
        average = round(sum(scores) / len(scores), 1) if scores else None
        accuracy = round(100 * correct / total, 1) if total else None
        return len(rows), len(rows), average, accuracy, history

    def _course_summary(self, course: dict[str, Any]) -> dict[str, Any]:
        course_id = int(course["id"])
        subjects = self._rows("SELECT * FROM education_subjects WHERE course_id=? ORDER BY name", (course_id,))
        for subject in subjects:
            attempts = self._one("SELECT count(*) AS n FROM education_attempts WHERE subject_id=?", (subject["id"],)) or {}
            subject["attempts"] = int(attempts.get("n") or 0)
        task_counts = self._rows("SELECT status, count(*) AS n FROM education_tasks WHERE course_id=? GROUP BY status", (course_id,))
        counts = {row["status"]: int(row["n"]) for row in task_counts}
        minutes = self._one("SELECT COALESCE(SUM(minutes),0) AS n, count(*) AS sessions FROM education_sessions WHERE course_id=? AND ended_at IS NOT NULL", (course_id,)) or {}
        quiz_count, completed_quizzes, average, accuracy, _history = self._quiz_stats(course)
        return {
            "id": course_id,
            "name": course["name"],
            "kind": course["kind"],
            "target": course["target"],
            "organization": course["organization"],
            "level": course["level"],
            "exam_date": course["exam_date"],
            "weekly_hours": course["weekly_hours"],
            "daily_goal_minutes": course["daily_goal_minutes"],
            "status": course["status"],
            "subjects": len(subjects),
            "lessons_completed": int(minutes.get("sessions") or 0),
            "study_minutes": round(float(minutes.get("n") or 0), 1),
            "quizzes": quiz_count,
            "quizzes_completed": completed_quizzes,
            "average_score": average,
            "accuracy": accuracy,
            "pending_tasks": counts.get("pending", 0),
        }

    def course_detail(self, course_id: int) -> dict[str, Any]:
        course = self._course(course_id)
        if not course:
            raise KeyError(course_id)
        subjects = self._rows("SELECT * FROM education_subjects WHERE course_id=? ORDER BY name", (course_id,))
        for subject in subjects:
            attempts = self._one("SELECT count(*) AS n FROM education_attempts WHERE subject_id=?", (subject["id"],)) or {}
            subject["attempts"] = int(attempts.get("n") or 0)
        tasks = self._rows(
            "SELECT t.*, s.name AS subject_name FROM education_tasks t LEFT JOIN education_subjects s ON s.id=t.subject_id WHERE t.course_id=? ORDER BY t.status, t.due_at, t.created_at DESC LIMIT 100",
            (course_id,),
        )
        for task in tasks:
            task["due_at"] = _iso(task.get("due_at"))
            task["completed_at"] = _iso(task.get("completed_at"))
        reviews = self._rows(
            "SELECT r.*, s.name AS subject_name FROM education_reviews r LEFT JOIN education_subjects s ON s.id=r.subject_id WHERE r.course_id=? ORDER BY r.completed_at IS NOT NULL, r.scheduled_at LIMIT 100",
            (course_id,),
        )
        for review in reviews:
            review["scheduled_at"] = _iso(review.get("scheduled_at"))
            review["completed_at"] = _iso(review.get("completed_at"))
        materials = self._rows("SELECT * FROM education_materials WHERE course_id=? ORDER BY official DESC, added_at DESC", (course_id,))
        for material in materials:
            material["added_at"] = _iso(material.get("added_at"))
        errors = self._rows(
            "SELECT e.*, s.name AS subject_name FROM education_errors e LEFT JOIN education_subjects s ON s.id=e.subject_id WHERE e.course_id=? AND e.resolved_at IS NULL ORDER BY e.next_review_at, e.created_at DESC LIMIT 100",
            (course_id,),
        )
        for error in errors:
            error["next_review_at"] = _iso(error.get("next_review_at"))
            error["created_at"] = _iso(error.get("created_at"))
        _quiz_count, _completed, average, accuracy, quiz_history = self._quiz_stats(course)
        try:
            lessons = self._rows("SELECT id, topic, at FROM lessons WHERE course=? ORDER BY at DESC LIMIT 100", (course["name"],))
        except sqlite3.Error:
            lessons = []
        for lesson in lessons:
            lesson["created_at"] = _iso(lesson.pop("at", None))
        history = self._rows(
            "SELECT event, detail, created_at FROM education_history WHERE course_id=? ORDER BY created_at DESC LIMIT 100",
            (course_id,),
        )
        for item in history:
            item["created_at"] = _iso(item.get("created_at"))
        return {
            "course": self._course_summary(course),
            "subjects": subjects,
            "tasks": tasks,
            "reviews": reviews,
            "materials": materials,
            "errors": errors,
            "lessons": lessons,
            "quiz_history": quiz_history,
            "average_score": average,
            "accuracy": accuracy,
            "history": history,
        }

    def _streak(self, course_id: int) -> int:
        rows = self._rows(
            "SELECT DISTINCT date(started_at,'localtime') AS day FROM education_sessions WHERE course_id=? AND ended_at IS NOT NULL ORDER BY day DESC LIMIT 90",
            (course_id,),
        )
        if not rows:
            return 0
        days = {row["day"] for row in rows}
        current = datetime.now().date()
        if current.isoformat() not in days:
            current -= timedelta(days=1)
        streak = 0
        while current.isoformat() in days:
            streak += 1
            current -= timedelta(days=1)
        return streak

    def workspace(self, course_id: int | None = None) -> dict[str, Any]:
        courses = self.list_courses()
        selected = int(course_id) if course_id else (courses[0]["id"] if courses else None)
        detail = self.course_detail(selected) if selected else None
        summary = {
            "courses": len(courses),
            "lessons_completed": detail["course"]["lessons_completed"] if detail else 0,
            "study_minutes": detail["course"]["study_minutes"] if detail else 0,
            "study_hours": round((detail["course"]["study_minutes"] if detail else 0) / 60, 1),
            "quizzes_completed": detail["course"]["quizzes_completed"] if detail else 0,
            "average_score": detail["average_score"] if detail else None,
            "accuracy": detail["accuracy"] if detail else None,
            "streak_days": self._streak(selected) if selected else 0,
            "reviews_due": sum(1 for row in (detail["reviews"] if detail else []) if not row.get("completed_at") and row.get("scheduled_at") and row["scheduled_at"] <= _iso(_now() + 60)[:16]),
            "pending_tasks": sum(1 for row in (detail["tasks"] if detail else []) if row.get("status") == "pending"),
            "exam_days": None,
        }
        if detail and detail["course"].get("exam_date"):
            try:
                summary["exam_days"] = (datetime.fromisoformat(detail["course"]["exam_date"]).date() - datetime.now().date()).days
            except ValueError:
                pass
        subjects = detail["subjects"] if detail else []
        difficult = sorted([s for s in subjects if float(s.get("mastery") or 0) < 70], key=lambda s: float(s.get("mastery") or 0))
        dominated = [s for s in subjects if float(s.get("mastery") or 0) >= float(s.get("target_mastery") or 80) and _int(s.get("attempts"), 0) >= 5]
        return {
            "ok": True,
            "selected_course_id": selected,
            "courses": courses,
            "summary": summary,
            "detail": detail,
            "difficult_subjects": difficult[:5],
            "dominated_subjects": dominated[:5],
            "generated_at": _iso(_now()),
        }

    def add_subject(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        if not self._course(course_id):
            raise KeyError(course_id)
        name = re.sub(r"\s+", " ", str(data.get("name") or data.get("disciplina") or "").strip())[:120]
        if not name:
            raise ValueError("informe a disciplina")
        with self._lock:
            self._db.execute(
                """INSERT INTO education_subjects(course_id,name,weight,mastery,target_mastery,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(course_id,name) DO UPDATE SET weight=excluded.weight,target_mastery=excluded.target_mastery""",
                (course_id, name, _float(data.get("weight"), 1, 0.1, 20), _float(data.get("mastery"), 0, 0, 100), _float(data.get("target_mastery"), 80, 1, 100), _now()),
            )
            self._db.commit()
        return self._one("SELECT * FROM education_subjects WHERE course_id=? AND lower(name)=lower(?)", (course_id, name)) or {}

    def create_plan(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        if not self._course(course_id):
            raise KeyError(course_id)
        days = _int(data.get("days_per_week"), 5, 1, 7)
        minutes = _int(data.get("minutes_per_day"), 60, 10, 720)
        start = _date_ts(data.get("start_date"), _now())
        subjects = self._rows("SELECT * FROM education_subjects WHERE course_id=? ORDER BY mastery, name", (course_id,))
        if not subjects:
            subject = self.add_subject(course_id, {"name": "Diagnóstico inicial", "target_mastery": 80})
            subjects = [subject]
        created: list[str] = []
        for index, subject in enumerate(subjects):
            due = start + ((index % max(days, 1)) * 86400)
            task_id = uuid.uuid4().hex[:12]
            title = f"Estudar {subject['name']}"
            self._write(
                "INSERT INTO education_tasks(id,course_id,subject_id,title,kind,due_at,status,minutes,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, course_id, subject["id"], title, "estudo", due, "pending", minutes, "plano_adaptativo", _now()),
            )
            created.append(task_id)
        self._write(
            "INSERT INTO education_history(course_id,event,detail,created_at) VALUES(?,?,?,?)",
            (course_id, "plan_created", json.dumps({"tasks": len(created), "days_per_week": days, "minutes_per_day": minutes}, ensure_ascii=False), _now()),
        )
        return {"ok": True, "created": len(created), "task_ids": created, "course_id": course_id}

    def create_task(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        title = re.sub(r"\s+", " ", str(data.get("title") or data.get("titulo") or "").strip())[:180]
        if not title:
            raise ValueError("informe a atividade")
        task_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO education_tasks(id,course_id,subject_id,title,kind,due_at,status,minutes,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, course_id, _int(data.get("subject_id"), 0) or None, title, str(data.get("kind") or "atividade")[:40], _date_ts(data.get("due_at"), _now()), "pending", _int(data.get("minutes"), 0, 0, 1440), str(data.get("source") or "")[:300], _now()),
        )
        return self._one("SELECT * FROM education_tasks WHERE id=?", (task_id,)) or {}

    def delete_course(self, course_id: int) -> str:
        """Apaga o curso e tudo dele (matérias, plano, sessões, revisões, materiais, tentativas, histórico)."""
        course = self._course(course_id)
        if not course:
            raise KeyError(course_id)
        self._write("DELETE FROM education_history WHERE course_id=?", (course_id,))
        self._write("DELETE FROM education_courses WHERE id=?", (course_id,))  # o resto sai por ON DELETE CASCADE
        return str(course.get("name") or "")

    def update_task(self, task_id: str, status: str) -> dict[str, Any]:
        status = status if status in {"pending", "done", "skipped"} else "pending"
        self._write("UPDATE education_tasks SET status=?, completed_at=? WHERE id=?", (status, _now() if status == "done" else None, task_id))
        return self._one("SELECT * FROM education_tasks WHERE id=?", (task_id,)) or {}

    def start_session(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        if not self._course(course_id):
            raise KeyError(course_id)
        session_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO education_sessions(id,course_id,subject_id,started_at,mode,notes) VALUES(?,?,?,?,?,?)",
            (session_id, course_id, _int(data.get("subject_id"), 0) or None, _now(), str(data.get("mode") or "estudo")[:40], str(data.get("notes") or "")[:1000]),
        )
        return self._one("SELECT * FROM education_sessions WHERE id=?", (session_id,)) or {}

    def finish_session(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        session = self._one("SELECT * FROM education_sessions WHERE id=?", (session_id,))
        if not session:
            raise KeyError(session_id)
        ended = _now()
        minutes = _float(data.get("minutes"), max(0.0, (ended - float(session["started_at"])) / 60), 0, 1440)
        self._write("UPDATE education_sessions SET ended_at=?, minutes=?, notes=CASE WHEN ?='' THEN notes ELSE ? END WHERE id=?", (ended, round(minutes, 1), str(data.get("notes") or ""), str(data.get("notes") or ""), session_id))
        self._write("INSERT INTO education_history(course_id,event,detail,created_at) VALUES(?,?,?,?)", (session["course_id"], "session_finished", json.dumps({"minutes": round(minutes, 1)}, ensure_ascii=False), ended))
        return self._one("SELECT * FROM education_sessions WHERE id=?", (session_id,)) or {}

    def schedule_review(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        topic = re.sub(r"\s+", " ", str(data.get("topic") or data.get("tema") or "").strip())[:160]
        if not topic:
            raise ValueError("informe o assunto da revisão")
        review_id = uuid.uuid4().hex[:12]
        scheduled = _date_ts(data.get("scheduled_at") or data.get("data"), _now())
        self._write(
            "INSERT INTO education_reviews(id,course_id,subject_id,topic,scheduled_at,interval_days,ease,source) VALUES(?,?,?,?,?,?,?,?)",
            (review_id, course_id, _int(data.get("subject_id"), 0) or None, topic, scheduled, _int(data.get("interval_days"), 1, 1, 365), _float(data.get("ease"), 2.5, 1, 4), str(data.get("source") or "manual")[:80]),
        )
        return self._one("SELECT * FROM education_reviews WHERE id=?", (review_id,)) or {}

    def complete_review(self, review_id: str) -> dict[str, Any]:
        review = self._one("SELECT * FROM education_reviews WHERE id=?", (review_id,))
        if not review:
            raise KeyError(review_id)
        now = _now()
        interval = _int(review.get("interval_days"), 1, 1, 365) * 2
        self._write("UPDATE education_reviews SET completed_at=?, interval_days=?, scheduled_at=? WHERE id=?", (now, interval, now + interval * 86400, review_id))
        return self._one("SELECT * FROM education_reviews WHERE id=?", (review_id,)) or {}

    def import_materials(self, course_id: int, urls: list[str], kind: str = "pesquisa") -> list[dict[str, Any]]:
        if not self._course(course_id):
            raise KeyError(course_id)
        result: list[dict[str, Any]] = []
        for url in list(dict.fromkeys(str(item).strip() for item in urls if str(item).strip()))[:40]:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            title = (Path(parsed.path).name or parsed.netloc or url)[:180]
            material_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{course_id}:{url}").hex[:12]
            with self._lock:
                self._db.execute(
                    """INSERT INTO education_materials(id,course_id,title,url,kind,source,official,status,added_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(course_id,title,url) DO UPDATE SET status=excluded.status, added_at=excluded.added_at""",
                    (material_id, course_id, title, url[:800], kind[:40], parsed.netloc[:120], int(self._official(url)), "discovered", _now()),
                )
                self._db.commit()
            row = self._one("SELECT * FROM education_materials WHERE course_id=? AND url=?", (course_id, url))
            if row:
                row["added_at"] = _iso(row.get("added_at"))
                result.append(row)
        return result

    def record_quiz_result(self, course_name: str, result: dict[str, Any]) -> None:
        course = self._course_by_name(course_name) or self.create_course({"name": course_name, "kind": "concurso"})
        course_id = int(course["id"])
        for item in result.get("items") or []:
            topic = str(item.get("tema") or "geral")[:160]
            subject = self._one("SELECT * FROM education_subjects WHERE course_id=? AND lower(name)=lower(?)", (course_id, topic))
            if not subject:
                subject = self.add_subject(course_id, {"name": topic})
            score = _float(item.get("nota"), 1 if item.get("certa") else 0, 0, 1)
            self._write(
                "INSERT INTO education_attempts(id,course_id,subject_id,topic,question_id,answer,correct,score,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], course_id, subject.get("id"), topic, str(item.get("n") or ""), str(item.get("resposta") or "")[:4000], int(bool(item.get("certa"))), score, str(item.get("fonte") or "")[:400], _now()),
            )
            if not item.get("certa"):
                self._write(
                    "INSERT INTO education_errors(id,course_id,subject_id,topic,question_id,explanation,next_review_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], course_id, subject.get("id"), topic, str(item.get("n") or ""), json.dumps(item.get("justificativas") or {}, ensure_ascii=False)[:4000], _now() + 86400, _now()),
                )
        self._recalculate_subjects(course_id)
        self._write("INSERT INTO education_history(course_id,event,detail,created_at) VALUES(?,?,?,?)", (course_id, "quiz_graded", json.dumps({"score": result.get("nota"), "correct": result.get("acertos"), "total": result.get("total")}, ensure_ascii=False), _now()))

    def _recalculate_subjects(self, course_id: int) -> None:
        subjects = self._rows("SELECT id FROM education_subjects WHERE course_id=?", (course_id,))
        for subject in subjects:
            row = self._one("SELECT count(*) AS total, COALESCE(sum(score),0) AS score FROM education_attempts WHERE subject_id=?", (subject["id"],)) or {}
            mastery = round(100 * float(row.get("score") or 0) / int(row.get("total") or 1), 1) if row.get("total") else 0
            self._write("UPDATE education_subjects SET mastery=? WHERE id=?", (mastery, subject["id"]))
            # Plano anda sozinho: matéria que atingiu a meta (com ao menos 5 questões) fecha as tarefas pendentes dela
            # e a próxima matéria vira a da vez — sem o Chefe precisar clicar em "concluir".
            target = self._one("SELECT target_mastery FROM education_subjects WHERE id=?", (subject["id"],)) or {}
            if row.get("total") and int(row["total"]) >= 5 and mastery >= float(target.get("target_mastery") or 80):
                closed = self._write(
                    "UPDATE education_tasks SET status='done', completed_at=? WHERE subject_id=? AND status='pending'",
                    (_now(), subject["id"]),
                )
                if closed:
                    self._write("INSERT INTO education_history(course_id,event,detail,created_at) VALUES(?,?,?,?)",
                                (course_id, "subject_mastered", json.dumps({"subject_id": subject["id"], "mastery": mastery, "tasks_closed": closed}, ensure_ascii=False), _now()))

    def assistant_summary(self, action: str, course_name: str = "") -> dict[str, Any]:
        course = self._course_by_name(course_name) if course_name else None
        if action == "desempenho":
            data = self.workspace(int(course["id"])) if course else self.workspace()
            return {"ok": True, "summary": data["summary"], "difficult_subjects": data["difficult_subjects"], "dominated_subjects": data["dominated_subjects"]}
        if action in {"revisoes", "revisao"}:
            data = self.workspace(int(course["id"])) if course else self.workspace()
            return {"ok": True, "reviews": (data.get("detail") or {}).get("reviews", [])}
        if action in {"aulas", "aula_anterior", "aulas_anteriores"}:
            data = self.workspace(int(course["id"])) if course else self.workspace()
            return {"ok": True, "lessons": (data.get("detail") or {}).get("lessons", [])}
        if action in {"erros", "caderno", "caderno_de_erros"}:
            data = self.workspace(int(course["id"])) if course else self.workspace()
            return {"ok": True, "errors": (data.get("detail") or {}).get("errors", [])}
        if action in {"historico", "histórico"}:
            selected = int(course["id"]) if course else (self.list_courses()[0]["id"] if self.list_courses() else None)
            return {"ok": True, "history": self._rows("SELECT event,detail,created_at FROM education_history WHERE course_id=? ORDER BY created_at DESC LIMIT 50", (selected,)) if selected else []}
        data = self.workspace(int(course["id"])) if course else self.workspace()
        return {**data, "workspace": data}


def create_education_router(
    store: EducationStore,
    *,
    token_provider: Callable[[], str],
    research: Callable[[str, Callable[[dict[str, Any]], None]], dict[str, Any]] | None = None,
    ingest: Callable[[str, str, Callable[[dict[str, Any]], None]], str] | None = None,
    quiz_generate: Callable[[str, str, int, str], Awaitable[dict[str, Any]]] | None = None,
    quiz_grade: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    quiz_export: Callable[[str, str], list[dict[str, Any]]] | None = None,
    forget_course: Callable[[str], Any] | None = None,
    publish: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
) -> APIRouter:
    router = APIRouter()

    def allow(request: Request, token: str | None) -> None:
        if not (is_local_request(request) or (token and token == token_provider())):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    def course_or_404(course_id: int) -> dict[str, Any]:
        course = store._course(course_id)
        if not course:
            raise HTTPException(status_code=404, detail="curso não encontrado")
        return course

    @router.get("/api/education/workspace")
    async def workspace(request: Request, course_id: int | None = None) -> dict[str, Any]:
        allow(request, None)
        return store.workspace(course_id)

    @router.get("/api/education/courses")
    async def courses(request: Request) -> dict[str, Any]:
        allow(request, None)
        return {"ok": True, "courses": store.list_courses()}

    @router.post("/api/education/courses")
    async def create_course(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "course": store.create_course(body)}

    @router.get("/api/education/courses/{course_id}")
    async def get_course(course_id: int, request: Request) -> dict[str, Any]:
        allow(request, None)
        return {"ok": True, **store.course_detail(course_or_404(course_id)["id"])}

    @router.post("/api/education/courses/{course_id}/subjects")
    async def add_subject(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        course_or_404(course_id)
        return {"ok": True, "subject": store.add_subject(course_id, body)}

    @router.post("/api/education/courses/{course_id}/plan")
    async def plan(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return store.create_plan(course_or_404(course_id)["id"], body)

    @router.post("/api/education/courses/{course_id}/tasks")
    async def task(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "task": store.create_task(course_or_404(course_id)["id"], body)}

    @router.patch("/api/education/tasks/{task_id}")
    async def update_task(task_id: str, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "task": store.update_task(task_id, str(body.get("status") or "pending"))}

    @router.post("/api/education/courses/{course_id}/sessions")
    async def start_session(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "session": store.start_session(course_or_404(course_id)["id"], body)}

    @router.post("/api/education/sessions/{session_id}/finish")
    async def finish_session(session_id: str, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "session": store.finish_session(session_id, body)}

    @router.post("/api/education/courses/{course_id}/reviews")
    async def review(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "review": store.schedule_review(course_or_404(course_id)["id"], body)}

    @router.post("/api/education/reviews/{review_id}/complete")
    async def complete_review(review_id: str, request: Request, x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        return {"ok": True, "review": store.complete_review(review_id)}

    @router.post("/api/education/courses/{course_id}/materials")
    async def material(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        course = course_or_404(course_id)
        source = str(body.get("url") or body.get("fonte") or "").strip()
        materials = store.import_materials(course_id, [source], str(body.get("kind") or "material"))
        job = None
        if ingest and source:
            try:
                job = ingest(course["name"], source, lambda _job: None)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:200], "materials": materials}
        return {"ok": True, "materials": materials, "job": job}

    @router.post("/api/education/courses/{course_id}/research")
    async def research_course(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        course = course_or_404(course_id)
        if not research:
            raise HTTPException(status_code=503, detail="pesquisa educacional indisponível")
        result = await asyncio.to_thread(research, course["name"], lambda _job: None)
        urls = list(result.get("editais") or []) + list(result.get("provas") or [])
        materials = store.import_materials(course_id, urls, "pesquisa")
        return {"ok": True, "result": result, "materials": materials}

    @router.post("/api/education/courses/{course_id}/quiz")
    async def quiz(course_id: int, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        course = course_or_404(course_id)
        if not quiz_generate:
            raise HTTPException(status_code=503, detail="gerador educacional indisponível")
        created = await quiz_generate(course["name"], str(body.get("topic") or body.get("tema") or course["name"]), _int(body.get("count") or body.get("quantidade"), 5, 1, 50), str(body.get("banca") or ""))
        return {"ok": True, "quiz": created}

    @router.delete("/api/education/courses/{course_id}")
    async def delete_course(course_id: int, request: Request, x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        try:
            name = store.delete_course(course_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="curso não encontrado")
        if forget_course and name:
            try:  # biblioteca, simulados e aulas da professora para esse curso
                forget_course(name)
            except Exception:  # noqa: BLE001 - o curso já saiu; biblioteca fica para limpar depois
                pass
        return {"ok": True, "deleted": name}

    @router.post("/api/education/quiz/{quiz_id}/grade")
    async def grade_quiz(quiz_id: str, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        if not quiz_grade:
            raise HTTPException(status_code=503, detail="corretor educacional indisponível")
        result = await quiz_grade(quiz_id, body.get("answers") or body.get("respostas") or {})
        course_name = str(result.get("curso") or body.get("curso") or "")
        if course_name:
            store.record_quiz_result(course_name, result)
        return {"ok": True, "result": result}

    @router.post("/api/education/quiz/{quiz_id}/export")
    async def export_quiz(quiz_id: str, request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        if not quiz_export:
            raise HTTPException(status_code=503, detail="exportação educacional indisponível")
        return {"ok": True, "files": quiz_export(quiz_id, str(body.get("format") or body.get("formato") or "pdf"))}

    @router.post("/api/education/assistant")
    async def assistant(request: Request, body: dict[str, Any] = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        action = fold(str(body.get("acao") or body.get("action") or "painel"))
        course_name = str(body.get("curso") or body.get("course") or "").strip()
        if action in {"curso", "criar curso", "criar_curso"}:
            return {"ok": True, "course": store.create_course(body)}
        if action in {"plano", "planejar", "planejamento"}:
            course = store._course_by_name(course_name)
            if not course:
                return {"ok": False, "error": "curso não encontrado"}
            return store.create_plan(int(course["id"]), body)
        data = store.assistant_summary(action, course_name)
        if action in {"painel", "abrir", "abrir estudos", "abrir_estudos"} and publish:
            await publish({"type": "ui_control", "command": "open_tab", "value": "estudos"})
        return data

    return router


__all__ = ["EducationStore", "create_education_router"]
