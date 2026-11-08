"""
Runtime Status Manager — Fonte única da verdade sobre o estado da Kira.

Centraliza todos os estados de prontidão (backend, livekit, worker, room,
gemini, voice) e fornece:
  - Snapshot em /api/runtime-status
  - Stream em /ws/runtime-status

A UI NÃO deve calcular readiness. Ela apenas renderiza o que vier daqui.

NOTA: este módulo é ADITIVO. Endpoints antigos (/api/ready, /api/agent/status,
/api/boot/state) continuam funcionando normalmente. Esta é uma camada nova
em paralelo, que pode ser adotada gradualmente pela UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from threading import Lock

from services.kira_feature_flags import get_optional_modules


class RuntimePhase(str, Enum):
    BOOTING = "BOOTING"
    MINIMAL_READY = "MINIMAL_READY"
    SESSION_STARTING = "SESSION_STARTING"
    ROOM_CONNECTED = "ROOM_CONNECTED"
    GEMINI_READY = "GEMINI_READY"
    VOICE_READY = "VOICE_READY"
    FUNCTIONAL_READY = "FUNCTIONAL_READY"
    ERROR = "ERROR"


class VoiceStatus(str, Enum):
    STANDBY = "standby"
    INITIALIZING = "initializing"
    ONLINE = "online"
    ERROR = "error"


class BrainStatus(str, Enum):
    STANDBY = "standby"
    CONNECTING = "connecting"
    ONLINE = "online"
    ERROR = "error"


@dataclass
class RuntimeStatusSnapshot:
    phase: str = RuntimePhase.BOOTING.value
    backend_ready: bool = False
    livekit_ready: bool = False
    worker_ready: bool = False
    memory_ready: bool = False
    memory_status: str = "unknown"
    memory_error: Optional[str] = None
    memory_checked_at: Optional[float] = None
    memory_elapsed_ms: Optional[int] = None
    room_ready: bool = False
    gemini_ready: bool = False
    voice_pipeline_ready: bool = False
    runtime_ready: bool = False
    functional_ready: bool = False
    can_start_session: bool = False
    recovery_active: bool = False
    agent_process_alive: bool = True
    voice_status: str = VoiceStatus.STANDBY.value
    brain_status: str = BrainStatus.STANDBY.value
    ui_message: str = "Aguardando sistema"
    fatal_error: bool = False
    error_message: Optional[str] = None
    source: str = "backend"
    core_ready: bool = False
    optional_status: Dict[str, Any] = field(default_factory=dict)
    internet_enabled: bool = True
    internet_runtime_state: str = "standby"
    internet_ai_available: bool = False
    internet_active_key_index: Optional[int] = None
    internet_active_key_email: str = ""
    internet_model: str = "gemini-2.5-flash"
    internet_headless: bool = True
    last_error_masked: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RuntimeStatusManager:
    """Mantém o estado oficial e notifica subscribers."""

    def __init__(self) -> None:
        self._snapshot = RuntimeStatusSnapshot()
        self._lock = Lock()
        self._subscribers: Set[asyncio.Queue] = set()
        self._sub_lock = Lock()

    # ---------- mutadores ----------
    def set_backend_ready(self, value: bool = True) -> None:
        self._update(backend_ready=value)

    def set_livekit_ready(self, value: bool = True) -> None:
        self._update(livekit_ready=value)

    def set_worker_ready(self, value: bool = True) -> None:
        self._update(worker_ready=value)

    def set_memory_ready(
        self,
        value: bool = True,
        *,
        status: str | None = None,
        error: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        self._update(
            memory_ready=value,
            memory_status=status or ("ready" if value else "failed"),
            memory_error=error,
            memory_checked_at=time.time(),
            memory_elapsed_ms=elapsed_ms,
        )

    def set_room_ready(self, value: bool = True) -> None:
        self._update(room_ready=value)

    def set_gemini_ready(self, value: bool = True) -> None:
        self._update(gemini_ready=value)

    def set_voice_pipeline_ready(self, value: bool = True) -> None:
        self._update(voice_pipeline_ready=value)

    def set_runtime_ready(self, value: bool = True) -> None:
        self._update(runtime_ready=value)

    def set_functional_ready(self, value: bool = True) -> None:
        self._update(functional_ready=value)

    def set_fatal_error(self, message: str) -> None:
        self._update(fatal_error=True, error_message=message)

    def clear_error(self) -> None:
        self._update(fatal_error=False, error_message=None)

    def reset_session_state(self) -> None:
        """Reseta apenas estados de sessão (mantém backend/livekit/worker)."""
        self._update(
            room_ready=False,
            gemini_ready=False,
            voice_pipeline_ready=False,
            runtime_ready=False,
            functional_ready=False,
        )

    # ---------- núcleo ----------
    def _update(self, **changes: Any) -> None:
        changed = False
        with self._lock:
            for key, value in changes.items():
                if hasattr(self._snapshot, key) and getattr(self._snapshot, key) != value:
                    setattr(self._snapshot, key, value)
                    changed = True
            if changed:
                self._snapshot.timestamp = time.time()
                self._recompute_derived_locked()
                payload = self._snapshot.to_dict()
            else:
                payload = None
        if payload is not None:
            self._broadcast(payload)

    def _recompute_derived_locked(self) -> None:
        s = self._snapshot
        session_ready = (
            s.room_ready
            and s.gemini_ready
            and s.voice_pipeline_ready
            and s.runtime_ready
        )
        s.core_ready = bool(
            s.backend_ready
            and s.livekit_ready
            and s.worker_ready
            and session_ready
        )
        s.optional_status = {
            name: {
                "enabled": bool(cfg.get("enabled")),
                "autoload": bool(cfg.get("autoload")),
                "show_when_disabled": bool(cfg.get("show_when_disabled")),
                "status": "disabled" if not cfg.get("enabled") else "available",
                "critical": False,
            }
            for name, cfg in get_optional_modules().items()
        }
        self._refresh_internet_status_locked()
        if s.functional_ready and not session_ready:
            s.functional_ready = False

        # Fase
        if s.fatal_error:
            s.phase = RuntimePhase.ERROR.value
        elif s.functional_ready:
            s.phase = RuntimePhase.FUNCTIONAL_READY.value
        elif s.voice_pipeline_ready:
            s.phase = RuntimePhase.VOICE_READY.value
        elif s.gemini_ready:
            s.phase = RuntimePhase.GEMINI_READY.value
        elif s.room_ready:
            s.phase = RuntimePhase.ROOM_CONNECTED.value
        elif s.backend_ready and s.livekit_ready and s.worker_ready:
            s.phase = RuntimePhase.MINIMAL_READY.value
        else:
            s.phase = RuntimePhase.BOOTING.value

        # Pode iniciar sessão?
        s.can_start_session = (
            s.backend_ready
            and s.livekit_ready
            and s.worker_ready
            and not s.fatal_error
        )

        # Voz
        if s.fatal_error:
            s.voice_status = VoiceStatus.ERROR.value
        elif s.voice_pipeline_ready:
            s.voice_status = VoiceStatus.ONLINE.value
        elif s.room_ready or s.gemini_ready:
            s.voice_status = VoiceStatus.INITIALIZING.value
        else:
            s.voice_status = VoiceStatus.STANDBY.value

        # Cérebro
        if s.fatal_error:
            s.brain_status = BrainStatus.ERROR.value
        elif s.gemini_ready:
            s.brain_status = BrainStatus.ONLINE.value
        elif s.room_ready:
            s.brain_status = BrainStatus.CONNECTING.value
        else:
            s.brain_status = BrainStatus.STANDBY.value

        # Mensagem
        if s.fatal_error:
            s.ui_message = f"Erro: {s.error_message or 'erro desconhecido'}"
        elif s.functional_ready:
            s.ui_message = "Kira pronta"
        elif s.voice_pipeline_ready:
            s.ui_message = "Voz online"
        elif s.gemini_ready:
            s.ui_message = "Cérebro conectado"
        elif s.room_ready:
            s.ui_message = "Sala conectada"
        elif s.can_start_session:
            s.ui_message = "Sistema pronto. Clique para iniciar sessão."
        else:
            missing = []
            if not s.backend_ready:
                missing.append("backend")
            if not s.livekit_ready:
                missing.append("LiveKit")
            if not s.worker_ready:
                missing.append("worker")
            s.ui_message = f"Aguardando: {', '.join(missing) if missing else 'sistema'}"

    # ---------- consulta ----------
    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_internet_status_locked()
            return self._snapshot.to_dict()

    def _refresh_internet_status_locked(self) -> None:
        s = self._snapshot
        internet_cfg = s.optional_status.get("internet") if isinstance(s.optional_status, dict) else {}
        s.internet_enabled = bool(internet_cfg.get("enabled", True)) if isinstance(internet_cfg, dict) else True
        s.internet_runtime_state = "disabled" if not s.internet_enabled else (s.internet_runtime_state or "standby")
        s.internet_model = str(os.getenv("GEMINI_INTERNET_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash").strip()
        s.internet_headless = str(os.getenv("KIRA_INTERNET_HEADLESS", os.getenv("INTERNET_HEADLESS", "true"))).strip().lower() in {
            "1",
            "true",
            "yes",
            "sim",
            "on",
        }
        active_index = None
        last_error = None
        temporarily_unhealthy = False
        try:
            state_path = Path(__file__).resolve().parents[1] / "configs" / "internet_provider_state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    active_index = int(state.get("last_good_index")) if state.get("last_good_index") is not None else None
                    last_error = state.get("last_error")
                    temporarily_unhealthy = bool(state.get("temporarily_unhealthy"))
        except Exception:
            active_index = None
        configured_indices = [
            index
            for index in range(1, 5)
            if str(os.getenv(f"GEMINI_INTERNET_API_KEY_{index}", "") or "").strip()
        ]
        if active_index not in configured_indices:
            unhealthy_index = None
            try:
                unhealthy_index = int(state.get("unhealthy_index")) if temporarily_unhealthy and state.get("unhealthy_index") is not None else None
            except Exception:
                unhealthy_index = None
            active_index = next((idx for idx in configured_indices if idx != unhealthy_index), configured_indices[0] if configured_indices else None)
        s.internet_ai_available = bool(configured_indices) and not (temporarily_unhealthy and len(configured_indices) <= 1)
        s.internet_active_key_index = active_index
        s.internet_active_key_email = str(os.getenv(f"GEMINI_INTERNET_API_KEY_{active_index}_EMAIL", "") or "").strip() if active_index else ""
        s.last_error_masked = str(last_error or "")[:180] if last_error else None

    # ---------- pub/sub ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        with self._sub_lock:
            self._subscribers.add(q)
        # entrega snapshot atual imediatamente
        try:
            q.put_nowait(self.get_snapshot())
        except asyncio.QueueFull:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers.discard(q)

    def _broadcast(self, payload: Dict[str, Any]) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


# Singleton oficial
_manager: Optional[RuntimeStatusManager] = None


def get_runtime_status_manager() -> RuntimeStatusManager:
    global _manager
    if _manager is None:
        _manager = RuntimeStatusManager()
    return _manager
