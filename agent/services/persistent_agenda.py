from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from bootstrap.memory_loader import preferred_user_id
from db.models import (
    CalendarEventRecord,
    CalendarNotificationRecord,
    ReminderRecord,
    utcnow,
)
from services.runtime_services import get_notifier_service, get_session_factory, init_runtime_services
from time_utils import (
    BRASILIA_TIMEZONE_NAME,
    combine_date_time_brasilia,
    ensure_brasilia,
    extract_time_reference_from_brasilia,
    get_brasilia_tz,
    now_brasilia,
    to_utc_from_brasilia,
)


logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = BRASILIA_TIMEZONE_NAME
REMINDER_POLL_SECONDS = max(float(os.getenv("KIRA_REMINDER_POLL_SECONDS", "1.0")), 0.5)
EVENT_POLL_SECONDS = max(float(os.getenv("KIRA_EVENT_POLL_SECONDS", "5.0")), 1.0)
LOCK_STALE_SECONDS = max(float(os.getenv("KIRA_JOB_LOCK_STALE_SECONDS", "120.0")), 30.0)


class AgendaError(Exception):
    pass


class AgendaManager:
    _startup_lock = threading.RLock()
    _migration_done = False
    _worker_started = False
    _worker_thread: threading.Thread | None = None
    _worker_stop = threading.Event()

    def __init__(self, base_dir: Path | None = None) -> None:
        init_runtime_services()
        self._base_dir = Path(base_dir or Path.cwd())
        self._agenda_path = self._base_dir / "agenda.json"
        self._reminders_path = self._base_dir / "lembretes.json"
        self._session_factory = get_session_factory()
        self._notifier = get_notifier_service()
        self._external_notifier = None
        self._ensure_runtime_ready()

    def set_notifier(self, notifier) -> None:
        self._external_notifier = notifier

    def create_event(self, titulo: str, data_texto: str, hora_texto: str) -> dict:
        event_date = self._parse_date(data_texto)
        event_time = self._parse_time(hora_texto)
        local_dt = self._combine_local_datetime(event_date, event_time)

        evento = CalendarEventRecord(
            user_id=self._default_user_id(),
            title=titulo.strip(),
            timezone_name=self._timezone_name(),
            event_at_utc=self._to_utc(local_dt),
            local_date=event_date.isoformat(),
            local_time=event_time.strftime("%H:%M"),
            status="pendente",
            active=True,
        )
        with self._session_factory() as session:
            session.add(evento)
            session.flush()
            self._create_notifications_for_event(session, evento)
            session.commit()
            session.refresh(evento)
            return self._serialize_event(evento)

    def create_event_from_text(self, frase: str) -> dict:
        titulo, event_date, event_time = self._parse_event_text(frase)
        return self.create_event(titulo, event_date.isoformat(), event_time.strftime("%H:%M"))

    def events_for_today(self) -> list[dict]:
        return self.events_for_date(self._local_now().date().isoformat())

    def events_for_date(self, data_texto: str) -> list[dict]:
        wanted = self._parse_date(data_texto).isoformat()
        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarEventRecord)
                .where(
                    CalendarEventRecord.active.is_(True),
                    CalendarEventRecord.local_date == wanted,
                )
                .order_by(CalendarEventRecord.local_time.asc(), CalendarEventRecord.title.asc())
            ).scalars().all()
            return [self._serialize_event(row) for row in rows]

    def create_one_time_reminder(
        self,
        texto: str,
        horario_texto: str,
        data_texto: str | None = None,
    ) -> dict:
        reminder_date = self._parse_date(data_texto) if data_texto else self._local_now().date()
        reminder_time = self._parse_time(horario_texto)
        local_dt = self._combine_local_datetime(reminder_date, reminder_time)
        return self.create_one_time_reminder_at(texto, local_dt)

    def create_one_time_reminder_at(self, texto: str, run_at: datetime) -> dict:
        """Cria um lembrete persistente para uma data/hora local exata."""
        clean_text = str(texto or "").strip()
        if not clean_text:
            raise AgendaError("O lembrete precisa ter uma mensagem.")
        local_dt = ensure_brasilia(run_at)
        if local_dt is None or local_dt <= self._local_now():
            raise AgendaError("O lembrete precisa ficar no futuro.")

        reminder_date = local_dt.date()
        reminder_time = local_dt.time()

        reminder = ReminderRecord(
            user_id=self._default_user_id(),
            reminder_type="one_time",
            text=clean_text,
            timezone_name=self._timezone_name(),
            due_at_utc=self._to_utc(local_dt),
            local_date=reminder_date.isoformat(),
            local_time=reminder_time.strftime("%H:%M"),
            final_message=f"Lembrete: {clean_text}.",
            status="pending",
            active=True,
            dedupe_key=self._build_reminder_dedupe_key(
                text=clean_text,
                local_date=reminder_date.isoformat(),
                local_time=reminder_time.strftime("%H:%M"),
                reminder_type="one_time",
            ),
        )
        return self._save_reminder(reminder)

    def create_one_time_reminder_from_text(self, frase: str) -> dict:
        texto, run_at = self._parse_one_time_reminder_text(frase)
        return self.create_one_time_reminder(
            texto=texto,
            horario_texto=run_at.strftime("%H:%M"),
            data_texto=run_at.date().isoformat(),
        )

    def create_recurring_reminder(
        self,
        texto: str,
        frequencia: str,
        horario_texto: str | None = None,
        dia_semana: str | None = None,
        dia_mes: int | None = None,
    ) -> dict:
        frequency = self._normalize_frequency(frequencia)
        run_time = self._parse_time(horario_texto or "09:00")
        normalized_weekday = self._normalize_weekday(dia_semana) if dia_semana else None
        next_run_local = self._next_recurring_run(
            {
                "frequencia": frequency,
                "hora": run_time.strftime("%H:%M"),
                "dia_semana": normalized_weekday,
                "dia_mes": dia_mes,
            },
            now=self._local_now(),
        )

        reminder = ReminderRecord(
            user_id=self._default_user_id(),
            reminder_type="recurring",
            text=texto.strip(),
            timezone_name=self._timezone_name(),
            due_at_utc=self._to_utc(next_run_local),
            local_date=next_run_local.date().isoformat(),
            local_time=run_time.strftime("%H:%M"),
            frequency=frequency,
            day_of_week=str(normalized_weekday) if normalized_weekday is not None else None,
            day_of_month=dia_mes,
            final_message=f"Lembrete: {texto.strip()}.",
            status="pending",
            active=True,
            dedupe_key=self._build_recurring_dedupe_key(
                text=texto,
                frequency=frequency,
                local_time=run_time.strftime("%H:%M"),
                day_of_week=str(normalized_weekday) if normalized_weekday is not None else "",
                day_of_month=dia_mes,
            ),
        )
        return self._save_reminder(reminder)

    def create_recurring_reminder_from_text(self, frase: str) -> dict:
        texto, frequencia, horario, dia_semana, dia_mes = self._parse_recurring_reminder_text(frase)
        return self.create_recurring_reminder(
            texto=texto,
            frequencia=frequencia,
            horario_texto=horario,
            dia_semana=dia_semana,
            dia_mes=dia_mes,
        )

    def list_active_reminders(self) -> list[dict]:
        with self._session_factory() as session:
            rows = session.execute(
                select(ReminderRecord)
                .where(ReminderRecord.active.is_(True))
                .order_by(ReminderRecord.due_at_utc.asc())
            ).scalars().all()
            return [self._serialize_reminder(row) for row in rows]

    def cancel_event(self, identificador: str) -> dict:
        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarEventRecord)
                .where(CalendarEventRecord.active.is_(True))
                .order_by(CalendarEventRecord.event_at_utc.asc())
            ).scalars().all()
            evento = self._find_matching_record(rows, identificador, "title")
            if evento is None:
                raise AgendaError("Nao encontrei evento com esse id ou titulo.")

            evento.active = False
            evento.status = "cancelled"
            notifications = session.execute(
                select(CalendarNotificationRecord).where(
                    CalendarNotificationRecord.event_id == evento.id,
                    CalendarNotificationRecord.status.in_(["pending", "matched"]),
                )
            ).scalars().all()
            for notification in notifications:
                notification.status = "cancelled"
            session.commit()
            session.refresh(evento)
            return self._serialize_event(evento)

    def check_upcoming_events(self) -> list[str]:
        now_utc = utcnow()
        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarNotificationRecord)
                .where(
                    CalendarNotificationRecord.status == "pending",
                    CalendarNotificationRecord.notify_at_utc <= now_utc + timedelta(seconds=90),
                    CalendarNotificationRecord.notify_at_utc >= now_utc - timedelta(minutes=15),
                )
                .order_by(CalendarNotificationRecord.notify_at_utc.asc())
            ).scalars().all()
            return [row.final_message for row in rows]

    def time_remaining_for_event(self, identificador: str, margem_min: int = 10) -> str:
        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarEventRecord)
                .where(CalendarEventRecord.active.is_(True))
                .order_by(CalendarEventRecord.event_at_utc.asc())
            ).scalars().all()
            evento = self._find_matching_record(rows, identificador, "title")
            if evento is None:
                raise AgendaError("Nao encontrei evento com esse id ou titulo.")

            event_dt = self._to_local(evento.event_at_utc, evento.timezone_name)
            minutos = int((event_dt - self._local_now()).total_seconds() / 60)
            if minutos <= 0:
                return f"O horario de {evento.title} ja chegou ou passou."
            if minutos <= margem_min:
                return f"Faltam {minutos} minutos para {evento.title}. Tu vai se atrasar."
            return f"Ainda faltam {minutos} minutos para {evento.title}."

    def detect_overdue_items(self) -> list[str]:
        now_utc = utcnow()
        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarEventRecord)
                .where(
                    CalendarEventRecord.active.is_(True),
                    CalendarEventRecord.status == "pendente",
                    CalendarEventRecord.event_at_utc < now_utc,
                )
                .order_by(CalendarEventRecord.event_at_utc.asc())
            ).scalars().all()
            return [f"Tu esqueceu: {row.title}." for row in rows]

    def mark_event_status(self, identificador: str, status: str) -> dict:
        normalized = self._normalize_text(status)
        allowed = {"pendente", "em_andamento", "feito", "atrasado", "cancelled"}
        if normalized not in allowed:
            raise AgendaError("Status invalido. Use pendente, em_andamento, feito ou atrasado.")

        with self._session_factory() as session:
            rows = session.execute(
                select(CalendarEventRecord)
                .where(CalendarEventRecord.active.is_(True))
                .order_by(CalendarEventRecord.event_at_utc.asc())
            ).scalars().all()
            evento = self._find_matching_record(rows, identificador, "title")
            if evento is None:
                raise AgendaError("Nao encontrei evento com esse id ou titulo.")
            evento.status = normalized
            if normalized == "cancelled":
                evento.active = False
            session.commit()
            session.refresh(evento)
            return self._serialize_event(evento)

    def cancel_reminder(self, identificador: str) -> dict:
        with self._session_factory() as session:
            rows = session.execute(
                select(ReminderRecord)
                .where(ReminderRecord.active.is_(True))
                .order_by(ReminderRecord.due_at_utc.asc())
            ).scalars().all()
            lembrete = self._find_matching_record(rows, identificador, "text")
            if lembrete is None:
                raise AgendaError("Nao encontrei lembrete com esse id ou texto.")
            lembrete.active = False
            lembrete.status = "cancelled"
            session.commit()
            session.refresh(lembrete)
            return self._serialize_reminder(lembrete)

    def _save_reminder(self, reminder: ReminderRecord) -> dict:
        with self._session_factory() as session:
            session.add(reminder)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(ReminderRecord).where(ReminderRecord.dedupe_key == reminder.dedupe_key)
                ).scalar_one_or_none()
                if existing is None:
                    raise AgendaError("Nao consegui registrar o lembrete.")
                return self._serialize_reminder(existing)
            session.refresh(reminder)
            return self._serialize_reminder(reminder)

    def _ensure_runtime_ready(self) -> None:
        with self._startup_lock:
            if not self.__class__._migration_done:
                self._migrate_legacy_json_if_needed()
                self.__class__._migration_done = True
            if not self.__class__._worker_started:
                self.__class__._worker_stop.clear()
                worker = threading.Thread(target=self._scheduler_loop, daemon=True, name="kira-agenda-worker")
                worker.start()
                self.__class__._worker_thread = worker
                self.__class__._worker_started = True

    def _scheduler_loop(self) -> None:
        while not self.__class__._worker_stop.is_set():
            try:
                self._process_due_reminders()
            except Exception:
                logger.exception("Falha ao processar lembretes persistentes.")

            try:
                self._process_due_calendar_notifications()
            except Exception:
                logger.exception("Falha ao processar notificacoes de agenda persistentes.")

            self.__class__._worker_stop.wait(timeout=min(REMINDER_POLL_SECONDS, EVENT_POLL_SECONDS))

    def _process_due_reminders(self) -> None:
        now_utc = utcnow()
        with self._session_factory() as session:
            due_ids = session.execute(
                select(ReminderRecord.id)
                .where(
                    ReminderRecord.active.is_(True),
                    ReminderRecord.status.in_(["pending", "matched"]),
                    ReminderRecord.due_at_utc <= now_utc,
                )
                .order_by(ReminderRecord.due_at_utc.asc())
                .limit(25)
            ).scalars().all()

        for reminder_id in due_ids:
            reminder = self._claim_due_reminder(reminder_id)
            if reminder is None:
                continue
            try:
                self._notifier.notify_user_sync(
                    user_id=reminder.user_id,
                    message=reminder.final_message,
                    label="LEMBRETE",
                    source_kind="reminder",
                    source_id=reminder.id,
                )
                self._finalize_reminder_fire(reminder.id)
            except Exception:
                logger.exception("Falha ao disparar lembrete %s.", reminder.id)
                self._mark_reminder_failed(reminder.id)

    def _process_due_calendar_notifications(self) -> None:
        now_utc = utcnow()
        with self._session_factory() as session:
            due_ids = session.execute(
                select(CalendarNotificationRecord.id)
                .where(
                    CalendarNotificationRecord.status.in_(["pending", "matched"]),
                    CalendarNotificationRecord.notify_at_utc <= now_utc,
                )
                .order_by(CalendarNotificationRecord.notify_at_utc.asc())
                .limit(25)
            ).scalars().all()

        for notification_id in due_ids:
            notification = self._claim_due_notification(notification_id)
            if notification is None:
                continue
            try:
                self._notifier.notify_user_sync(
                    user_id=notification.user_id,
                    message=notification.final_message,
                    label="AGENDA",
                    source_kind="calendar",
                    source_id=notification.id,
                )
                self._finalize_notification_fire(notification.id)
            except Exception:
                logger.exception("Falha ao disparar notificacao %s.", notification.id)
                self._mark_notification_failed(notification.id)

    def _claim_due_reminder(self, reminder_id: str) -> ReminderRecord | None:
        lock_token = uuid.uuid4().hex
        stale_before = utcnow() - timedelta(seconds=LOCK_STALE_SECONDS)
        with self._session_factory() as session:
            result = session.execute(
                update(ReminderRecord)
                .where(ReminderRecord.id == reminder_id)
                .where(ReminderRecord.active.is_(True))
                .where(ReminderRecord.status.in_(["pending", "matched"]))
                .where(
                    or_(
                        ReminderRecord.locked_at_utc.is_(None),
                        ReminderRecord.locked_at_utc < stale_before,
                    )
                )
                .values(
                    status="matched",
                    lock_token=lock_token,
                    locked_at_utc=utcnow(),
                    updated_at_utc=utcnow(),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return session.execute(
                select(ReminderRecord).where(
                    ReminderRecord.id == reminder_id,
                    ReminderRecord.lock_token == lock_token,
                )
            ).scalar_one_or_none()

    def _claim_due_notification(self, notification_id: str) -> CalendarNotificationRecord | None:
        lock_token = uuid.uuid4().hex
        stale_before = utcnow() - timedelta(seconds=LOCK_STALE_SECONDS)
        with self._session_factory() as session:
            result = session.execute(
                update(CalendarNotificationRecord)
                .where(CalendarNotificationRecord.id == notification_id)
                .where(CalendarNotificationRecord.status.in_(["pending", "matched"]))
                .where(
                    or_(
                        CalendarNotificationRecord.locked_at_utc.is_(None),
                        CalendarNotificationRecord.locked_at_utc < stale_before,
                    )
                )
                .values(
                    status="matched",
                    lock_token=lock_token,
                    locked_at_utc=utcnow(),
                    updated_at_utc=utcnow(),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return session.execute(
                select(CalendarNotificationRecord).where(
                    CalendarNotificationRecord.id == notification_id,
                    CalendarNotificationRecord.lock_token == lock_token,
                )
            ).scalar_one_or_none()

    def _finalize_reminder_fire(self, reminder_id: str) -> None:
        with self._session_factory() as session:
            reminder = session.get(ReminderRecord, reminder_id)
            if reminder is None:
                return
            reminder.fired_at_utc = utcnow()
            reminder.lock_token = None
            reminder.locked_at_utc = None
            if reminder.reminder_type == "recurring" and reminder.active:
                next_local = self._next_recurring_run(
                    {
                        "frequencia": reminder.frequency,
                        "hora": reminder.local_time,
                        "dia_semana": int(reminder.day_of_week) if reminder.day_of_week is not None else None,
                        "dia_mes": reminder.day_of_month,
                    },
                    now=self._local_now(),
                )
                reminder.due_at_utc = self._to_utc(next_local)
                reminder.local_date = next_local.date().isoformat()
                reminder.local_time = next_local.strftime("%H:%M")
                reminder.status = "pending"
                reminder.updated_at_utc = utcnow()
            else:
                reminder.active = False
                reminder.status = "fired"
            session.commit()

    def _finalize_notification_fire(self, notification_id: str) -> None:
        with self._session_factory() as session:
            notification = session.get(CalendarNotificationRecord, notification_id)
            if notification is None:
                return
            notification.status = "fired"
            notification.fired_at_utc = utcnow()
            notification.lock_token = None
            notification.locked_at_utc = None
            session.commit()

    def _mark_reminder_failed(self, reminder_id: str) -> None:
        with self._session_factory() as session:
            reminder = session.get(ReminderRecord, reminder_id)
            if reminder is None:
                return
            reminder.status = "failed"
            reminder.last_error = "Falha ao entregar lembrete proativo."
            reminder.lock_token = None
            reminder.locked_at_utc = None
            session.commit()

    def _mark_notification_failed(self, notification_id: str) -> None:
        with self._session_factory() as session:
            notification = session.get(CalendarNotificationRecord, notification_id)
            if notification is None:
                return
            notification.status = "failed"
            notification.last_error = "Falha ao entregar notificacao de agenda."
            notification.lock_token = None
            notification.locked_at_utc = None
            session.commit()

    def _create_notifications_for_event(self, session, evento: CalendarEventRecord) -> None:
        checkpoints = [
            ("30_min", timedelta(minutes=30), f"Faltam 30 minutos para {evento.title}."),
            ("10_min", timedelta(minutes=10), f"Faltam 10 minutos para {evento.title}."),
            ("at_time", timedelta(seconds=0), f"Agora sao {evento.local_time}. Voce tem {evento.title}."),
        ]
        now_utc = utcnow()
        for notify_kind, delta, message in checkpoints:
            notify_at = evento.event_at_utc - delta
            if notify_at < now_utc - timedelta(minutes=1):
                continue
            session.add(
                CalendarNotificationRecord(
                    event_id=evento.id,
                    user_id=evento.user_id,
                    notify_kind=notify_kind,
                    timezone_name=evento.timezone_name,
                    notify_at_utc=notify_at,
                    event_at_utc=evento.event_at_utc,
                    final_message=message,
                    status="pending",
                    dedupe_key=self._build_calendar_dedupe_key(evento.id, notify_kind),
                )
            )

    def _serialize_event(self, row: CalendarEventRecord) -> dict:
        return {
            "id": row.id,
            "titulo": row.title,
            "data": row.local_date,
            "hora": row.local_time,
            "status": row.status,
            "criado_em": row.created_at_utc.isoformat() if row.created_at_utc else "",
        }

    def _serialize_reminder(self, row: ReminderRecord) -> dict:
        payload = {
            "id": row.id,
            "tipo": "recorrente" if row.reminder_type == "recurring" else "unico",
            "texto": row.text,
            "data": row.local_date,
            "hora": row.local_time,
            "proxima_execucao": self._to_local(row.due_at_utc, row.timezone_name).isoformat(),
            "ativo": bool(row.active),
            "status": row.status,
            "criado_em": row.created_at_utc.isoformat() if row.created_at_utc else "",
        }
        if row.reminder_type == "recurring":
            payload["frequencia"] = row.frequency
            payload["dia_semana"] = int(row.day_of_week) if row.day_of_week is not None else None
            payload["dia_mes"] = row.day_of_month
        return payload

    def _build_reminder_dedupe_key(self, *, text: str, local_date: str, local_time: str, reminder_type: str) -> str:
        base = f"{self._default_user_id()}|{reminder_type}|{self._normalize_text(text)}|{local_date}|{local_time}"
        return base[:191]

    def _build_recurring_dedupe_key(
        self,
        *,
        text: str,
        frequency: str,
        local_time: str,
        day_of_week: str,
        day_of_month: int | None,
    ) -> str:
        base = (
            f"{self._default_user_id()}|recurring|{self._normalize_text(text)}|{frequency}|"
            f"{local_time}|{day_of_week}|{day_of_month or ''}"
        )
        return base[:191]

    def _build_calendar_dedupe_key(self, event_id: str, notify_kind: str) -> str:
        return f"{event_id}|{notify_kind}"[:191]

    def _timezone_name(self) -> str:
        return DEFAULT_TIMEZONE

    def _timezone(self):
        return get_brasilia_tz()

    def _resolve_timezone(self, timezone_name: str | None):
        del timezone_name
        return get_brasilia_tz()

    def _default_user_id(self) -> str:
        try:
            return preferred_user_id()
        except Exception:
            return "Chefe"

    def _local_now(self) -> datetime:
        return now_brasilia()

    def _combine_local_datetime(self, target_date: date, target_time: dt_time) -> datetime:
        return combine_date_time_brasilia(target_date, target_time)

    def _to_utc(self, local_dt: datetime) -> datetime:
        return to_utc_from_brasilia(local_dt)

    def _to_local(self, utc_dt: datetime, timezone_name: str | None = None) -> datetime:
        del timezone_name
        aware = utc_dt if utc_dt.tzinfo is not None else utc_dt.replace(tzinfo=timezone.utc)
        return ensure_brasilia(aware)

    def _migrate_legacy_json_if_needed(self) -> None:
        with self._session_factory() as session:
            has_data = session.execute(select(ReminderRecord.id).limit(1)).scalar_one_or_none()
            if has_data is not None:
                return

        if self._reminders_path.exists():
            for reminder in self._read_json(self._reminders_path):
                try:
                    self._import_legacy_reminder(reminder)
                except Exception:
                    logger.exception("Falha ao migrar lembrete legado para SQLite.")
        if self._agenda_path.exists():
            for event in self._read_json(self._agenda_path):
                try:
                    self._import_legacy_event(event)
                except Exception:
                    logger.exception("Falha ao migrar evento legado para SQLite.")

    def _import_legacy_reminder(self, payload: dict) -> None:
        if not isinstance(payload, dict) or not payload.get("ativo", True):
            return
        due_text = str(payload.get("proxima_execucao") or "").strip()
        if not due_text:
            return
        try:
            due_local = datetime.fromisoformat(due_text)
        except ValueError:
            return
        if due_local.tzinfo is None:
            due_local = combine_date_time_brasilia(due_local.date(), due_local.time())

        reminder = ReminderRecord(
            id=str(payload.get("id") or uuid.uuid4().hex[:10]),
            user_id=self._default_user_id(),
            reminder_type="recurring" if payload.get("tipo") == "recorrente" else "one_time",
            text=str(payload.get("texto") or "").strip(),
            timezone_name=self._timezone_name(),
            due_at_utc=self._to_utc(due_local),
            local_date=str(payload.get("data") or due_local.date().isoformat()),
            local_time=str(payload.get("hora") or due_local.strftime("%H:%M")),
            frequency=str(payload.get("frequencia") or "") or None,
            day_of_week=str(payload.get("dia_semana")) if payload.get("dia_semana") is not None else None,
            day_of_month=payload.get("dia_mes"),
            final_message=f"Lembrete: {str(payload.get('texto') or '').strip()}.",
            status="pending",
            active=True,
            dedupe_key=self._build_reminder_dedupe_key(
                text=str(payload.get("texto") or ""),
                local_date=str(payload.get("data") or due_local.date().isoformat()),
                local_time=str(payload.get("hora") or due_local.strftime("%H:%M")),
                reminder_type="recurring" if payload.get("tipo") == "recorrente" else "one_time",
            ),
        )
        with self._session_factory() as session:
            session.merge(reminder)
            session.commit()

    def _import_legacy_event(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        data_text = str(payload.get("data") or "").strip()
        hora_text = str(payload.get("hora") or "").strip()
        if not data_text or not hora_text:
            return
        event_local = self._combine_local_datetime(self._parse_date(data_text), self._parse_time(hora_text))
        with self._session_factory() as session:
            existing = session.get(CalendarEventRecord, str(payload.get("id") or ""))
            if existing is not None:
                return
            evento = CalendarEventRecord(
                id=str(payload.get("id") or uuid.uuid4().hex[:10]),
                user_id=self._default_user_id(),
                title=str(payload.get("titulo") or "").strip(),
                timezone_name=self._timezone_name(),
                event_at_utc=self._to_utc(event_local),
                local_date=data_text,
                local_time=hora_text,
                status=str(payload.get("status") or "pendente"),
                active=str(payload.get("status") or "pendente") != "cancelled",
            )
            session.add(evento)
            session.flush()
            self._create_notifications_for_event(session, evento)
            session.commit()

    def _read_json(self, path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Falha ao ler %s durante migracao.", path)
            return []
        return data if isinstance(data, list) else []

    def _find_matching_record(self, rows: list, identificador: str, text_field: str):
        key = self._normalize_text(identificador)
        if not key:
            return None

        for row in rows:
            if str(getattr(row, "id", "")).lower() == key:
                return row

        exact_matches = []
        partial_matches = []
        for row in rows:
            text = self._normalize_text(str(getattr(row, text_field, "")))
            if text == key:
                exact_matches.append(row)
            elif key in text or text in key:
                partial_matches.append(row)

        matches = exact_matches or partial_matches
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AgendaError("Encontrei mais de um item com esse nome. Use o id para cancelar o certo.")
        return None

    def _parse_event_text(self, frase: str) -> tuple[str, date, dt_time]:
        original = frase.strip()
        normalized = self._normalize_text(original)
        date_match = self._extract_date_match(normalized)
        time_match = self._extract_time_match(normalized)
        if date_match is None:
            raise AgendaError("Nao consegui entender a data do evento.")
        if time_match is None:
            raise AgendaError("Nao consegui entender o horario do evento.")

        titulo = original
        for token in [date_match.group(0), time_match.group(0)]:
            titulo = re.sub(re.escape(token), " ", titulo, flags=re.IGNORECASE)
        titulo = re.sub(r"\b(marca|marcar|agenda|agendar|cria|criar)\b", " ", titulo, flags=re.IGNORECASE)
        titulo = re.sub(r"\s+", " ", titulo).strip(" -,:")
        if not titulo:
            raise AgendaError("Nao consegui entender o titulo do evento.")

        return titulo, self._parse_date(date_match.group(0)), self._parse_time(time_match.group(0))

    def _parse_one_time_reminder_text(self, frase: str) -> tuple[str, datetime]:
        original = frase.strip()
        normalized = self._normalize_text(original)
        now = self._local_now()
        cleaned_by_time_utils, parsed_run_at = extract_time_reference_from_brasilia(
            original,
            now=now,
        )
        if parsed_run_at is not None:
            texto = self._cleanup_reminder_text(cleaned_by_time_utils)
            return texto, parsed_run_at

        date_match = self._extract_date_match(normalized)
        time_match = self._extract_time_match(normalized)
        if time_match is None:
            raise AgendaError("Nao consegui entender o horario do lembrete.")

        reminder_date = self._parse_date(date_match.group(0)) if date_match else now.date()
        run_at = self._combine_local_datetime(reminder_date, self._parse_time(time_match.group(0)))
        if run_at <= now:
            if date_match is None:
                run_at += timedelta(days=1)
            else:
                raise AgendaError("O lembrete precisa ficar no futuro.")

        texto = original
        if date_match:
            texto = re.sub(re.escape(date_match.group(0)), " ", texto, flags=re.IGNORECASE)
        texto = re.sub(re.escape(time_match.group(0)), " ", texto, flags=re.IGNORECASE)
        texto = self._cleanup_reminder_text(texto)
        return texto, run_at

    def _parse_recurring_reminder_text(self, frase: str) -> tuple[str, str, str | None, str | None, int | None]:
        original = frase.strip()
        normalized = self._normalize_text(original)

        if any(token in normalized for token in ["todo dia", "todos os dias", "diario", "diaria"]):
            frequencia = "daily"
        elif any(token in normalized for token in ["toda semana", "toda as semanas", "semanal"]):
            frequencia = "weekly"
        elif any(token in normalized for token in ["todo mes", "todos os meses", "mensal"]):
            frequencia = "monthly"
        else:
            raise AgendaError("Nao consegui entender a frequencia do lembrete recorrente.")

        time_match = self._extract_time_match(normalized)
        weekday_match = self._extract_weekday_match(normalized)
        month_day_match = re.search(r"\bdia\s+(\d{1,2})\b", normalized)

        texto = original
        for token in [
            time_match.group(0) if time_match else None,
            weekday_match.group(0) if weekday_match else None,
            month_day_match.group(0) if month_day_match else None,
        ]:
            if token:
                texto = re.sub(re.escape(token), " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\b(me lembra|lembrar|lembra)\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(
            r"\b(todo dia|todos os dias|diario|diaria|toda semana|semanal|todo mes|todos os meses|mensal)\b",
            " ",
            texto,
            flags=re.IGNORECASE,
        )
        texto = re.sub(r"\s+", " ", texto).strip(" -,:")
        if not texto:
            raise AgendaError("Nao consegui entender o texto do lembrete recorrente.")

        return (
            texto,
            frequencia,
            time_match.group(0) if time_match else None,
            weekday_match.group(0) if weekday_match else None,
            int(month_day_match.group(1)) if month_day_match else None,
        )

    def _cleanup_reminder_text(self, texto: str) -> str:
        texto = re.sub(r"\b(me lembra|lembrar|lembra)\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\bde\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s+", " ", texto).strip(" -,:")
        if not texto:
            raise AgendaError("Nao consegui entender o texto do lembrete.")
        return texto

    def _parse_date(self, value: str | None) -> date:
        if not value:
            return self._local_now().date()

        cleaned = self._normalize_text(value)
        today = self._local_now().date()
        if cleaned in {"hoje", "today"}:
            return today
        if cleaned in {"amanha", "tomorrow"}:
            return today + timedelta(days=1)

        weekday = self._weekday_from_text(cleaned)
        if weekday is not None:
            return self._next_weekday(today, weekday)

        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                continue

        raise AgendaError("Nao consegui entender a data. Use hoje, amanha, um dia da semana, YYYY-MM-DD ou DD/MM/YYYY.")

    def _parse_time(self, value: str) -> dt_time:
        normalized = self._normalize_text(value).replace(" ", "")
        normalized = re.sub(r"^(as|a)", "", normalized)

        patterns = [
            (r"^(\d{1,2}):(\d{2})$", "%H:%M"),
            (r"^(\d{1,2})h(\d{2})$", None),
            (r"^(\d{1,2})h$", None),
            (r"^(\d{1,2})$", None),
        ]
        for regex, fmt in patterns:
            match = re.match(regex, normalized)
            if not match:
                continue
            if fmt:
                return datetime.strptime(normalized, fmt).time()
            hour = int(match.group(1))
            minute = int(match.group(2)) if len(match.groups()) > 1 and match.group(2) else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return dt_time(hour=hour, minute=minute)

        raise AgendaError("Nao consegui entender o horario. Use formatos como 18h, 18:00 ou 14h30.")

    def _normalize_frequency(self, value: str) -> str:
        cleaned = self._normalize_text(value)
        mapping = {
            "todo dia": "daily",
            "todos os dias": "daily",
            "diario": "daily",
            "diaria": "daily",
            "toda semana": "weekly",
            "semanal": "weekly",
            "todo mes": "monthly",
            "todos os meses": "monthly",
            "mensal": "monthly",
        }
        if cleaned in mapping:
            return mapping[cleaned]
        if cleaned in {"daily", "weekly", "monthly"}:
            return cleaned
        raise AgendaError("Frequencia invalida. Use diaria, semanal ou mensal.")

    def _normalize_weekday(self, value: str) -> int:
        cleaned = self._normalize_text(value)
        weekday = self._weekday_from_text(cleaned)
        if weekday is None:
            raise AgendaError("Dia da semana invalido para lembrete recorrente.")
        return weekday

    def _next_recurring_run(self, lembrete: dict, now: datetime) -> datetime:
        reminder_time = self._parse_time(str(lembrete.get("hora", "09:00")))
        base = (ensure_brasilia(now) or now_brasilia()).replace(second=0, microsecond=0)
        frequency = lembrete.get("frequencia")

        if frequency == "daily":
            candidate = combine_date_time_brasilia(base.date(), reminder_time)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        if frequency == "weekly":
            target_weekday = lembrete.get("dia_semana")
            if target_weekday is None:
                target_weekday = now.weekday()
            days_ahead = (int(target_weekday) - base.weekday()) % 7
            candidate = combine_date_time_brasilia(
                base.date() + timedelta(days=days_ahead),
                reminder_time,
            )
            if candidate <= now:
                candidate += timedelta(days=7)
            return candidate

        if frequency == "monthly":
            day = int(lembrete.get("dia_mes") or now.day)
            candidate = self._build_monthly_datetime(now.year, now.month, day, reminder_time)
            if candidate <= now:
                month = now.month + 1
                year = now.year
                if month > 12:
                    month = 1
                    year += 1
                candidate = self._build_monthly_datetime(year, month, day, reminder_time)
            return candidate

        raise AgendaError("Frequencia recorrente nao suportada.")

    def _build_monthly_datetime(self, year: int, month: int, day: int, reminder_time: dt_time) -> datetime:
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        last_day = (next_month - date(year, month, 1)).days
        safe_day = min(max(day, 1), last_day)
        return combine_date_time_brasilia(
            date(year, month, safe_day),
            reminder_time,
        )

    def _normalize_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.strip().lower())
        return "".join(char for char in normalized if not unicodedata.combining(char))

    def _weekday_from_text(self, value: str) -> int | None:
        weekdays = {
            "segunda": 0,
            "segunda-feira": 0,
            "terca": 1,
            "terca-feira": 1,
            "quarta": 2,
            "quarta-feira": 2,
            "quinta": 3,
            "quinta-feira": 3,
            "sexta": 4,
            "sexta-feira": 4,
            "sabado": 5,
            "domingo": 6,
        }
        return weekdays.get(value)

    def _next_weekday(self, base: date, weekday: int) -> date:
        days_ahead = (weekday - base.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return base + timedelta(days=days_ahead)

    def _extract_date_match(self, normalized_text: str):
        patterns = [
            r"\bhoje\b",
            r"\bamanha\b",
            r"\bsegunda(?:-feira)?\b",
            r"\bterca(?:-feira)?\b",
            r"\bquarta(?:-feira)?\b",
            r"\bquinta(?:-feira)?\b",
            r"\bsexta(?:-feira)?\b",
            r"\bsabado\b",
            r"\bdomingo\b",
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{1,2}/\d{1,2}/\d{4}\b",
            r"\b\d{1,2}-\d{1,2}-\d{4}\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized_text)
            if match:
                return match
        return None

    def _extract_weekday_match(self, normalized_text: str):
        return re.search(
            r"\b(segunda(?:-feira)?|terca(?:-feira)?|quarta(?:-feira)?|quinta(?:-feira)?|sexta(?:-feira)?|sabado|domingo)\b",
            normalized_text,
        )

    def _extract_time_match(self, normalized_text: str):
        for pattern in [
            r"\bas\s+\d{1,2}:\d{2}\b",
            r"\b\d{1,2}:\d{2}\b",
            r"\b\d{1,2}h\d{2}\b",
            r"\b\d{1,2}h\b",
            r"\ba\s+\d{1,2}\b",
            r"\bas\s+\d{1,2}\b",
        ]:
            match = re.search(pattern, normalized_text)
            if match:
                value = re.sub(r"^(as|a)\s+", "", match.group(0))
                return _ValueMatch(value)
        return None


class _ValueMatch:
    def __init__(self, value: str) -> None:
        self._value = value

    def group(self, _index: int = 0) -> str:
        return self._value
