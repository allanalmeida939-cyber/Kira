"""
Boot Orchestrator central da Kira.

Mantem uma linha unica de estado para o boot do backend, LiveKit e worker,
separando readiness de infraestrutura do readiness de sessao.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class BootPhase(Enum):
    STARTING = "starting"
    BACKEND_STARTING = "backend_starting"
    BACKEND_READY = "backend_ready"
    LIVEKIT_STARTING = "livekit_starting"
    LIVEKIT_READY = "livekit_ready"
    AGENT_STARTING = "agent_starting"
    AGENT_READY = "agent_ready"
    SYSTEM_READY = "system_ready"
    SESSION_READY = "session_ready"
    FAILED = "failed"


_PHASE_ORDER = {
    BootPhase.STARTING: 0,
    BootPhase.BACKEND_STARTING: 10,
    BootPhase.BACKEND_READY: 20,
    BootPhase.LIVEKIT_STARTING: 30,
    BootPhase.LIVEKIT_READY: 40,
    BootPhase.AGENT_STARTING: 50,
    BootPhase.AGENT_READY: 60,
    BootPhase.SYSTEM_READY: 70,
    BootPhase.SESSION_READY: 80,
    BootPhase.FAILED: 999,
}


class BootOrchestrator:
    """
    Fonte unica de verdade para o boot.

    A fase principal avanca monotonicamente. Flags informam estado de cada
    subsistema sem competir com a fase oficial.
    """

    def __init__(self) -> None:
        self._phase = BootPhase.STARTING
        self._lock = threading.Lock()
        self._timestamps: dict[str, float] = {BootPhase.STARTING.value: time.time()}
        self._error: str | None = None
        self._started_at = time.time()
        self._flags = {
            "backend_http_ok": False,
            "livekit_server_ok": False,
            "agent_process_spawned": False,
            "agent_worker_ready": False,
            "memory_ready": False,
            "gemini_available": False,
        }

    def set_phase(self, phase: BootPhase, details: str = "") -> bool:
        with self._lock:
            if self._phase == BootPhase.FAILED and phase != BootPhase.FAILED:
                return False

            old_phase = self._phase
            old_rank = _PHASE_ORDER.get(old_phase, 0)
            new_rank = _PHASE_ORDER.get(phase, 0)
            if phase != BootPhase.FAILED and new_rank < old_rank:
                return False
            if phase == old_phase:
                return False

            self._phase = phase
            self._timestamps[phase.value] = time.time()
            elapsed = time.time() - self._started_at

        logger.info("=" * 80)
        logger.info("[BOOT] TRANSICAO: %s -> %s", old_phase.value, phase.value)
        logger.info("[BOOT] Tempo desde inicio: %.2fs", elapsed)
        if details:
            logger.info("[BOOT] Detalhes: %s", details)
        logger.info("=" * 80)
        return True

    def fail(self, error: str) -> None:
        with self._lock:
            self._phase = BootPhase.FAILED
            self._error = str(error or "Falha desconhecida no boot.")
            self._timestamps[BootPhase.FAILED.value] = time.time()
        logger.error("[BOOT] FALHA: %s", self._error)

    def set_flag(self, flag: str, value: bool) -> None:
        with self._lock:
            if flag not in self._flags:
                self._flags[flag] = bool(value)
            else:
                self._flags[flag] = bool(value)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            phase = self._phase
            elapsed = time.time() - self._started_at
            flags = dict(self._flags)
            error = self._error
            timestamps = dict(self._timestamps)

        memory_ready = flags.get("memory_ready", False)
        system_ready = phase in {BootPhase.SYSTEM_READY, BootPhase.SESSION_READY}
        functional_ready = phase == BootPhase.SESSION_READY
        can_start = system_ready and not error
        return {
            "phase": phase.value,
            "ready": phase != BootPhase.STARTING,
            "functional_ready": functional_ready,
            "can_start_session": can_start,
            "backend_ready": flags.get("backend_http_ok", False),
            "livekit_ready": flags.get("livekit_server_ok", False),
            "agent_ready": flags.get("agent_worker_ready", False),
            "agent_process_spawned": flags.get("agent_process_spawned", False),
            "memory_ready": memory_ready,
            "gemini_ready": flags.get("gemini_available", False),
            "uptime_seconds": elapsed,
            "error": error,
            "timestamps": timestamps,
        }

    def wait_for_phase(self, target: BootPhase, timeout: float = 60.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            state = self.get_state()
            if state["phase"] == target.value:
                return True
            if state["phase"] == BootPhase.FAILED.value:
                return False
            time.sleep(0.1)
        return False


boot_orchestrator = BootOrchestrator()
