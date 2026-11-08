from __future__ import annotations

import asyncio
import logging
import os
import time

from core.realtime import KiraRealtimeSession
from resilience import log_exception_safely


logger = logging.getLogger(__name__)

GEMINI_HEARTBEAT_INTERVAL_SECONDS = max(
    float(os.getenv("GEMINI_HEARTBEAT_INTERVAL_SECONDS", "20")),
    5.0,
)
GEMINI_HEARTBEAT_IDLE_THRESHOLD_SECONDS = max(
    float(os.getenv("GEMINI_HEARTBEAT_IDLE_THRESHOLD_SECONDS", "12")),
    3.0,
)


def _observer_last_user_activity_at(observer) -> float:
    state = getattr(observer, "state", None)
    if callable(state):
        try:
            state = state()
        except Exception as exc:
            logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=heartbeat_observer_state_failed:%s", exc)
            return time.monotonic()
    try:
        return float(getattr(state, "last_user_activity_at", 0.0) or 0.0)
    except Exception as exc:
        logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=heartbeat_last_user_activity_failed:%s", exc)
        return time.monotonic()


async def gemini_live_heartbeat_loop(
    *,
    session,
    agent,
    observer,
    interval_seconds: float = GEMINI_HEARTBEAT_INTERVAL_SECONDS,
    idle_threshold_seconds: float = GEMINI_HEARTBEAT_IDLE_THRESHOLD_SECONDS,
) -> None:
    await asyncio.sleep(interval_seconds)

    while True:
        await asyncio.sleep(interval_seconds)

        if session.user_state == "speaking" or session.agent_state == "speaking":
            continue

        idle_for = time.monotonic() - _observer_last_user_activity_at(observer)
        if idle_for < idle_threshold_seconds:
            continue

        try:
            realtime_session = agent.realtime_llm_session
        except RuntimeError:
            continue

        if not isinstance(realtime_session, KiraRealtimeSession):
            continue

        try:
            sent = realtime_session.send_keepalive_audio()
        except Exception as exc:
            log_exception_safely(
                logger,
                "Falha ao enviar heartbeat do Gemini Live",
                exc,
                level=logging.WARNING,
            )
            continue

        if sent:
            logger.debug(
                "Heartbeat silencioso enviado para manter o Gemini Live ativo apos %.1fs de inatividade.",
                idle_for,
            )
