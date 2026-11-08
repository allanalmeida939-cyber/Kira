"""Politica de reinicio do processo do agente: backoff curto e limite por janela."""

from __future__ import annotations

from collections import deque
from typing import Callable

BACKOFF_SECONDS = (2.0, 5.0, 10.0)
MAX_ATTEMPTS = 3
WINDOW_SECONDS = 600.0


class AgentRestartPolicy:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._attempts: deque[float] = deque()

    def _prune(self) -> None:
        now = self._clock()
        while self._attempts and now - self._attempts[0] >= WINDOW_SECONDS:
            self._attempts.popleft()

    def request(self) -> float | None:
        """Registra uma tentativa e devolve a espera em segundos, ou None se o limite estourou."""
        self._prune()
        if len(self._attempts) >= MAX_ATTEMPTS:
            return None
        delay = BACKOFF_SECONDS[min(len(self._attempts), len(BACKOFF_SECONDS) - 1)]
        self._attempts.append(self._clock())
        return delay

    @property
    def attempts(self) -> int:
        self._prune()
        return len(self._attempts)


__all__ = ["AgentRestartPolicy", "BACKOFF_SECONDS", "MAX_ATTEMPTS", "WINDOW_SECONDS"]
