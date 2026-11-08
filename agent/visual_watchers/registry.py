from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Awaitable, Callable

from resilience import log_exception_safely

from .conditions import VisualWatchError, build_condition, build_visual_watch_plan
from .goal_manager import VisualGoalManager
from .models import VisualWatcherInfo, VisualWatcherStartResult


logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]
TaskScheduler = Callable[[asyncio.Task], None]

VISUAL_WATCHER_MAX_ACTIVE = max(int(os.getenv("KIRA_VISUAL_WATCHER_MAX_ACTIVE", "4")), 1)
VISUAL_WATCHER_MIN_TIMEOUT_SECONDS = 15.0


class VisualWatcherRegistry:
    def __init__(self) -> None:
        self._runtime = None
        self._notifier: Notifier | None = None
        self._task_scheduler: TaskScheduler | None = None
        self._watchers: dict[str, VisualWatcherInfo] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._counter = 0
        self._goal_manager = VisualGoalManager()

    def configure(self, *, runtime=None, task_scheduler: TaskScheduler | None = None) -> None:
        if runtime is not None:
            self._runtime = runtime
        self._task_scheduler = task_scheduler

    def set_notifier(self, notifier: Notifier | None) -> None:
        self._notifier = notifier

    def list_watchers(self) -> list[VisualWatcherInfo]:
        return sorted(self._watchers.values(), key=lambda watcher: watcher.created_at)

    def cancel_watcher(self, watcher_id: str) -> bool:
        target = str(watcher_id or "").strip()
        if not target:
            return False

        task = self._tasks.get(target)
        watcher = self._watchers.get(target)
        if watcher is not None:
            watcher.status = "cancelled"
        if task is not None:
            task.cancel()
        self._watchers.pop(target, None)
        self._tasks.pop(target, None)
        self._goal_manager.cancel_goal(target, reason="cancelled_by_user")
        return watcher is not None or task is not None

    def cancel_all(self) -> int:
        active_ids = list(self._watchers.keys())
        for watcher_id in active_ids:
            self.cancel_watcher(watcher_id)
        return len(active_ids)

    async def shutdown(self) -> None:
        active_tasks = list(self._tasks.values())
        for watcher in self._watchers.values():
            watcher.status = "cancelled"
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        self._tasks.clear()
        self._watchers.clear()

    async def start_from_request(
        self,
        request_text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> VisualWatcherStartResult:
        runtime = self._runtime
        if runtime is None:
            raise VisualWatchError("O runtime visual ainda nao esta pronto.")

        if not runtime.share_active:
            raise VisualWatchError("Nao estou vendo a tela agora. Ativa o compartilhamento que eu monitoro.")

        if not await runtime.wait_until_ready():
            raise VisualWatchError("A tela acabou de abrir. Me chama de novo em alguns segundos.")

        plan = build_visual_watch_plan(request_text)
        safe_timeout = plan.timeout_seconds if timeout_seconds is None else timeout_seconds
        if safe_timeout is not None:
            safe_timeout = max(float(safe_timeout), VISUAL_WATCHER_MIN_TIMEOUT_SECONDS)

        duplicate = self._find_duplicate(plan.dedupe_key)
        if duplicate is not None:
            return VisualWatcherStartResult(
                watcher=duplicate,
                confirmation_message=plan.confirmation_message,
                duplicate=True,
            )

        if len(self._watchers) >= VISUAL_WATCHER_MAX_ACTIVE:
            raise VisualWatchError("Ja tem monitoramento demais ativo. Para algum antes de abrir outro.")

        baseline = await runtime.get_snapshot(require_ui=plan.requires_ui)
        if baseline is None or baseline.frame_token <= 0:
            raise VisualWatchError("Nao estou vendo a tela agora. Ativa o compartilhamento que eu monitoro.")

        condition = build_condition(plan, baseline)

        self._counter += 1
        watcher_id = f"visual-{self._counter:03d}"
        watcher = VisualWatcherInfo(
            watcher_id=watcher_id,
            kind=plan.kind,
            request_text=plan.request_text,
            target_text=plan.target_text,
            description=plan.description,
            interval_seconds=max(plan.interval_seconds, 0.5),
            timeout_seconds=safe_timeout,
            dedupe_key=plan.dedupe_key,
        )
        self._watchers[watcher_id] = watcher
        target_value = self._extract_target_value(plan.request_text) if plan.kind == "progress_complete" else None
        self._goal_manager.create_goal(
            goal_id=watcher_id,
            source_command=plan.request_text,
            goal_type=plan.kind,
            target_value=target_value,
            target_label=plan.target_text or "progresso",
            app_hint=baseline.window_title or "",
        )

        task = asyncio.create_task(
            self._run_watcher(
                watcher=watcher,
                condition=condition,
                success_message=plan.success_message,
                requires_ui=plan.requires_ui,
            ),
            name=f"visual-watcher-{watcher_id}",
        )
        self._tasks[watcher_id] = task
        task.add_done_callback(lambda done, wid=watcher_id: self._finalize_watcher(wid, done))

        if callable(self._task_scheduler):
            self._task_scheduler(task)

        return VisualWatcherStartResult(
            watcher=watcher,
            confirmation_message=plan.confirmation_message,
            duplicate=False,
        )

    @staticmethod
    def _extract_target_value(request_text: str) -> int | None:
        match = re.search(r"(\d{1,3})\s*%", str(request_text or ""))
        if not match:
            match = re.search(r"\b(\d{1,3})\b", str(request_text or ""))
        if not match:
            return None
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        return max(0, min(100, value))

    def _find_duplicate(self, dedupe_key: str) -> VisualWatcherInfo | None:
        target = str(dedupe_key or "").strip()
        if not target:
            return None
        for watcher in self._watchers.values():
            if watcher.status == "active" and watcher.dedupe_key == target:
                return watcher
        return None

    async def _run_watcher(
        self,
        *,
        watcher: VisualWatcherInfo,
        condition,
        success_message: str,
        requires_ui: bool,
    ) -> None:
        deadline = None
        if watcher.timeout_seconds is not None:
            deadline = time.monotonic() + float(watcher.timeout_seconds)

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                watcher.status = "timed_out"
                watcher.last_reason = "timeout"
                return

            try:
                runtime = self._runtime
                snapshot = await runtime.get_snapshot(require_ui=requires_ui) if runtime is not None else None
                if snapshot is None or not snapshot.share_active:
                    watcher.status = "cancelled"
                    watcher.last_reason = "visual_context_off"
                    self._goal_manager.cancel_goal(watcher.watcher_id, reason="visual_context_off")
                    return
                if snapshot.frame_age_seconds > 12.0:
                    watcher.last_reason = "stale_frame"
                    await asyncio.sleep(watcher.interval_seconds)
                    continue

                outcome = condition.evaluate(snapshot)
                watcher.last_reason = outcome.reason
                observed_value = None
                if snapshot.ui is not None and snapshot.ui.progress_values:
                    observed_value = max(snapshot.ui.progress_values)
                matched_candidate = str(outcome.reason or "").startswith("progress_100_candidate")
                self._goal_manager.frame_evaluated(
                    watcher.watcher_id,
                    observed_value=observed_value,
                    matched=bool(outcome.triggered),
                    reason=outcome.reason,
                )
                if matched_candidate:
                    self._goal_manager.candidate_match(
                        watcher.watcher_id,
                        observed_value=observed_value,
                        confidence=0.78,
                    )
                logger.debug(
                    "visual_goal_value_detected goal_id=%s observed_value=%s reason=%s",
                    watcher.watcher_id,
                    observed_value,
                    outcome.reason,
                )
            except asyncio.CancelledError:
                watcher.status = "cancelled"
                self._goal_manager.cancel_goal(watcher.watcher_id, reason="task_cancelled")
                raise
            except Exception as exc:
                watcher.last_reason = str(exc)
                log_exception_safely(
                    logger,
                    f"Falha no watcher visual {watcher.watcher_id}",
                    exc,
                    level=logging.WARNING,
                )
                await asyncio.sleep(watcher.interval_seconds)
                continue

            if outcome.cancel_message:
                watcher.status = "cancelled"
                self._goal_manager.cancel_goal(watcher.watcher_id, reason="condition_cancelled")
                await self._notify(outcome.cancel_message)
                return

            if outcome.triggered:
                watcher.status = "triggered"
                self._goal_manager.completed(
                    watcher.watcher_id,
                    observed_value=observed_value,
                    reason=outcome.reason,
                )
                await self._notify(success_message)
                logger.info("visual_goal_speech_dispatched goal_id=%s", watcher.watcher_id)
                return

            await asyncio.sleep(watcher.interval_seconds)

    async def _notify(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        notifier = self._notifier
        if notifier is None:
            logger.info("Watcher visual disparado sem notifier configurado: %s", text)
            return
        try:
            await notifier(text)
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao notificar watcher visual: {text}",
                exc,
                level=logging.WARNING,
            )

    def _finalize_watcher(self, watcher_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(watcher_id, None)
        watcher = self._watchers.get(watcher_id)
        if watcher is not None and watcher.status != "active":
            self._watchers.pop(watcher_id, None)
            if watcher.status == "triggered":
                self._goal_manager.archive_goal(watcher_id, reason="triggered")
            elif watcher.status in {"cancelled", "timed_out"}:
                self._goal_manager.archive_goal(watcher_id, reason=watcher.status)

        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Watcher visual {watcher_id} terminou com erro inesperado",
                exc,
                level=logging.WARNING,
            )
