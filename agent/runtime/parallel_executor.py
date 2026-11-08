from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    task_id: str
    success: bool
    result: Any
    error: Exception | None
    duration_ms: float


class ParallelExecutor:
    def __init__(self, max_workers: int = 5):
        self.max_workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
        logger.info("[PARALLEL] Executor inicializado - max_workers=%d", self.max_workers)

    async def execute_parallel(self, tasks: dict[str, Any], timeout: float | None = None) -> dict[str, TaskResult]:
        if not tasks:
            return {}

        async def _run_task(task_id: str, coro) -> TaskResult:
            started = time.time()
            try:
                res = await coro
                return TaskResult(task_id, True, res, None, (time.time() - started) * 1000.0)
            except Exception as exc:
                return TaskResult(task_id, False, None, exc, (time.time() - started) * 1000.0)

        logger.info("[PARALLEL] Executando %d tarefas em paralelo - ids=%s", len(tasks), list(tasks.keys()))
        wrapped = {k: _run_task(k, c) for k, c in tasks.items()}
        start_all = time.time()
        try:
            results_list = await asyncio.wait_for(asyncio.gather(*wrapped.values()), timeout=timeout) if timeout else await asyncio.gather(*wrapped.values())
        except asyncio.TimeoutError:
            logger.error("[PARALLEL] Timeout - limit=%ss", timeout)
            return {}
        out = {k: v for k, v in zip(wrapped.keys(), results_list)}
        ok = sum(1 for r in out.values() if r.success)
        logger.info(
            "[PARALLEL] Concluido - total=%dms tasks=%d success=%d failed=%d",
            round((time.time() - start_all) * 1000.0),
            len(out),
            ok,
            len(out) - ok,
        )
        return out

    async def run_in_thread(self, func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, lambda: func(*args, **kwargs))

