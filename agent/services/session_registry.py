from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class ActiveSessionHandle:
    user_id: str
    session_id: str
    loop: asyncio.AbstractEventLoop
    speech_coordinator: object
    visual_runtime: object | None
    observer: object | None = None

    def visual_context_active(self) -> bool:
        observer = self.observer
        state = getattr(observer, "state", None)
        return bool(getattr(state, "visual_context_active", False))


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, ActiveSessionHandle] = {}

    def register(self, handle: ActiveSessionHandle) -> None:
        self._sessions[handle.user_id] = handle

    def unregister(self, *, user_id: str, session_id: str) -> None:
        active = self._sessions.get(user_id)
        if active is not None and active.session_id == session_id:
            self._sessions.pop(user_id, None)

    def get(self, user_id: str) -> ActiveSessionHandle | None:
        return self._sessions.get(user_id)

    def list_active_user_ids(self) -> list[str]:
        return sorted(self._sessions.keys())
