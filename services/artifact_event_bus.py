from __future__ import annotations

import asyncio
from typing import Any


class ArtifactEventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    async def connect(self, queue: asyncio.Queue) -> None:
        self._subscribers.add(queue)

    async def disconnect(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def publish(self, payload: dict[str, Any]) -> None:
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)


artifact_event_bus = ArtifactEventBus()
