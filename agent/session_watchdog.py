from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable


logger = logging.getLogger(__name__)

GEMINI_SILENCE_THRESHOLD_SECONDS = 10.0
PLANNED_RECONNECT_GRACE_SECONDS = 30.0
EVENT_LOOP_LAG_THRESHOLD_MS = 500.0


def classify_gemini_silence(
    *,
    elapsed_s: float,
    planned_reconnect: bool,
    reconnect_elapsed_s: float,
    event_loop_lag_ms: float = 0.0,
    event_loop_lag_age_s: float = float("inf"),
) -> str | None:
    """Classifica um silêncio sem esconder reconexão nem travamento do loop."""
    if float(elapsed_s) <= GEMINI_SILENCE_THRESHOLD_SECONDS:
        return None
    if planned_reconnect and float(reconnect_elapsed_s) < PLANNED_RECONNECT_GRACE_SECONDS:
        return "planned_reconnect"
    if (
        float(event_loop_lag_ms) > EVENT_LOOP_LAG_THRESHOLD_MS
        and float(event_loop_lag_age_s) <= PLANNED_RECONNECT_GRACE_SECONDS
    ):
        return "event_loop_lag"
    return "gemini_silence"


@dataclass(frozen=True)
class SessionCloseClassification:
    recoverable: bool
    reason: str
    has_explicit_error: bool


def _normalize_close_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_error_text(value: object) -> str:
    text = _normalize_close_text(value)
    if text.lower() in {"", "none", "null"}:
        return ""
    return text


def classify_session_close(reason: object = "", error: object = "") -> SessionCloseClassification:
    normalized_reason = _normalize_close_text(reason)
    normalized_error = _normalize_error_text(error)
    reason_upper = normalized_reason.upper()
    is_participant_disconnect = "PARTICIPANT_DISCONNECTED" in reason_upper
    if is_participant_disconnect and not normalized_error:
        logger.warning("PARTICIPANT_DISCONNECT_NO_ERROR_DETECTED reason=%s", normalized_reason or "unknown")
        logger.info(
            "SESSION_CLOSE_CLASSIFIED recoverable=true reason=participant_disconnected_no_error"
        )
        logger.info("SESSION_CLOSE_RECOVERABLE reason=participant_disconnected_no_error")
        return SessionCloseClassification(
            recoverable=True,
            reason="participant_disconnected_no_error",
            has_explicit_error=False,
        )

    fatal_reason = "participant_disconnected_with_error" if is_participant_disconnect else "session_close"
    logger.info(
        "SESSION_CLOSE_CLASSIFIED recoverable=false reason=%s explicit_error=%s",
        fatal_reason,
        str(bool(normalized_error)).lower(),
    )
    logger.warning("SESSION_CLOSE_FATAL reason=%s", fatal_reason)
    return SessionCloseClassification(
        recoverable=False,
        reason=fatal_reason,
        has_explicit_error=bool(normalized_error),
    )


class SessionWatchdog:
    """
    Monitora a sessao ativa e aciona um callback de recovery quando ela morre.
    """

    CHECK_INTERVAL_SECONDS = 10
    MAX_RESTART_ATTEMPTS = 5
    RESTART_COOLDOWN_SECONDS = 30

    def __init__(
        self,
        *,
        session_ref: Callable[[], object | None],
        recovery_callback: Callable[[str], Awaitable[None] | None] | None = None,
        status_callback: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self.session_ref = session_ref
        self.recovery_callback = recovery_callback
        self.status_callback = status_callback
        self.is_running = False
        self.is_alive = False
        self.last_check: datetime | None = None
        self.restart_count = 0
        self.last_restart: datetime | None = None
        self.last_close_reason = ""
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._recovery_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._watchdog_loop(), name="kira-session-watchdog")
        logger.info("[WATCHDOG] Iniciado")

    async def stop(self) -> None:
        self.is_running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("[WATCHDOG] Parado")

    def register_close_event(self, *, reason: str = "", error: str = "") -> None:
        self.last_close_reason = str(reason or "").strip()
        self.last_error = str(error or "").strip()
        self.is_alive = False
        logger.error(
            "[WATCHDOG] close_event_detectado reason=%s error=%s",
            self.last_close_reason or "unknown",
            self.last_error or "none",
        )

    def _is_session_alive(self) -> bool:
        session = self.session_ref()
        if session is None:
            return False
        if hasattr(session, "closed") and bool(getattr(session, "closed")):
            return False
        if hasattr(session, "is_running") and not bool(getattr(session, "is_running")):
            return False
        return True

    async def _watchdog_loop(self) -> None:
        while self.is_running:
            try:
                await self._check_and_recover()
            except Exception as exc:
                logger.error("[WATCHDOG] Erro no loop: %s", exc)
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)

    async def _check_and_recover(self) -> None:
        self.last_check = datetime.now()
        self.is_alive = self._is_session_alive()
        if self.is_alive:
            return
        await self.request_recovery(
            reason=self.last_close_reason or self.last_error or "session_dead",
        )

    async def request_recovery(self, *, reason: str) -> None:
        async with self._recovery_lock:
            if self.restart_count >= self.MAX_RESTART_ATTEMPTS:
                logger.critical(
                    "[WATCHDOG] Limite de recovery atingido attempts=%s/%s reason=%s",
                    self.restart_count,
                    self.MAX_RESTART_ATTEMPTS,
                    reason,
                )
                return
            if self.last_restart is not None:
                elapsed = (datetime.now() - self.last_restart).total_seconds()
                if elapsed < self.RESTART_COOLDOWN_SECONDS:
                    logger.info(
                        "[WATCHDOG] Cooldown de recovery ativo elapsed=%.1fs/%ss reason=%s",
                        elapsed,
                        self.RESTART_COOLDOWN_SECONDS,
                        reason,
                    )
                    return

            logger.warning(
                "[WATCHDOG] Recovery solicitado attempt=%s/%s reason=%s",
                self.restart_count + 1,
                self.MAX_RESTART_ATTEMPTS,
                reason,
            )
            if self.status_callback is not None:
                try:
                    maybe_result = self.status_callback("session_recovering")
                    if asyncio.iscoroutine(maybe_result):
                        await maybe_result
                except Exception as exc:
                    logger.warning("[WATCHDOG] Falha ao publicar status session_recovering: %s", exc)

            self.restart_count += 1
            self.last_restart = datetime.now()
            if self.recovery_callback is None:
                return
            maybe_result = self.recovery_callback(reason)
            if asyncio.iscoroutine(maybe_result):
                await maybe_result

    def get_status(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "is_alive": self.is_alive,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "restart_count": self.restart_count,
            "last_restart": self.last_restart.isoformat() if self.last_restart else None,
            "last_close_reason": self.last_close_reason,
            "last_error": self.last_error,
        }
