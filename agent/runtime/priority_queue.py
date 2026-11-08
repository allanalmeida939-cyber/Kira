from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from enum import IntEnum
from typing import Any, Callable


logger = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    """Prioridades de tarefa."""

    VOICE = 1
    URGENT_ACTION = 2
    VISUAL = 3


class PriorityQueue:
    """Gate de prioridade que preserva voz como prioridade maxima."""

    def __init__(
        self,
        speech_coordinator,
        *,
        visual_wait_timeout_seconds: float = 30.0,
        poll_seconds: float = 1.0,
    ) -> None:
        self.speech_coordinator = speech_coordinator
        self._visual_wait_timeout_seconds = max(float(visual_wait_timeout_seconds), 0.0)
        self._poll_seconds = max(float(poll_seconds), 0.05)
        self._non_voice_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._visual_generation = 0
        self._pending_visual_tasks = 0

    async def enqueue(
        self,
        priority: TaskPriority,
        task_fn: Callable[..., Any],
        *args,
        **kwargs,
    ) -> Any:
        """
        Executa uma tarefa respeitando prioridade e estado da voz.
        """
        if priority == TaskPriority.VOICE:
            logger.debug("[PRIORITY_QUEUE] VOICE - execucao imediata")
            return await self._run_task(task_fn, *args, **kwargs)

        if priority == TaskPriority.VISUAL:
            visual_generation = self._register_visual_task()
            try:
                if not await self._wait_for_voice_if_needed(visual_generation=visual_generation):
                    logger.info("[PRIORITY_QUEUE] VISUAL cancelado antes da execucao")
                    return None

                async with self._non_voice_lock:
                    if not self._is_visual_generation_current(visual_generation):
                        logger.info("[PRIORITY_QUEUE] VISUAL descartado apos lock")
                        return None
                    if not await self._wait_for_voice_if_needed(visual_generation=visual_generation):
                        logger.info("[PRIORITY_QUEUE] VISUAL cancelado durante espera no lock")
                        return None
                    return await self._run_task(task_fn, *args, **kwargs)
            finally:
                self._finish_visual_task()

        async with self._non_voice_lock:
            return await self._run_task(task_fn, *args, **kwargs)

    def clear_visual_tasks(self) -> None:
        with self._state_lock:
            removed = int(self._pending_visual_tasks)
            self._visual_generation += 1
            generation = self._visual_generation
        logger.info(
            "[PRIORITY_QUEUE] Tarefas visuais removidas - remaining=0 removed=%d generation=%d",
            removed,
            generation,
        )

    async def _run_task(self, task_fn: Callable[..., Any], *args, **kwargs) -> Any:
        result = task_fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _register_visual_task(self) -> int:
        with self._state_lock:
            self._pending_visual_tasks += 1
            return self._visual_generation

    def _finish_visual_task(self) -> None:
        with self._state_lock:
            self._pending_visual_tasks = max(self._pending_visual_tasks - 1, 0)

    def _is_visual_generation_current(self, visual_generation: int) -> bool:
        with self._state_lock:
            return int(visual_generation) == int(self._visual_generation)

    async def _wait_for_voice_if_needed(self, *, visual_generation: int | None = None) -> bool:
        if visual_generation is not None and not self._is_visual_generation_current(visual_generation):
            return False

        is_speaking = bool(self.speech_coordinator.is_speaking())
        is_listening = bool(self.speech_coordinator.is_listening())
        if not (is_speaking or is_listening):
            return True

        logger.info(
            "[PRIORITY_QUEUE] VISUAL aguardando - speaking=%s listening=%s",
            is_speaking,
            is_listening,
        )
        if self._visual_wait_timeout_seconds <= 0:
            logger.warning("[PRIORITY_QUEUE] VISUAL timeout - executando mesmo assim")
            return True

        waited = 0.0
        while waited < self._visual_wait_timeout_seconds:
            await asyncio.sleep(self._poll_seconds)
            waited += self._poll_seconds
            if visual_generation is not None and not self._is_visual_generation_current(visual_generation):
                logger.info("[PRIORITY_QUEUE] VISUAL cancelado durante espera")
                return False
            is_speaking = bool(self.speech_coordinator.is_speaking())
            is_listening = bool(self.speech_coordinator.is_listening())
            if not (is_speaking or is_listening):
                logger.info("[PRIORITY_QUEUE] VISUAL liberado - voz terminou")
                return True

        logger.warning("[PRIORITY_QUEUE] VISUAL timeout - executando mesmo assim")
        return True


__all__ = ["PriorityQueue", "TaskPriority"]
