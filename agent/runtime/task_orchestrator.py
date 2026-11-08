from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from runtime.parallel_executor import ParallelExecutor

logger = logging.getLogger(__name__)


@dataclass
class QueryPlan:
    needs_web_search: bool = False
    needs_tools: list[str] | None = None
    is_simple: bool = False


class TaskOrchestrator:
    def __init__(self, parallel_executor: ParallelExecutor):
        self.executor = parallel_executor

    def analyze_query(self, query: str) -> QueryPlan:
        q = str(query or "").lower()
        plan = QueryPlan()
        if any(k in q for k in ("noticia", "recente", "hoje", "agora", "pesquisa", "busca", "procura")):
            plan.needs_web_search = True
        tools = []
        if "clima" in q or "temperatura" in q or "tempo em" in q:
            tools.append("weather")
        plan.needs_tools = tools or None
        if any(k in q for k in ("oi", "ola", "tudo bem", "obrigad", "valeu")):
            plan.is_simple = True
        return plan

    async def execute_query_parallel(
        self,
        query: str,
        gemini_callback,
        web_search_callback=None,
        tools_callbacks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self.analyze_query(query)
        logger.info("[ORCHESTRATOR] Plan - web=%s tools=%s simple=%s", plan.needs_web_search, plan.needs_tools, plan.is_simple)
        tasks = {"gemini": gemini_callback}
        if plan.needs_web_search and web_search_callback is not None:
            tasks["web_search"] = web_search_callback
        if plan.needs_tools and tools_callbacks:
            for t in plan.needs_tools:
                if t in tools_callbacks:
                    tasks[f"tool_{t}"] = tools_callbacks[t]
        if len(tasks) == 1:
            return {"gemini": await gemini_callback}
        logger.info("[ORCHESTRATOR] Executando %d tarefas em PARALELO", len(tasks))
        timeout_s = 10.0
        results = await self.executor.execute_parallel(tasks=tasks, timeout=timeout_s)
        out: dict[str, Any] = {}
        for k, r in results.items():
            out[k] = r.result if r.success else None
        return out

