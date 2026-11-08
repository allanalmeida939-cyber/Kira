from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class TransportDecision:
    should_switch: bool
    reason: str
    from_provider: str
    to_provider: str = "local"


@dataclass(frozen=True, slots=True)
class TransportSwitchResult:
    ok: bool
    status: str
    from_provider: str
    to_provider: str
    provider: str
    reason: str = ""
    error: str = ""
    rollback_attempted: bool = False
    rollback_ok: bool = False
    cooldown_seconds: int = 0
    next_handoff_allowed_after_seconds: int = 0
    next_handoff_allowed_at: float = 0.0
    connection: dict | None = None
    switch_id: str = ""


@dataclass
class SessionTransportSwitchManager:
    now: Callable[[], float] = time.time
    _switch_in_progress: bool = False
    _next_allowed_at: float = 0.0
    _last_result: TransportSwitchResult | None = None
    _events: list[str] = field(default_factory=list)

    def cooldown_remaining(self) -> int:
        return int(max(self._next_allowed_at - self.now(), 0.0))

    @property
    def next_allowed_at(self) -> float:
        return float(self._next_allowed_at)

    def can_start_manual_switch(self, config: dict, runtime_snapshot: dict | None, blockers: dict[str, bool] | None = None) -> TransportDecision:
        provider = provider_kind_from_snapshot(runtime_snapshot)
        if self._switch_in_progress:
            return TransportDecision(False, "switch_in_progress", provider)
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return TransportDecision(False, f"cooldown:{remaining}", provider)
        if not bool(config.get("enabled", True)):
            return TransportDecision(False, "disabled", provider)
        if not bool(config.get("manual_handoff_enabled", True)):
            return TransportDecision(False, "manual_handoff_disabled", provider)
        if not bool(config.get("enable_real_cloud_to_local_handoff", False)):
            return TransportDecision(False, "real_handoff_disabled", provider)
        reasons = transport_block_reasons(config=config, runtime_snapshot=runtime_snapshot, explicit=blockers or {})
        if reasons:
            return TransportDecision(False, f"blocked:{','.join(reasons)}", provider)
        if provider != "cloud":
            return TransportDecision(False, f"provider_not_cloud:{provider}", provider)
        return TransportDecision(True, "manual_switch_allowed", provider)

    def can_start_manual_cloud_switch(self, config: dict, runtime_snapshot: dict | None, blockers: dict[str, bool] | None = None) -> TransportDecision:
        provider = provider_kind_from_snapshot(runtime_snapshot)
        if self._switch_in_progress:
            return TransportDecision(False, "switch_in_progress", provider, to_provider="cloud")
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return TransportDecision(False, f"cooldown:{remaining}", provider, to_provider="cloud")
        if not bool(config.get("manual_handoff_enabled", True)):
            return TransportDecision(False, "manual_handoff_disabled", provider, to_provider="cloud")
        reasons = transport_block_reasons(config=config, runtime_snapshot=runtime_snapshot, explicit=blockers or {})
        if reasons:
            return TransportDecision(False, f"blocked:{','.join(reasons)}", provider, to_provider="cloud")
        if provider != "local":
            return TransportDecision(False, f"provider_not_local:{provider}", provider, to_provider="cloud")
        return TransportDecision(True, "manual_switch_allowed", provider, to_provider="cloud")

    def clear_transition(self, *, reason: str = "manual_clear") -> None:
        self._switch_in_progress = False
        self._events.append(f"transition_cleared:{reason}")

    def begin(self) -> None:
        self._switch_in_progress = True

    def finish(self, result: TransportSwitchResult) -> TransportSwitchResult:
        self._switch_in_progress = False
        self._last_result = result
        return result

    def set_cooldown(self, seconds: int | float) -> int:
        cooldown = max(int(seconds or 0), 0)
        self._next_allowed_at = self.now() + cooldown
        return cooldown

    @property
    def switch_in_progress(self) -> bool:
        return self._switch_in_progress

    @property
    def last_result(self) -> TransportSwitchResult | None:
        return self._last_result


def provider_kind_from_snapshot(snapshot: dict | None) -> str:
    data = snapshot or {}
    provider = str(data.get("livekit_provider_type") or data.get("livekit_provider_active") or "").strip().lower()
    url = str(data.get("livekit_url") or "").strip().lower()
    if provider in {"cloud", "local"}:
        return provider
    if url.startswith("ws://127.0.0.1") or url.startswith("ws://localhost"):
        return "local"
    if url.startswith("wss://"):
        return "cloud"
    return "unknown"


@dataclass(frozen=True, slots=True)
class TransportLockCleanupResult:
    path: str
    existed: bool
    removed: bool
    reason: str
    lock_pid: int = 0
    expected_pid: int = 0
    lock_pid_active: bool = False
    lock_age_seconds: float = 0.0


def cleanup_transport_lock_if_safe(
    lock_path: str | Path,
    *,
    switch_in_progress: bool,
    expected_pid: int | None,
    expected_process_terminated: bool,
    pid_exists: Callable[[int], bool],
    now: Callable[[], float] = time.time,
) -> TransportLockCleanupResult:
    path = Path(lock_path)
    expected = int(expected_pid or 0)
    if not path.exists():
        return TransportLockCleanupResult(str(path), existed=False, removed=False, reason="missing", expected_pid=expected)
    if not switch_in_progress:
        return TransportLockCleanupResult(str(path), existed=True, removed=False, reason="switch_not_in_progress", expected_pid=expected)
    if expected <= 0:
        return TransportLockCleanupResult(str(path), existed=True, removed=False, reason="missing_expected_pid", expected_pid=expected)

    try:
        lock_pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        lock_pid = 0
    try:
        lock_age = max(float(now()) - float(path.stat().st_mtime), 0.0)
    except Exception:
        lock_age = 0.0

    active = False
    if lock_pid > 0:
        try:
            active = bool(pid_exists(lock_pid))
        except Exception:
            active = True

    removable = False
    reason = "active_other_process"
    if lock_pid <= 0:
        removable = True
        reason = "invalid_pid"
    elif lock_pid == expected and expected_process_terminated:
        removable = True
        reason = "expected_process_terminated"
    elif not active:
        removable = True
        reason = "stale_pid"
    elif lock_pid == expected:
        reason = "expected_process_still_active"

    if not removable:
        return TransportLockCleanupResult(
            str(path),
            existed=True,
            removed=False,
            reason=reason,
            lock_pid=lock_pid,
            expected_pid=expected,
            lock_pid_active=active,
            lock_age_seconds=lock_age,
        )

    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        return TransportLockCleanupResult(
            str(path),
            existed=True,
            removed=False,
            reason=f"remove_failed:{exc.__class__.__name__}",
            lock_pid=lock_pid,
            expected_pid=expected,
            lock_pid_active=active,
            lock_age_seconds=lock_age,
        )
    return TransportLockCleanupResult(
        str(path),
        existed=True,
        removed=True,
        reason=reason,
        lock_pid=lock_pid,
        expected_pid=expected,
        lock_pid_active=active,
        lock_age_seconds=lock_age,
    )


def transport_block_reasons(
    *,
    config: dict,
    runtime_snapshot: dict | None,
    explicit: dict[str, bool] | None = None,
) -> list[str]:
    data = runtime_snapshot or {}
    explicit_map = explicit or {}
    agent_state = str(data.get("agent_state") or data.get("voice_state") or "").strip().lower()
    voice_status = str(data.get("voice_status") or "").strip().lower()
    provider = provider_kind_from_snapshot(data)
    runtime_already_ready = provider in {"cloud", "local"} and runtime_ready_for_transport(data, provider)
    reasons: list[str] = []
    if bool(config.get("do_not_enter_if_speaking", True)) and (agent_state in {"speaking", "talking"} or voice_status in {"speaking", "talking"}):
        reasons.append("speaking")
    if agent_state in {"thinking", "generating"} or bool(data.get("gemini_connecting")):
        reasons.append("gemini_busy")
    if not runtime_already_ready and (bool(data.get("room_connecting")) or bool(data.get("boot_recovery_active"))):
        reasons.append("session_transitioning")
    if bool(config.get("do_not_enter_if_tool_running", True)) and bool(explicit_map.get("tool_running") or data.get("tool_running")):
        reasons.append("tool_running")
    if bool(config.get("do_not_enter_if_manus_running", True)) and bool(explicit_map.get("manus_running") or data.get("manus_running")):
        reasons.append("manus_running")
    if bool(config.get("do_not_enter_if_pc_operator_running", True)) and bool(explicit_map.get("pc_operator_running") or data.get("pc_operator_running")):
        reasons.append("pc_operator_running")
    if bool(config.get("do_not_enter_if_messages_sending", True)) and bool(explicit_map.get("messages_sending") or data.get("messages_sending")):
        reasons.append("messages_sending")
    if bool(explicit_map.get("pending_confirmation") or data.get("pending_confirmation")):
        reasons.append("pending_confirmation")
    return reasons


def runtime_ready_for_transport(snapshot: dict | None, target_provider: str) -> bool:
    data = snapshot or {}
    target = str(target_provider or "").strip().lower()
    if target and provider_kind_from_snapshot(data) != target:
        return False
    return all(
        bool(data.get(flag))
        for flag in (
            "agent_worker_ready",
            "room_ready",
            "gemini_ready",
            "voice_pipeline_ready",
            "runtime_ready",
            "functional_ready",
        )
    ) and not bool(data.get("agent_process_dead"))


def late_transport_failure_reason(snapshot: dict | None) -> str:
    data = snapshot or {}
    if bool(data.get("agent_process_dead")):
        return "agent_process_died_before_functional_ready"
    failure_text = str(
        data.get("failure_reason")
        or data.get("agent_last_error")
        or data.get("error")
        or ""
    ).strip()
    failure_low = failure_text.lower()
    if "agent encerrou" in failure_low or "agent process" in failure_low or "agent_failed" in failure_low:
        return "agent_process_died_before_functional_ready"
    event = str(data.get("event") or "").strip().lower()
    if event in {"agent_failed", "session_runtime_aborted", "agent_runtime_failed"}:
        return event
    if bool(data.get("session_failed")):
        return "session_failed_before_functional_ready"
    return ""


def transition_is_stale(
    *,
    started_at: float,
    now: float,
    timeout_seconds: float,
    phase: str = "",
) -> bool:
    del phase
    return max(float(now) - float(started_at or 0.0), 0.0) >= max(float(timeout_seconds or 0.0), 1.0)


def recovery_to_cloud_allowed(
    runtime_snapshot: dict | None,
    *,
    switch_in_progress: bool,
    switch_failed: bool = False,
    switch_stale: bool = False,
) -> bool:
    data = runtime_snapshot or {}
    provider = provider_kind_from_snapshot(data)
    if provider != "local":
        return False
    if switch_failed or switch_stale:
        return True
    # Sessão local quebrada (recuperando e sem funcionar) também depois de a troca terminar: voltar
    # para a nuvem é justamente o conserto; antes ficava bloqueado por "session_transitioning".
    if bool(data.get("boot_recovery_active")) and not bool(data.get("functional_ready")):
        return True
    if not switch_in_progress:
        return False
    if late_transport_failure_reason(data):
        return True
    return False


def decide_cloud_to_local_switch(
    *,
    config: dict,
    idle_seconds: float,
    runtime_snapshot: dict | None,
    blocked: bool = False,
) -> TransportDecision:
    if str(config.get("mode") or "").strip().lower() == "gemini_only" or bool(config.get("livekit_local_default", False)):
        return TransportDecision(False, "not_applicable_local_default", provider_kind_from_snapshot(runtime_snapshot))
    if not bool(config.get("enabled", True)):
        return TransportDecision(False, "disabled", provider_kind_from_snapshot(runtime_snapshot))
    if not bool(config.get("prefer_livekit_local_in_economy", True)):
        return TransportDecision(False, "local_preference_disabled", provider_kind_from_snapshot(runtime_snapshot))
    if blocked:
        return TransportDecision(False, "blocked", provider_kind_from_snapshot(runtime_snapshot))
    provider = provider_kind_from_snapshot(runtime_snapshot)
    threshold_seconds = max(int(config.get("cloud_to_local_after_minutes", 60)), 1) * 60
    if idle_seconds < threshold_seconds:
        return TransportDecision(False, "below_threshold", provider)
    if provider != "cloud":
        return TransportDecision(False, f"provider_not_cloud:{provider}", provider)
    if not bool(config.get("auto_handoff_enabled", False)):
        return TransportDecision(False, "auto_handoff_disabled", provider)
    if not bool(config.get("enable_real_cloud_to_local_handoff", False)):
        return TransportDecision(False, "real_handoff_disabled", provider)
    return TransportDecision(True, "idle_threshold_reached", provider)
