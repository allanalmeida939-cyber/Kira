"""Serviço persistente de lembretes da UI e do agente.

O serviço é síncrono nas operações de estado para poder ser usado tanto pelas
rotas FastAPI quanto por testes com relógio injetado. O loop assíncrono apenas
faz a varredura periódica; ele nunca depende da janela desktop estar aberta.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("kira.reminders")

MAX_ITEMS = 200
MAX_TEXT = 200
TICK_S = 1.0
MISSED_GRACE_S = 120
ALARM_REPEAT_VOICE_S = 30
ALARM_MAX_VOICE_REPEATS = 3


def now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip())
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _add_month(value: datetime) -> datetime:
    """Mesmo dia no mês seguinte (dia 31 vira o último dia de um mês mais curto)."""
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return value.replace(year=year, month=month, day=min(value.day, last_day))


def next_due(due: datetime, repeat: str, ref: datetime, *, interval_minutes: int = 0, until: str | None = None) -> datetime | None:
    """Próxima ocorrência; "interval" repete a cada interval_minutes (ex.: remédio de 8 em 8 horas) até `until`."""
    if repeat == "monthly":
        while due <= ref:
            due = _add_month(due)
        return None if until and due > _parse(until) else due
    if repeat == "interval" and interval_minutes > 0:
        step = timedelta(minutes=interval_minutes)
    else:
        step = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}.get(repeat)
    if not step:
        return None
    while due <= ref:
        due += step
    if until and due > _parse(until):
        return None
    return due


@dataclass
class Reminder:
    id: str
    text: str
    due_at: str
    kind: str = "reminder"
    repeat: str = "none"
    status: str = "scheduled"
    created_at: str = ""
    fired_at: str | None = None
    snooze_until: str | None = None
    voice_pending: bool = False
    voice_count: int = 0
    next_voice_at: str | None = None
    interval_minutes: int = 0
    until: str | None = None
    notify_phone: bool = False  # também notificação no celular (pedido "me avisa no celular" ou feito pelo celular)

    def as_dict(self) -> dict:
        return asdict(self)


class ReminderService:
    def __init__(self, path: Path, clock: Callable[[], datetime] = now) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, Reminder] = {}
        self._events: list[dict] = []
        self._seq = 0
        self._stop = asyncio.Event()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return
            items = {}
            for raw in data.get("items", []):
                if not isinstance(raw, dict):
                    continue
                allowed = {field for field in Reminder.__dataclass_fields__}
                item = Reminder(**{key: value for key, value in raw.items() if key in allowed})
                items[item.id] = item
            self._items = items
        except FileNotFoundError:
            return
        except Exception:
            log.exception("REMINDERS_LOAD_FAILED")
            try:
                if self.path.exists():
                    self.path.replace(self.path.with_suffix(".bad"))
            except Exception:
                log.exception("REMINDERS_BAD_FILE_PRESERVE_FAILED")

    def _save(self) -> None:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "items": [item.as_dict() for item in self._items.values()]}
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            log.exception("REMINDERS_SAVE_FAILED")
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def create(
        self, text: str, due_at: str, kind: str = "reminder", repeat: str = "none", *,
        interval_minutes: int = 0, until: str | None = None, notify_phone: bool = False,
    ) -> Reminder:
        clean = " ".join(str(text or "").split())
        if not clean:
            raise ValueError("O lembrete precisa ter uma mensagem.")
        if len(clean) > MAX_TEXT:
            raise ValueError(f"A mensagem pode ter no máximo {MAX_TEXT} caracteres.")
        if kind not in {"reminder", "alarm"}:
            raise ValueError("Tipo inválido. Use reminder ou alarm.")
        if repeat not in {"none", "daily", "weekly", "monthly", "interval"}:
            raise ValueError("Repetição inválida. Use none, daily, weekly, monthly ou interval.")
        if repeat == "interval" and not 15 <= int(interval_minutes or 0) <= 7 * 24 * 60:
            raise ValueError("O intervalo precisa ficar entre 15 minutos e 7 dias.")
        due = _parse(due_at)
        current = self.clock().astimezone()
        if due < current - timedelta(seconds=60):
            raise ValueError("O horário precisa ficar no futuro.")
        with self._lock:
            if len(self._items) >= MAX_ITEMS:
                raise ValueError("Limite de lembretes atingido.")
            item = Reminder(
                id=uuid.uuid4().hex,
                text=clean,
                due_at=_iso(due),
                kind=kind,
                repeat=repeat,
                created_at=_iso(current),
                interval_minutes=int(interval_minutes or 0) if repeat == "interval" else 0,
                until=_iso(_parse(until)) if until else None,
                notify_phone=bool(notify_phone),
            )
            self._items[item.id] = item
            self._save()
            return item

    def list(self) -> list[Reminder]:
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.snooze_until or item.due_at)

    def delete(self, reminder_id: str) -> bool:
        with self._lock:
            removed = self._items.pop(str(reminder_id), None) is not None
            if removed:
                self._save()
            return removed

    def dismiss(self, reminder_id: str) -> Reminder:
        with self._lock:
            item = self._items.get(str(reminder_id))
            if item is None:
                raise KeyError("Lembrete não encontrado.")
            due = _parse(item.due_at)
            next_run = next_due(due, item.repeat, self.clock().astimezone(), interval_minutes=item.interval_minutes, until=item.until)
            if next_run:
                item.due_at = _iso(next_run)
                item.status = "scheduled"
                item.snooze_until = None
                item.fired_at = None
            else:
                item.status = "dismissed"
            item.voice_pending = False
            item.next_voice_at = None
            self._save()
            return item

    def snooze(self, reminder_id: str, minutes: int) -> Reminder:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
            raise ValueError("A soneca deve estar entre 1 e 60 minutos.")
        with self._lock:
            item = self._items.get(str(reminder_id))
            if item is None:
                raise KeyError("Lembrete não encontrado.")
            item.status = "scheduled"
            item.snooze_until = _iso(self.clock().astimezone() + timedelta(minutes=minutes))
            item.voice_pending = False
            item.next_voice_at = None
            self._save()
            return item

    def _event(self, event_type: str, item: Reminder, *, late: float = 0) -> None:
        self._seq += 1
        self._events.append({
            "seq": self._seq,
            "type": event_type,
            "reminder": item.as_dict(),
            "late_seconds": max(0, int(late)),
            "at": _iso(self.clock().astimezone()),
        })
        del self._events[:-500]

    def tick(self, at: datetime | None = None) -> list[dict]:
        current = (at or self.clock()).astimezone()
        fired: list[dict] = []
        with self._lock:
            for item in list(self._items.values()):
                if item.status != "scheduled":
                    continue
                effective = _parse(item.snooze_until or item.due_at)
                if effective > current:
                    continue
                late = (current - effective).total_seconds()
                missed = late > MISSED_GRACE_S
                item.fired_at = _iso(current)
                item.snooze_until = None
                item.voice_pending = True
                item.voice_count = 0
                item.next_voice_at = None
                next_run = next_due(effective, item.repeat, current, interval_minutes=item.interval_minutes, until=item.until)
                if missed:
                    item.status = "missed"
                    event_type = "reminder_missed"
                elif item.kind == "alarm":
                    item.status = "firing"
                    event_type = "reminder_fired"
                else:
                    event_type = "reminder_fired"
                    if next_run:
                        item.due_at = _iso(next_run)
                        item.status = "scheduled"
                    else:
                        item.status = "done"
                self._event(event_type, item, late=late)
                if missed and next_run:
                    item.due_at = _iso(next_run)
                    item.status = "scheduled"
                fired.append(self._events[-1])
            if fired:
                self._save()
        return fired

    def events_since(self, sequence: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            wanted = max(0, int(sequence or 0))
            return [event for event in self._events if event["seq"] > wanted], self._seq

    def voice_pending_items(self) -> list[Reminder]:
        current = self.clock().astimezone()
        with self._lock:
            result = []
            for item in self._items.values():
                repeating = item.kind == "alarm" and item.status == "firing" and bool(item.next_voice_at)
                if (item.voice_pending or repeating) and (not item.next_voice_at or _parse(item.next_voice_at) <= current):
                    if item.kind != "alarm" or item.voice_count < ALARM_MAX_VOICE_REPEATS:
                        result.append(item)
            return sorted(result, key=lambda item: item.fired_at or item.due_at)

    def voice_ack(self, reminder_id: str) -> Reminder:
        with self._lock:
            item = self._items.get(str(reminder_id))
            if item is None:
                raise KeyError("Lembrete não encontrado.")
            item.voice_count += 1
            if item.kind == "alarm" and item.status == "firing" and item.voice_count < ALARM_MAX_VOICE_REPEATS:
                item.voice_pending = False
                item.next_voice_at = _iso(self.clock().astimezone() + timedelta(seconds=ALARM_REPEAT_VOICE_S))
            else:
                item.voice_pending = False
                item.next_voice_at = None
            self._save()
            return item

    async def run(self, on_fire: Callable[[Reminder], Awaitable[None]] | None = None) -> None:
        log.info("REMINDERS_SCHEDULER_STARTED items=%d", len(self._items))
        while not self._stop.is_set():
            try:
                events = self.tick()
                if on_fire:
                    for event in events:
                        item = Reminder(**event["reminder"])
                        await on_fire(item)
            except Exception:
                log.exception("REMINDERS_TICK_FAILED")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


__all__ = ["Reminder", "ReminderService", "next_due", "MISSED_GRACE_S"]
