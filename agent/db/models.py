from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from time_utils import ensure_brasilia, now_brasilia


def utcnow() -> datetime:
    return now_brasilia().astimezone(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    ensured = ensure_brasilia(value)
    if ensured is None:
        return None
    return ensured.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class ReminderRecord(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rem"))
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    reminder_type: Mapped[str] = mapped_column(String(24), default="one_time")
    text: Mapped[str] = mapped_column(Text)
    timezone_name: Mapped[str] = mapped_column(String(64), default="America/Fortaleza")
    due_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    local_date: Mapped[str] = mapped_column(String(16), default="")
    local_time: Mapped[str] = mapped_column(String(8), default="")
    frequency: Mapped[str | None] = mapped_column(String(24), nullable=True)
    day_of_week: Mapped[str | None] = mapped_column(String(16), nullable=True)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    dedupe_key: Mapped[str] = mapped_column(String(191), unique=True, index=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    locked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fired_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class CalendarEventRecord(Base):
    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("evt"))
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(Text)
    timezone_name: Mapped[str] = mapped_column(String(64), default="America/Fortaleza")
    event_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    local_date: Mapped[str] = mapped_column(String(16), default="")
    local_time: Mapped[str] = mapped_column(String(8), default="")
    status: Mapped[str] = mapped_column(String(24), index=True, default="pendente")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class CalendarNotificationRecord(Base):
    __tablename__ = "calendar_notifications"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("caln"))
    event_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    notify_kind: Mapped[str] = mapped_column(String(24), default="at_time")
    timezone_name: Mapped[str] = mapped_column(String(64), default="America/Fortaleza")
    notify_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    final_message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    dedupe_key: Mapped[str] = mapped_column(String(191), unique=True, index=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    locked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fired_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class VisualWatchRecord(Base):
    __tablename__ = "visual_watches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("vw"))
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    request_text: Mapped[str] = mapped_column(Text)
    watch_kind: Mapped[str] = mapped_column(String(32), index=True)
    detector_kind: Mapped[str] = mapped_column(String(32), index=True)
    target_text: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    final_message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    dedupe_key: Mapped[str] = mapped_column(String(191), unique=True, index=True)
    interval_seconds: Mapped[float] = mapped_column(Float, default=1.2)
    timeout_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requires_ui: Mapped[bool] = mapped_column(Boolean, default=False)
    condition_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    consecutive_matches: Mapped[int] = mapped_column(Integer, default=0)
    required_matches: Mapped[int] = mapped_column(Integer, default=2)
    last_reason: Mapped[str] = mapped_column(Text, default="")
    last_checked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fired_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    locked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class NotificationOutboxRecord(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("out"))
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    source_kind: Mapped[str] = mapped_column(String(24), index=True)
    source_id: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
