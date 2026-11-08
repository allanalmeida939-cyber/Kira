from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from db.models import NotificationOutboxRecord, utcnow
from .persistence import get_session_factory


logger = logging.getLogger(__name__)


class NotifierService:
    def __init__(self, session_registry) -> None:
        self._session_registry = session_registry
        self._session_factory = get_session_factory()

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        label: str,
        source_kind: str,
        source_id: str,
    ) -> bool:
        text = str(message or "").strip()
        if not text:
            return False

        handle = self._session_registry.get(user_id)
        if handle is None:
            self.enqueue_outbox(
                user_id=user_id,
                source_kind=source_kind,
                source_id=source_id,
                label=label,
                message=text,
            )
            return False

        try:
            await handle.speech_coordinator.speak_proactive_message(
                text,
                label=label,
                add_to_chat_ctx=True,
                allow_interruptions=True,
            )
        except Exception:
            logger.exception("Falha ao falar notificacao proativa para %s.", user_id)
            self.enqueue_outbox(
                user_id=user_id,
                source_kind=source_kind,
                source_id=source_id,
                label=label,
                message=text,
            )
            return False

        self.mark_delivered(source_kind=source_kind, source_id=source_id)
        return True

    def notify_user_sync(
        self,
        *,
        user_id: str,
        message: str,
        label: str,
        source_kind: str,
        source_id: str,
        timeout_seconds: float = 20.0,
    ) -> bool:
        text = str(message or "").strip()
        if not text:
            return False

        handle = self._session_registry.get(user_id)
        if handle is None:
            self.enqueue_outbox(
                user_id=user_id,
                source_kind=source_kind,
                source_id=source_id,
                label=label,
                message=text,
            )
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                handle.speech_coordinator.speak_proactive_message(
                    text,
                    label=label,
                    add_to_chat_ctx=True,
                    allow_interruptions=True,
                ),
                handle.loop,
            )
            future.result(timeout=max(float(timeout_seconds), 1.0))
        except Exception:
            logger.exception("Falha ao entregar notificacao sincrona para %s.", user_id)
            self.enqueue_outbox(
                user_id=user_id,
                source_kind=source_kind,
                source_id=source_id,
                label=label,
                message=text,
            )
            return False

        self.mark_delivered(source_kind=source_kind, source_id=source_id)
        return True

    async def flush_pending(self, user_id: str) -> int:
        delivered = 0
        for item in self.list_pending_outbox(user_id):
            ok = await self.notify_user(
                user_id=item.user_id,
                message=item.message,
                label=item.label,
                source_kind=item.source_kind,
                source_id=item.source_id,
            )
            if ok:
                delivered += 1
        return delivered

    def enqueue_outbox(
        self,
        *,
        user_id: str,
        source_kind: str,
        source_id: str,
        label: str,
        message: str,
    ) -> NotificationOutboxRecord:
        with self._session_factory() as session:
            existing = session.execute(
                select(NotificationOutboxRecord).where(
                    NotificationOutboxRecord.source_kind == source_kind,
                    NotificationOutboxRecord.source_id == source_id,
                    NotificationOutboxRecord.status == "pending",
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing

            row = NotificationOutboxRecord(
                user_id=user_id,
                source_kind=source_kind,
                source_id=source_id,
                label=str(label or "KIRA")[:24],
                message=str(message or "").strip(),
            )
            session.add(row)
            session.commit()
            return row

    def list_pending_outbox(self, user_id: str) -> list[NotificationOutboxRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(NotificationOutboxRecord)
                .where(
                    NotificationOutboxRecord.user_id == user_id,
                    NotificationOutboxRecord.status == "pending",
                )
                .order_by(NotificationOutboxRecord.created_at_utc.asc())
            ).scalars().all()
            return list(rows)

    def mark_delivered(self, *, source_kind: str, source_id: str) -> None:
        with self._session_factory() as session:
            rows = session.execute(
                select(NotificationOutboxRecord).where(
                    NotificationOutboxRecord.source_kind == source_kind,
                    NotificationOutboxRecord.source_id == source_id,
                    NotificationOutboxRecord.status == "pending",
                )
            ).scalars().all()
            for row in rows:
                row.status = "delivered"
                row.delivered_at_utc = utcnow()
            session.commit()
