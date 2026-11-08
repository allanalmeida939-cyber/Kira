"""Pesquisa com estados reais: cada pedido tem requestId, seções que chegam conforme ficam prontas e cancelamento.

Um pedido novo cancela o anterior; eventos de pedidos antigos nunca chegam à interface.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from services.controllers.coordinator import ConversationPresentationCoordinator

logger = logging.getLogger("kira.app")
PUBLISH = Callable[[dict[str, object]], Awaitable[object]]
SECTION_TIMEOUT_SECONDS = 8.0
MAX_CONCURRENT = 3


@dataclass
class SectionPlan:
    """Uma seção da apresentação e a tarefa que produz seus dados ({"data": {...}, "sources": [...]})."""

    id: str
    type: str
    heading: str
    task: Callable[[], Awaitable[dict[str, object]]]
    media: bool = False


@dataclass
class ResearchResult:
    request_id: str
    status: str
    sections: dict[str, dict[str, object]] = field(default_factory=dict)
    sources: list[dict[str, object]] = field(default_factory=list)


class ResearchController:
    def __init__(
        self,
        publish: PUBLISH,
        coordinator: ConversationPresentationCoordinator | None = None,
        *,
        timeout: float = SECTION_TIMEOUT_SECONDS,
        concurrency: int = MAX_CONCURRENT,
    ) -> None:
        self._publish = publish
        self._coordinator = coordinator or ConversationPresentationCoordinator()
        self._timeout = timeout
        self._concurrency = concurrency
        self._current: asyncio.Task[ResearchResult] | None = None
        self._current_id = ""
        self._stopped: set[asyncio.Task[ResearchResult]] = set()

    @property
    def coordinator(self) -> ConversationPresentationCoordinator:
        return self._coordinator

    async def _send(self, request_id: str, action: dict[str, object]) -> None:
        if request_id != self._current_id:
            logger.info("PRESENTATION_EVENT_DROPPED request_id=%s reason=superseded", request_id)
            return
        if not self._coordinator.accepts(request_id):
            logger.info("PRESENTATION_EVENT_DROPPED request_id=%s reason=old_turn current_turn=%s", request_id, self._coordinator.turn_id)
            return
        await self._publish(action)

    async def cancel(self) -> None:
        task = self._current
        if task is not None and not task.done():
            self._stopped.add(task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(
        self, *, presentation_id: str, title: str, sections: list[SectionPlan], turn_id: str | None = None, mode: str | None = None,
    ) -> ResearchResult:
        await self.cancel()
        request_id = f"rq-{uuid.uuid4().hex[:12]}"
        self._current_id = request_id
        if turn_id:
            # O pedido traz o turno em que foi feito: ele é o turno atual (não depende do aviso de turno chegar antes).
            self._coordinator.begin_turn(turn_id)
        self._coordinator.attach(request_id, presentation_id, turn_id)
        task = asyncio.ensure_future(self._execute(request_id, presentation_id, title, sections, turn_id, mode))
        self._current = task
        try:
            return await task
        except asyncio.CancelledError:
            if task in self._stopped:
                return ResearchResult(request_id, "cancelled")
            raise
        finally:
            self._stopped.discard(task)

    async def _execute(
        self, request_id: str, presentation_id: str, title: str, sections: list[SectionPlan], turn_id: str | None, mode: str | None = None,
    ) -> ResearchResult:
        base = {"presentationId": presentation_id, "requestId": request_id}
        result = ResearchResult(request_id, "preparing_query")
        present: dict[str, object] = {
            "type": "present", **base, "title": title, "status": "preparing_query",
            "sections": [{"id": s.id, "type": s.type, "heading": s.heading, "status": "pending"} for s in sections],
            "sources": [],
        }
        if turn_id:
            present["turnId"] = turn_id
        if mode:
            present["mode"] = mode
        try:
            await self._send(request_id, present)
            await self._send(request_id, {"type": "present_patch", **base, "status": "researching"})
            limit = asyncio.Semaphore(self._concurrency)
            media_announced = False

            async def one(plan: SectionPlan) -> None:
                nonlocal media_announced
                async with limit:
                    if plan.media and not media_announced:
                        media_announced = True
                        await self._send(request_id, {"type": "present_patch", **base, "status": "collecting_media"})
                    try:
                        produced = await asyncio.wait_for(plan.task(), timeout=self._timeout)
                        section = {"id": plan.id, "status": "ready", "data": dict(produced.get("data") or {})}
                        new_sources = [s for s in produced.get("sources") or [] if isinstance(s, dict)]
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        section, new_sources = {"id": plan.id, "status": "unavailable", "reason": "a fonte demorou demais"}, []
                        logger.info("RESEARCH_SECTION_UNAVAILABLE id=%s error=timeout", plan.id)
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "fonte indisponível"
                        section, new_sources = {"id": plan.id, "status": "unavailable", "reason": reason[:200]}, []
                        logger.info("RESEARCH_SECTION_UNAVAILABLE id=%s error=%s detail=%s", plan.id, type(exc).__name__, str(exc)[:120])
                result.sections[plan.id] = section
                known = {s["id"] for s in result.sources}
                result.sources.extend(s for s in new_sources if s.get("id") not in known)
                patch: dict[str, object] = {"type": "present_patch", **base, "sections": [section]}
                if new_sources:
                    patch["sources"] = list(result.sources)
                await self._send(request_id, patch)

            await asyncio.gather(*(one(plan) for plan in sections))
            await self._send(request_id, {"type": "present_patch", **base, "status": "organizing"})
            ready = [s for s in result.sections.values() if s["status"] == "ready"]
            result.status = "ready" if len(ready) == len(sections) else ("partial" if ready else "error")
            await self._send(request_id, {"type": "present_patch", **base, "status": result.status})
            return result
        except asyncio.CancelledError:
            result.status = "cancelled"
            if request_id == self._current_id:
                await self._publish({"type": "present_patch", **base, "status": "cancelled"})
            raise

    async def narrate(self, presentation_id: str, request_id: str, section_id: str, *, user_request: bool = False) -> bool:
        """Destaca a seção que a Kira está narrando; nada acontece depois de interrupção ou com pedido antigo,
        a menos que o próprio usuário peça para continuar (user_request)."""
        if user_request:
            self._coordinator.reopen(request_id)
        if not self._coordinator.can_narrate(request_id):
            return False
        await self._publish({"type": "present_patch", "presentationId": presentation_id, "requestId": request_id, "activeSectionId": section_id})
        return True

    async def interrupt(self, turn_id: str | None) -> None:
        """Interrupção da fala: congela a narração do turno e cancela a pesquisa dele se ainda estiver rodando."""
        affected = self._coordinator.interrupted(turn_id)
        if self._current_id in affected:
            await self.cancel()
