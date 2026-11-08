from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal

from time_utils import now_brasilia

from .pending_store import PendingItem, PendingPriority, PendingStore, coerce_pending_now


logger = logging.getLogger(__name__)

SpeakFn = Callable[..., Awaitable[None]]
ReminderTone = Literal["leve", "medio", "insistente"]

_IDLE_STATES = {"idle", "away"}
_FOOTER_COOLDOWN = timedelta(minutes=4)
_IDLE_RETURN_WINDOW = timedelta(minutes=2)
_ACTIONABLE_FUTURE_WINDOW = timedelta(minutes=20)
_PRIORITY_BASE_WINDOWS: dict[PendingPriority, timedelta] = {
    "alta": timedelta(minutes=15),
    "media": timedelta(minutes=45),
    "baixa": timedelta(hours=2),
}
_LEVEL_REPEAT_WINDOWS: dict[ReminderTone, timedelta] = {
    "leve": timedelta(minutes=25),
    "medio": timedelta(minutes=45),
    "insistente": timedelta(minutes=70),
}
_FOOTER_MIN_WINDOWS: dict[PendingPriority, timedelta] = {
    "alta": timedelta(minutes=20),
    "media": timedelta(hours=1),
    "baixa": timedelta(hours=3),
}


class ProactiveEngine:
    def __init__(
        self,
        *,
        store: PendingStore,
        speak_fn: SpeakFn,
        loop_interval_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._speak_fn = speak_fn
        self._loop_interval_seconds = max(loop_interval_seconds, 5.0)
        self._runner_task: asyncio.Task | None = None
        self._last_footer_at: datetime | None = None
        self._user_idle_since: datetime | None = None
        self._user_is_idle = False
        self._last_user_activity_at: datetime | None = None

    async def start(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            return
        self._runner_task = asyncio.create_task(
            self._run_loop(),
            name="kira-proactive-engine",
        )
        logger.info("[PROATIVO] Engine iniciado.")

    async def shutdown(self) -> None:
        task = self._runner_task
        self._runner_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("[PROATIVO] Engine encerrado.")

    def register_scheduler_reminder(
        self,
        *,
        job_id: str,
        mensagem: str,
        run_at: datetime,
        prioridade: PendingPriority,
    ) -> PendingItem:
        existing = self._store.find_by_source(job_id)
        if existing is not None:
            existing.descricao = str(mensagem or "").strip()
            existing.prioridade = prioridade
            existing.horario_lembrete = coerce_pending_now(run_at)
            existing.status = "pendente"
            existing.delivered = False
            existing.expired = False
            existing.attempts = 0
            existing.ultimo_lembrete = None
            self._store.record_history(
                existing,
                "scheduler_atualizado",
                detail="ReminderScheduler atualizou um lembrete ja registrado.",
            )
            logger.info(
                "[PROATIVO] Lembrete do scheduler atualizado item=%s job=%s prioridade=%s",
                existing.id,
                job_id,
                prioridade,
            )
            return existing

        item = self._store.create(
            item_id=f"pend_{job_id}",
            source_id=job_id,
            descricao=mensagem,
            tipo="lembrete",
            prioridade=prioridade,
            horario_lembrete=coerce_pending_now(run_at),
            app_origem="reminder_scheduler",
            categoria="pessoal",
        )
        self._store.record_history(
            item,
            "scheduler_registrado",
            detail="ReminderScheduler registrou o lembrete para proatividade.",
        )
        logger.info(
            "[PROATIVO] Lembrete do scheduler registrado item=%s job=%s prioridade=%s",
            item.id,
            job_id,
            prioridade,
        )
        return item

    def record_scheduler_attempt(
        self,
        job_id: str,
        *,
        attempt_number: int,
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self._store.find_by_source(job_id)
        if item is None:
            return None
        current_now = coerce_pending_now(now)
        item.attempts = max(item.attempts, attempt_number)
        item.ultimo_lembrete = current_now
        self._store.record_history(
            item,
            "scheduler_tentativa",
            detail=f"ReminderScheduler iniciou a tentativa {attempt_number}.",
            now=current_now,
        )
        logger.info(
            "[LEMBRETE] Scheduler registrou tentativa=%s item=%s job=%s",
            attempt_number,
            item.id,
            job_id,
        )
        return item

    def mark_scheduler_delivered(
        self,
        job_id: str,
        *,
        attempt_number: int | None = None,
        source: str = "tts",
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self._store.find_by_source(job_id)
        if item is None:
            return None
        current_now = coerce_pending_now(now)
        if attempt_number is not None:
            item.attempts = max(item.attempts, attempt_number)
        item.ultimo_lembrete = current_now
        item.delivered = True
        item.expired = False
        item.status = "feito"
        item.tempo_parado = timedelta(0)
        self._store.record_history(
            item,
            "scheduler_disparado",
            detail="ReminderScheduler disparou o lembrete no horario agendado.",
            now=current_now,
        )
        self._store.record_history(
            item,
            "lembrete_emitido",
            detail=f"Lembrete entregue pelo ReminderScheduler via {source}.",
            now=current_now,
        )
        logger.info(
            "[LEMBRETE] Scheduler entregou lembrete item=%s job=%s attempts=%s source=%s",
            item.id,
            job_id,
            item.attempts,
            source,
        )
        return item

    def expire_scheduler_reminder(
        self,
        job_id: str,
        *,
        attempt_number: int,
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self._store.find_by_source(job_id)
        if item is None:
            return None
        current_now = coerce_pending_now(now)
        item.attempts = max(item.attempts, attempt_number)
        item.expired = True
        item.delivered = False
        item.status = "cancelado"
        item.tempo_parado = timedelta(0)
        item.ultimo_lembrete = current_now
        self._store.record_history(
            item,
            "scheduler_expirado",
            detail=(
                "ReminderScheduler expirou o lembrete apos atingir o maximo de tentativas "
                f"({attempt_number})."
            ),
            now=current_now,
        )
        logger.error(
            "[LEMBRETE] Scheduler expirou lembrete item=%s job=%s attempts=%s",
            item.id,
            job_id,
            item.attempts,
        )
        return item

    def cancel_scheduler_reminder(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self._store.find_by_source(job_id)
        if item is None:
            return None
        item.status = "cancelado"
        item.tempo_parado = timedelta(0)
        self._store.record_history(
            item,
            "cancelada",
            detail="ReminderScheduler cancelou o lembrete.",
            now=now,
        )
        logger.info(
            "[LEMBRETE] Scheduler cancelou lembrete item=%s job=%s",
            item.id,
            job_id,
        )
        return item

    def fail_scheduler_reminder(
        self,
        job_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self._store.find_by_source(job_id)
        if item is None:
            return None
        current_now = coerce_pending_now(now)
        item.status = "falhou"
        item.delivered = False
        item.expired = False
        item.tempo_parado = timedelta(0)
        self._store.record_history(
            item,
            "scheduler_falhou",
            detail=f"ReminderScheduler marcou falha do job. reason={reason}",
            now=current_now,
        )
        logger.error(
            "[LEMBRETE] Scheduler marcou falha item=%s job=%s reason=%s",
            item.id,
            job_id,
            reason,
        )
        return item

    def observe_user_activity(
        self,
        text: str = "",
        *,
        source: str = "",
        now: datetime | None = None,
    ) -> None:
        current_now = coerce_pending_now(now)
        self._last_user_activity_at = current_now
        if self._user_is_idle:
            logger.info(
                "[PROATIVO] Atividade recebida enquanto usuario estava idle source=%s texto=%s",
                source or "desconhecido",
                str(text or "").strip()[:80],
            )

    async def handle_user_state_change(
        self,
        *,
        old_state: str,
        new_state: str,
        now: datetime | None = None,
    ) -> None:
        current_now = coerce_pending_now(now)
        folded_old = str(old_state or "").strip().lower()
        folded_new = str(new_state or "").strip().lower()
        was_idle = folded_old in _IDLE_STATES
        is_idle = folded_new in _IDLE_STATES
        if was_idle == is_idle:
            self._user_is_idle = is_idle
            return

        self._user_is_idle = is_idle
        if is_idle:
            self._user_idle_since = current_now
            logger.info("[PROATIVO] Usuario entrou em idle estado=%s", folded_new or "idle")
            return

        idle_started_at = self._user_idle_since
        self._user_idle_since = None
        self._last_user_activity_at = current_now
        idle_duration = (
            current_now - idle_started_at
            if idle_started_at is not None
            else timedelta(0)
        )
        logger.info(
            "[PROATIVO] Usuario voltou do idle estado=%s duracao=%ss",
            folded_new or "ativo",
            int(idle_duration.total_seconds()),
        )
        if idle_duration < _IDLE_RETURN_WINDOW:
            return
        digest = self._build_idle_return_message(now=current_now)
        if not digest:
            return
        await self._speak_fn(
            digest,
            label="PROATIVO",
            add_to_chat_ctx=True,
        )

    def build_reply_instruction(self, *, now: datetime | None = None) -> str:
        current_now = coerce_pending_now(now)
        if self._last_footer_at is not None and (current_now - self._last_footer_at) < _FOOTER_COOLDOWN:
            return ""
        candidate = self._select_footer_candidate(now=current_now)
        if candidate is None:
            return ""
        footer = self._build_footer_message(candidate)
        self._last_footer_at = current_now
        self._store.record_history(
            candidate,
            "footer_sugerido",
            detail=footer,
            now=current_now,
        )
        logger.info(
            "[PROATIVO] Footer preparado para resposta item=%s prioridade=%s",
            candidate.id,
            candidate.prioridade,
        )
        return (
            "Antes de encerrar a resposta, adicione uma frase curta e natural no final. "
            f'Use este lembrete final: "{footer}"'
        )

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._loop_interval_seconds)
                await self._emit_due_messages()
        except asyncio.CancelledError:
            raise

    async def _emit_due_messages(self) -> None:
        if self._user_is_idle:
            return
        current_now = now_brasilia()
        candidate = self._select_due_item(now=current_now)
        if candidate is None:
            return
        tone = self._reminder_tone(candidate)
        message = self._build_nudge_message(candidate, tone)
        candidate.ultimo_lembrete = current_now
        self._store.record_history(
            candidate,
            "lembrete_emitido",
            detail=f"{tone}:{message}",
            now=current_now,
        )
        logger.info(
            "[PROATIVO] Disparando lembrete %s item=%s prioridade=%s",
            tone,
            candidate.id,
            candidate.prioridade,
        )
        await self._speak_fn(
            message,
            label="PROATIVO",
            add_to_chat_ctx=True,
        )

    def _select_due_item(self, *, now: datetime) -> PendingItem | None:
        open_items = self._store.open_items(now=now)
        due_items = [item for item in open_items if self._should_nudge(item, now=now)]
        if not due_items:
            return None
        due_items.sort(
            key=lambda item: (
                self._priority_rank(item.prioridade),
                item.quantidade_adiamentos,
                item.tempo_parado.total_seconds(),
                item.horario_criacao.timestamp(),
            ),
            reverse=True,
        )
        return due_items[0]

    def _select_footer_candidate(self, *, now: datetime) -> PendingItem | None:
        for item in sorted(
            self._store.open_items(now=now),
            key=lambda candidate: (
                self._priority_rank(candidate.prioridade),
                candidate.quantidade_adiamentos,
                candidate.tempo_parado.total_seconds(),
                candidate.horario_criacao.timestamp(),
            ),
            reverse=True,
        ):
            if not self._is_actionable_now(item, now=now):
                continue
            if item.tempo_parado < _FOOTER_MIN_WINDOWS[item.prioridade]:
                continue
            return item
        return None

    def _build_idle_return_message(self, *, now: datetime) -> str:
        open_items = [
            item
            for item in self._store.open_items(now=now)
            if self._is_actionable_now(item, now=now)
        ]
        if not open_items:
            return ""
        important = sorted(
            open_items,
            key=lambda item: (
                self._priority_rank(item.prioridade),
                item.quantidade_adiamentos,
                item.tempo_parado.total_seconds(),
            ),
            reverse=True,
        )[:2]
        parts = [self._short_item_label(item) for item in important if self._short_item_label(item)]
        if not parts:
            return ""
        if len(parts) == 1:
            tail = parts[0]
        else:
            tail = f"{parts[0]} e {parts[1]}"
        summary = (
            f"Voce voltou e ainda tem pendencias importantes: {tail}."
        )
        item = important[0]
        self._store.record_history(
            item,
            "idle_resume_digest",
            detail=summary,
            now=now,
        )
        return summary

    def _should_nudge(self, item: PendingItem, *, now: datetime) -> bool:
        self._store.refresh_dynamic_fields(now=now)
        if item.tempo_parado < _PRIORITY_BASE_WINDOWS[item.prioridade]:
            return False
        if item.horario_lembrete is not None and item.horario_lembrete > now:
            return False
        if item.ultimo_lembrete is None:
            return True
        tone = self._reminder_tone(item)
        return (now - item.ultimo_lembrete) >= _LEVEL_REPEAT_WINDOWS[tone]

    def _is_actionable_now(self, item: PendingItem, *, now: datetime) -> bool:
        if item.horario_lembrete is None:
            return True
        return item.horario_lembrete <= (now + _ACTIONABLE_FUTURE_WINDOW)

    def _reminder_tone(self, item: PendingItem) -> ReminderTone:
        if item.quantidade_adiamentos >= 2 or item.tempo_parado >= timedelta(hours=4):
            return "insistente"
        if item.quantidade_adiamentos >= 1 or item.tempo_parado >= timedelta(hours=2):
            return "medio"
        return "leve"

    def _build_nudge_message(self, item: PendingItem, tone: ReminderTone) -> str:
        base = self._short_item_label(item)
        if tone == "insistente":
            return f"Voce ainda nao resolveu isso: {base}. Vale fechar essa pendencia agora."
        if tone == "medio":
            return f"So reforcando: voce ainda tem pendencia em aberto: {base}."
        return f"Ah, e voce ainda tem uma pendencia: {base}."

    def _build_footer_message(self, item: PendingItem) -> str:
        return f"Ah, e voce ainda tem uma pendencia: {self._short_item_label(item)}."

    def _short_item_label(self, item: PendingItem) -> str:
        if item.tipo == "comunicacao":
            return item.pessoa or item.descricao
        return item.descricao

    def _priority_rank(self, priority: PendingPriority) -> int:
        return {"baixa": 1, "media": 2, "alta": 3}[priority]


__all__ = ["ProactiveEngine", "ReminderTone", "SpeakFn"]
