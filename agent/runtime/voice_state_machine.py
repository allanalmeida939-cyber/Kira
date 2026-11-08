from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    READY_TO_SPEAK = "ready_to_speak"
    SPEAKING = "speaking"
    FINISHING = "finishing"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


VALID_TRANSITIONS: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE: {
        VoiceState.LISTENING,
        VoiceState.THINKING,
        VoiceState.READY_TO_SPEAK,
        VoiceState.FAILED,
        VoiceState.INTERRUPTED,
    },
    VoiceState.LISTENING: {
        VoiceState.IDLE,
        VoiceState.THINKING,
        VoiceState.FAILED,
        VoiceState.INTERRUPTED,
    },
    VoiceState.THINKING: {
        VoiceState.IDLE,
        VoiceState.READY_TO_SPEAK,
        VoiceState.FAILED,
        VoiceState.INTERRUPTED,
    },
    VoiceState.READY_TO_SPEAK: {
        VoiceState.IDLE,
        VoiceState.SPEAKING,
        VoiceState.FAILED,
        VoiceState.INTERRUPTED,
    },
    VoiceState.SPEAKING: {
        VoiceState.IDLE,
        VoiceState.FINISHING,
        VoiceState.FAILED,
        VoiceState.INTERRUPTED,
    },
    VoiceState.FINISHING: {
        VoiceState.IDLE,
        VoiceState.LISTENING,
        VoiceState.FAILED,
    },
    VoiceState.FAILED: {
        VoiceState.IDLE,
        VoiceState.LISTENING,
        VoiceState.THINKING,
    },
    VoiceState.INTERRUPTED: {
        VoiceState.IDLE,
        VoiceState.LISTENING,
        VoiceState.THINKING,
    },
}


@dataclass(frozen=True, slots=True)
class VoiceStateTransition:
    previous_state: VoiceState
    current_state: VoiceState
    reason: str
    changed_at: float
    age_seconds: float


class VoiceStateMachine:
    def __init__(self, *, initial_state: VoiceState = VoiceState.IDLE, name: str = "kira-voice") -> None:
        self._state = initial_state
        now = time.monotonic()
        self._state_started_at = now
        self._last_transition_at = now
        self._name = str(name or "kira-voice")
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[VoiceStateTransition], Awaitable[None] | None]] = []

    @property
    def current_state(self) -> VoiceState:
        return self._state

    @property
    def state_age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._state_started_at)

    def add_listener(
        self,
        listener: Callable[[VoiceStateTransition], Awaitable[None] | None],
    ) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    async def transition_to(
        self,
        new_state: VoiceState,
        *,
        reason: str,
        force: bool = False,
    ) -> VoiceStateTransition:
        async with self._lock:
            previous_state = self._state
            if previous_state == new_state:
                transition = VoiceStateTransition(
                    previous_state=previous_state,
                    current_state=new_state,
                    reason=str(reason or "noop"),
                    changed_at=self._last_transition_at,
                    age_seconds=self.state_age_seconds,
                )
                logger.debug(
                    "[STATE] noop state=%s reason=%s name=%s",
                    new_state.value,
                    transition.reason,
                    self._name,
                )
                return transition

            if not force and new_state not in VALID_TRANSITIONS.get(previous_state, set()):
                raise ValueError(
                    f"invalid transition {previous_state.value} -> {new_state.value} reason={reason}"
                )

            now = time.monotonic()
            age_seconds = max(0.0, now - self._state_started_at)
            self._state = new_state
            self._state_started_at = now
            self._last_transition_at = now
            transition = VoiceStateTransition(
                previous_state=previous_state,
                current_state=new_state,
                reason=str(reason or "unspecified"),
                changed_at=now,
                age_seconds=age_seconds,
            )

        if force:
            logger.warning(
                "[STATE] %s -> %s reason=%s forced=true age_s=%.2f name=%s",
                transition.previous_state.value,
                transition.current_state.value,
                transition.reason,
                transition.age_seconds,
                self._name,
            )
        else:
            logger.info(
                "[STATE] %s -> %s reason=%s age_s=%.2f name=%s",
                transition.previous_state.value,
                transition.current_state.value,
                transition.reason,
                transition.age_seconds,
                self._name,
            )

        await self._notify_listeners(transition)
        return transition

    async def force_idle(self, *, reason: str) -> VoiceStateTransition:
        return await self.transition_to(VoiceState.IDLE, reason=reason, force=True)

    async def _notify_listeners(self, transition: VoiceStateTransition) -> None:
        for listener in list(self._listeners):
            try:
                maybe_result = listener(transition)
                if inspect.isawaitable(maybe_result):
                    await maybe_result
            except Exception as exc:
                logger.debug(
                    "[STATE] listener_failed name=%s reason=%s detail=%s",
                    self._name,
                    transition.reason,
                    exc,
                )

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self._name,
            "state": self._state.value,
            "state_age_seconds": round(self.state_age_seconds, 3),
            "last_transition_at_monotonic": round(self._last_transition_at, 3),
        }
