from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable


logger = logging.getLogger(__name__)


class HealthCheck:
    """
    Faz verificacoes periodicas simples e aciona recovery se entrar em estado critico.
    """

    CHECK_INTERVAL = 30

    def __init__(
        self,
        *,
        session_ref: Callable[[], object | None],
        key_manager,
        watchdog=None,
        room_connected_ref: Callable[[], bool] | None = None,
        status_ref: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.session_ref = session_ref
        self.key_manager = key_manager
        self.watchdog = watchdog
        self.room_connected_ref = room_connected_ref
        self.status_ref = status_ref
        self.last_check: datetime | None = None
        self.health_status: dict[str, object] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop(), name="kira-health-check")
        logger.info("[HEALTH] Iniciado intervalo=%ss", self.CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _check_loop(self) -> None:
        while self._running:
            try:
                await self._do_check()
            except Exception as exc:
                logger.error("[HEALTH] Erro no loop: %s", exc)
            await asyncio.sleep(self.CHECK_INTERVAL)

    def _check_session_running(self) -> bool:
        session = self.session_ref()
        if session is None:
            return False
        if hasattr(session, "closed") and bool(getattr(session, "closed")):
            return False
        if hasattr(session, "is_running"):
            return bool(getattr(session, "is_running"))
        return True

    def _check_room_connected(self) -> bool:
        session = self.session_ref()
        if session is None:
            return False

        room_io = getattr(session, "_room_io", None) or getattr(session, "room_io", None)
        if room_io is not None:
            room = getattr(room_io, "room", None)
            if self._room_is_connected(room):
                return True
            linked_participant = getattr(room_io, "linked_participant", None)
            if linked_participant is not None and self._check_mic_subscribed():
                return True

        room = getattr(session, "room", None)
        if self._room_is_connected(room):
            return True

        if bool(getattr(session, "_started", False)) and bool(getattr(session, "_room_connected", False)):
            return True

        if self.room_connected_ref is not None:
            try:
                return bool(self.room_connected_ref())
            except Exception:
                return False

        return False

    def _room_is_connected(self, room) -> bool:
        if room is None:
            return False

        connection_state = str(getattr(room, "connection_state", "") or "").strip().lower()
        if connection_state and "disconnected" not in connection_state and "connected" in connection_state:
            return True

        is_connected = getattr(room, "isconnected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return False

    def _check_participant_linked(self) -> bool:
        session = self.session_ref()
        if session is None:
            return False

        room_io = getattr(session, "_room_io", None) or getattr(session, "room_io", None)
        if room_io is None:
            return False

        linked_participant = getattr(room_io, "linked_participant", None)
        if linked_participant is not None:
            return True

        participant_identity = str(getattr(room_io, "_participant_identity", "") or "").strip()
        return bool(participant_identity)

    def _check_mic_subscribed(self) -> bool:
        session = self.session_ref()
        if session is None:
            return False

        room_io = getattr(session, "_room_io", None) or getattr(session, "room_io", None)
        if room_io is None:
            return False

        subscribed_ready = getattr(room_io, "subscribed_ready", None)
        if subscribed_ready is not None:
            return bool(subscribed_ready)

        subscribed_fut = getattr(room_io, "subscribed_fut", None)
        if subscribed_fut is None:
            return False

        try:
            return bool(subscribed_fut.done())
        except Exception:
            return False

    def _check_tts_available(self) -> bool:
        try:
            status = self.key_manager.get_status_report()
        except Exception:
            return False
        return bool(status.get("available_tts_keys", 0))

    def _check_key_available(self) -> bool:
        try:
            status = self.key_manager.get_status_report()
        except Exception:
            return False
        return bool(status.get("available_text_keys", 0) or status.get("available_tts_keys", 0))

    async def _do_check(self) -> None:
        self.last_check = datetime.now()
        session_running = self._check_session_running()
        room_connected = self._check_room_connected()
        participant_linked = self._check_participant_linked()
        mic_subscribed = self._check_mic_subscribed()
        tts_available = self._check_tts_available()
        key_available = self._check_key_available()
        results = {
            "timestamp": self.last_check.isoformat(),
            "session_running": session_running,
            "room_connected": room_connected,
            "participant_linked": participant_linked,
            "mic_subscribed": mic_subscribed,
            "tts_available": tts_available,
            "key_available": key_available,
            "overall": "healthy",
        }
        if self.status_ref is not None:
            try:
                details = self.status_ref()
            except Exception:
                details = {}
            if isinstance(details, dict):
                results["details"] = details
                results.update(
                    {
                        key: value
                        for key, value in details.items()
                        if key not in results
                    }
                )
            else:
                results["details"] = {}

        if not session_running or not key_available or not room_connected:
            results["overall"] = "critical"
        elif not participant_linked or not mic_subscribed or not tts_available:
            results["overall"] = "degraded"

        self.health_status = results
        if results["overall"] != "healthy":
            logger.warning("[HEALTH] Status=%s details=%s", results["overall"], results)
        else:
            logger.debug("[HEALTH] Status=healthy")

        if results["overall"] == "critical":
            await self._trigger_recovery()

    async def _trigger_recovery(self) -> None:
        logger.warning("[HEALTH] Estado critico; acionando watchdog.")
        if self.watchdog is not None and hasattr(self.watchdog, "request_recovery"):
            await self.watchdog.request_recovery(reason="health_check_critical")

    def get_status(self) -> dict[str, object]:
        return dict(self.health_status)
