"""
reminders/scheduler.py
Scheduler de lembretes para a Kira usando APScheduler + asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Awaitable, Callable

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_REMOVED,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.pending_store import PendingPriority
from time_utils import (
    BRASILIA_TIMEZONE_NAME,
    format_brasilia,
    get_brasilia_tz,
    now_brasilia,
)

logger = logging.getLogger(__name__)

SpeakFn = Callable[..., Awaitable[bool | None]]

REMINDER_MAX_ATTEMPTS = 3
REMINDER_RETRY_INTERVAL = timedelta(minutes=2)
REMINDER_MISFIRE_GRACE_SECONDS = 30


class ReminderStatus(str, Enum):
    SCHEDULED = "scheduled"
    DUE = "due"
    QUEUED_FOR_DELIVERY = "queued_for_delivery"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    FAILED_NOTIFIED = "failed_notified"
    EXPIRED = "expired"
    RESCHEDULED = "rescheduled"
    CANCELED = "canceled"


@dataclass(slots=True)
class ScheduledReminder:
    job_id: str
    mensagem: str
    run_at: datetime
    current_job_id: str | None = None
    status: ReminderStatus = ReminderStatus.SCHEDULED
    delivered: bool = False
    attempts: int = 0
    expired: bool = False
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None
    last_error: str = ""
    failure_notified: bool = False
    max_attempts: int = REMINDER_MAX_ATTEMPTS

    def is_active(self) -> bool:
        return self.status in {
            ReminderStatus.SCHEDULED,
            ReminderStatus.DUE,
            ReminderStatus.QUEUED_FOR_DELIVERY,
            ReminderStatus.DELIVERING,
            ReminderStatus.RESCHEDULED,
        }


class ReminderScheduler:
    """
    Agenda e dispara lembretes de voz via APScheduler/asyncio.
    Quando o timer dispara, tenta entregar a fala e so considera
    o lembrete concluido quando a sessao confirma a entrega.
    """

    def __init__(self, speak_fn: SpeakFn, *, proactive_engine=None) -> None:
        self._speak_fn = speak_fn
        self._proactive_engine = proactive_engine
        self._scheduler = self._build_scheduler()
        self._pending: dict[str, ScheduledReminder] = {}
        self._job_to_reminder_id: dict[str, str] = {}
        self._expected_removed_job_ids: set[str] = set()
        self._listeners_registered = False

    async def start(self) -> None:
        self._register_listeners()
        self._scheduler.start()
        logger.info(
            "[LEMBRETE] ReminderScheduler iniciado com timezone=%s.",
            BRASILIA_TIMEZONE_NAME,
        )

    async def shutdown(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("[LEMBRETE] ReminderScheduler encerrado.")

    async def schedule(self, minutos: float, mensagem: str) -> str:
        """Agenda um lembrete para daqui a `minutos` minutos."""
        reminder_id = f"reminder_{uuid.uuid4().hex[:8]}"
        run_at = now_brasilia() + timedelta(minutes=minutos)
        reminder = ScheduledReminder(
            job_id=reminder_id,
            mensagem=mensagem,
            run_at=run_at,
            current_job_id=reminder_id,
            status=ReminderStatus.SCHEDULED,
        )
        self._pending[reminder_id] = reminder
        self._schedule_job(reminder=reminder, job_id=reminder_id, run_at=run_at)
        logger.info(
            "REMINDER_SCHEDULED message=%r in_minutes=%.1f job_id=%s run_at=%s",
            mensagem,
            minutos,
            reminder_id,
            format_brasilia(run_at, include_date=True, include_seconds=True),
        )
        self._register_with_proactive_engine(
            job_id=reminder_id,
            mensagem=mensagem,
            run_at=run_at,
            prioridade=self._infer_priority(minutos),
        )
        return reminder_id

    async def cancel(self, job_id: str) -> bool:
        """Cancela um lembrete pelo id logico do lembrete."""
        reminder = self._pending.pop(job_id, None)
        if reminder is None:
            return False

        scheduler_job_id = reminder.current_job_id or job_id
        self._expected_removed_job_ids.add(scheduler_job_id)
        self._forget_job_mapping(scheduler_job_id)
        self._safe_remove_scheduler_job(scheduler_job_id)

        reminder.status = ReminderStatus.CANCELED
        reminder.failure_notified = False
        logger.info("[LEMBRETE] Lembrete cancelado: job=%s", job_id)
        self._cancel_in_proactive_engine(job_id)
        return True

    def confirm_delivery(self, job_id: str, *, source: str = "chat") -> bool:
        reminder = self._pending.get(job_id)
        if reminder is None:
            return False

        reminder.delivered = True
        reminder.status = ReminderStatus.DELIVERED
        reminder.delivered_at = now_brasilia()
        self._pending.pop(job_id, None)
        if reminder.current_job_id:
            self._forget_job_mapping(reminder.current_job_id)
        self._mark_delivered_in_proactive_engine(
            job_id,
            attempt_number=reminder.attempts,
            source=source,
        )
        logger.info(
            "[LEMBRETE] Lembrete job=%s confirmado manualmente via %s.",
            job_id,
            source,
        )
        return True

    def list_pending(self) -> list[dict]:
        """Retorna lista de lembretes pendentes."""
        pending: list[dict] = []
        for reminder in self._pending.values():
            if not reminder.is_active():
                continue
            pending.append(
                {
                    "job_id": reminder.job_id,
                    "mensagem": reminder.mensagem,
                    "dispara_em": format_brasilia(
                        reminder.run_at,
                        include_date=True,
                        include_seconds=True,
                    ),
                    "status": reminder.status.value,
                }
            )
        return pending

    def get_all_active(self) -> list[dict[str, object]]:
        active: list[dict[str, object]] = []
        for reminder in self._pending.values():
            if not reminder.is_active():
                continue
            active.append(
                {
                    "job_id": reminder.job_id,
                    "current_job_id": reminder.current_job_id,
                    "mensagem": reminder.mensagem,
                    "run_at": reminder.run_at,
                    "status": reminder.status.value,
                    "attempts": reminder.attempts,
                }
            )
        return active

    async def _fire(
        self,
        reminder_id: str,
        mensagem: str | None = None,
        scheduled_job_id: str | None = None,
    ) -> None:
        reminder = self._pending.get(reminder_id)
        reconstructed = reminder is None
        if reminder is None:
            reminder = ScheduledReminder(
                job_id=reminder_id,
                mensagem=mensagem or reminder_id,
                run_at=now_brasilia(),
                current_job_id=scheduled_job_id or reminder_id,
            )
            self._pending[reminder_id] = reminder

        active_job_id = scheduled_job_id or reminder.current_job_id or reminder_id
        reminder.current_job_id = active_job_id
        # A remocao esperada ja foi registrada em _schedule_job (antes do disparo),
        # entao NAO reregistramos aqui no caminho normal -- isso deixaria entradas
        # orfas no set a cada lembrete entregue. So registramos no caminho de
        # reconstrucao, em que o reminder foi recriado sem passar por _schedule_job.
        if reconstructed:
            self._expected_removed_job_ids.add(active_job_id)

        if reminder.delivered or reminder.expired:
            logger.info(
                "[LEMBRETE] Ignorando job=%s porque o lembrete ja foi finalizado.",
                reminder_id,
            )
            return

        reminder.status = ReminderStatus.DUE
        reminder.attempts += 1
        reminder.last_attempt_at = now_brasilia()
        self._record_attempt_in_proactive_engine(reminder_id, reminder.attempts)
        logger.info(
            "REMINDER_DUE job_id=%s attempt=%s/%s message=%r",
            reminder_id,
            reminder.attempts,
            reminder.max_attempts,
            reminder.mensagem,
        )
        reminder.status = ReminderStatus.QUEUED_FOR_DELIVERY
        logger.info("REMINDER_DELIVERY_QUEUED job_id=%s", reminder_id)
        reminder.status = ReminderStatus.DELIVERING
        logger.info("REMINDER_DELIVERY_STARTED job_id=%s", reminder_id)

        delivered = False
        try:
            result = await self._speak_fn(
                f"Lembrete: {reminder.mensagem}",
                label="LEMBRETE",
                add_to_chat_ctx=True,
            )
            delivered = bool(result) if result is not None else True
        except Exception as exc:
            reminder.last_error = str(exc)
            logger.warning(
                "REMINDER_FAILED job_id=%s attempt=%d/%d reason=%s",
                reminder_id,
                reminder.attempts,
                reminder.max_attempts,
                exc,
            )

        if delivered:
            reminder.delivered = True
            reminder.status = ReminderStatus.DELIVERED
            reminder.delivered_at = now_brasilia()
            self._pending.pop(reminder_id, None)
            self._forget_job_mapping(active_job_id)
            logger.info(
                "REMINDER_DELIVERED job_id=%s attempt=%d/%d message=%r",
                reminder_id,
                reminder.attempts,
                reminder.max_attempts,
                reminder.mensagem,
            )
            self._mark_delivered_in_proactive_engine(
                reminder_id,
                attempt_number=reminder.attempts,
                source="tts",
            )
            return

        if reminder.attempts >= reminder.max_attempts:
            reminder.expired = True
            reminder.status = ReminderStatus.EXPIRED
            self._forget_job_mapping(active_job_id)
            logger.error(
                "REMINDER_FAILED job_id=%s reason=expired attempts=%d",
                reminder_id,
                reminder.attempts,
            )
            self._expire_in_proactive_engine(reminder_id, attempt_number=reminder.attempts)
            await self._notify_reminder_failure(
                reminder_id,
                reason="expired",
                last_error=reminder.last_error or "tentativas esgotadas",
            )
            return

        retry_at = now_brasilia() + REMINDER_RETRY_INTERVAL
        retry_job_id = self._build_retry_job_id(reminder_id, reminder.attempts)
        reminder.run_at = retry_at
        reminder.current_job_id = retry_job_id
        reminder.status = ReminderStatus.RESCHEDULED
        logger.warning(
            "REMINDER_RESCHEDULED job_id=%s next_run=%s",
            reminder_id,
            format_brasilia(retry_at, include_date=True, include_seconds=True),
        )
        self._schedule_job(reminder=reminder, job_id=retry_job_id, run_at=retry_at)

    def _infer_priority(self, minutos: float) -> PendingPriority:
        if minutos <= 15:
            return "alta"
        if minutos <= 120:
            return "media"
        return "baixa"

    def _build_scheduler(self) -> AsyncIOScheduler:
        return AsyncIOScheduler(
            timezone=get_brasilia_tz(),
            job_defaults={
                "misfire_grace_time": REMINDER_MISFIRE_GRACE_SECONDS,
                "coalesce": True,
                "max_instances": 1,
            },
        )

    def _register_listeners(self) -> None:
        if self._listeners_registered:
            return
        add_listener = getattr(self._scheduler, "add_listener", None)
        if not callable(add_listener):
            return

        def _spawn(coro) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(coro)

        def _on_job_executed(event) -> None:
            logger.info("[LEMBRETE] Job %s executado com sucesso.", getattr(event, "job_id", "?"))

        def _on_job_error(event) -> None:
            job_id = str(getattr(event, "job_id", "") or "").strip()
            logger.error(
                "[LEMBRETE] Job %s falhou: %s",
                job_id or "?",
                getattr(event, "exception", None),
            )
            if job_id:
                _spawn(
                    self._handle_scheduler_failure_event(
                        job_id,
                        reason="job_error",
                        last_error=str(getattr(event, "exception", "") or "job_error"),
                    )
                )

        def _on_job_missed(event) -> None:
            job_id = str(getattr(event, "job_id", "") or "").strip()
            logger.error("[LEMBRETE] Job %s foi perdido (missed).", job_id or "?")
            if job_id:
                _spawn(
                    self._handle_scheduler_failure_event(
                        job_id,
                        reason="job_missed",
                        last_error="job_missed",
                    )
                )

        def _on_job_removed(event) -> None:
            job_id = str(getattr(event, "job_id", "") or "").strip()
            if not job_id:
                return
            logger.warning("[LEMBRETE] Job %s removido do store.", job_id)
            if job_id in self._expected_removed_job_ids:
                self._expected_removed_job_ids.discard(job_id)
                self._forget_job_mapping(job_id)
                return
            reminder_id = self._resolve_reminder_id(job_id)
            reminder = self._pending.get(reminder_id)
            if reminder is None:
                self._forget_job_mapping(job_id)
                return
            if reminder.status in {
                ReminderStatus.DELIVERED,
                ReminderStatus.EXPIRED,
                ReminderStatus.CANCELED,
                ReminderStatus.FAILED,
                ReminderStatus.FAILED_NOTIFIED,
            }:
                self._forget_job_mapping(job_id)
                return
            _spawn(
                self._handle_scheduler_failure_event(
                    job_id,
                    reason="job_removed",
                    last_error="job_removed",
                )
            )

        add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
        add_listener(_on_job_error, EVENT_JOB_ERROR)
        add_listener(_on_job_missed, EVENT_JOB_MISSED)
        add_listener(_on_job_removed, EVENT_JOB_REMOVED)
        self._listeners_registered = True

    def _schedule_job(
        self,
        *,
        reminder: ScheduledReminder,
        job_id: str,
        run_at: datetime,
    ) -> None:
        reminder.current_job_id = job_id
        reminder.run_at = run_at
        self._job_to_reminder_id[job_id] = reminder.job_id
        # Jobs com trigger `date` sao auto-removidos do store logo apos o
        # APScheduler despachar a execucao (dentro do mesmo ciclo sincrono de
        # _process_jobs, ANTES da corrotina _fire ganhar CPU). O EVENT_JOB_REMOVED
        # eh dispatchado sincronamente nesse momento, entao a protecao precisa
        # existir ja no agendamento -- registra-la dentro de _fire chegava tarde
        # demais e fazia _on_job_removed tratar a remocao normal como falha,
        # disparando um "lembrete falhou" duplicado. Marcar aqui fecha a corrida.
        self._expected_removed_job_ids.add(job_id)
        self._scheduler.add_job(
            self._fire,
            trigger="date",
            run_date=run_at,
            kwargs={
                "reminder_id": reminder.job_id,
                "scheduled_job_id": job_id,
            },
            id=job_id,
            name=f"lembrete_{reminder.job_id}",
            misfire_grace_time=REMINDER_MISFIRE_GRACE_SECONDS,
            replace_existing=False,
        )

    async def _handle_scheduler_failure_event(
        self,
        job_id: str,
        *,
        reason: str,
        last_error: str,
    ) -> None:
        reminder_id = self._resolve_reminder_id(job_id)
        reminder = self._pending.get(reminder_id)
        if reminder is None:
            self._forget_job_mapping(job_id)
            logger.error(
                "[LEMBRETE] Falha em job=%s sem lembrete correspondente reason=%s",
                job_id,
                reason,
            )
            return
        if reminder.status in {
            ReminderStatus.DELIVERED,
            ReminderStatus.EXPIRED,
            ReminderStatus.CANCELED,
            ReminderStatus.FAILED_NOTIFIED,
        }:
            self._forget_job_mapping(job_id)
            return

        reminder.failed_at = now_brasilia()
        reminder.last_error = str(last_error or reason)
        reminder.status = ReminderStatus.FAILED
        reminder.failure_notified = False
        reminder.current_job_id = None
        self._forget_job_mapping(job_id)
        self._mark_failed_in_proactive_engine(reminder_id, reason=reason)
        await self._notify_reminder_failure(
            reminder_id,
            reason=reason,
            last_error=reminder.last_error,
        )

    async def _notify_reminder_failure(
        self,
        reminder_id: str,
        *,
        reason: str,
        last_error: str = "",
    ) -> None:
        reminder = self._pending.get(reminder_id)
        label = self._reminder_label(reminder)
        logger.error(
            "[LEMBRETE] Falha no job %s reason=%s erro=%s - notificando usuario",
            reminder_id,
            reason,
            last_error or "n/a",
        )

        if reminder is not None:
            reminder.failed_at = now_brasilia()
            reminder.last_error = str(last_error or reason)

        spoken = False
        try:
            result = await self._speak_fn(
                (
                    f"Chefe, tive um problema interno e nao consegui disparar seu lembrete de {label}. "
                    "Quer que eu reagende?"
                ),
                label="LEMBRETE",
                add_to_chat_ctx=True,
            )
            spoken = bool(result) if result is not None else True
        except Exception as exc:
            logger.error(
                "[LEMBRETE] Falha ao avisar o usuario sobre erro no lembrete job=%s: %s",
                reminder_id,
                exc,
            )

        if reminder is not None:
            if spoken or reminder.status == ReminderStatus.FAILED:
                reminder.status = ReminderStatus.FAILED_NOTIFIED
                reminder.failure_notified = True

    def _reminder_label(self, reminder: ScheduledReminder | None) -> str:
        if reminder is None:
            return "seu lembrete"
        return str(reminder.mensagem or "seu lembrete").strip() or "seu lembrete"

    def _build_retry_job_id(self, reminder_id: str, attempt_number: int) -> str:
        return f"{reminder_id}_retry{attempt_number}"

    def _resolve_reminder_id(self, job_id: str) -> str:
        return self._job_to_reminder_id.get(job_id, job_id)

    def _forget_job_mapping(self, job_id: str) -> None:
        self._job_to_reminder_id.pop(job_id, None)

    def _safe_remove_scheduler_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def _register_with_proactive_engine(
        self,
        *,
        job_id: str,
        mensagem: str,
        run_at: datetime,
        prioridade: PendingPriority,
    ) -> None:
        if self._proactive_engine is None:
            return
        try:
            self._proactive_engine.register_scheduler_reminder(
                job_id=job_id,
                mensagem=mensagem,
                run_at=run_at,
                prioridade=prioridade,
            )
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao registrar lembrete no engine proativo: %s",
                exc,
            )

    def _record_attempt_in_proactive_engine(self, job_id: str, attempt_number: int) -> None:
        if self._proactive_engine is None:
            return
        try:
            self._proactive_engine.record_scheduler_attempt(
                job_id,
                attempt_number=attempt_number,
            )
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao registrar tentativa no engine proativo: %s",
                exc,
            )

    def _mark_delivered_in_proactive_engine(
        self,
        job_id: str,
        *,
        attempt_number: int,
        source: str,
    ) -> None:
        if self._proactive_engine is None:
            return
        try:
            self._proactive_engine.mark_scheduler_delivered(
                job_id,
                attempt_number=attempt_number,
                source=source,
            )
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao marcar entrega no engine proativo: %s",
                exc,
            )

    def _expire_in_proactive_engine(self, job_id: str, *, attempt_number: int) -> None:
        if self._proactive_engine is None:
            return
        try:
            self._proactive_engine.expire_scheduler_reminder(
                job_id,
                attempt_number=attempt_number,
            )
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao expirar lembrete no engine proativo: %s",
                exc,
            )

    def _mark_failed_in_proactive_engine(self, job_id: str, *, reason: str) -> None:
        if self._proactive_engine is None:
            return
        mark_failed = getattr(self._proactive_engine, "fail_scheduler_reminder", None)
        if not callable(mark_failed):
            return
        try:
            mark_failed(job_id, reason=reason)
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao marcar falha no engine proativo: %s",
                exc,
            )

    def _cancel_in_proactive_engine(self, job_id: str) -> None:
        if self._proactive_engine is None:
            return
        try:
            self._proactive_engine.cancel_scheduler_reminder(job_id)
        except Exception as exc:
            logger.warning(
                "[LEMBRETE] Falha ao cancelar lembrete no engine proativo: %s",
                exc,
            )
