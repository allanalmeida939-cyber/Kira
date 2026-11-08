from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreventiveRotationStatus:
    enabled: bool = False
    active: bool = False
    epoch: int = 0
    next_at: float = 0.0
    last_error: str = ""
    mode: str = "idle_recreate"


class GeminiPreventiveRotation:
    def __init__(
        self,
        *,
        config_loader: Callable[[], dict[str, object]],
        runtime_status_provider: Callable[[], dict[str, object]],
        restart_callback: Callable[[str], Awaitable[bool]],
        poll_seconds: float = 10.0,
    ) -> None:
        self._config_loader = config_loader
        self._runtime_status_provider = runtime_status_provider
        self._restart_callback = restart_callback
        self._poll_seconds = max(float(poll_seconds), 1.0)
        self._task: asyncio.Task | None = None
        self._status = PreventiveRotationStatus()
        self._session_started_at = time.time()
        self._last_seen_epoch = 0
        self._retry_after = 0.0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="kira-gemini-preventive-rotation")
        logger.info("PREVENTIVE_ROTATION_SCHEDULER_START")
        logger.warning("PREVENTIVE_ROTATION_PARALLEL_NOT_SUPPORTED")
        logger.warning("PREVENTIVE_ROTATION_USING_IDLE_RECREATE_MODE")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def get_status(self) -> dict[str, object]:
        return {
            "preventive_rotation_enabled": bool(self._status.enabled),
            "preventive_rotation_active": bool(self._status.active),
            "preventive_rotation_epoch": int(self._status.epoch),
            "preventive_rotation_next_at": float(self._status.next_at),
            "preventive_rotation_last_error": self._status.last_error,
            "preventive_rotation_mode": self._status.mode,
        }

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._poll_seconds)
                await self._tick()
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        config = dict(self._config_loader() or {})
        enabled = bool(config.get("gemini_preventive_rotation_enabled", False))
        self._status.enabled = enabled
        if not enabled:
            self._status.active = False
            return

        runtime = dict(self._runtime_status_provider() or {})
        epoch = int(runtime.get("live_session_epoch") or runtime.get("gemini_active_epoch") or 0)
        self._status.epoch = epoch
        if epoch != self._last_seen_epoch:
            self._last_seen_epoch = epoch
            self._session_started_at = time.time()

        after_minutes = max(int(config.get("gemini_preventive_rotation_after_minutes", 30) or 30), 1)
        retry_minutes = max(int(config.get("gemini_preventive_rotation_retry_after_minutes", 5) or 5), 1)
        eligible_at = self._session_started_at + after_minutes * 60
        self._status.next_at = max(eligible_at, self._retry_after)
        now = time.time()
        if now < self._status.next_at:
            return

        logger.warning(
            "PREVENTIVE_ROTATION_ELIGIBLE epoch=%s age_s=%.0f",
            epoch,
            now - self._session_started_at,
        )
        if not self._safe_to_rotate(runtime, config):
            logger.warning("PREVENTIVE_ROTATION_WAITING_SAFE_WINDOW")
            return

        self._status.active = True
        logger.warning("PREVENTIVE_ROTATION_BEGIN mode=idle_recreate")
        logger.warning("PREVENTIVE_ROTATION_CONTEXT_BUILT mode=idle_recreate")
        logger.warning("PREVENTIVE_ROTATION_B_CREATE_BEGIN mode=idle_recreate")
        logger.warning("PREVENTIVE_ROTATION_B_READY mode=idle_recreate")
        logger.warning("PREVENTIVE_ROTATION_B_WARMUP_BEGIN mode=idle_recreate")
        before = epoch
        try:
            ok = await self._restart_callback("preventive_rotation_idle_recreate")
        except Exception as exc:
            ok = False
            self._status.last_error = str(exc or exc.__class__.__name__)
        if ok:
            after = before + 1
            try:
                after_runtime = dict(self._runtime_status_provider() or {})
                after = int(after_runtime.get("live_session_epoch") or after_runtime.get("gemini_active_epoch") or after)
            except Exception:
                pass
            self._status.epoch = after
            self._session_started_at = time.time()
            self._retry_after = 0.0
            self._status.last_error = ""
            logger.warning("PREVENTIVE_ROTATION_B_WARMUP_OK")
            logger.warning("PREVENTIVE_ROTATION_SWITCH_BEGIN mode=idle_recreate")
            logger.warning("PREVENTIVE_ROTATION_SWITCHED epoch_before=%s epoch_after=%s", before, after)
        else:
            self._retry_after = time.time() + retry_minutes * 60
            logger.warning("PREVENTIVE_ROTATION_B_WARMUP_FAILED reason=%s", self._status.last_error or "restart_failed")
            logger.warning("PREVENTIVE_ROTATION_ABORTED reason=idle_recreate_failed")
            logger.warning("PREVENTIVE_ROTATION_RETRY_SCHEDULED retry_after=%s", int(self._retry_after))
        self._status.next_at = max(self._session_started_at + after_minutes * 60, self._retry_after)
        self._status.active = False

    def _safe_to_rotate(self, runtime: dict[str, object], config: dict[str, object]) -> bool:
        if bool(runtime.get("gemini_restart_in_progress")):
            return False
        if bool(runtime.get("warmup_required")) or bool(runtime.get("post_restart_warmup_in_progress")):
            return False
        if bool(runtime.get("economy_standby")):
            return False
        if str(runtime.get("voice_output_state") or "").strip().lower() not in {"", "idle"}:
            return False
        if bool(runtime.get("voice_output_speaking")):
            return False
        idle_seconds = float(runtime.get("idle_seconds") or 0.0)
        standby_minutes = float(config.get("gemini_standby_after_minutes") or 0.0)
        if standby_minutes > 0 and idle_seconds >= standby_minutes * 60 * 0.8:
            return False
        return True
