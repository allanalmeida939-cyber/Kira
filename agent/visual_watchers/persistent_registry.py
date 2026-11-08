from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta

from sqlalchemy import or_, select, update

from bootstrap.memory_loader import preferred_user_id
from db.models import VisualWatchRecord, ensure_utc, utcnow
from services.runtime_services import get_notifier_service, get_session_factory, get_session_registry

from .conditions import VisualWatchError, build_visual_watch_plan
from .models import VisualWatcherInfo, VisualWatcherStartResult, normalize_text


logger = logging.getLogger(__name__)

LOCK_STALE_SECONDS = 120.0
WATCH_LOOP_SECONDS = 1.0

_LOADING_KEYWORDS = (
    "carregando",
    "loading",
    "aguarde",
    "abrindo",
    "processando",
    "sincronizando",
    "baixando",
    "download",
    "instalando",
    "instalacao",
    "updating",
    "atualizando",
)
_SUCCESS_KEYWORDS = ("concluido", "concluida", "feito", "sucesso", "success", "pronto", "done")
_ERROR_KEYWORDS = ("erro", "error", "falha", "failed", "nao foi possivel", "problema")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


class VisualWatcherRegistry:
    _runner_lock = asyncio.Lock()
    _runner_task: asyncio.Task | None = None

    def __init__(self) -> None:
        self._session_factory = get_session_factory()
        self._session_registry = get_session_registry()
        self._notifier = get_notifier_service()
        self._task_scheduler = None
        self._legacy_notifier = None

    def configure(self, *, runtime=None, task_scheduler=None) -> None:
        self._task_scheduler = task_scheduler
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.__class__._runner_task is None or self.__class__._runner_task.done():
            task = loop.create_task(self._watch_loop(), name="persistent-visual-watches")
            self.__class__._runner_task = task
            if callable(task_scheduler):
                task_scheduler(task)

    def set_notifier(self, notifier) -> None:
        self._legacy_notifier = notifier

    def list_watchers(self) -> list[VisualWatcherInfo]:
        with self._session_factory() as session:
            rows = session.execute(
                select(VisualWatchRecord)
                .where(VisualWatchRecord.status.in_(["pending", "observing", "matched"]))
                .order_by(VisualWatchRecord.created_at_utc.asc())
            ).scalars().all()
            return [self._to_info(row) for row in rows]

    def cancel_watcher(self, watcher_id: str) -> bool:
        target = str(watcher_id or "").strip()
        if not target:
            return False
        with self._session_factory() as session:
            row = session.get(VisualWatchRecord, target)
            if row is None:
                return False
            row.status = "cancelled"
            session.commit()
            return True

    def cancel_all(self) -> int:
        with self._session_factory() as session:
            rows = session.execute(
                select(VisualWatchRecord).where(VisualWatchRecord.status.in_(["pending", "observing", "matched"]))
            ).scalars().all()
            total = len(rows)
            for row in rows:
                row.status = "cancelled"
            session.commit()
            return total

    async def shutdown(self) -> None:
        task = self.__class__._runner_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.__class__._runner_task = None

    async def start_from_request(
        self,
        request_text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> VisualWatcherStartResult:
        handle = self._active_handle()
        if handle is None or handle.visual_runtime is None or not handle.visual_context_active():
            raise VisualWatchError("Nao estou vendo a tela agora. Ativa o compartilhamento que eu monitoro.")

        if not await handle.visual_runtime.wait_until_ready():
            raise VisualWatchError("A tela acabou de abrir. Me chama de novo em alguns segundos.")

        plan = build_visual_watch_plan(request_text)
        safe_timeout = plan.timeout_seconds if timeout_seconds is None else timeout_seconds
        if safe_timeout is not None:
            safe_timeout = max(float(safe_timeout), 15.0)

        with self._session_factory() as session:
            duplicate = session.execute(
                select(VisualWatchRecord).where(
                    VisualWatchRecord.dedupe_key == plan.dedupe_key,
                    VisualWatchRecord.status.in_(["pending", "observing", "matched"]),
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return VisualWatcherStartResult(
                    watcher=self._to_info(duplicate),
                    confirmation_message=plan.confirmation_message,
                    duplicate=True,
                )

        baseline = await handle.visual_runtime.get_snapshot(require_ui=plan.requires_ui)
        if baseline is None:
            raise VisualWatchError("Nao consegui capturar a tela inicial para monitorar.")

        seen_target = False
        if plan.target_text:
            seen_target = baseline.contains_text(plan.target_text) or baseline.contains_button(plan.target_text)

        row = VisualWatchRecord(
            user_id=handle.user_id,
            request_text=plan.request_text,
            watch_kind=plan.kind,
            detector_kind=self._detector_kind_for(plan),
            target_text=plan.target_text,
            description=plan.description,
            final_message=plan.success_message,
            status="observing",
            dedupe_key=plan.dedupe_key,
            interval_seconds=max(plan.interval_seconds, 0.5),
            timeout_at_utc=None if safe_timeout is None else utcnow() + timedelta(seconds=safe_timeout),
            requires_ui=plan.requires_ui,
            condition_payload={
                "baseline_frame_token": baseline.frame_token,
                "baseline_window_title": baseline.window_title,
                "min_change_score": plan.min_change_score,
                "stable_seconds": plan.stable_seconds,
                "seen_loading": self._has_loading(baseline),
                "seen_target": seen_target,
                "seen_progress": any(value < 100 for value in self._extract_progress_values(baseline)),
                "seen_activity": False,
            },
            required_matches=2,
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)

        return VisualWatcherStartResult(
            watcher=self._to_info(row),
            confirmation_message=plan.confirmation_message,
            duplicate=False,
        )

    async def _watch_loop(self) -> None:
        while True:
            try:
                rows = self._list_observing_rows()
                for row in rows:
                    await self._tick_watch(row.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha no loop persistente de visual watches.")
            await asyncio.sleep(WATCH_LOOP_SECONDS)

    def _list_observing_rows(self) -> list[VisualWatchRecord]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(VisualWatchRecord)
                    .where(VisualWatchRecord.status.in_(["pending", "observing", "matched"]))
                    .order_by(VisualWatchRecord.created_at_utc.asc())
                ).scalars().all()
            )

    async def _tick_watch(self, watch_id: str) -> None:
        with self._session_factory() as session:
            watch = session.get(VisualWatchRecord, watch_id)
            if watch is None or watch.status not in {"pending", "observing", "matched"}:
                return
            timeout_at = ensure_utc(watch.timeout_at_utc)
            if timeout_at is not None and utcnow() >= timeout_at:
                watch.status = "expired"
                watch.last_reason = "timeout"
                session.commit()
                return

        handle = self._session_registry.get(watch.user_id)
        if handle is None or handle.visual_runtime is None or not handle.visual_context_active():
            return

        snapshot = await handle.visual_runtime.get_snapshot(require_ui=watch.requires_ui)
        if snapshot is None or not snapshot.share_active or snapshot.frame_age_seconds > 12.0:
            return

        matched, reason, payload_update = self._evaluate_watch(watch, snapshot)
        with self._session_factory() as session:
            current = session.get(VisualWatchRecord, watch_id)
            if current is None or current.status not in {"pending", "observing", "matched"}:
                return
            payload = dict(current.condition_payload or {})
            payload.update(payload_update)
            current.condition_payload = payload
            current.last_reason = reason
            current.last_checked_at_utc = utcnow()
            current.consecutive_matches = current.consecutive_matches + 1 if matched else 0
            session.commit()

        if not matched:
            return

        claimed = self._claim_for_fire(watch_id)
        if claimed is None:
            return

        await self._notifier.notify_user(
            user_id=claimed.user_id,
            message=claimed.final_message,
            label="WATCH",
            source_kind="watch",
            source_id=claimed.id,
        )
        with self._session_factory() as session:
            current = session.get(VisualWatchRecord, watch_id)
            if current is None:
                return
            current.status = "fired"
            current.fired_at_utc = utcnow()
            current.lock_token = None
            current.locked_at_utc = None
            session.commit()

    def _claim_for_fire(self, watch_id: str) -> VisualWatchRecord | None:
        lock_token = f"watch-{int(time.time() * 1000)}"
        stale_before = utcnow() - timedelta(seconds=LOCK_STALE_SECONDS)
        with self._session_factory() as session:
            result = session.execute(
                update(VisualWatchRecord)
                .where(VisualWatchRecord.id == watch_id)
                .where(VisualWatchRecord.status.in_(["observing", "matched", "pending"]))
                .where(VisualWatchRecord.consecutive_matches >= VisualWatchRecord.required_matches)
                .where(
                    or_(
                        VisualWatchRecord.locked_at_utc.is_(None),
                        VisualWatchRecord.locked_at_utc < stale_before,
                    )
                )
                .values(
                    status="matched",
                    matched_at_utc=utcnow(),
                    lock_token=lock_token,
                    locked_at_utc=utcnow(),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return session.execute(
                select(VisualWatchRecord).where(
                    VisualWatchRecord.id == watch_id,
                    VisualWatchRecord.lock_token == lock_token,
                )
            ).scalar_one_or_none()

    def _evaluate_watch(self, watch: VisualWatchRecord, snapshot) -> tuple[bool, str, dict]:
        payload = dict(watch.condition_payload or {})
        target = normalize_text(watch.target_text or "")
        kind = watch.watch_kind

        if kind == "screen_change":
            return (
                snapshot.frame_token > int(payload.get("baseline_frame_token", 0))
                and snapshot.change_score >= float(payload.get("min_change_score", 8.0)),
                "frame_changed",
                payload,
            )

        if kind in {"text_appears", "object_appears"}:
            present = self._matches_target(snapshot, target or watch.request_text)
            return present, "target_appeared" if present else "waiting_target", payload

        if kind in {"text_disappears", "window_closes"}:
            present = self._matches_target(snapshot, target or watch.request_text)
            seen_target = bool(payload.get("seen_target", False) or present)
            payload["seen_target"] = seen_target
            matched = seen_target and not present
            return matched, "target_disappeared" if matched else "waiting_disappear", payload

        if kind == "button_appears":
            present = snapshot.contains_button(target or watch.request_text)
            return present, "button_appeared" if present else "waiting_button", payload

        if kind == "button_clickable":
            present = snapshot.contains_button(target or watch.request_text, enabled_only=True)
            return present, "button_clickable" if present else "waiting_button", payload

        if kind == "error_appears":
            visible = self._contains_keywords(snapshot, _ERROR_KEYWORDS)
            return visible, "error_visible" if visible else "waiting_error", payload

        if kind == "success_appears":
            visible = self._contains_keywords(snapshot, _SUCCESS_KEYWORDS)
            return visible, "success_visible" if visible else "waiting_success", payload

        if kind == "progress_complete":
            values = self._extract_progress_values(snapshot)
            seen_progress = bool(payload.get("seen_progress", False))
            if values and max(values) < 100:
                seen_progress = True
            payload["seen_progress"] = seen_progress
            matched = bool(values and max(values) >= 100)
            if not matched and seen_progress and not self._has_loading(snapshot):
                matched = True
            return matched, "progress_done" if matched else "waiting_progress", payload

        if kind == "window_opens":
            if target:
                present = self._matches_target(snapshot, target)
                return present, "target_visible" if present else "waiting_window", payload
            baseline_title = normalize_text(str(payload.get("baseline_window_title") or ""))
            changed = normalize_text(snapshot.window_title) not in {"", baseline_title}
            matched = changed and snapshot.stable_for_seconds >= max(float(payload.get("stable_seconds", 2.5)), 1.5)
            return matched, "window_changed" if matched else "waiting_window", payload

        seen_loading = bool(payload.get("seen_loading", False))
        seen_activity = bool(payload.get("seen_activity", False))
        if self._has_loading(snapshot):
            seen_loading = True
        if snapshot.change_score >= float(payload.get("min_change_score", 6.5)):
            seen_activity = True
        payload["seen_loading"] = seen_loading
        payload["seen_activity"] = seen_activity

        values = self._extract_progress_values(snapshot)
        if values and max(values) >= 100:
            return True, "progress_100", payload
        if self._contains_keywords(snapshot, _SUCCESS_KEYWORDS):
            return True, "success_visible", payload
        if seen_loading and not self._has_loading(snapshot):
            stable_for = max(float(payload.get("stable_seconds", 3.0)), 1.0)
            if snapshot.stable_for_seconds >= stable_for:
                return True, "loading_finished", payload
        if seen_activity and snapshot.stable_for_seconds >= max(float(payload.get("stable_seconds", 3.0)), 1.0):
            return True, "visual_settled", payload
        return False, "waiting_completion", payload

    def _detector_kind_for(self, plan) -> str:
        request = normalize_text(plan.request_text)
        if any(token in request for token in ("zumbi", "creeper", "inimigo", "boss", "monstro", "personagem", "objeto")):
            return "summary_object"
        if plan.kind in {"progress_complete", "completion"}:
            return "ui_progress"
        if plan.kind in {"button_appears", "button_clickable"}:
            return "ui_button"
        return "ui_text"

    def _matches_target(self, snapshot, target: str) -> bool:
        needle = normalize_text(target)
        if not needle:
            return False
        if snapshot.contains_text(needle):
            return True
        if snapshot.contains_button(needle):
            return True
        analysis = getattr(snapshot, "analysis", None)
        for candidate in (
            getattr(analysis, "summary", ""),
            getattr(analysis, "prominent_text", ""),
            getattr(analysis, "primary_action", ""),
            getattr(snapshot, "screen_summary", ""),
            getattr(snapshot, "window_title", ""),
        ):
            if needle in normalize_text(str(candidate or "")):
                return True
        return False

    def _contains_keywords(self, snapshot, keywords: tuple[str, ...]) -> bool:
        haystacks = [
            normalize_text(str(getattr(snapshot, "screen_summary", "") or "")),
            normalize_text(str(getattr(snapshot, "window_title", "") or "")),
        ]
        ui = getattr(snapshot, "ui", None)
        if ui is not None:
            haystacks.extend(list(getattr(ui, "texts", ()) or ()))
            haystacks.extend(list(getattr(ui, "buttons", ()) or ()))
            haystacks.extend(list(getattr(ui, "enabled_buttons", ()) or ()))
        return any(any(keyword in text for keyword in keywords) for text in haystacks)

    def _has_loading(self, snapshot) -> bool:
        ui = getattr(snapshot, "ui", None)
        if ui is not None and getattr(ui, "loading_visible", False):
            return True
        return self._contains_keywords(snapshot, _LOADING_KEYWORDS)

    def _extract_progress_values(self, snapshot) -> tuple[int, ...]:
        values: list[int] = []
        ui = getattr(snapshot, "ui", None)
        if ui is not None:
            values.extend(list(getattr(ui, "progress_values", ()) or ()))
        for text in (
            normalize_text(str(getattr(snapshot, "screen_summary", "") or "")),
            normalize_text(str(getattr(snapshot, "window_title", "") or "")),
        ):
            for match in _PERCENT_RE.findall(text):
                try:
                    percent = int(match)
                except ValueError:
                    continue
                if 0 <= percent <= 100 and percent not in values:
                    values.append(percent)
        return tuple(sorted(set(values)))

    def _active_handle(self):
        preferred_id = ""
        try:
            preferred_id = preferred_user_id()
        except Exception:
            preferred_id = ""
        if preferred_id:
            handle = self._session_registry.get(preferred_id)
            if handle is not None:
                return handle
        active_ids = self._session_registry.list_active_user_ids()
        if not active_ids:
            return None
        return self._session_registry.get(active_ids[0])

    def _to_info(self, row: VisualWatchRecord) -> VisualWatcherInfo:
        age_seconds = 0
        created_at = ensure_utc(row.created_at_utc)
        if created_at is not None:
            age_seconds = max(int((utcnow() - created_at).total_seconds()), 0)
        timeout_at = ensure_utc(row.timeout_at_utc)
        return VisualWatcherInfo(
            watcher_id=row.id,
            kind=row.watch_kind,
            request_text=row.request_text,
            target_text=row.target_text,
            description=row.description,
            interval_seconds=row.interval_seconds,
            timeout_seconds=None if timeout_at is None else max(int((timeout_at - utcnow()).total_seconds()), 0),
            created_at=time.monotonic() - age_seconds,
            status=row.status,
            last_reason=row.last_reason,
            dedupe_key=row.dedupe_key,
        )
